"""Tests for the mid-turn steer branch in POST /api/chat (api_chat handler).

Exercises the steer path that reaches the running turn's live AcpClient via
``slot._acp_client``:
  * steered success -> broadcasts ``steer_push`` and returns ``{steered: True}``;
  * steer unavailable (no live client) -> safe fall-through to the queue;
  * steer raises -> caught, falls through to the queue (message never dropped).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat import api_chat_slot_queue_steer


@pytest.fixture
def _patch_sel():
    """Patch sel() so the handler doesn't touch a real SecurityEventLog."""
    mock_sel = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


def _running_slot(state, key="test"):
    """Create a slot and make it look like a turn is in flight.

    ``running`` is ``task is not None and not task.done()``.
    """
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


class TestApiChatSteer:
    @pytest.mark.asyncio
    async def test_steer_injects_into_running_turn(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("steered") is True
            assert data.get("queued") is not True

        client_mock.steer.assert_awaited_once_with("go left")
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" in events
        assert "queue_push" not in events  # steered, not queued

    @pytest.mark.asyncio
    async def test_app_authenticated_steer_falls_back_to_fail_closed_queue(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """An app cannot inject into a live human-origin turn and inherit its
        authority; its text waits as an automation-origin successor turn."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._app = "app-A"
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        @web.middleware
        async def _inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, _inject_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        client_mock.steer.assert_not_awaited()
        assert slot._queue[-1].get("_directive_user_origin") is not True

    @pytest.mark.asyncio
    async def test_steer_unavailable_falls_back_to_queue(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = None  # no live client -> cannot steer

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        # message queued (not dropped) and broadcast as queue_push, not steer_push
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "queue_push" in events
        assert "steer_push" not in events

    @pytest.mark.asyncio
    async def test_steer_error_falls_back_to_queue(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=RuntimeError("boom"))
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        client_mock.steer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_steer_cuts_segment_before_user_append(self, tmp_path, monkeypatch, _patch_sel):
        """The segment cut runs BEFORE the steer user message is persisted, so
        the flushed pre-steer assistant text lands ABOVE the steer bubble."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        roles_at_cut: list[str] = []

        def _cut() -> None:
            # Snapshot the transcript at cut time, then flush like the real
            # closure does (persist the accumulated segment as assistant).
            roles_at_cut.extend(m["role"] for m in slot.messages)
            slot.append("assistant", "pre-steer text", "msg msg-a")

        slot._steer_segment_cut = _cut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        # Cut ran before the user append: no user message in the snapshot.
        assert "user" not in roles_at_cut
        # Persisted order: pre-steer assistant ABOVE the steer user bubble.
        roles = [m["role"] for m in slot.messages]
        assert roles.index("assistant") < roles.index("user")
        steer_msg = next(m for m in slot.messages if m["role"] == "user")
        assert steer_msg.get("meta", {}).get("steer") is True

    @pytest.mark.asyncio
    async def test_steer_cut_failure_does_not_lose_steer(self, tmp_path, monkeypatch, _patch_sel):
        """A raising cut closure is best-effort: the steer user message is
        still persisted and steer_push still broadcast."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock
        slot._steer_segment_cut = MagicMock(side_effect=RuntimeError("boom"))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        slot._steer_segment_cut.assert_called_once()
        assert any(m["role"] == "user" and m.get("meta", {}).get("steer") for m in slot.messages)
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" in events


class TestApiChatQueueSteer:
    def _app(self, state):
        app = _make_app(state)
        app.router.add_post(
            "/api/chat/slots/{slot}/queue/{queue_id}/steer",
            api_chat_slot_queue_steer,
        )
        return app

    @pytest.mark.asyncio
    async def test_claims_queue_item_and_steers_live_turn(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock(supports_steer=True)
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock
        queue_id = slot.queue_append("also check the docs", directive_user_origin=True)
        slot.append(
            "queued",
            "also check the docs",
            json.dumps({"queue_id": queue_id}),
        )
        state.broadcast_ws.reset_mock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(f"/api/chat/slots/test/queue/{queue_id}/steer")
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True, "steered": True, "queued": False}

        client_mock.steer.assert_awaited_once_with("also check the docs")
        assert slot._queue == []
        assert not any(msg["role"] == "queued" for msg in slot.messages)
        assert any(
            msg["role"] == "user" and msg.get("meta", {}).get("steer") for msg in slot.messages
        )
        events = [call.args[0] for call in state.broadcast_ws.call_args_list]
        assert "steer_push" in events
        assert "queue_pop" in events
        state.sessions.stop_turn.assert_not_called()
        audit = _patch_sel.log_tool_invocation.call_args.kwargs
        assert audit["outcome"] == "success"
        assert audit["metadata"] == {
            "slot": "test",
            "queue_id": queue_id,
            "delivery": "steered",
        }

    @pytest.mark.asyncio
    async def test_unavailable_steer_restores_same_item_and_position(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock(supports_steer=True)
        client_mock.steer = AsyncMock(return_value=False)
        slot._acp_client = client_mock
        first_id = slot.queue_append("first", directive_user_origin=True)
        second_id = slot.queue_append("second", directive_user_origin=True)
        slot.append("queued", "first", json.dumps({"queue_id": first_id}))
        slot.append("queued", "second", json.dumps({"queue_id": second_id}))
        state.broadcast_ws.reset_mock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(f"/api/chat/slots/test/queue/{first_id}/steer")
            assert resp.status == 409
            assert (await resp.json())["code"] == "steer_unavailable"

        assert [item["id"] for item in slot._queue] == [first_id, second_id]
        assert [msg["content"] for msg in slot.messages if msg["role"] == "queued"] == [
            "first",
            "second",
        ]
        assert not state.broadcast_ws.called
        audit = _patch_sel.log_tool_invocation.call_args.kwargs
        assert audit["outcome"] == "failed"
        assert audit["metadata"]["reason"] == "steer_unavailable"

    @pytest.mark.asyncio
    async def test_unavailable_steer_uses_surviving_anchor_after_predecessor_cancel(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _running_slot(state)
        first_id = slot.queue_append("first", directive_user_origin=True)
        selected_id = slot.queue_append("selected", directive_user_origin=True)
        third_id = slot.queue_append("third", directive_user_origin=True)

        async def _cancel_predecessor(_message):
            assert slot.queue_remove_by_id(first_id) == "first"
            return False

        client_mock = MagicMock(supports_steer=True)
        client_mock.steer = AsyncMock(side_effect=_cancel_predecessor)
        slot._acp_client = client_mock

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(f"/api/chat/slots/test/queue/{selected_id}/steer")
            assert resp.status == 409

        assert [item["id"] for item in slot._queue] == [selected_id, third_id]

    @pytest.mark.asyncio
    async def test_unavailable_steer_stays_ahead_of_work_enqueued_after_claim(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _running_slot(state)
        selected_id = slot.queue_append("selected", directive_user_origin=True)
        trailing_id = slot.queue_append("trailing", directive_user_origin=True)
        enqueued_ids: list[str] = []

        async def _replace_original_neighbour(_message):
            assert slot.queue_remove_by_id(trailing_id) == "trailing"
            enqueued_ids.append(slot.queue_append("new", directive_user_origin=True))
            return False

        client_mock = MagicMock(supports_steer=True)
        client_mock.steer = AsyncMock(side_effect=_replace_original_neighbour)
        slot._acp_client = client_mock

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(f"/api/chat/slots/test/queue/{selected_id}/steer")
            assert resp.status == 409

        assert [item["id"] for item in slot._queue] == [selected_id, enqueued_ids[0]]

    @pytest.mark.asyncio
    async def test_app_structural_callback_and_non_user_entries_cannot_be_steered(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _running_slot(state)
        slot._app = "app-A"
        client_mock = MagicMock(supports_steer=True)
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock
        app_queue_id = slot.queue_append("app follow-up")

        @web.middleware
        async def _inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app = self._app(state)
        app.middlewares.insert(0, _inject_app)
        async with TestClient(TestServer(app)) as client:
            app_denied = await client.post(f"/api/chat/slots/test/queue/{app_queue_id}/steer")
            assert app_denied.status == 403
            assert (await app_denied.json())["code"] == "app_scoped_caller"

        async with TestClient(TestServer(self._app(state))) as client:
            app_slot_denied = await client.post(f"/api/chat/slots/test/queue/{app_queue_id}/steer")
            assert app_slot_denied.status == 403
            assert (await app_slot_denied.json())["code"] == "app_scoped_caller"

        slot._app = ""
        automatic_id = slot.queue_append(
            "recover",
            kind="synthetic_recovery",
            directive_user_origin=True,
        )
        structural_id = slot.queue_append(
            "Go",
            kind="plan_approval",
            directive_user_origin=True,
        )
        callback_id = slot.queue_insert(
            len(slot._queue),
            "retry",
            on_consumed=MagicMock(),
            directive_user_origin=True,
        )
        refused_ids = [app_queue_id, automatic_id, structural_id, callback_id]
        async with TestClient(TestServer(self._app(state))) as client:
            for refused_id in refused_ids:
                refused = await client.post(f"/api/chat/slots/test/queue/{refused_id}/steer")
                assert refused.status == 409
                assert (await refused.json())["code"] == "queue_item_not_steerable"

        client_mock.steer.assert_not_awaited()
        assert [item["id"] for item in slot._queue] == refused_ids
        audit_reasons = [
            call.kwargs["metadata"]["reason"]
            for call in _patch_sel.log_tool_invocation.call_args_list
        ]
        assert audit_reasons == [
            "app_scoped_context",
            "app_scoped_context",
            "queue_item_not_steerable",
            "queue_item_not_steerable",
            "queue_item_not_steerable",
            "queue_item_not_steerable",
        ]

    @pytest.mark.asyncio
    async def test_refuses_when_no_live_turn_or_queue_item(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        queue_id = slot.queue_append("later", directive_user_origin=True)

        async with TestClient(TestServer(self._app(state))) as client:
            not_running = await client.post(f"/api/chat/slots/test/queue/{queue_id}/steer")
            assert not_running.status == 409
            assert (await not_running.json())["code"] == "turn_not_steerable"

            task = MagicMock()
            task.done.return_value = False
            slot.task = task
            missing = await client.post("/api/chat/slots/test/queue/missing/steer")
            assert missing.status == 404
            assert (await missing.json())["code"] == "queue_item_not_found"

            missing_slot = await client.post("/api/chat/slots/absent/queue/missing/steer")
            assert missing_slot.status == 404
            assert (await missing_slot.json())["code"] == "slot_not_found"

        assert slot._queue[0]["id"] == queue_id
        assert [
            call.kwargs["metadata"]["reason"]
            for call in _patch_sel.log_tool_invocation.call_args_list
        ] == ["turn_not_steerable", "queue_item_not_found", "slot_not_found"]


class TestFlushSegmentQuietPersist:
    """Pin the steer-cut persistence contract: quiet_persist suppresses the
    per-message chat_message broadcast slot.append emits for the finalized
    assistant message. At the cut boundary every client has already frozen its
    streaming message, so that broadcast would render a duplicate copy of the
    pre-steer text below the steer bubble."""

    def _slot_with_chunks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot._on_message = MagicMock()
        slot.append("chunk", "pre-steer", "chunk", broadcast=False)
        return state, slot

    def test_quiet_persist_suppresses_message_broadcast(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _flush_segment

        state, slot = self._slot_with_chunks(tmp_path, monkeypatch)
        _flush_segment(state, slot, "pre-steer text", broadcast=False, quiet_persist=True)
        # Persisted (chunks collapsed into the assistant message)…
        roles = [m["role"] for m in slot.messages]
        assert "assistant" in roles and "chunk" not in roles
        # …but with NO chat_message broadcast and NO chat_segment event.
        slot._on_message.assert_not_called()
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_segment" not in events

    def test_default_flush_still_broadcasts_message(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _flush_segment

        state, slot = self._slot_with_chunks(tmp_path, monkeypatch)
        _flush_segment(state, slot, "normal segment", broadcast=False)
        slot._on_message.assert_called_once()
