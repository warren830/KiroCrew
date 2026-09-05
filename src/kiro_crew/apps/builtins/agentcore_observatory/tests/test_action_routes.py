"""Action HTTP surface: confirmation gate, status mapping, field plumbing.

The confirmation tests are the load-bearing ones. Every other guard in this app
is structural — a catalog row, an argv shape, a regex — but nothing below the
route can distinguish a deliberate click from a retry loop, so a missing
confirmation must be refused *here* and the refusal must be pinned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.agentcore_observatory.backend import actions, routes

pytestmark = pytest.mark.asyncio

BASE = "/api/apps/agentcore-observatory"
ARN = "arn:aws:bedrock-agentcore:us-east-2:111122223333:runtime/orders_agent-AbCdEf1234"

INVOKE = f"{BASE}/action/invoke"
EVAL = f"{BASE}/action/batch-evaluation"
CARD = f"{BASE}/agent-card"

_EVAL_BODY: dict[str, Any] = {
    "confirm": True,
    "name": "nightly_regression",
    "evaluatorIds": ["Builtin.Helpfulness"],
    "serviceName": "orders_agent",
    "logGroupNames": ["/aws/bedrock-agentcore/runtimes/orders_agent-AbCdEf1234"],
}


@pytest.fixture
def app_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app's data dir at a tmp dir, so no real config is touched."""
    monkeypatch.setattr(
        "kiro_crew.apps.builtins.agentcore_observatory.backend.config.app_data_dir",
        lambda _name: tmp_path,
    )
    return tmp_path


def _enable(monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> None:
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: enabled)


def _configure(app_root: Path) -> None:
    (app_root / "config.json").write_text(
        '{"profile": "prof", "region": "us-east-2"}', encoding="utf-8"
    )


async def _client() -> TestClient:
    app = web.Application()
    routes.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _stub_action(
    monkeypatch: pytest.MonkeyPatch, name: str, result: actions.ActionResult
) -> list[dict[str, Any]]:
    """Replace one action, recording the keyword arguments the route passed."""
    seen: list[dict[str, Any]] = []

    def fake(_cfg: Any, **kwargs: Any) -> actions.ActionResult:
        seen.append(kwargs)
        return result

    monkeypatch.setattr(actions, name, fake)
    return seen


# --------------------------------------------------------------------------
# The enablement gate covers the new routes too.
# --------------------------------------------------------------------------


async def test_disabled_app_denies_the_action_routes(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch, False)
    client = await _client()
    try:
        for path in (INVOKE, EVAL):
            res = await client.post(path, json={"confirm": True})
            assert res.status == 403, path
            assert (await res.json())["code"] == "app_disabled"
        res = await client.get(CARD, params={"runtimeArn": ARN})
        assert res.status == 403
        assert (await res.json())["code"] == "app_disabled"
    finally:
        await client.close()


# --------------------------------------------------------------------------
# Confirmation: the only thing standing between a click and a paid loop.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "confirm",
    [None, False, "true", "yes", 1, {}, []],
    ids=["absent", "false", "str-true", "str-yes", "int-1", "obj", "list"],
)
async def test_action_without_a_boolean_true_confirmation_is_refused(
    monkeypatch: pytest.MonkeyPatch, app_root: Path, confirm: Any
) -> None:
    """A truthy string must not confirm a paid action.

    ``"false"`` is truthy in Python, and a form that serialises checkboxes as
    strings would otherwise confirm by accident — which is exactly the accident
    this field exists to prevent.
    """
    _enable(monkeypatch)
    _configure(app_root)
    invoked = _stub_action(monkeypatch, "invoke_runtime", actions.ActionResult(ok=True))
    started = _stub_action(monkeypatch, "start_batch_evaluation", actions.ActionResult(ok=True))
    client = await _client()
    try:
        body: dict[str, Any] = {"runtimeArn": ARN, "payload": "{}"}
        if confirm is not None:
            body["confirm"] = confirm
        res = await client.post(INVOKE, json=body)
        assert res.status == 400
        assert (await res.json())["code"] == "confirmation_required"

        eval_body = dict(_EVAL_BODY)
        eval_body.pop("confirm")
        if confirm is not None:
            eval_body["confirm"] = confirm
        res = await client.post(EVAL, json=eval_body)
        assert res.status == 400
        assert (await res.json())["code"] == "confirmation_required"
    finally:
        await client.close()
    assert invoked == [], "an unconfirmed action must never reach the action layer"
    assert started == []


async def test_malformed_body_is_a_400_with_a_code(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch)
    _configure(app_root)
    client = await _client()
    try:
        res = await client.post(INVOKE, data="not json")
        assert res.status == 400
        assert (await res.json())["code"] == "invalid_json"
        res = await client.post(INVOKE, json=[1, 2])
        assert res.status == 400
        assert (await res.json())["code"] == "invalid_json"
    finally:
        await client.close()


async def test_unconfigured_region_is_a_409_before_any_call(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """No region means no call is possible; say so rather than attempting one."""
    _enable(monkeypatch)
    invoked = _stub_action(monkeypatch, "invoke_runtime", actions.ActionResult(ok=True))
    client = await _client()
    try:
        res = await client.post(INVOKE, json={"confirm": True, "runtimeArn": ARN})
        assert res.status == 409
        assert (await res.json())["code"] == "not_configured"
    finally:
        await client.close()
    assert invoked == []


# --------------------------------------------------------------------------
# Field plumbing: the route must hand the action layer what the user typed.
# --------------------------------------------------------------------------


async def test_invoke_forwards_every_field(monkeypatch: pytest.MonkeyPatch, app_root: Path) -> None:
    _enable(monkeypatch)
    _configure(app_root)
    session = actions.new_session_id()
    seen = _stub_action(
        monkeypatch,
        "invoke_runtime",
        actions.ActionResult(ok=True, result={"traceId": "1-abc"}, body="hi"),
    )
    client = await _client()
    try:
        res = await client.post(
            INVOKE,
            json={
                "confirm": True,
                "runtimeArn": ARN,
                "payload": '{"prompt":"hi"}',
                "qualifier": "V3",
                "sessionId": session,
            },
        )
        assert res.status == 200
        payload = await res.json()
        assert payload["ok"] is True
        assert payload["result"] == {"traceId": "1-abc"}
        assert payload["body"] == "hi"
    finally:
        await client.close()
    assert seen == [
        {
            "runtime_arn": ARN,
            "payload": '{"prompt":"hi"}',
            "qualifier": "V3",
            "session_id": session,
        }
    ]


async def test_batch_evaluation_forwards_lists_and_time_range(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch)
    _configure(app_root)
    seen = _stub_action(
        monkeypatch,
        "start_batch_evaluation",
        actions.ActionResult(ok=True, result={"batchEvaluationId": "eval_1"}),
    )
    client = await _client()
    try:
        body = dict(_EVAL_BODY)
        body["startTime"] = "2026-09-01T00:00:00Z"
        body["endTime"] = "2026-09-02T00:00:00Z"
        body["sessionIds"] = ["sess-1", "sess-2"]
        res = await client.post(EVAL, json=body)
        assert res.status == 200
        assert (await res.json())["result"] == {"batchEvaluationId": "eval_1"}
    finally:
        await client.close()
    assert seen[0]["evaluator_ids"] == ["Builtin.Helpfulness"]
    assert seen[0]["log_group_names"] == _EVAL_BODY["logGroupNames"]
    assert seen[0]["session_ids"] == ["sess-1", "sess-2"]
    assert seen[0]["start_time"] == "2026-09-01T00:00:00Z"
    assert seen[0]["end_time"] == "2026-09-02T00:00:00Z"


async def test_non_string_list_entries_are_dropped_not_stringified(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """A nested object must not arrive as a plausible-looking identifier."""
    _enable(monkeypatch)
    _configure(app_root)
    seen = _stub_action(monkeypatch, "start_batch_evaluation", actions.ActionResult(ok=True))
    client = await _client()
    try:
        body = dict(_EVAL_BODY)
        body["evaluatorIds"] = ["Builtin.Helpfulness", {"evaluatorId": "x"}, 7, None]
        res = await client.post(EVAL, json=body)
        assert res.status == 200
    finally:
        await client.close()
    assert seen[0]["evaluator_ids"] == ["Builtin.Helpfulness"]


async def test_non_string_scalar_field_does_not_reach_the_action_layer(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    _enable(monkeypatch)
    _configure(app_root)
    seen = _stub_action(monkeypatch, "invoke_runtime", actions.ActionResult(ok=True))
    client = await _client()
    try:
        res = await client.post(
            INVOKE, json={"confirm": True, "runtimeArn": {"nested": 1}, "payload": "{}"}
        )
        assert res.status == 200
    finally:
        await client.close()
    assert seen[0]["runtime_arn"] == ""


# --------------------------------------------------------------------------
# Status mapping: AWS refusing is a 200; the chokepoint refusing is a 403.
# --------------------------------------------------------------------------


async def test_aws_failure_is_a_200_with_ok_false(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """Same shape as `GET /resource/{type}`, so the UI has one error path."""
    _enable(monkeypatch)
    _configure(app_root)
    _stub_action(
        monkeypatch,
        "invoke_runtime",
        actions.ActionResult(ok=False, error="AccessDeniedException: not authorized"),
    )
    client = await _client()
    try:
        res = await client.post(INVOKE, json={"confirm": True, "runtimeArn": ARN})
        assert res.status == 200
        payload = await res.json()
        assert payload["ok"] is False
        assert "AccessDeniedException" in payload["error"]
    finally:
        await client.close()


async def test_chokepoint_denial_is_a_403(monkeypatch: pytest.MonkeyPatch, app_root: Path) -> None:
    """A denial means no IAM change helps, so it is this app refusing."""
    _enable(monkeypatch)
    _configure(app_root)
    denial = actions.ActionResult(
        ok=False, denied=True, error="not permitted from an agent session"
    )
    for name, call in (
        ("invoke_runtime", "post"),
        ("start_batch_evaluation", "post"),
        ("fetch_agent_card", "get"),
    ):
        _stub_action(monkeypatch, name, denial)
    client = await _client()
    try:
        for path, body in ((INVOKE, {"confirm": True}), (EVAL, dict(_EVAL_BODY))):
            res = await client.post(path, json=body)
            assert res.status == 403, path
            assert (await res.json())["code"] == "chokepoint_denied"
        res = await client.get(CARD, params={"runtimeArn": ARN})
        assert res.status == 403
        assert (await res.json())["code"] == "chokepoint_denied"
    finally:
        await client.close()


# --------------------------------------------------------------------------
# Agent card: a free read, and absence is a 200.
# --------------------------------------------------------------------------


async def test_agent_card_absence_is_a_200(monkeypatch: pytest.MonkeyPatch, app_root: Path) -> None:
    """ "No card published" is a fact about the runtime, not a request failure."""
    _enable(monkeypatch)
    _configure(app_root)
    seen = _stub_action(
        monkeypatch,
        "fetch_agent_card",
        actions.ActionResult(ok=True, result={"published": False}),
    )
    client = await _client()
    try:
        res = await client.get(CARD, params={"runtimeArn": ARN, "qualifier": "V3"})
        assert res.status == 200
        payload = await res.json()
        assert payload["ok"] is True
        assert payload["result"] == {"published": False}
    finally:
        await client.close()
    assert seen == [{"runtime_arn": ARN, "qualifier": "V3"}]


async def test_agent_card_needs_no_confirmation(
    monkeypatch: pytest.MonkeyPatch, app_root: Path
) -> None:
    """It is a read that costs nothing; gating it would train users to click through."""
    _enable(monkeypatch)
    _configure(app_root)
    _stub_action(
        monkeypatch,
        "fetch_agent_card",
        actions.ActionResult(ok=True, result={"published": True, "card": {}}),
    )
    client = await _client()
    try:
        res = await client.get(CARD, params={"runtimeArn": ARN})
        assert res.status == 200
    finally:
        await client.close()
