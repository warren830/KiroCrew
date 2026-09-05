"""What payload does this runtime accept? — answered from evidence, never a guess.

The panel used to seed a bare ``{"prompt": ""}`` and label it a generic
template. That is honest but useless: it hands the question back to the operator,
who then goes and reads the runtime's source. This module exists to close that
gap **without** inventing a schema, by asking the runtime's own artefacts.

Three sources exist, and only three. Ranked by how much they can be trusted:

1. **A published agent card** (:func:`.actions.fetch_agent_card`) — the runtime's
   own declared contract. Authoritative, and absent for every runtime that is not
   an A2A agent, which is most of them.
2. **A validation error the runtime itself produced.** When a framework rejects a
   request it names the fields it required. That is the runtime stating its
   contract, so the field names are facts, not inference.
3. **JSON objects the runtime logged.** Their top-level keys are fields this
   runtime has actually handled. Weaker than (2) — a logged object may be an
   internal record rather than a request — so every key is returned WITH the log
   excerpt it came from and the caller must show that provenance.

**The honest limit, which the UI has to state rather than paper over:** none of
these recovers a field that is optional AND never logged. A real case: a
Pydantic model whose ``messages`` list defaults to empty is never named in a
missing-field error, and a runtime that deliberately does not log customer text
never logs it either — so the one field carrying the actual question is
invisible to all three sources. Only an agent card closes that. Claiming
otherwise would reproduce the fabricated-schema failure this whole path avoids,
one level up.

Everything here is a READ. The log scan uses ``logs`` — a different service from
the rest of the app, which is why it lives in its own module and why the app's
manifest declares the extra CloudWatch Logs permission it needs.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.apps.builtins.agentcore_observatory.backend import catalog
from kiro_crew.apps.builtins.agentcore_observatory.backend.agentcore import (
    describe_denial,
    safe_identifier,
)
from kiro_crew.apps.builtins.agentcore_observatory.backend.config import ObservatoryConfig
from kiro_crew.cloud.aws import CloudActionDenied, run_aws

logger = logging.getLogger(__name__)

#: Per-call CLI timeout. A bounded log scan is one API call.
HINT_TIMEOUT_SECS = 30

#: How far back the scan looks, in hours. A day covers "I deployed it yesterday
#: and tried it once"; going wider mostly returns cold-start noise.
DEFAULT_LOOKBACK_HOURS = 24

#: How many of the most-recently-active log streams to read. A runtime writes a
#: per-container stream plus an ``otel-rt-logs`` stream, so a handful covers the
#: latest deployment without paging an entire group.
MAX_STREAMS = 4

#: Cap on events pulled per stream. The scan is a hint, not an audit.
MAX_EVENTS = 300

#: Cap on how much of a log line is kept as evidence. Log lines can carry an
#: entire OTEL resource block; the excerpt only has to let a human recognise it.
MAX_EXCERPT = 240

#: Cap on distinct field names reported, so a noisy log cannot flood the UI.
MAX_FIELDS = 40

#: Top-level keys that identify a JSON object as an OTEL log record rather than
#: anything a caller sent. Detection is STRUCTURAL — the whole object is skipped —
#: because the alternative, a denylist of individual key names, loses: the real
#: envelope carries ``flags``, ``spanId``, ``severityNumber``,
#: ``observedTimeUnixNano`` and more, and a live run surfaced ``flags`` as a
#: "field this runtime accepts" precisely because one name was missing from such
#: a list. Recognising the record type cannot be defeated by a new key.
_OTEL_MARKERS = frozenset({"resource", "scope", "severityNumber", "severityText", "spanId"})

#: How many markers must be present. Two avoids skipping an application object
#: that happens to carry one word like ``scope``.
_OTEL_MARKER_MIN = 2


def is_otel_record(obj: dict[str, Any]) -> bool:
    """Whether this JSON object is a telemetry envelope rather than a payload."""
    return sum(1 for key in _OTEL_MARKERS if key in obj) >= _OTEL_MARKER_MIN


#: Pydantic v2 prints the offending field on its own line, then an indented
#: reason. ``type=missing`` is the only reason that names a REQUIRED field —
#: ``type=string_type`` means the field was supplied with the wrong type, which
#: is a different fact and must not be reported as "required".
_PYDANTIC_MISSING = re.compile(r"^\s*Field required \[type=missing", re.MULTILINE)

#: A field name line: a bare identifier, or a dotted/indexed path for a nested
#: model (``businessData.sourceApp``, ``messages.0.content``).
_FIELD_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*$")

#: JSON Schema validators phrase it differently; both spellings occur.
_JSONSCHEMA_REQUIRED = re.compile(
    r"""['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s+is\s+a\s+required\s+property"""
)

#: Pydantic echoes the rejected input. It is a Python repr, not JSON, so it is
#: kept as text: converting it would silently change quoting and None/null.
_INPUT_VALUE = re.compile(r"input_value=(\{.*?\})(?:,\s*input_type=|\])", re.DOTALL)


@dataclass
class FieldEvidence:
    """One candidate field name and where it was observed."""

    name: str
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "excerpt": self.excerpt}


@dataclass
class HintResult:
    """What could be learned about a runtime's expected input.

    ``ok=True`` with everything empty is a successful scan that found nothing —
    a real and common outcome for a runtime that logs no request detail. The
    caller must present that as "nothing observed", never as a schema.
    """

    ok: bool
    log_group: str = ""
    required_fields: list[str] = field(default_factory=list)
    rejected_examples: list[str] = field(default_factory=list)
    observed_fields: list[FieldEvidence] = field(default_factory=list)
    scanned_events: int = 0
    error: str = ""
    denied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "logGroup": self.log_group,
            "requiredFields": self.required_fields,
            "rejectedExamples": self.rejected_examples,
            "observedFields": [f.to_dict() for f in self.observed_fields],
            "scannedEvents": self.scanned_events,
            "error": self.error,
            "denied": self.denied,
        }


def parse_required_fields(text: str) -> list[str]:
    """Field names a validation error says were REQUIRED and missing.

    Handles the two framework spellings that actually occur in AgentCore Python
    runtimes: Pydantic v2's two-line form, and JSON Schema's one-liner. Order is
    preserved and duplicates are dropped, so a repeated error across many log
    lines does not inflate the list.

    Only ``type=missing`` counts. A ``type=string_type`` or ``type=int_parsing``
    error means the caller DID send the field, so reporting it as required would
    be a different claim than the runtime made.
    """
    found: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _PYDANTIC_MISSING.match(line):
            continue
        # Walk back to the nearest non-blank line: Pydantic puts the field name
        # immediately above its reason.
        for j in range(i - 1, -1, -1):
            candidate = lines[j].strip()
            if not candidate:
                continue
            if _FIELD_LINE.match(candidate):
                found.append(candidate)
            break
    for match in _JSONSCHEMA_REQUIRED.finditer(text):
        found.append(match.group(1))
    out: list[str] = []
    for name in found:
        if name not in out:
            out.append(name)
    return out


def parse_rejected_inputs(text: str) -> list[str]:
    """The rejected request bodies a validation error echoed back.

    Returned verbatim as text. These are the single most useful hint available —
    they show the shape someone actually sent — but they are a Python repr, so
    the caller must present them as an excerpt rather than as a payload to
    submit.
    """
    out: list[str] = []
    for match in _INPUT_VALUE.finditer(text):
        value = match.group(1).strip()
        if value and value not in out:
            out.append(value)
    return out


def _json_objects(line: str) -> list[dict[str, Any]]:
    """Every top-level JSON object in one log line.

    Scans for balanced braces rather than trying ``json.loads`` on the whole
    line, because a log line is usually prose with an object embedded in it.
    """

    out: list[dict[str, Any]] = []
    depth = 0
    start = -1
    for i, ch in enumerate(line):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(line[start : i + 1])
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    out.append(parsed)
                start = -1
    return out


def parse_observed_fields(messages: list[str]) -> list[FieldEvidence]:
    """Candidate field names from JSON objects the runtime logged.

    Weaker evidence than a validation error, so each name carries the excerpt it
    came from: a logged object may be an internal record rather than a request,
    and only a human looking at the line can tell. That is also why the caller
    must NOT offer these as a one-click payload merge — a live run did, and
    produced a payload containing a telemetry key.

    A telemetry envelope is skipped as a whole object rather than having its keys
    filtered out one at a time; see :func:`is_otel_record`.
    """
    out: list[FieldEvidence] = []
    seen: set[str] = set()
    for message in messages:
        for obj in _json_objects(message):
            if is_otel_record(obj):
                continue
            for key in obj:
                if key in seen:
                    continue
                if not isinstance(key, str) or not _FIELD_LINE.match(key):
                    continue
                seen.add(key)
                out.append(FieldEvidence(name=key, excerpt=message.strip()[:MAX_EXCERPT]))
                if len(out) >= MAX_FIELDS:
                    return out
    return out


def runtime_id_from_arn(runtime_arn: str) -> str:
    """The runtime id an ARN ends with, or ``''``.

    The log group is discovered from this id rather than assembled from a
    guessed naming convention, so a runtime whose group does not exist reports
    an absence instead of a permission error on a path that was never real.
    """
    tail = runtime_arn.rsplit("/", 1)[-1] if "/" in runtime_arn else ""
    return tail if tail and safe_identifier(tail) else ""


def _run(cfg: ObservatoryConfig, args: list[str]) -> tuple[dict[str, Any] | None, str, bool]:
    """One CLI read. Returns ``(payload, error, denied)``."""
    try:
        rc, out, err = run_aws(args, cfg.profile, cfg.region, timeout=HINT_TIMEOUT_SECS)
    except CloudActionDenied as exc:
        return None, describe_denial(exc), True
    except Exception as exc:  # noqa: BLE001 - a broken CLI must not 500 the page
        logger.debug("payload hint call failed: %s", args[:2], exc_info=True)
        return None, f"could not run the aws CLI: {exc}", False
    if rc != 0:
        detail = (err or out or "").strip()
        return None, detail or f"aws exited {rc}", False
    if not (out or "").strip():
        return {}, "", False
    try:
        payload = json.loads(out)
    except ValueError:
        return None, "the aws CLI returned output that is not JSON", False
    if not isinstance(payload, dict):
        return None, "the aws CLI returned JSON that is not an object", False
    return payload, "", False


def find_log_group(cfg: ObservatoryConfig, runtime_id: str) -> tuple[str, str, bool]:
    """Discover this runtime's log group. Returns ``(name, error, denied)``.

    An empty name with no error means the runtime has no log group — an
    absence, exactly like an unpublished agent card, and not a failure.
    """
    prefix = f"/aws/bedrock-agentcore/runtimes/{runtime_id}"
    payload, error, denied = _run(
        cfg,
        [catalog.SERVICE_LOGS, "describe-log-groups", "--log-group-name-prefix", prefix],
    )
    if payload is None:
        return "", error, denied
    groups = payload.get("logGroups")
    if not isinstance(groups, list):
        return "", "", False
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("logGroupName"), str):
            return group["logGroupName"], "", False
    return "", "", False


def recent_streams(cfg: ObservatoryConfig, group: str) -> tuple[list[str], str, bool]:
    """The most recently active log streams. Returns ``(names, error, denied)``."""
    payload, error, denied = _run(
        cfg,
        [
            catalog.SERVICE_LOGS,
            "describe-log-streams",
            "--log-group-name",
            group,
            "--order-by",
            "LastEventTime",
            "--descending",
            "--max-items",
            str(MAX_STREAMS),
        ],
    )
    if payload is None:
        return [], error, denied
    streams = payload.get("logStreams")
    out: list[str] = []
    for stream in streams if isinstance(streams, list) else []:
        if isinstance(stream, dict) and isinstance(stream.get("logStreamName"), str):
            out.append(stream["logStreamName"])
    return out, "", False


def _tail_events(cfg: ObservatoryConfig, group: str, stream: str) -> tuple[list[str], str, bool]:
    """The NEWEST events in one stream.

    ``--no-start-from-head`` is the whole point. A live run against a real
    runtime read 300 events and found nothing, because ``filter-log-events``
    returns a window in ASCENDING time order — so a bounded read over 24 hours
    returns the oldest events, and the recent rejection that carries the field
    names sits past the cap. Reading a stream from its tail is what puts the
    interesting events inside the bound.
    """
    payload, error, denied = _run(
        cfg,
        [
            catalog.SERVICE_LOGS,
            "get-log-events",
            "--log-group-name",
            group,
            "--log-stream-name",
            stream,
            "--limit",
            str(MAX_EVENTS),
            "--no-start-from-head",
        ],
    )
    if payload is None:
        return [], error, denied
    events = payload.get("events")
    return (
        [
            str(e.get("message", ""))
            for e in (events if isinstance(events, list) else [])
            if isinstance(e, dict)
        ],
        "",
        False,
    )


def log_text(messages: list[str]) -> str:
    """Flatten log events into text a line-oriented parser can read.

    Every AgentCore log event arrives twice: once as a plain line, and once
    wrapped in an OTEL envelope whose ``body`` is a JSON **string**. A multi-line
    framework error only survives in the envelope — and inside a JSON string its
    newlines are escaped, so ``splitlines`` sees one long line and the
    "field name above its reason" layout that Pydantic uses is invisible.

    Decoding ``body`` is reading a documented structure, not guessing: without it
    a live scan finds the rejected request body (matched by a regex that needs no
    newlines) while silently missing the field names, which is the more useful
    half.
    """
    parts: list[str] = []
    for message in messages:
        parts.append(message)
        stripped = message.strip()
        if not stripped.startswith("{"):
            continue
        try:
            envelope = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(envelope, dict) and isinstance(envelope.get("body"), str):
            parts.append(envelope["body"])
    return "\n".join(parts)


def scan_payload_hints(
    cfg: ObservatoryConfig,
    *,
    runtime_arn: str,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> HintResult:
    """Learn what input this runtime accepts, from its own log group.

    ``lookback_hours`` is retained for callers but no longer bounds the read:
    the scan takes the newest events from the most recent streams, which is
    strictly better than a time window for this purpose — a runtime nobody has
    called in a week still has its last rejection at the tail of its last stream.
    """
    if not cfg.configured:
        return HintResult(ok=False, error="no AWS region is configured")
    runtime_id = runtime_id_from_arn(runtime_arn)
    if not runtime_id:
        return HintResult(ok=False, error="the runtime ARN is not a well-formed identifier")

    group, error, denied = find_log_group(cfg, runtime_id)
    if error:
        return HintResult(ok=False, error=error, denied=denied)
    if not group:
        # No log group is a fact about the runtime, reported as one.
        return HintResult(ok=True)

    streams, error, denied = recent_streams(cfg, group)
    if error:
        return HintResult(ok=False, log_group=group, error=error, denied=denied)

    messages: list[str] = []
    for stream in streams:
        chunk, error, denied = _tail_events(cfg, group, stream)
        if error:
            # One unreadable stream must not discard what the others gave.
            logger.debug("stream %s unreadable: %s", stream, error)
            continue
        messages += chunk

    joined = log_text(messages)
    return HintResult(
        ok=True,
        log_group=group,
        required_fields=parse_required_fields(joined),
        rejected_examples=parse_rejected_inputs(joined)[:5],
        observed_fields=parse_observed_fields(messages),
        scanned_events=len(messages),
    )
