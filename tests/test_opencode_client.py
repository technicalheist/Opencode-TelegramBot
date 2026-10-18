from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

import config
from opencode_client import OpenCodeClient, OpenCodeError, PermissionRequest
from opencode_client.client import _parse_sse_lines

BASE_URL = "http://opencode.test"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make(handler, tmp_path: Path) -> OpenCodeClient:
    return OpenCodeClient(
        base_url=BASE_URL, directory=tmp_path, client=_client(handler)
    )


def _json_response(body, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


# --------------------------------------------------------------------------
# SSE parsing
# --------------------------------------------------------------------------


def test_parse_sse_lines_collects_events():
    lines = [
        "data: {\"id\": \"e1\"}",
        "",
        ": keep-alive",
        "data: {\"id\": \"e2\"}",
        "",
    ]
    assert _parse_sse_lines(lines) == [{"id": "e1"}, {"id": "e2"}]


def test_parse_sse_lines_ignores_malformed():
    assert _parse_sse_lines(["data: not-json", ""]) == []


@pytest.mark.asyncio
async def test_stream_events_parses_sse(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/event"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"id": "e1", "type": "server.connected"}\n\n',
        )

    oc = _make(handler, tmp_path)
    events = [event async for event in oc.stream_events()]
    assert events == [{"id": "e1", "type": "server.connected"}]


# --------------------------------------------------------------------------
# construction & auth
# --------------------------------------------------------------------------


def test_constructor_reads_config_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OPENCODE_BASE_URL", "http://custom.test/")
    monkeypatch.setattr(config, "OPENCODE_DIRECTORY", tmp_path)
    monkeypatch.setattr(config, "OPENCODE_AGENT", "plan")
    monkeypatch.setattr(config, "OPENCODE_TIMEOUT", 12.5)
    monkeypatch.setattr(config, "OPENCODE_SERVER_PASSWORD", "")

    oc = OpenCodeClient()

    assert oc.base_url == "http://custom.test"
    assert oc.directory == tmp_path
    assert oc.timeout == 12.5
    assert config.OPENCODE_AGENT == "plan"


def _capture_auth_client(monkeypatch, handler) -> dict:
    real = httpx.AsyncClient
    captured: dict = {}

    def factory(*args, **kwargs):
        captured["auth"] = kwargs.get("auth")
        captured["headers"] = kwargs.get("headers")
        forward = {}
        if kwargs.get("auth") is not None:
            forward["auth"] = kwargs["auth"]
        if kwargs.get("headers") is not None:
            forward["headers"] = kwargs["headers"]
        return real(transport=httpx.MockTransport(handler), **forward)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


def _basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


@pytest.mark.asyncio
async def test_basic_auth_header_sent_when_username_set(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return _json_response({"version": "2.0.18"})

    _capture_auth_client(monkeypatch, handler)
    oc = OpenCodeClient(base_url=BASE_URL, username="alice", password="s3cret")

    assert await oc.health() is True
    assert seen["authorization"] == _basic_header("alice", "s3cret")


@pytest.mark.asyncio
async def test_bearer_auth_header_sent_when_only_secret_set(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return _json_response({"version": "2.0.18"})

    _capture_auth_client(monkeypatch, handler)
    oc = OpenCodeClient(base_url=BASE_URL, token="tok-123")

    assert await oc.health() is True
    assert seen["authorization"] == "Bearer tok-123"


@pytest.mark.asyncio
async def test_no_authorization_header_without_credentials(monkeypatch):
    seen: dict = {"authorization": "unset"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return _json_response({"version": "2.0.18"})

    _capture_auth_client(monkeypatch, handler)
    monkeypatch.setattr(config, "OPENCODE_SERVER_PASSWORD", "")
    oc = OpenCodeClient(base_url=BASE_URL)

    assert await oc.health() is True
    assert seen["authorization"] is None


@pytest.mark.asyncio
async def test_default_server_uses_config_basic_auth(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return _json_response({"version": "2.0.18"})

    _capture_auth_client(monkeypatch, handler)
    monkeypatch.setattr(config, "OPENCODE_BASE_URL", BASE_URL)
    monkeypatch.setattr(config, "OPENCODE_SERVER_USERNAME", "opencode")
    monkeypatch.setattr(config, "OPENCODE_SERVER_PASSWORD", "s3cret")

    oc = OpenCodeClient(base_url=BASE_URL)

    assert await oc.health() is True
    assert seen["authorization"] == _basic_header("opencode", "s3cret")


@pytest.mark.asyncio
async def test_non_default_server_ignores_config_auth(monkeypatch):
    seen: dict = {"authorization": "unset"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return _json_response({"version": "2.0.18"})

    _capture_auth_client(monkeypatch, handler)
    monkeypatch.setattr(config, "OPENCODE_BASE_URL", "http://other.test:4096")
    monkeypatch.setattr(config, "OPENCODE_SERVER_PASSWORD", "s3cret")

    oc = OpenCodeClient(base_url=BASE_URL)

    assert await oc.health() is True
    assert seen["authorization"] is None


@pytest.mark.asyncio
async def test_explicit_credentials_win_over_config(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return _json_response({"version": "2.0.18"})

    _capture_auth_client(monkeypatch, handler)
    monkeypatch.setattr(config, "OPENCODE_BASE_URL", BASE_URL)
    monkeypatch.setattr(config, "OPENCODE_SERVER_PASSWORD", "config-pw")

    oc = OpenCodeClient(base_url=BASE_URL, username="alice", password="explicit")

    assert await oc.health() is True
    assert seen["authorization"] == _basic_header("alice", "explicit")


@pytest.mark.asyncio
async def test_stream_events_carries_auth(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"id": "e1"}\n\n',
        )

    _capture_auth_client(monkeypatch, handler)
    oc = OpenCodeClient(base_url=BASE_URL, username="alice", password="s3cret")

    events = [event async for event in oc.stream_events()]

    assert events == [{"id": "e1"}]
    assert seen["authorization"] == _basic_header("alice", "s3cret")


@pytest.mark.asyncio
async def test_error_message_does_not_include_credentials(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _capture_auth_client(monkeypatch, handler)
    oc = OpenCodeClient(base_url=BASE_URL, username="alice", password="s3cret")

    with pytest.raises(OpenCodeError) as excinfo:
        await oc.create_session()

    assert "s3cret" not in str(excinfo.value)


# --------------------------------------------------------------------------
# health / location
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_true(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/info"
        return _json_response({"version": "2.0.18", "pid": 1})

    assert await _make(handler, tmp_path).health() is True


@pytest.mark.asyncio
async def test_health_false_on_error_status(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    assert await _make(handler, tmp_path).health() is False


@pytest.mark.asyncio
async def test_get_path_returns_body(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/location"
        assert request.url.params.get("location[directory]") == str(tmp_path)
        return _json_response({"directory": "/work", "project": {"id": "p"}})

    assert await _make(handler, tmp_path).get_path() == {
        "directory": "/work",
        "project": {"id": "p"},
    }


@pytest.mark.asyncio
async def test_default_directory_returns_directory(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"directory": "/work", "project": {"id": "p"}})

    assert await _make(handler, tmp_path).default_directory() == "/work"


@pytest.mark.asyncio
async def test_default_directory_falls_back_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    assert await _make(handler, tmp_path).default_directory() == "/"


# --------------------------------------------------------------------------
# directory listing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_directory_normalizes_entries(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/fs/list"
        assert request.url.params.get("path") == str(tmp_path)
        assert request.url.params.get("location[directory]") == str(tmp_path)
        return _json_response(
            {
                "location": {"directory": str(tmp_path)},
                "data": [
                    {"path": "src\\", "type": "directory"},
                    {"path": "README.md", "type": "file"},
                ],
            }
        )

    entries = await _make(handler, tmp_path).list_directory(str(tmp_path))
    names = [entry["name"] for entry in entries]
    assert names == ["src", "README.md"]
    assert entries[0]["type"] == "directory"
    assert entries[0]["absolute"].endswith("src")
    assert entries[1]["type"] == "file"


@pytest.mark.asyncio
async def test_list_directory_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(OpenCodeError):
        await _make(handler, tmp_path).list_directory(str(tmp_path))


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_sends_fields_and_unwraps(tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session"
        captured["body"] = json.loads(request.content)
        return _json_response({"data": {"id": "ses_1", "title": "hi"}})

    oc = _make(handler, tmp_path)
    session = await oc.create_session(
        title="hi", agent="build", directory=str(tmp_path)
    )

    assert session == {"id": "ses_1", "title": "hi"}
    assert captured["body"] == {
        "title": "hi",
        "agent": "build",
        "location": {"directory": str(tmp_path)},
    }


@pytest.mark.asyncio
async def test_create_session_includes_model_ref(tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _json_response({"data": {"id": "ses_1"}})

    await _make(handler, tmp_path).create_session(
        model={"providerID": "p", "modelID": "m"}
    )
    assert captured["body"]["model"] == {"id": "m", "providerID": "p"}


@pytest.mark.asyncio
async def test_create_session_invalid_model_omitted(tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _json_response({"data": {"id": "ses_1"}})

    await _make(handler, tmp_path).create_session(model={"modelID": "m"})
    assert "model" not in captured["body"]


@pytest.mark.asyncio
async def test_list_sessions_sends_directory_and_limit(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session"
        assert request.url.params.get("directory") == str(tmp_path)
        assert request.url.params.get("limit") == "5"
        return _json_response({"data": [{"id": "ses_1"}], "cursor": {}})

    sessions = await _make(handler, tmp_path).list_sessions(
        directory=str(tmp_path), limit=5
    )
    assert sessions == [{"id": "ses_1"}]


@pytest.mark.asyncio
async def test_get_session_returns_dict(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1"
        return _json_response({"data": {"id": "ses_1"}})

    assert await _make(handler, tmp_path).get_session("ses_1") == {"id": "ses_1"}


@pytest.mark.asyncio
async def test_get_session_not_found_carries_status(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_missing"
        return _json_response(
            {"_tag": "SessionNotFoundError", "message": "not found"}, status=404
        )

    with pytest.raises(OpenCodeError) as excinfo:
        await _make(handler, tmp_path).get_session("ses_missing")
    assert excinfo.value.status == 404


@pytest.mark.asyncio
async def test_get_session_other_error_carries_status(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "boom"}, status=500)

    with pytest.raises(OpenCodeError) as excinfo:
        await _make(handler, tmp_path).get_session("ses_1")
    assert excinfo.value.status == 500


@pytest.mark.asyncio
async def test_get_session_status_maps_active_to_busy(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/active"
        return _json_response({"data": {"ses_1": {"type": "running"}}})

    status = await _make(handler, tmp_path).get_session_status()
    assert status["ses_1"]["type"] == "busy"


@pytest.mark.asyncio
async def test_get_session_todos_returns_empty(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert await _make(handler, tmp_path).get_session_todos("ses_1") == []


# --------------------------------------------------------------------------
# prompting
# --------------------------------------------------------------------------


def _prompt_handler(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        captured.setdefault("calls", []).append((request.method, path))
        if path == "/api/session/ses_1/model":
            captured["model"] = json.loads(request.content)
            return httpx.Response(204)
        if path == "/api/session/ses_1/agent":
            captured["agent"] = json.loads(request.content)
            return httpx.Response(204)
        if path == "/api/session/ses_1/prompt":
            captured["prompt"] = json.loads(request.content)
            return _json_response({"data": {"id": "msg_u"}})
        if path == "/api/experimental/session/ses_1/wait":
            return httpx.Response(204)
        if path == "/api/session/ses_1/message":
            return _json_response(
                {
                    "data": [
                        {
                            "id": "msg_u",
                            "type": "user",
                            "text": "hi",
                            "time": {"created": 1},
                        },
                        {
                            "id": "msg_a",
                            "type": "assistant",
                            "content": [
                                {"type": "text", "text": "po"},
                                {"type": "reasoning", "text": "..."},
                                {"type": "text", "text": "ng"},
                            ],
                            "time": {"created": 2},
                        },
                        {
                            "id": "msg_i",
                            "type": "idle",
                            "outcome": "succeeded",
                            "time": {"created": 3},
                        },
                    ],
                    "cursor": {},
                }
            )
        raise AssertionError(f"unexpected {request.method} {path}")

    return handler


@pytest.mark.asyncio
async def test_send_prompt_switches_model_agent_waits_and_extracts_text(tmp_path):
    captured: dict = {}
    oc = _make(_prompt_handler(captured), tmp_path)

    reply = await oc.send_prompt(
        "ses_1",
        "hi",
        agent="build",
        model={"providerID": "p", "modelID": "m"},
    )

    assert reply == "po\nng"
    assert captured["model"] == {"model": {"id": "m", "providerID": "p"}}
    assert captured["agent"] == {"agent": "build"}
    assert captured["prompt"] == {"text": "hi"}
    assert captured["calls"] == [
        ("POST", "/api/session/ses_1/model"),
        ("POST", "/api/session/ses_1/agent"),
        ("POST", "/api/session/ses_1/prompt"),
        ("POST", "/api/experimental/session/ses_1/wait"),
        ("GET", "/api/session/ses_1/message"),
    ]


@pytest.mark.asyncio
async def test_send_prompt_without_agent_or_model_skips_switches(tmp_path):
    captured: dict = {}
    oc = _make(_prompt_handler(captured), tmp_path)

    await oc.send_prompt("ses_1", "hi")

    assert captured["calls"] == [
        ("POST", "/api/session/ses_1/prompt"),
        ("POST", "/api/experimental/session/ses_1/wait"),
        ("GET", "/api/session/ses_1/message"),
    ]


@pytest.mark.asyncio
async def test_prompt_async_posts_without_wait(tmp_path):
    captured: dict = {}
    oc = _make(_prompt_handler(captured), tmp_path)

    result = await oc.prompt_async("ses_1", "hi")

    assert result is None
    assert captured["calls"] == [("POST", "/api/session/ses_1/prompt")]


@pytest.mark.asyncio
async def test_abort_interrupts_session(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/interrupt"
        assert request.url.params.get("resume") == "false"
        return _json_response({"interrupted": True})

    await _make(handler, tmp_path).abort("ses_1")


@pytest.mark.asyncio
async def test_compact_session_posts(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/compact"
        return _json_response({"data": {"type": "compaction"}})

    await _make(handler, tmp_path).compact_session("ses_1")


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_returns_data(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/message"
        assert request.url.params.get("order") == "asc"
        return _json_response({"data": [{"id": "m1"}], "cursor": {}})

    assert await _make(handler, tmp_path).list_messages("ses_1") == [{"id": "m1"}]


@pytest.mark.asyncio
async def test_get_turn_assistant_parts_after_last_user(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "data": [
                    {
                        "id": "a0",
                        "type": "assistant",
                        "content": [{"type": "text", "text": "old"}],
                        "time": {"created": 0},
                    },
                    {"id": "u", "type": "user", "text": "q", "time": {"created": 1}},
                    {
                        "id": "a1",
                        "type": "assistant",
                        "content": [
                            {"type": "text", "text": "new"},
                            {"type": "tool", "name": "bash", "state": "completed"},
                        ],
                        "time": {"created": 2},
                    },
                ],
                "cursor": {},
            }
        )

    parts = await _make(handler, tmp_path).get_turn_assistant_parts("ses_1")
    assert [part["type"] for part in parts] == ["text", "tool"]


@pytest.mark.asyncio
async def test_get_last_assistant_message(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "data": [
                    {
                        "id": "a1",
                        "type": "assistant",
                        "content": [{"type": "text", "text": "x"}],
                        "time": {"created": 2},
                    },
                    {
                        "id": "a2",
                        "type": "assistant",
                        "content": [{"type": "text", "text": "y"}],
                        "time": {"created": 5},
                    },
                ],
                "cursor": {},
            }
        )

    message = await _make(handler, tmp_path).get_last_assistant_message("ses_1")
    assert message["id"] == "a2"


@pytest.mark.asyncio
async def test_get_last_activity_returns_last_part(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "data": [
                    {
                        "id": "a1",
                        "type": "assistant",
                        "content": [
                            {"type": "text", "text": "hi"},
                            {"type": "tool", "name": "bash", "state": "running"},
                        ],
                        "time": {"created": 2},
                    }
                ],
                "cursor": {},
            }
        )

    part = await _make(handler, tmp_path).get_last_activity("ses_1")
    assert part["type"] == "tool"


@pytest.mark.asyncio
async def test_get_session_diff_returns_data(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/diff"
        return _json_response(
            {"data": [{"file": "a.png", "status": "added", "patch": "@@"}]}
        )

    diff = await _make(handler, tmp_path).get_session_diff("ses_1")
    assert diff == [{"file": "a.png", "status": "added", "patch": "@@"}]


# --------------------------------------------------------------------------
# models & mcp
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_models_filters_disabled(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/model"
        return _json_response(
            {
                "data": [
                    {
                        "modelID": "m1",
                        "providerID": "p",
                        "name": "M One",
                        "enabled": True,
                    },
                    {
                        "modelID": "m2",
                        "providerID": "p",
                        "name": "M Two",
                        "enabled": False,
                    },
                ]
            }
        )

    models = await _make(handler, tmp_path).list_models()
    assert models == [{"providerID": "p", "modelID": "m1", "name": "M One"}]


@pytest.mark.asyncio
async def test_get_mcp_status_returns_dict(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp"
        return _json_response(
            {
                "data": [
                    {"name": "playwright", "status": {"type": "connected"}},
                    {"name": "telegram", "status": {"type": "failed"}},
                ]
            }
        )

    servers = await _make(handler, tmp_path).get_mcp_status()
    assert servers == {
        "playwright": {"status": "connected"},
        "telegram": {"status": "failed"},
    }


@pytest.mark.asyncio
async def test_list_tool_ids_returns_empty(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert await _make(handler, tmp_path).list_tool_ids() == []


# --------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------


def test_permission_request_from_v2_event():
    event = {
        "type": "permission.v2.asked",
        "data": {
            "id": "per_1",
            "sessionID": "ses_1",
            "action": "bash",
            "resources": ["git push"],
            "save": ["git push"],
            "metadata": {"k": "v"},
        },
    }
    request = PermissionRequest.from_event(event)
    assert request.id == "per_1"
    assert request.session_id == "ses_1"
    assert request.action == "bash"
    assert request.resources == ["git push"]
    assert request.save == ["git push"]


@pytest.mark.asyncio
async def test_reply_permission_posts_decision(tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/permission/per_1/reply"
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    await _make(handler, tmp_path).reply_permission(
        "per_1", "always", session_id="ses_1"
    )
    assert captured["body"] == {"decision": "always"}


@pytest.mark.asyncio
async def test_reply_permission_rejects_invalid_reply(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(ValueError):
        await _make(handler, tmp_path).reply_permission(
            "per_1", "bogus", session_id="ses_1"
        )


@pytest.mark.asyncio
async def test_list_permissions_returns_list(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/permission/request"
        return _json_response({"data": [{"id": "per_1"}]})

    assert await _make(handler, tmp_path).list_permissions() == [{"id": "per_1"}]


# --------------------------------------------------------------------------
# forms
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_forms_returns_list(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/form"
        return _json_response({"data": [{"id": "frm_1"}]})

    assert await _make(handler, tmp_path).list_forms() == [{"id": "frm_1"}]


@pytest.mark.asyncio
async def test_reply_form_posts_answer(tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/form/frm_1/reply"
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    await _make(handler, tmp_path).reply_form(
        "frm_1", {"q0": "Red"}, session_id="ses_1"
    )
    assert captured["body"] == {"answer": {"q0": "Red"}}


@pytest.mark.asyncio
async def test_cancel_form_deletes(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/session/ses_1/form/frm_1"
        return httpx.Response(204)

    await _make(handler, tmp_path).cancel_form("frm_1", session_id="ses_1")
