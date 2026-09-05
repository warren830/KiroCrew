"""Payload hints: parsing real validation errors and real log lines.

Every fixture here has the shape of a live run against a real runtime — the
Pydantic error it produced when sent a payload it did not accept, and the request
line it logs for an accepted call, with identifiers replaced. Synthetic fixtures
would have let the parser pass while missing the two-line Pydantic layout and the
OTEL envelope that wraps every real AgentCore log message.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.apps.builtins.agentcore_observatory.backend import catalog, payload_hints
from kiro_crew.apps.builtins.agentcore_observatory.backend.config import ObservatoryConfig
from kiro_crew.cloud.aws import CloudActionDenied

CFG = ObservatoryConfig(profile="prof", region="us-east-2")
ARN = "arn:aws:bedrock-agentcore:us-east-2:111122223333:runtime/orders_agent-AbCdEf1234"
GROUP = "/aws/bedrock-agentcore/runtimes/orders_agent-AbCdEf1234-DEFAULT"

# Verbatim from the runtime's own log, identifiers aside.
REAL_PYDANTIC_ERROR = """请求体不符合契约: 2 validation errors for OrdersChatRequest
chatId
  Field required [type=missing, input_value={'prompt': 'hello'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
messageLogId
  Field required [type=missing, input_value={'prompt': 'hello'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.13/v/missing
"""

REAL_ACCEPTED_LINE = "INFO:orders.main:invoke chatId=146826600 log_id=407829079 stream=True"


def _stub(
    monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, str, str]]
) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_run_aws(
        args: list[str], profile: str = "", region: str = "", **kwargs: Any
    ) -> tuple[int, str, str]:
        seen.append(list(args))
        return responses[len(seen) - 1]

    monkeypatch.setattr(payload_hints, "run_aws", fake_run_aws)
    return seen


def _groups(*names: str) -> tuple[int, str, str]:
    return (0, json.dumps({"logGroups": [{"logGroupName": n} for n in names]}), "")


def _events(*messages: str) -> tuple[int, str, str]:
    return (0, json.dumps({"events": [{"message": m} for m in messages]}), "")


# --------------------------------------------------------------------------
# Required fields: what the runtime itself said it needed
# --------------------------------------------------------------------------


def test_extracts_required_fields_from_the_real_pydantic_error() -> None:
    """Pydantic puts the field name on the line ABOVE its reason."""
    assert payload_hints.parse_required_fields(REAL_PYDANTIC_ERROR) == [
        "chatId",
        "messageLogId",
    ]


def test_a_wrong_type_is_not_reported_as_required() -> None:
    """`type=string_type` means the field WAS sent — a different claim entirely."""
    text = "1 validation error for X\nchatId\n  Input should be a valid string [type=string_type]"
    assert payload_hints.parse_required_fields(text) == []


def test_nested_paths_survive() -> None:
    text = (
        "businessData.sourceApp\n  Field required [type=missing, input_value={}, input_type=dict]"
    )
    assert payload_hints.parse_required_fields(text) == ["businessData.sourceApp"]


def test_json_schema_phrasing_is_also_understood() -> None:
    assert payload_hints.parse_required_fields("'chatId' is a required property") == ["chatId"]


def test_the_same_error_repeated_across_lines_is_not_counted_twice() -> None:
    doubled = REAL_PYDANTIC_ERROR + REAL_PYDANTIC_ERROR
    assert payload_hints.parse_required_fields(doubled) == ["chatId", "messageLogId"]


def test_prose_without_a_validation_error_yields_nothing() -> None:
    assert payload_hints.parse_required_fields("INFO: warmup complete 1.1s") == []


# --------------------------------------------------------------------------
# The OTEL envelope: where a multi-line error actually lives
# --------------------------------------------------------------------------


def test_field_names_survive_the_otel_envelope() -> None:
    """The defect a live run exposed, pinned.

    AgentCore emits every event twice: a plain line, and an OTEL envelope whose
    ``body`` is a JSON **string**. Only the envelope carries the full multi-line
    error — and inside a JSON string its newlines are escaped, so a line-oriented
    parser reading the raw event sees one long line and finds nothing.

    The first live scan hit exactly this: it recovered the rejected request body
    (a regex needing no newlines) while silently missing the field names.
    """
    envelope = json.dumps(
        {
            "resource": {"attributes": {"service.name": "orders.DEFAULT"}},
            "scope": {"name": "orders.main"},
            "severityText": "WARN",
            "body": REAL_PYDANTIC_ERROR,
        }
    )
    # Raw, the escaped newlines defeat the parser — this is the bug, asserted.
    assert payload_hints.parse_required_fields(envelope) == []
    # Flattened, the same event yields the field names.
    flattened = payload_hints.log_text([envelope])
    assert payload_hints.parse_required_fields(flattened) == ["chatId", "messageLogId"]


def test_log_text_keeps_plain_lines_as_they_are() -> None:
    assert "invoke chatId=" in payload_hints.log_text([REAL_ACCEPTED_LINE])


def test_log_text_tolerates_an_envelope_that_is_not_json() -> None:
    assert payload_hints.log_text(["{not json", "plain"]).splitlines() == ["{not json", "plain"]


# --------------------------------------------------------------------------
# Rejected inputs: the shape somebody actually sent
# --------------------------------------------------------------------------


def test_echoed_rejected_body_is_kept_verbatim() -> None:
    """It is a Python repr, so converting it to JSON would change the quoting."""
    assert payload_hints.parse_rejected_inputs(REAL_PYDANTIC_ERROR) == ["{'prompt': 'hello'}"]


def test_rejected_inputs_are_deduplicated() -> None:
    assert len(payload_hints.parse_rejected_inputs(REAL_PYDANTIC_ERROR * 3)) == 1


# --------------------------------------------------------------------------
# Observed fields: weaker evidence, so it carries provenance
# --------------------------------------------------------------------------


def test_observed_field_carries_the_line_it_came_from() -> None:
    hits = payload_hints.parse_observed_fields(['{"chatId": 1, "messages": []}'])
    assert [h.name for h in hits] == ["chatId", "messages"]
    assert all('"chatId"' in h.excerpt for h in hits), "evidence must be shown, not implied"


def test_the_otel_envelope_does_not_masquerade_as_request_fields() -> None:
    """The denylist this replaced lost to a real key set.

    A live scan reported ``flags`` as "a field this runtime accepts", because the
    envelope's real top-level keys are ``attributes, body, flags,
    observedTimeUnixNano, resource, scope, severityNumber, severityText, spanId,
    timeUnixNano, traceId`` — and a list of names to exclude will always be one
    key behind. Detection is structural now, so a new envelope key cannot leak.
    """
    envelope = json.dumps(
        {
            "resource": {"attributes": {"service.name": "orders.DEFAULT"}},
            "scope": {"name": "orders.main"},
            "severityText": "WARN",
            "severityNumber": 13,
            "spanId": "abc",
            "traceId": "def",
            "flags": 1,
            "timeUnixNano": 1,
            "observedTimeUnixNano": 2,
            "attributes": {},
            "body": "boom",
        }
    )
    assert payload_hints.parse_observed_fields([envelope]) == []
    assert payload_hints.is_otel_record(json.loads(envelope)) is True


def test_an_application_object_sharing_one_word_with_otel_is_kept() -> None:
    """Two markers are required, so a real field named `scope` still counts."""
    obj = json.dumps({"scope": "orders", "chatId": 1})
    assert payload_hints.is_otel_record(json.loads(obj)) is False
    assert [f.name for f in payload_hints.parse_observed_fields([obj])] == ["scope", "chatId"]


def test_an_object_embedded_in_prose_is_still_read() -> None:
    hits = payload_hints.parse_observed_fields(['INFO:orders: got {"chatId": 7} from caller'])
    assert [h.name for h in hits] == ["chatId"]


def test_a_line_with_no_json_contributes_nothing() -> None:
    # The accepted-request line uses key=value, not JSON: it is real, and this
    # parser deliberately does not mine it, because `stream=True` is not JSON and
    # guessing at ad-hoc formats is how a wrong field name gets presented as fact.
    assert payload_hints.parse_observed_fields([REAL_ACCEPTED_LINE]) == []


# --------------------------------------------------------------------------
# The AWS calls
# --------------------------------------------------------------------------


def _streams(*names: str) -> tuple[int, str, str]:
    return (0, json.dumps({"logStreams": [{"logStreamName": n} for n in names]}), "")


def test_argv_uses_the_logs_service_and_reads_each_stream_from_its_TAIL(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the tail is the fix for a real, silent miss.

    A live run against a real runtime scanned 300 events and reported nothing,
    because ``filter-log-events`` returns a window in ascending time order — the
    oldest events fill the cap and the recent rejection never arrives. Each
    stream is now read newest-first, which is what puts the field names inside
    the bound.
    """
    seen = _stub(
        monkeypatch,
        [_groups(GROUP), _streams("runtime-logs-a"), _events(REAL_PYDANTIC_ERROR)],
    )
    result = payload_hints.scan_payload_hints(CFG, runtime_arn=ARN)

    assert result.ok is True, result.error
    assert result.log_group == GROUP
    assert result.required_fields == ["chatId", "messageLogId"]

    discover, streams, read = seen
    assert discover[0] == catalog.SERVICE_LOGS
    assert discover[1] == "describe-log-groups"
    # Discovered by prefix, never assembled from a guessed naming convention.
    assert discover[discover.index("--log-group-name-prefix") + 1] == (
        "/aws/bedrock-agentcore/runtimes/orders_agent-AbCdEf1234"
    )

    assert streams[1] == "describe-log-streams"
    assert streams[streams.index("--order-by") + 1] == "LastEventTime"
    assert "--descending" in streams

    assert read[1] == "get-log-events"
    assert read[read.index("--log-stream-name") + 1] == "runtime-logs-a"
    # The load-bearing flag. Without it the read starts at the oldest event.
    assert "--no-start-from-head" in read
    assert "--start-from-head" not in read


def test_several_streams_are_combined(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime writes a container stream and an otel stream; both may matter."""
    _stub(
        monkeypatch,
        [
            _groups(GROUP),
            _streams("a", "b"),
            _events("chatId\n  Field required [type=missing, input_value={}, input_type=dict]"),
            _events('{"messages": []}'),
        ],
    )
    result = payload_hints.scan_payload_hints(CFG, runtime_arn=ARN)
    assert result.required_fields == ["chatId"]
    assert [f.name for f in result.observed_fields] == ["messages"]


def test_one_unreadable_stream_does_not_discard_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(
        monkeypatch,
        [
            _groups(GROUP),
            _streams("bad", "good"),
            (255, "", "ResourceNotFoundException: stream gone"),
            _events(REAL_PYDANTIC_ERROR),
        ],
    )
    result = payload_hints.scan_payload_hints(CFG, runtime_arn=ARN)
    assert result.ok is True
    assert result.required_fields == ["chatId", "messageLogId"]


def test_a_runtime_with_no_log_group_is_an_absence_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule as an unpublished agent card: absence must not read as error."""
    seen = _stub(monkeypatch, [_groups()])
    result = payload_hints.scan_payload_hints(CFG, runtime_arn=ARN)
    assert result.ok is True
    assert result.log_group == ""
    assert result.required_fields == []
    assert len(seen) == 1, "no scan should be attempted without a group"


def test_an_authorized_scan_that_finds_nothing_is_still_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(
        monkeypatch,
        [_groups(GROUP), _streams("a"), _events("INFO:orders.warmup:预热完成 1.1s")],
    )
    result = payload_hints.scan_payload_hints(CFG, runtime_arn=ARN)
    assert result.ok is True
    assert result.scanned_events == 1
    assert result.required_fields == []
    assert result.observed_fields == []


def test_denial_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_a: Any, **_k: Any) -> tuple[int, str, str]:
        raise CloudActionDenied("logs:DescribeLogGroups is not allowlisted")

    monkeypatch.setattr(payload_hints, "run_aws", fake)
    result = payload_hints.scan_payload_hints(CFG, runtime_arn=ARN)
    assert result.ok is False
    assert result.denied is True


def test_a_missing_permission_reports_as_an_error_not_an_empty_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the app: an authorization failure is never "nothing"."""
    _stub(monkeypatch, [(255, "", "AccessDeniedException: logs:DescribeLogGroups denied")])
    result = payload_hints.scan_payload_hints(CFG, runtime_arn=ARN)
    assert result.ok is False
    assert "AccessDenied" in result.error
    assert result.required_fields == []


@pytest.mark.parametrize("arn", ["", "--profile", "no-slash", "arn:aws:x:::runtime/bad id"])
def test_malformed_arn_never_reaches_a_log_group_name(
    monkeypatch: pytest.MonkeyPatch, arn: str
) -> None:
    seen = _stub(monkeypatch, [])
    assert payload_hints.scan_payload_hints(CFG, runtime_arn=arn).ok is False
    assert seen == []


def test_unconfigured_region_blocks_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [])
    assert payload_hints.scan_payload_hints(ObservatoryConfig(), runtime_arn=ARN).ok is False
    assert seen == []
