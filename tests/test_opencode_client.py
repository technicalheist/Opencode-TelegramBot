from __future__ import annotations

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


@pytest.mark.asyncio
async def test_health_true(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/health"
        assert request.url.params.get("directory") == str(tmp_path)
        return httpx.Response(200, json={"healthy": True})

    oc = _make(handler, tmp_path)
    assert await oc.health() is True


@pytest.mark.asyncio
async def test_health_false(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"healthy": False})

    oc = _make(handler, tmp_path)
    assert await oc.health() is False


@pytest.mark.asyncio
async def test_health_false_on_error_status(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    oc = _make(handler, tmp_path)
    assert await oc.health() is False


@pytest.mark.asyncio
async def test_create_session_sends_only_provided_fields(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["directory"] = request.url.params.get("directory")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ses_1", "title": "hello"})

    oc = _make(handler, tmp_path)
    result = await oc.create_session(title="hello")

    assert result == {"id": "ses_1", "title": "hello"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/session"
    assert captured["directory"] == str(tmp_path)
    assert captured["body"] == {"title": "hello"}


@pytest.mark.asyncio
async def test_create_session_body_empty_without_options(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ses_1"})

    oc = _make(handler, tmp_path)
    await oc.create_session()

    assert captured["body"] == {}


@pytest.mark.asyncio
async def test_create_session_raises_on_error_status(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.create_session()


@pytest.mark.asyncio
async def test_send_prompt_concatenates_text_parts_and_sends_agent(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "info": {"id": "msg_1"},
                "parts": [
                    {"type": "text", "text": "Hello"},
                    {"type": "reasoning", "text": "ignore this"},
                    {"type": "step-start", "text": "ignore"},
                    {"type": "tool", "text": "ignore"},
                    {"type": "text", "text": "world"},
                ],
            },
        )

    oc = _make(handler, tmp_path)
    result = await oc.send_prompt("ses_1", "hi", agent="plan")

    assert result == "Hello\nworld"
    assert captured["method"] == "POST"
    assert captured["path"] == "/session/ses_1/message"
    assert captured["body"] == {
        "parts": [{"type": "text", "text": "hi"}],
        "agent": "plan",
    }


@pytest.mark.asyncio
async def test_send_prompt_omits_agent_when_not_given(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"info": {}, "parts": [{"type": "text", "text": "ok"}]}
        )

    oc = _make(handler, tmp_path)
    await oc.send_prompt("ses_1", "hi")

    assert captured["body"] == {"parts": [{"type": "text", "text": "hi"}]}


@pytest.mark.asyncio
async def test_send_prompt_raises_on_error_status(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.send_prompt("ses_1", "hi")


@pytest.mark.asyncio
async def test_send_prompt_raises_when_info_has_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "info": {"error": {"name": "ProviderError", "message": "nope"}},
                "parts": [],
            },
        )

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.send_prompt("ses_1", "hi")


@pytest.mark.asyncio
async def test_reply_permission_posts_reply(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    oc = _make(handler, tmp_path)
    await oc.reply_permission("per_x", "once")

    assert captured["method"] == "POST"
    assert captured["path"] == "/permission/per_x/reply"
    assert captured["body"] == {"reply": "once"}


@pytest.mark.asyncio
async def test_reply_permission_rejects_invalid_reply(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent")

    oc = _make(handler, tmp_path)
    with pytest.raises(ValueError):
        await oc.reply_permission("per_x", "maybe")


@pytest.mark.asyncio
async def test_prompt_async_posts_and_returns_none(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    oc = _make(handler, tmp_path)
    result = await oc.prompt_async("ses_1", "hi", agent="build")

    assert result is None
    assert captured["method"] == "POST"
    assert captured["path"] == "/session/ses_1/prompt_async"
    assert captured["body"]["agent"] == "build"


@pytest.mark.asyncio
async def test_abort_posts_to_session(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json=True)

    oc = _make(handler, tmp_path)
    result = await oc.abort("ses_1")

    assert result is None
    assert captured["method"] == "POST"
    assert captured["path"] == "/session/ses_1/abort"


@pytest.mark.asyncio
async def test_list_messages_returns_list(tmp_path):
    messages = [{"info": {"id": "m1"}, "parts": []}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/session/ses_1/message"
        return httpx.Response(200, json=messages)

    oc = _make(handler, tmp_path)
    assert await oc.list_messages("ses_1") == messages


@pytest.mark.asyncio
async def test_reply_question_posts_answers(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    oc = _make(handler, tmp_path)
    await oc.reply_question("que_1", ["a", "b"])

    assert captured["path"] == "/question/que_1/reply"
    assert captured["body"] == {"answers": ["a", "b"]}


@pytest.mark.asyncio
async def test_list_questions_returns_list_with_directory(tmp_path):
    questions = [{"id": "que_1", "sessionID": "ses_1", "questions": []}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/question"
        assert request.url.params.get("directory") == str(tmp_path)
        return httpx.Response(200, json=questions)

    oc = _make(handler, tmp_path)
    assert await oc.list_questions() == questions


@pytest.mark.asyncio
async def test_list_questions_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.list_questions()


@pytest.mark.asyncio
async def test_reject_question_posts_reject(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={})

    oc = _make(handler, tmp_path)
    await oc.reject_question("que_1")

    assert captured["method"] == "POST"
    assert captured["path"] == "/question/que_1/reject"


@pytest.mark.asyncio
async def test_reject_question_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.reject_question("que_1")


@pytest.mark.asyncio
async def test_reply_question_sends_directory_override(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["directory"] = request.url.params.get("directory")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    oc = _make(handler, tmp_path)
    await oc.reply_question("que_1", [["Purple"]], directory="D:/x")

    assert captured["path"] == "/question/que_1/reply"
    assert captured["directory"] == str(Path("D:/x"))
    assert captured["body"] == {"answers": [["Purple"]]}


@pytest.mark.asyncio
async def test_reject_question_sends_directory_override(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["directory"] = request.url.params.get("directory")
        return httpx.Response(200, json={})

    oc = _make(handler, tmp_path)
    await oc.reject_question("que_1", directory="D:/x")

    assert captured["path"] == "/question/que_1/reject"
    assert captured["directory"] == str(Path("D:/x"))


@pytest.mark.asyncio
async def test_reply_reject_question_use_default_directory_when_omitted(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["directory"] = request.url.params.get("directory")
        return httpx.Response(200, json={})

    oc = _make(handler, tmp_path)
    await oc.reply_question("que_1", ["a"])
    assert captured["directory"] == str(tmp_path)

    await oc.reject_question("que_1")
    assert captured["directory"] == str(tmp_path)


def test_parse_sse_lines_handles_comments_keepalives_and_multiline():
    lines = [
        ": keepalive",
        "",
        'data: {"id": "e1", "type": "server.connected"}',
        "",
        "",
        ": another comment",
        "event: message.updated",
        'data: {"id": "e2",',
        'data: "type": "message.updated"}',
        "",
    ]

    assert _parse_sse_lines(lines) == [
        {"id": "e1", "type": "server.connected"},
        {"id": "e2", "type": "message.updated"},
    ]


def test_parse_sse_lines_ignores_malformed_payload():
    assert _parse_sse_lines(["data: not-json", ""]) == []


def test_permission_request_from_event():
    event = {
        "id": "evt_1",
        "type": "permission.asked",
        "properties": {
            "id": "per_1",
            "sessionID": "ses_1",
            "permission": "bash",
            "patterns": ["git push"],
            "metadata": {"command": "git push"},
            "always": ["git push"],
        },
    }

    request = PermissionRequest.from_event(event)

    assert request.id == "per_1"
    assert request.session_id == "ses_1"
    assert request.permission == "bash"
    assert request.patterns == ["git push"]
    assert request.metadata == {"command": "git push"}
    assert request.always == ["git push"]


def test_permission_request_from_event_defaults():
    request = PermissionRequest.from_event({"properties": {}})

    assert request.id == ""
    assert request.session_id == ""
    assert request.patterns == []
    assert request.metadata == {}
    assert request.always == []


@pytest.mark.asyncio
async def test_stream_events_parses_sse_from_transport(tmp_path):
    body = (
        'data: {"id": "e1", "type": "server.connected"}\n'
        "\n"
        ": keepalive\n"
        "\n"
        'data: {"id": "e2", "type": "permission.asked", '
        '"properties": {"id": "per_1"}}\n'
        "\n"
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["accept"] = request.headers.get("accept")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
        )

    oc = _make(handler, tmp_path)
    events = [event async for event in oc.stream_events()]

    assert [e["id"] for e in events] == ["e1", "e2"]
    assert events[1]["properties"]["id"] == "per_1"
    assert captured["path"] == "/event"
    assert captured["accept"] == "text/event-stream"


@pytest.mark.asyncio
async def test_injected_client_is_not_closed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"healthy": True})

    client = _client(handler)
    oc = OpenCodeClient(base_url=BASE_URL, directory=tmp_path, client=client)

    await oc.health()
    assert client.is_closed is False

    await client.aclose()
    assert client.is_closed is True


@pytest.mark.asyncio
async def test_injected_client_not_closed_after_stream(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"id": "e1"}\n\n',
        )

    client = _client(handler)
    oc = OpenCodeClient(base_url=BASE_URL, directory=tmp_path, client=client)

    events = [event async for event in oc.stream_events()]

    assert events == [{"id": "e1"}]
    assert client.is_closed is False
    await client.aclose()


@pytest.mark.asyncio
async def test_list_models_filters_to_connected_providers(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/provider":
            return httpx.Response(
                200,
                json={
                    "all": [{"id": "opencode-go"}, {"id": "opencode"}],
                    "connected": ["opencode-go"],
                    "default": {},
                },
            )
        assert request.url.path == "/config/providers"
        return httpx.Response(
            200,
            json={
                "providers": [
                    {
                        "id": "opencode-go",
                        "name": "Go",
                        "models": {
                            "deepseek-v4.1-flash": {
                                "id": "deepseek-v4.1-flash",
                                "providerID": "opencode-go",
                                "name": "DeepSeek Flash",
                            },
                            "zeta": {
                                "id": "zeta",
                                "providerID": "opencode-go",
                                "name": "Zeta",
                            },
                        },
                    },
                    {
                        "id": "opencode",
                        "name": "OpenCode",
                        "models": {
                            "gpt-5": {
                                "id": "gpt-5",
                                "providerID": "opencode",
                                "name": "GPT-5",
                            }
                        },
                    },
                ],
                "default": {},
            },
        )

    oc = _make(handler, tmp_path)
    models = await oc.list_models()

    assert models == [
        {
            "providerID": "opencode-go",
            "modelID": "deepseek-v4.1-flash",
            "name": "DeepSeek Flash",
        },
        {"providerID": "opencode-go", "modelID": "zeta", "name": "Zeta"},
    ]


@pytest.mark.asyncio
async def test_list_models_falls_back_when_connected_missing(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/provider":
            return httpx.Response(200, json={"all": [], "default": {}})
        return httpx.Response(
            200,
            json={
                "providers": [
                    {
                        "id": "opencode",
                        "name": "OpenCode",
                        "models": {
                            "gpt-5": {"id": "gpt-5", "name": "GPT-5"},
                        },
                    }
                ],
                "default": {},
            },
        )

    oc = _make(handler, tmp_path)
    models = await oc.list_models()

    assert models == [
        {"providerID": "opencode", "modelID": "gpt-5", "name": "GPT-5"}
    ]


@pytest.mark.asyncio
async def test_list_models_raises_on_providers_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/provider":
            return httpx.Response(200, json={"connected": []})
        return httpx.Response(500, text="boom")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.list_models()


@pytest.mark.asyncio
async def test_list_sessions_sends_directory_and_limit(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["directory"] = request.url.params.get("directory")
        captured["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json=[{"id": "ses_1", "title": "hello"}])

    oc = _make(handler, tmp_path)
    result = await oc.list_sessions(directory="/tmp/work", limit=7)

    assert result == [{"id": "ses_1", "title": "hello"}]
    assert captured == {
        "method": "GET",
        "path": "/session",
        "directory": str(Path("/tmp/work")),
        "limit": "7",
    }


@pytest.mark.asyncio
async def test_list_sessions_default_directory(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["directory"] = request.url.params.get("directory")
        captured["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json=[])

    oc = _make(handler, tmp_path)
    await oc.list_sessions()

    assert captured["directory"] == str(tmp_path)
    assert captured["limit"] == "50"


@pytest.mark.asyncio
async def test_compact_session_posts_without_directory(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["has_directory"] = "directory" in request.url.params
        return httpx.Response(204)

    oc = _make(handler, tmp_path)
    result = await oc.compact_session("ses_1")

    assert result is None
    assert captured == {
        "method": "POST",
        "path": "/api/session/ses_1/compact",
        "has_directory": False,
    }


@pytest.mark.asyncio
async def test_compact_session_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.compact_session("ses_1")


@pytest.mark.asyncio
async def test_compact_session_falls_back_to_summarize_with_session_model(tmp_path):
    summarize_bodies: list[object] = []
    summarize_directories: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/ses_1/compact":
            assert request.method == "POST"
            return httpx.Response(
                503,
                json={
                    "_tag": "ServiceUnavailableError",
                    "message": "Session compact is not available yet",
                    "service": "session.compact",
                },
            )
        if request.url.path == "/session/ses_1":
            assert request.method == "GET"
            assert request.url.params.get("directory") == str(tmp_path)
            return httpx.Response(
                200,
                json={
                    "id": "ses_1",
                    "model": {
                        "id": "muse-spark-1.3-contributor",
                        "providerID": "opencode-go",
                        "variant": None,
                    },
                },
            )
        if request.url.path == "/session/ses_1/summarize":
            summarize_bodies.append(json.loads(request.content))
            summarize_directories.append(request.url.params.get("directory"))
            return httpx.Response(200, json=True)
        raise AssertionError(f"unexpected path {request.url.path}")

    oc = _make(handler, tmp_path)
    result = await oc.compact_session("ses_1")

    assert result is None
    assert summarize_bodies == [
        {
            "providerID": "opencode-go",
            "modelID": "muse-spark-1.3-contributor",
            "auto": True,
        }
    ]
    assert summarize_directories == [str(tmp_path)]


@pytest.mark.asyncio
async def test_compact_session_uses_provided_model_without_get_session(tmp_path):
    summarize_bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/ses_1/compact":
            return httpx.Response(503, json={"_tag": "ServiceUnavailableError"})
        if request.url.path == "/session/ses_1":
            raise AssertionError("get_session should not be called with a model")
        if request.url.path == "/session/ses_1/summarize":
            summarize_bodies.append(json.loads(request.content))
            return httpx.Response(200, json=True)
        raise AssertionError(f"unexpected path {request.url.path}")

    oc = _make(handler, tmp_path)
    await oc.compact_session(
        "ses_1", model={"providerID": "opencode-go", "modelID": "muse"}
    )

    assert summarize_bodies == [
        {"providerID": "opencode-go", "modelID": "muse", "auto": True}
    ]


@pytest.mark.asyncio
async def test_compact_session_reraises_when_no_model_available(tmp_path):
    summarize_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal summarize_called
        if request.url.path == "/api/session/ses_1/compact":
            return httpx.Response(503, json={"_tag": "ServiceUnavailableError"})
        if request.url.path == "/session/ses_1":
            return httpx.Response(200, json={"id": "ses_1"})
        if request.url.path == "/session/ses_1/summarize":
            summarize_called = True
            return httpx.Response(200, json=True)
        raise AssertionError(f"unexpected path {request.url.path}")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.compact_session("ses_1")

    assert summarize_called is False


@pytest.mark.asyncio
async def test_compact_session_falls_back_when_session_mismatched_model(tmp_path):
    summarize_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal summarize_called
        if request.url.path == "/api/session/ses_1/compact":
            return httpx.Response(503, json={"_tag": "ServiceUnavailableError"})
        if request.url.path == "/session/ses_1":
            return httpx.Response(200, json={"id": "ses_1", "model": {"variant": 1}})
        if request.url.path == "/session/ses_1/summarize":
            summarize_called = True
            return httpx.Response(200, json=True)
        raise AssertionError(f"unexpected path {request.url.path}")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.compact_session("ses_1")

    assert summarize_called is False


@pytest.mark.asyncio
async def test_get_session_returns_dict(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/session/ses_1"
        assert request.url.params.get("directory") == str(tmp_path)
        return httpx.Response(200, json={"id": "ses_1", "title": "hello"})

    oc = _make(handler, tmp_path)
    assert await oc.get_session("ses_1") == {"id": "ses_1", "title": "hello"}


@pytest.mark.asyncio
async def test_get_session_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.get_session("ses_1")


@pytest.mark.asyncio
async def test_get_session_status_returns_map(tmp_path):
    status = {"ses_1": {"type": "busy"}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/session/status"
        assert "directory" not in request.url.params
        return httpx.Response(200, json=status)

    oc = _make(handler, tmp_path)
    assert await oc.get_session_status() == status


@pytest.mark.asyncio
async def test_get_session_status_returns_empty_dict(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    oc = _make(handler, tmp_path)
    assert await oc.get_session_status() == {}


@pytest.mark.asyncio
async def test_get_session_status_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.get_session_status()


@pytest.mark.asyncio
async def test_get_session_todos_returns_list_with_directory(tmp_path):
    todos = [
        {"content": "do it", "status": "pending", "priority": "high"},
        {"content": "done", "status": "completed", "priority": "low"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/session/ses_1/todo"
        assert request.url.params.get("directory") == str(tmp_path)
        return httpx.Response(200, json=todos)

    oc = _make(handler, tmp_path)
    assert await oc.get_session_todos("ses_1") == todos


@pytest.mark.asyncio
async def test_get_session_todos_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.get_session_todos("ses_1")


@pytest.mark.asyncio
async def test_get_last_activity_returns_last_meaningful_part(tmp_path):
    messages = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
        {
            "info": {"role": "assistant"},
            "parts": [{"type": "step-start"}, {"type": "text", "text": "working"}],
        },
        {
            "info": {"role": "assistant"},
            "parts": [{"type": "reasoning", "text": "thinking"}, {"type": "step-finish"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/session/ses_1/message"
        assert request.url.params.get("directory") == str(tmp_path)
        return httpx.Response(200, json=messages)

    oc = _make(handler, tmp_path)
    activity = await oc.get_last_activity("ses_1")

    assert activity == {"type": "reasoning", "text": "thinking"}


@pytest.mark.asyncio
async def test_get_last_activity_returns_none_when_no_meaningful_part(tmp_path):
    messages = [
        {"info": {"role": "assistant"}, "parts": [{"type": "step-start"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "step-finish"}]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=messages)

    oc = _make(handler, tmp_path)
    assert await oc.get_last_activity("ses_1") is None


@pytest.mark.asyncio
async def test_get_last_activity_returns_none_for_empty_list(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    oc = _make(handler, tmp_path)
    assert await oc.get_last_activity("ses_1") is None


@pytest.mark.asyncio
async def test_get_last_activity_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.get_last_activity("ses_1")


@pytest.mark.asyncio
async def test_get_last_assistant_message_returns_last_with_parts(tmp_path):
    messages = [
        {
            "info": {"role": "assistant"},
            "parts": [{"type": "text", "text": "first"}],
        },
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
        {
            "info": {"role": "assistant"},
            "parts": [{"type": "file", "url": "file:///x.png"}],
        },
        {"info": {"role": "assistant"}, "parts": []},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/ses_1/message"
        assert request.url.params.get("directory") == str(tmp_path)
        return httpx.Response(200, json=messages)

    oc = _make(handler, tmp_path)
    message = await oc.get_last_assistant_message("ses_1")

    assert message is not None
    assert message["parts"][0]["url"] == "file:///x.png"


@pytest.mark.asyncio
async def test_get_last_assistant_message_returns_none(tmp_path):
    messages = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
        {"info": {"role": "assistant"}, "parts": []},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=messages)

    oc = _make(handler, tmp_path)
    assert await oc.get_last_assistant_message("ses_1") is None


@pytest.mark.asyncio
async def test_get_last_assistant_message_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.get_last_assistant_message("ses_1")


@pytest.mark.asyncio
async def test_get_session_diff_returns_list_with_message_id(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["directory"] = request.url.params.get("directory")
        captured["messageID"] = request.url.params.get("messageID")
        return httpx.Response(200, json=[{"path": "a.png", "additions": 1}])

    oc = _make(handler, tmp_path)
    result = await oc.get_session_diff("ses_1", message_id="msg_9")

    assert result == [{"path": "a.png", "additions": 1}]
    assert captured == {
        "path": "/session/ses_1/diff",
        "directory": str(tmp_path),
        "messageID": "msg_9",
    }


@pytest.mark.asyncio
async def test_get_session_diff_omits_message_id_when_absent(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["messageID"] = request.url.params.get("messageID")
        return httpx.Response(200, json=[])

    oc = _make(handler, tmp_path)
    assert await oc.get_session_diff("ses_1") == []
    assert captured["messageID"] is None


@pytest.mark.asyncio
async def test_get_session_diff_raises_on_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.get_session_diff("ses_1")


@pytest.mark.asyncio
async def test_get_mcp_status_returns_dict(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/mcp"
        assert request.url.params.get("directory") == str(tmp_path)
        return httpx.Response(200, json={"playwright": {"status": "connected"}})

    oc = _make(handler, tmp_path)
    assert await oc.get_mcp_status() == {"playwright": {"status": "connected"}}


@pytest.mark.asyncio
async def test_list_tool_ids_returns_strings(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/experimental/tool/ids"
        return httpx.Response(200, json=["bash", "read", 42])

    oc = _make(handler, tmp_path)
    assert await oc.list_tool_ids() == ["bash", "read", "42"]


@pytest.mark.asyncio
async def test_send_prompt_includes_model_when_provided(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["directory"] = request.url.params.get("directory")
        return httpx.Response(
            200, json={"info": {}, "parts": [{"type": "text", "text": "ok"}]}
        )

    oc = _make(handler, tmp_path)
    await oc.send_prompt(
        "ses_1",
        "hi",
        model={"providerID": "opencode-go", "modelID": "deepseek-v4.1-flash"},
        directory="/tmp/work",
    )

    assert captured["body"] == {
        "parts": [{"type": "text", "text": "hi"}],
        "model": {"providerID": "opencode-go", "modelID": "deepseek-v4.1-flash"},
    }
    assert captured["directory"] == str(Path("/tmp/work"))


@pytest.mark.asyncio
async def test_create_session_includes_model_when_provided(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["directory"] = request.url.params.get("directory")
        return httpx.Response(200, json={"id": "ses_1"})

    oc = _make(handler, tmp_path)
    await oc.create_session(
        title="hello",
        model={"providerID": "opencode", "modelID": "gpt-5"},
        directory="/tmp/work",
    )

    assert captured["body"] == {
        "title": "hello",
        "model": {"id": "gpt-5", "providerID": "opencode"},
    }
    assert captured["directory"] == str(Path("/tmp/work"))


@pytest.mark.asyncio
async def test_create_session_accepts_id_key_and_variant(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ses_1"})

    oc = _make(handler, tmp_path)
    await oc.create_session(
        model={"providerID": "opencode-go", "id": "muse", "variant": "lite"}
    )

    assert captured["body"] == {
        "model": {"id": "muse", "providerID": "opencode-go", "variant": "lite"}
    }


@pytest.mark.asyncio
async def test_create_session_invalid_model_raises(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent")

    oc = _make(handler, tmp_path)
    with pytest.raises(OpenCodeError):
        await oc.create_session(model={"providerID": "opencode-go"})


@pytest.mark.asyncio
async def test_create_session_omits_model_when_not_provided(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "ses_1"})

    oc = _make(handler, tmp_path)
    await oc.create_session(title="hi")

    assert captured["body"] == {"title": "hi"}
    assert "model" not in captured["body"]


@pytest.mark.asyncio
async def test_send_prompt_keeps_modelID_key(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"info": {}, "parts": [{"type": "text", "text": "ok"}]}
        )

    oc = _make(handler, tmp_path)
    await oc.send_prompt(
        "ses_1", "hi", model={"providerID": "p", "modelID": "m"}
    )

    assert captured["body"] == {
        "parts": [{"type": "text", "text": "hi"}],
        "model": {"providerID": "p", "modelID": "m"},
    }


def test_constructor_reads_config_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OPENCODE_BASE_URL", "http://custom.test/")
    monkeypatch.setattr(config, "OPENCODE_DIRECTORY", tmp_path)
    monkeypatch.setattr(config, "OPENCODE_AGENT", "plan")
    monkeypatch.setattr(config, "OPENCODE_TIMEOUT", 12.5)

    oc = OpenCodeClient()

    assert oc.base_url == "http://custom.test"
    assert oc.directory == tmp_path
    assert oc.timeout == 12.5
    assert config.OPENCODE_AGENT == "plan"
