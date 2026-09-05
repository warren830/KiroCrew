"""Action layer: argv shape, payload guards, idempotency, absence vs failure.

The argv assertions are the point of this file. Three of them encode facts that
were verified against the live CLI and that a plausible reading of the API
reference gets wrong — the binary-format flag, the positional outfile, and the
33-character session-id floor. Each is asserted exactly, so removing one fails a
test instead of silently shipping an agent that receives garbage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.agentcore_observatory.backend import actions, agentcore
from kiro_crew.apps.builtins.agentcore_observatory.backend.config import ObservatoryConfig
from kiro_crew.cloud.aws import CloudActionDenied

CFG = ObservatoryConfig(profile="prof", region="us-east-2")
DATA = "bedrock-agentcore"
ARN = "arn:aws:bedrock-agentcore:us-east-2:111122223333:runtime/orders_agent-AbCdEf1234"


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[int, str, str]],
    body: bytes | None = None,
) -> list[list[str]]:
    """Replace the CLI chokepoint, recording each argv and replaying responses.

    When ``body`` is given the stub writes it to the outfile the argv names, so
    the test exercises the real "reply arrives in a file" path rather than a
    mocked-out shortcut.
    """
    seen: list[list[str]] = []

    def fake_run_aws(
        args: list[str], profile: str = "", region: str = "", **kwargs: Any
    ) -> tuple[int, str, str]:
        seen.append(list(args))
        assert profile == CFG.profile
        assert region == CFG.region
        if body is not None:
            Path(args[-1]).write_bytes(body)
        return responses[len(seen) - 1]

    monkeypatch.setattr(actions, "run_aws", fake_run_aws)
    return seen


def _deny(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_aws(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        raise CloudActionDenied("bedrock-agentcore:InvokeAgentRuntime is not allowlisted")

    monkeypatch.setattr(actions, "run_aws", fake_run_aws)


# --------------------------------------------------------------------------
# The module split is the read layer's guarantee — pin it.
# --------------------------------------------------------------------------


def test_read_layer_names_no_mutating_verb() -> None:
    """`agentcore.py` must stay provably read-only by inspection.

    The whole reason the paid verbs live in their own module is that a reader can
    confirm the read path is a read path without tracing branches. If a mutating
    verb ever appears there, that claim is false and this fails.
    """
    source = Path(agentcore.__file__).read_text(encoding="utf-8")
    for verb in (
        "invoke-agent-runtime",
        "start-batch-evaluation",
        "stop-batch-evaluation",
        "delete-batch-evaluation",
        "create-agent-runtime",
        "update-agent-runtime",
    ):
        assert verb not in source, f"{verb} belongs in actions.py, not the read layer"


# --------------------------------------------------------------------------
# invoke: the three easy-to-get-wrong argv facts
# --------------------------------------------------------------------------


def test_invoke_argv_carries_binary_format_and_positional_outfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payload must be sent raw and the reply collected from a file."""
    meta = {"runtimeSessionId": "s0", "traceId": "1-abc", "statusCode": 200}
    seen = _stub(monkeypatch, [(0, json.dumps(meta), "")], body=b"hello from the agent")

    result = actions.invoke_runtime(CFG, runtime_arn=ARN, payload='{"prompt":"hi"}')

    assert result.ok is True, result.error
    assert result.result == meta
    assert result.body == "hello from the agent"
    assert result.body_truncated is False

    argv = seen[0]
    assert argv[:2] == [DATA, "invoke-agent-runtime"]
    # CLI v2 defaults this to base64; without the flag the agent is handed the
    # caller's JSON decoded as base64.
    idx = argv.index("--cli-binary-format")
    assert argv[idx + 1] == "raw-in-base64-out"
    assert argv[argv.index("--payload") + 1] == '{"prompt":"hi"}'
    # The outfile is positional and last — it is not a flag.
    assert not argv[-1].startswith("--")
    assert "--outfile" not in argv


def test_invoke_passes_qualifier_so_a_version_can_be_tested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Testing "the version I am looking at" is the reason qualifier exists here."""
    seen = _stub(monkeypatch, [(0, "{}", "")], body=b"")
    result = actions.invoke_runtime(CFG, runtime_arn=ARN, payload="{}", qualifier="V3")
    assert result.ok is True
    assert seen[0][seen[0].index("--qualifier") + 1] == "V3"


def test_invoke_omits_session_flag_when_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No session id means let AgentCore mint one, not send an empty flag."""
    seen = _stub(monkeypatch, [(0, "{}", "")], body=b"")
    actions.invoke_runtime(CFG, runtime_arn=ARN, payload="{}")
    assert "--runtime-session-id" not in seen[0]


def test_generated_session_id_clears_the_33_character_floor() -> None:
    """`uuid4().hex` is 32 — one short — so the helper must not return bare hex."""
    session = actions.new_session_id()
    assert len(session) >= 33
    assert actions._check_session_id(session) == ""


def test_short_session_id_is_refused_with_the_real_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A doomed call is refused locally, naming the limit the service enforces."""
    seen = _stub(monkeypatch, [])
    result = actions.invoke_runtime(CFG, runtime_arn=ARN, payload="{}", session_id="abc123")
    assert result.ok is False
    assert "33" in result.error
    assert seen == []


# --------------------------------------------------------------------------
# invoke: payload guards — never synthesise, never overrun
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["", "   ", "\n"])
def test_empty_payload_is_refused_and_never_invented(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    seen = _stub(monkeypatch, [])
    result = actions.invoke_runtime(CFG, runtime_arn=ARN, payload=payload)
    assert result.ok is False
    assert "payload is required" in result.error
    assert seen == []


def test_oversize_payload_is_refused_before_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub(monkeypatch, [])
    result = actions.invoke_runtime(
        CFG, runtime_arn=ARN, payload="x" * (actions.MAX_PAYLOAD_BYTES + 1)
    )
    assert result.ok is False
    assert str(actions.MAX_PAYLOAD_BYTES) in result.error
    assert seen == []


def test_long_reply_is_marked_truncated_not_silently_clipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = b"a" * (actions.MAX_RESPONSE_BYTES + 500)
    _stub(monkeypatch, [(0, "{}", "")], body=big)
    result = actions.invoke_runtime(CFG, runtime_arn=ARN, payload="{}")
    assert result.ok is True
    assert result.body_truncated is True
    assert len(result.body) == actions.MAX_RESPONSE_BYTES


def test_non_json_reply_body_is_preserved_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent may answer plain text or SSE; that is not a parse failure."""
    _stub(monkeypatch, [(0, "{}", "")], body=b"data: partial\n\ndata: done\n\n")
    result = actions.invoke_runtime(CFG, runtime_arn=ARN, payload="{}")
    assert result.ok is True
    assert "data: done" in result.body


def test_invoke_failure_reports_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [(255, "", "AccessDeniedException: not authorized")], body=b"")
    result = actions.invoke_runtime(CFG, runtime_arn=ARN, payload="{}")
    assert result.ok is False
    assert "AccessDeniedException" in result.error


def test_a_rejected_payload_yields_the_fields_the_runtime_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wasted call becomes the answer.

    Verbatim shape from a live rejection: HTTP 200, and the agent's own Pydantic
    error naming the two fields it required. Without this the operator reads the
    error, opens the runtime's source, and types the fields by hand — which is
    exactly the round trip this exists to remove.
    """
    reply = (
        b"2 validation errors for OrdersChatRequest\n"
        b"chatId\n  Field required [type=missing, input_value={'prompt': 'x'}, input_type=dict]\n"
        b"messageLogId\n  Field required [type=missing, input_value={'prompt': 'x'},"
        b" input_type=dict]\n"
    )
    _stub(monkeypatch, [(0, '{"statusCode": 200}', "")], body=reply)
    result = actions.invoke_runtime(CFG, runtime_arn=ARN, payload='{"prompt":"x"}')
    assert result.ok is True
    assert result.derived_fields == ["chatId", "messageLogId"]


def test_a_healthy_reply_derives_no_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty list must never read as "the payload was fine" by accident."""
    _stub(monkeypatch, [(0, "{}", "")], body=b'{"answer": "hello"}')
    result = actions.invoke_runtime(CFG, runtime_arn=ARN, payload="{}")
    assert result.ok is True
    assert result.derived_fields == []


# --------------------------------------------------------------------------
# batch evaluation: name rules, idempotency, the max-1 service list
# --------------------------------------------------------------------------


def _start(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[Any, list[list[str]]]:
    seen = _stub(monkeypatch, [(0, json.dumps({"batchEvaluationId": "eval_1"}), "")])
    params: dict[str, Any] = {
        "name": "nightly_regression",
        "evaluator_ids": ["Builtin.Helpfulness"],
        "service_name": "orders_agent",
        "log_group_names": ["/aws/bedrock-agentcore/runtimes/orders_agent-AbCdEf1234"],
    }
    params.update(kwargs)
    return actions.start_batch_evaluation(CFG, **params), seen


def test_start_batch_evaluation_argv_and_generated_client_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token is always sent: a duplicate paid job cannot be detected later."""
    result, seen = _start(monkeypatch)
    assert result.ok is True, result.error
    assert result.result == {"batchEvaluationId": "eval_1"}

    argv = seen[0]
    assert argv[:2] == [DATA, "start-batch-evaluation"]
    assert argv[argv.index("--batch-evaluation-name") + 1] == "nightly_regression"
    assert json.loads(argv[argv.index("--evaluators") + 1]) == [
        {"evaluatorId": "Builtin.Helpfulness"}
    ]
    token = argv[argv.index("--client-token") + 1]
    assert token, "a client token must always be sent"

    source = json.loads(argv[argv.index("--data-source-config") + 1])
    logs = source["cloudWatchLogs"]
    # The API bound is min 1 / max 1 — exactly one service name.
    assert logs["serviceNames"] == ["orders_agent"]
    assert len(logs["logGroupNames"]) == 1
    assert "filterConfig" not in logs


def test_supplied_client_token_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry must be able to reuse the token, or idempotency buys nothing."""
    _, seen = _start(monkeypatch, client_token="caller-chosen-token")
    assert seen[0][seen[0].index("--client-token") + 1] == "caller-chosen-token"


def test_hyphenated_name_is_refused_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """AgentCore's name pattern forbids '-', which the obvious name uses."""
    seen = _stub(monkeypatch, [])
    result = actions.start_batch_evaluation(
        CFG,
        name="nightly-regression",
        evaluator_ids=["Builtin.Helpfulness"],
        service_name="orders_agent",
        log_group_names=["/aws/x"],
    )
    assert result.ok is False
    assert "hyphen" in result.error.lower()
    assert seen == []


def test_time_range_reaches_the_filter_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _, seen = _start(
        monkeypatch, start_time="2026-09-01T00:00:00Z", end_time="2026-09-02T00:00:00Z"
    )
    source = json.loads(seen[0][seen[0].index("--data-source-config") + 1])
    assert source["cloudWatchLogs"]["filterConfig"]["timeRange"] == {
        "startTime": "2026-09-01T00:00:00Z",
        "endTime": "2026-09-02T00:00:00Z",
    }


def test_half_open_time_range_is_a_caller_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """One bound alone would silently widen a paid job's scope."""
    seen = _stub(monkeypatch, [])
    result = actions.start_batch_evaluation(
        CFG,
        name="nightly_regression",
        evaluator_ids=["Builtin.Helpfulness"],
        service_name="orders_agent",
        log_group_names=["/aws/x"],
        start_time="2026-09-01T00:00:00Z",
    )
    assert result.ok is False
    assert "end time" in result.error
    assert seen == []


@pytest.mark.parametrize(
    "kwargs,fragment",
    [
        ({"evaluator_ids": []}, "at least one evaluator"),
        ({"evaluator_ids": ["Builtin.X"] * 11}, "at most 10"),
        ({"evaluator_ids": ["not a valid id"]}, "well-formed evaluator id"),
        ({"log_group_names": []}, "at least one CloudWatch log group"),
        ({"log_group_names": [f"/aws/g{n}" for n in range(6)]}, "at most 5"),
        ({"service_name": ""}, "service name"),
    ],
)
def test_batch_evaluation_bounds_are_checked_locally(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], fragment: str
) -> None:
    """A bound the service enforces must not be discovered after confirmation."""
    seen = _stub(monkeypatch, [])
    params: dict[str, Any] = {
        "name": "nightly_regression",
        "evaluator_ids": ["Builtin.Helpfulness"],
        "service_name": "orders_agent",
        "log_group_names": ["/aws/x"],
    }
    params.update(kwargs)
    result = actions.start_batch_evaluation(CFG, **params)
    assert result.ok is False
    assert fragment in result.error
    assert seen == []


# --------------------------------------------------------------------------
# agent card: absence is not failure
# --------------------------------------------------------------------------


def test_non_a2a_runtime_reports_no_card_rather_than_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed message for an ordinary runtime, mapped to an absence."""
    _stub(
        monkeypatch,
        [
            (
                254,
                "",
                "ValidationException: GetAgentCard API is only supported for A2A agents",
            )
        ],
    )
    result = actions.fetch_agent_card(CFG, runtime_arn=ARN)
    assert result.ok is True
    assert result.result == {"published": False}


def test_other_validation_errors_stay_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the A2A message is an absence; everything else is a real failure."""
    _stub(monkeypatch, [(254, "", "ValidationException: runtime does not exist")])
    result = actions.fetch_agent_card(CFG, runtime_arn=ARN)
    assert result.ok is False
    assert "does not exist" in result.error


def test_published_card_is_returned_under_a_published_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = {"name": "orders", "skills": []}
    _stub(monkeypatch, [(0, json.dumps(card), "")])
    result = actions.fetch_agent_card(CFG, runtime_arn=ARN)
    assert result.ok is True
    assert result.result == {"published": True, "card": card}


# --------------------------------------------------------------------------
# Cross-cutting: denial, configuration, malformed identifiers
# --------------------------------------------------------------------------


def test_chokepoint_denial_is_classified_on_every_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`denied` must be distinguishable from an AWS error: different remedies."""
    _deny(monkeypatch)
    for result in (
        actions.invoke_runtime(CFG, runtime_arn=ARN, payload="{}"),
        actions.fetch_agent_card(CFG, runtime_arn=ARN),
        actions.start_batch_evaluation(
            CFG,
            name="nightly_regression",
            evaluator_ids=["Builtin.Helpfulness"],
            service_name="orders_agent",
            log_group_names=["/aws/x"],
        ),
    ):
        assert result.ok is False
        assert result.denied is True
        assert "agent session" in result.error


def test_unconfigured_region_blocks_every_action(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch, [])
    blank = ObservatoryConfig()
    assert actions.invoke_runtime(blank, runtime_arn=ARN, payload="{}").ok is False
    assert actions.fetch_agent_card(blank, runtime_arn=ARN).ok is False
    assert (
        actions.start_batch_evaluation(
            blank,
            name="nightly_regression",
            evaluator_ids=["Builtin.Helpfulness"],
            service_name="orders_agent",
            log_group_names=["/aws/x"],
        ).ok
        is False
    )
    assert seen == []


@pytest.mark.parametrize("arn", ["--profile", "arn with spaces", "", "a" * 300])
def test_malformed_runtime_arn_never_reaches_an_argv(
    monkeypatch: pytest.MonkeyPatch, arn: str
) -> None:
    """A leading dash would read as an option; length and charset are bounded."""
    seen = _stub(monkeypatch, [])
    assert actions.invoke_runtime(CFG, runtime_arn=arn, payload="{}").ok is False
    assert actions.fetch_agent_card(CFG, runtime_arn=arn).ok is False
    assert seen == []
