"""AgentCore Observatory — the calls that change something or cost money.

Deliberately a separate module from :mod:`.agentcore`. That module's promise is
structural: read it and every argv it can build is a ``list-*`` or a ``get-*``
verb, because the verb comes from a :mod:`.catalog` row and from nowhere else.
Putting ``invoke-agent-runtime`` there would cost the reader that guarantee —
the claim would degrade from "the verbs are reads" to "read all the branches and
check". Keeping the paid verbs here means the read path stays provable by
reading one file and the write surface is auditable by reading this one.

What the cloud chokepoint does and does not give us
--------------------------------------------------
:func:`kiro_crew.cloud.aws.run_aws` accepts an arbitrary argv, so these verbs
are reachable from the gateway process. ``assert_chokepoint_allowed`` refuses
non-allowlisted operations only when ``KIROCREW_SESSION_KEY`` is set — that is,
from an agent session — and the gateway carries no such variable. That guard is
defence in depth, and its own docstring states the load-bearing control is the
least-privilege IAM scope of the operator's own credentials. So **if the
configured profile is allowed to invoke a runtime, this app can invoke it.** Do
not widen that allowlist to make anything here reachable from an agent session.

Two consequences the caller must carry, because the guard will not:

* **Every action needs its own human confirmation.** Nothing in this module can
  tell a deliberate click from a retry loop, so the route layer requires an
  explicit per-action confirmation field.
* **A duplicate batch evaluation is expensive and invisible.** Evaluator token
  spend appears in no log or span field this app can read, so a job started
  twice cannot be detected after the fact — only prevented. Hence
  :func:`start_batch_evaluation` always sends a ``clientToken``.

Live-verified CLI shapes, each of which contradicted a plausible guess
---------------------------------------------------------------------
* ``invoke-agent-runtime`` takes a **required positional outfile**. The agent's
  reply is a streaming blob written to that file; stdout carries only metadata
  (``runtimeSessionId``, ``traceId``, ``statusCode``, ``contentType``). Code
  that looks for the reply on stdout finds nothing and reports an empty answer.
* ``--payload`` is a **blob** and the CLI v2 default ``cli-binary-format`` is
  ``base64``. Without an explicit ``raw-in-base64-out`` the agent receives the
  caller's JSON decoded as though it were base64 — garbage, delivered with a
  success status, which is the failure mode nobody notices.
* ``--runtime-session-id`` has a **minimum length of 33**, so ``uuid4().hex``
  (32) is one character short of valid. A short id is refused here, with the
  real constraint, rather than sent to be refused by the service.
* ``start-batch-evaluation`` lives on the **data plane**, its name pattern
  forbids ``-`` and caps at 48 characters, and ``cloudWatchLogs.serviceNames``
  is min 1 **max 1** — exactly one service name, not a list to grow into.
* ``get-agent-card`` answers ``GetAgentCard API is only supported for A2A
  agents`` on an ordinary runtime. That is an **absence, not a failure**: the
  caller must render "this runtime publishes no card" and must never fabricate a
  schema to fill the gap.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.agentcore_observatory.backend import catalog, payload_hints
from kiro_crew.apps.builtins.agentcore_observatory.backend.agentcore import (
    describe_denial,
    safe_identifier,
)
from kiro_crew.apps.builtins.agentcore_observatory.backend.config import ObservatoryConfig
from kiro_crew.cloud.aws import CloudActionDenied, run_aws

logger = logging.getLogger(__name__)

#: Ceiling on one invoke. An agent that reasons and calls tools is legitimately
#: slow, so this is far above the read layer's 20s; it exists to stop a hung
#: connection holding a gateway worker forever, not to bound agent thinking.
INVOKE_TIMEOUT_SECS = 180

#: Ceiling on the non-streaming actions. Starting a batch evaluation and reading
#: an agent card are both single fast API calls; a longer wait means the CLI is
#: stuck resolving credentials, which should surface as an error.
ACTION_TIMEOUT_SECS = 30

#: Cap on a test payload. Far below the API's 100 MB: this is a payload a human
#: typed into a text box, and the reply is buffered in memory to be rendered.
#: Anyone who needs megabytes is not testing interactively and should use the
#: CLI directly rather than have this app grow a streaming upload path.
MAX_PAYLOAD_BYTES = 100_000

#: Cap on how much of the reply is read back for display. A streaming agent can
#: emit far more than a browser should be handed at once; past this the result is
#: marked truncated rather than silently clipped.
MAX_RESPONSE_BYTES = 256 * 1024

#: ``batchEvaluationName``: starts with a letter, then letters/digits/underscore,
#: 48 characters total. Note the absence of ``-`` — the obvious
#: ``my-eval-2026`` is rejected by the service, so it is rejected here with a
#: message that says why.
_EVAL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")

#: ``evaluatorId``: either a built-in (``Builtin.Helpfulness``) or a custom
#: evaluator, whose id carries a 10-character generated suffix after a dash.
_EVALUATOR_ID_RE = re.compile(
    r"^(?:Builtin\.[a-zA-Z0-9_-]+|[a-zA-Z][a-zA-Z0-9\-_]{0,99}-[a-zA-Z0-9]{10})$"
)

#: ``runtimeSessionId``: leading alphanumeric, then alphanumerics, ``-`` and
#: ``_``. The length floor is enforced separately so the error can name it.
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_]*$")

_SESSION_ID_MIN = 33
_SESSION_ID_MAX = 256

#: ``serviceNames`` is min 1 / max 1 in the API, and ``logGroupNames`` maxes at
#: 5. Both are pinned here so a caller cannot quietly send a list the service
#: will reject after the user has already confirmed a paid action.
_MAX_SERVICE_NAMES = 1
_MAX_LOG_GROUPS = 5
_MAX_EVALUATORS = 10
_MAX_SESSION_FILTER_IDS = 500

#: The substring AgentCore uses when a runtime is simply not an A2A agent. The
#: match is deliberately narrow: any other ValidationException is a real error
#: and must not be flattened into "no card published".
_NO_CARD_MARKER = "only supported for a2a agents"


@dataclass
class ActionResult:
    """The outcome of one action.

    ``ok`` False is a genuine failure. ``denied`` distinguishes the cloud
    chokepoint refusing an agent session from AWS refusing the call, because the
    two need opposite responses from the user: open the dashboard page, versus
    fix an IAM policy.

    ``result`` is the parsed metadata AWS returned. ``body`` is the agent's own
    reply, which arrives in a file rather than on stdout and may not be JSON at
    all — an agent may answer plain text or a server-sent-event stream — so it is
    carried as text and left for the caller to interpret.
    """

    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    body_truncated: bool = False
    #: Field names the reply itself said were required and missing, extracted by
    #: :func:`.payload_hints.parse_required_fields`. Present only when the runtime
    #: rejected the payload with a structured validation error, and empty
    #: otherwise — an empty list means "the reply named no fields", never "the
    #: payload was fine". Parsed here rather than in the browser so there is one
    #: implementation, tested once, shared with the log scan.
    derived_fields: list[str] = field(default_factory=list)
    error: str = ""
    denied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "result": self.result,
            "body": self.body,
            "body_truncated": self.body_truncated,
            "derived_fields": self.derived_fields,
            "error": self.error,
            "denied": self.denied,
        }


def new_session_id() -> str:
    """Mint a ``runtimeSessionId`` that satisfies the service's constraints.

    ``uuid4().hex`` is 32 characters and the minimum is 33, so the bare hex is
    invalid by exactly one character. The prefix both fixes the length and
    guarantees the required leading alphanumeric.
    """
    return f"s{uuid.uuid4().hex}"


def _check_session_id(session_id: str) -> str:
    """Return an error string for a malformed session id, or ``""``."""
    if len(session_id) < _SESSION_ID_MIN:
        return (
            f"a runtime session id must be at least {_SESSION_ID_MIN} characters "
            f"(got {len(session_id)})"
        )
    if len(session_id) > _SESSION_ID_MAX:
        return f"a runtime session id must be at most {_SESSION_ID_MAX} characters"
    if not _SESSION_ID_RE.match(session_id):
        return "a runtime session id may contain only letters, digits, '-' and '_'"
    return ""


def _iso_or_error(label: str, value: str) -> tuple[str, str]:
    """Normalize an ISO-8601 timestamp, returning ``(normalized, error)``.

    A trailing ``Z`` is accepted and rewritten, because that is the spelling the
    console and the CLI's own output use while :meth:`datetime.fromisoformat`
    rejects it on older supported Pythons.
    """
    text = (value or "").strip()
    if not text:
        return "", f"{label} is required when a time range is given"
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "", f"{label} is not an ISO-8601 timestamp"
    return text, ""


def _run(cfg: ObservatoryConfig, args: list[str], timeout: int) -> tuple[int, str, str, str, bool]:
    """Run one CLI call. Returns ``(rc, stdout, stderr, error, denied)``.

    ``error`` is set only when the call could not be made at all; a non-zero
    ``rc`` is returned as-is so the caller can inspect stderr before deciding
    whether it is a failure or, for an agent card, an absence.
    """
    try:
        rc, out, err = run_aws(args, cfg.profile, cfg.region, timeout=timeout)
    except CloudActionDenied as exc:
        return 1, "", "", describe_denial(exc), True
    except Exception as exc:  # noqa: BLE001 - a broken CLI must not 500 the page
        logger.debug("agentcore action failed: %s", args[:2], exc_info=True)
        return 1, "", "", f"could not run the aws CLI: {exc}", False
    return rc, out, err, "", False


def _payload_json(out: str) -> tuple[dict[str, Any], str]:
    """Parse a metadata payload, returning ``(payload, error)``."""
    if not (out or "").strip():
        return {}, ""
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return {}, "the aws CLI returned output that is not JSON"
    if not isinstance(payload, dict):
        return {}, "the aws CLI returned JSON that is not an object"
    return payload, ""


def invoke_runtime(
    cfg: ObservatoryConfig,
    *,
    runtime_arn: str,
    payload: str,
    qualifier: str = "",
    session_id: str = "",
    content_type: str = "application/json",
    accept: str = "application/json",
) -> ActionResult:
    """Invoke one agent runtime with a caller-supplied payload.

    ``payload`` is never synthesised or defaulted. A test whose input this app
    invented proves nothing about the agent, and a payload shaped like a guess
    is worse than no payload because a 200 response makes the guess look
    correct.

    ``qualifier`` lets the caller test the exact version it is looking at rather
    than whatever the default endpoint currently points to.
    """
    if not cfg.configured:
        return ActionResult(ok=False, error="no AWS region is configured")
    if not safe_identifier(runtime_arn):
        return ActionResult(ok=False, error="the runtime ARN is not a well-formed identifier")
    if not (payload or "").strip():
        return ActionResult(ok=False, error="a payload is required")

    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        return ActionResult(
            ok=False,
            error=(
                f"the payload is {len(encoded)} bytes, above this app's "
                f"{MAX_PAYLOAD_BYTES}-byte limit for an interactive test"
            ),
        )
    if qualifier and not safe_identifier(qualifier):
        return ActionResult(ok=False, error="the qualifier is not a well-formed identifier")
    if session_id:
        problem = _check_session_id(session_id)
        if problem:
            return ActionResult(ok=False, error=problem)

    with tempfile.TemporaryDirectory(prefix="agentcore-invoke-") as tmp:
        # The reply is a streaming blob the CLI writes to this path; stdout
        # carries only metadata. The directory is removed on the way out, so a
        # response body never outlives the request that asked for it.
        outfile = Path(tmp) / "response"
        args = [
            catalog.SERVICE_DATA,
            "invoke-agent-runtime",
            "--agent-runtime-arn",
            runtime_arn,
            "--payload",
            payload,
            # Without this the CLI reads the payload as base64 and the agent is
            # handed decoded garbage together with a success status.
            "--cli-binary-format",
            "raw-in-base64-out",
            "--content-type",
            content_type,
            "--accept",
            accept,
        ]
        if qualifier:
            args += ["--qualifier", qualifier]
        if session_id:
            args += ["--runtime-session-id", session_id]
        args.append(str(outfile))

        rc, out, err, error, denied = _run(cfg, args, INVOKE_TIMEOUT_SECS)
        if error:
            return ActionResult(ok=False, error=error, denied=denied)
        if rc != 0:
            detail = (err or out or "").strip()
            return ActionResult(ok=False, error=detail or f"aws exited {rc}")

        result, parse_error = _payload_json(out)
        if parse_error:
            return ActionResult(ok=False, error=parse_error)

        body, truncated = _read_body(outfile)

    # A 200 with a validation error in the body is the common case for a payload
    # the caller had to guess at. Surfacing the field names the runtime itself
    # named turns that wasted call into the answer.
    return ActionResult(
        ok=True,
        result=result,
        body=body,
        body_truncated=truncated,
        derived_fields=payload_hints.parse_required_fields(body),
    )


def _read_body(outfile: Path) -> tuple[str, bool]:
    """Read the reply file back as display text, bounded by the response cap.

    Decoding is lossy on purpose: the agent chooses the encoding and a
    mis-declared byte must not turn a successful invocation into an error the
    user cannot act on.
    """
    try:
        raw = outfile.read_bytes()
    except OSError:
        # A zero-length or absent file is a legitimate empty reply, not a
        # failure: the metadata on stdout already told us the call succeeded.
        return "", False
    truncated = len(raw) > MAX_RESPONSE_BYTES
    return raw[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace"), truncated


def start_batch_evaluation(
    cfg: ObservatoryConfig,
    *,
    name: str,
    evaluator_ids: list[str],
    service_name: str,
    log_group_names: list[str],
    start_time: str = "",
    end_time: str = "",
    session_ids: list[str] | None = None,
    description: str = "",
    client_token: str = "",
) -> ActionResult:
    """Start a batch evaluation over spans AWS reads from CloudWatch Logs.

    The spans are pulled by AWS from the named log groups, so this app needs no
    Logs Insights query and never handles span data itself.

    A ``clientToken`` is always sent — generated when the caller does not supply
    one — because evaluator token spend is not observable after the fact, which
    makes a duplicate job something to prevent rather than detect.
    """
    if not cfg.configured:
        return ActionResult(ok=False, error="no AWS region is configured")
    if not _EVAL_NAME_RE.match(name or ""):
        return ActionResult(
            ok=False,
            error=(
                "the evaluation name must start with a letter and contain only "
                "letters, digits and underscores, up to 48 characters "
                "(hyphens are not accepted by AgentCore)"
            ),
        )

    ids = [str(value).strip() for value in (evaluator_ids or []) if str(value).strip()]
    if not ids:
        return ActionResult(ok=False, error="at least one evaluator is required")
    if len(ids) > _MAX_EVALUATORS:
        return ActionResult(ok=False, error=f"at most {_MAX_EVALUATORS} evaluators are allowed")
    for value in ids:
        if not _EVALUATOR_ID_RE.match(value):
            return ActionResult(ok=False, error=f"{value!r} is not a well-formed evaluator id")

    if not safe_identifier(service_name or ""):
        return ActionResult(ok=False, error="a single agent service name is required")

    groups = [str(value).strip() for value in (log_group_names or []) if str(value).strip()]
    if not groups:
        return ActionResult(ok=False, error="at least one CloudWatch log group is required")
    if len(groups) > _MAX_LOG_GROUPS:
        return ActionResult(ok=False, error=f"at most {_MAX_LOG_GROUPS} log groups are allowed")
    for value in groups:
        if not safe_identifier(value):
            return ActionResult(ok=False, error=f"{value!r} is not a well-formed log group name")

    source: dict[str, Any] = {
        "cloudWatchLogs": {
            # A list of exactly one, because the API's own bound is min 1 max 1.
            "serviceNames": [service_name][:_MAX_SERVICE_NAMES],
            "logGroupNames": groups,
        }
    }

    filters: dict[str, Any] = {}
    picked = [str(value).strip() for value in (session_ids or []) if str(value).strip()]
    if picked:
        if len(picked) > _MAX_SESSION_FILTER_IDS:
            return ActionResult(
                ok=False,
                error=f"at most {_MAX_SESSION_FILTER_IDS} session ids may be filtered",
            )
        for value in picked:
            if not safe_identifier(value):
                return ActionResult(ok=False, error=f"{value!r} is not a well-formed session id")
        filters["sessionIds"] = picked

    if start_time or end_time:
        begin, begin_error = _iso_or_error("the start time", start_time)
        if begin_error:
            return ActionResult(ok=False, error=begin_error)
        finish, finish_error = _iso_or_error("the end time", end_time)
        if finish_error:
            return ActionResult(ok=False, error=finish_error)
        filters["timeRange"] = {"startTime": begin, "endTime": finish}
    if filters:
        source["cloudWatchLogs"]["filterConfig"] = filters

    args = [
        catalog.SERVICE_DATA,
        "start-batch-evaluation",
        "--batch-evaluation-name",
        name,
        "--evaluators",
        json.dumps([{"evaluatorId": value} for value in ids]),
        "--data-source-config",
        json.dumps(source),
        "--client-token",
        client_token or uuid.uuid4().hex,
    ]
    if description.strip():
        args += ["--description", description.strip()]

    rc, out, err, error, denied = _run(cfg, args, ACTION_TIMEOUT_SECS)
    if error:
        return ActionResult(ok=False, error=error, denied=denied)
    if rc != 0:
        detail = (err or out or "").strip()
        return ActionResult(ok=False, error=detail or f"aws exited {rc}")
    result, parse_error = _payload_json(out)
    if parse_error:
        return ActionResult(ok=False, error=parse_error)
    return ActionResult(ok=True, result=result)


def fetch_agent_card(
    cfg: ObservatoryConfig,
    *,
    runtime_arn: str,
    qualifier: str = "",
) -> ActionResult:
    """Read a runtime's agent card, treating "not an A2A agent" as an absence.

    This is the only honest source of a runtime's input schema, and most
    runtimes do not publish one. When AgentCore says the API applies only to A2A
    agents, the answer is ``ok=True`` with ``published`` False — a fact about the
    runtime, not a failure of the request. Rendering it as an error would push
    the caller toward inventing an example payload, which is the outcome this
    whole path exists to avoid.
    """
    if not cfg.configured:
        return ActionResult(ok=False, error="no AWS region is configured")
    if not safe_identifier(runtime_arn):
        return ActionResult(ok=False, error="the runtime ARN is not a well-formed identifier")
    if qualifier and not safe_identifier(qualifier):
        return ActionResult(ok=False, error="the qualifier is not a well-formed identifier")

    args = [catalog.SERVICE_DATA, "get-agent-card", "--agent-runtime-arn", runtime_arn]
    if qualifier:
        args += ["--qualifier", qualifier]

    rc, out, err, error, denied = _run(cfg, args, ACTION_TIMEOUT_SECS)
    if error:
        return ActionResult(ok=False, error=error, denied=denied)
    if rc != 0:
        detail = (err or out or "").strip()
        if _NO_CARD_MARKER in detail.lower():
            return ActionResult(ok=True, result={"published": False})
        return ActionResult(ok=False, error=detail or f"aws exited {rc}")
    result, parse_error = _payload_json(out)
    if parse_error:
        return ActionResult(ok=False, error=parse_error)
    return ActionResult(ok=True, result={"published": True, "card": result})
