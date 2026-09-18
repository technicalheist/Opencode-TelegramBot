from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Iterable

import httpx

import config

logger = logging.getLogger(__name__)

VALID_PERMISSION_REPLIES = {"once", "always", "reject"}


class OpenCodeError(RuntimeError):
    pass


@dataclass
class PermissionRequest:
    id: str
    session_id: str
    permission: str
    patterns: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    always: list[str] = field(default_factory=list)

    @classmethod
    def from_event(cls, event: dict) -> "PermissionRequest":
        properties = event.get("properties") or {}
        return cls(
            id=str(properties.get("id", "")),
            session_id=str(properties.get("sessionID", "")),
            permission=str(properties.get("permission", "")),
            patterns=list(properties.get("patterns") or []),
            metadata=dict(properties.get("metadata") or {}),
            always=list(properties.get("always") or []),
        )


class _SSEParser:
    def __init__(self) -> None:
        self._data: list[str] = []

    def feed(self, line: str) -> dict | None:
        if line.endswith("\r"):
            line = line[:-1]
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        if line.startswith("data:"):
            value = line[len("data:") :]
            if value.startswith(" "):
                value = value[1:]
            self._data.append(value)
        return None

    def _dispatch(self) -> dict | None:
        if not self._data:
            return None
        payload = "\n".join(self._data)
        self._data = []
        try:
            return json.loads(payload)
        except ValueError:
            logger.warning("Ignoring malformed SSE data payload.")
            return None

    def flush(self) -> dict | None:
        return self._dispatch()


def _parse_sse_lines(lines: Iterable[str]) -> list[dict]:
    parser = _SSEParser()
    events: list[dict] = []
    for line in lines:
        event = parser.feed(line)
        if event is not None:
            events.append(event)
    trailing = parser.flush()
    if trailing is not None:
        events.append(trailing)
    return events


def _normalize_model(model: dict | None) -> tuple[str, str] | None:
    if not isinstance(model, dict):
        return None
    provider_id = model.get("providerID") or model.get("providerId")
    model_id = model.get("id") or model.get("modelID") or model.get("modelId")
    if not provider_id or not model_id:
        return None
    return str(provider_id), str(model_id)


def _session_model_payload(model: dict | None) -> dict | None:
    normalized = _normalize_model(model)
    if normalized is None:
        return None
    provider_id, model_id = normalized
    payload: dict = {"id": model_id, "providerID": provider_id}
    if isinstance(model, dict) and model.get("variant") is not None:
        payload["variant"] = model["variant"]
    return payload


class OpenCodeClient:
    def __init__(
        self,
        base_url: str | None = None,
        directory: str | Path | None = None,
        timeout: float | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or config.OPENCODE_BASE_URL).rstrip("/")
        self._directory = (
            Path(config.OPENCODE_DIRECTORY) if directory is None else Path(directory)
        )
        self._timeout = (
            float(config.OPENCODE_TIMEOUT) if timeout is None else float(timeout)
        )
        self._client = client

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def timeout(self) -> float:
        return self._timeout

    @asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                yield client

    def _params(self, directory: str | Path | None = None) -> dict[str, str]:
        target = self._directory if directory is None else Path(directory)
        return {"directory": str(target)}

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _raise_for_status(
        self, method: str, path: str, response: httpx.Response
    ) -> None:
        logger.warning("%s %s failed with status %s", method, path, response.status_code)
        snippet = response.text[:500]
        raise OpenCodeError(
            f"{method} {path} failed with status {response.status_code}: {snippet}"
        )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def health(self) -> bool:
        path = "/api/health"
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=self._params(), timeout=self._timeout
            )
        if response.status_code >= 400:
            logger.warning("GET %s failed with status %s", path, response.status_code)
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("healthy") is True

    async def create_session(
        self,
        *,
        title: str | None = None,
        agent: str | None = None,
        permission: list[dict] | None = None,
        model: dict | None = None,
        directory: str | Path | None = None,
    ) -> dict:
        path = "/session"
        payload: dict = {}
        if title is not None:
            payload["title"] = title
        if agent is not None:
            payload["agent"] = agent
        if permission is not None:
            payload["permission"] = permission
        if model is not None:
            translated = _session_model_payload(model)
            if translated is None:
                raise OpenCodeError("Invalid model payload for session creation.")
            payload["model"] = translated

        async with self._http() as client:
            response = await client.post(
                self._url(path),
                params=self._params(directory),
                json=payload,
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Session response was not valid JSON.") from exc
        if not isinstance(body, dict):
            raise OpenCodeError("Session response was not a JSON object.")
        return body

    async def send_prompt(
        self,
        session_id: str,
        text: str,
        *,
        agent: str | None = None,
        model: dict | None = None,
        directory: str | Path | None = None,
    ) -> str:
        path = f"/session/{session_id}/message"
        payload: dict = {"parts": [{"type": "text", "text": text}]}
        if agent is not None:
            payload["agent"] = agent
        if model is not None:
            payload["model"] = model

        async with self._http() as client:
            response = await client.post(
                self._url(path),
                params=self._params(directory),
                json=payload,
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Prompt response was not valid JSON.") from exc
        if not isinstance(body, dict):
            raise OpenCodeError("Prompt response was not a JSON object.")

        info = body.get("info") or {}
        if isinstance(info, dict) and info.get("error"):
            raise OpenCodeError(f"opencode reported an error: {info['error']}")

        parts = body.get("parts") or []
        chunks = [
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        return "\n".join(chunks).strip()

    async def prompt_async(
        self, session_id: str, text: str, *, agent: str | None = None
    ) -> None:
        path = f"/session/{session_id}/prompt_async"
        payload: dict = {"parts": [{"type": "text", "text": text}]}
        if agent is not None:
            payload["agent"] = agent

        async with self._http() as client:
            response = await client.post(
                self._url(path),
                params=self._params(),
                json=payload,
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def abort(self, session_id: str) -> None:
        path = f"/session/{session_id}/abort"
        async with self._http() as client:
            response = await client.post(
                self._url(path), params=self._params(), timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def list_messages(self, session_id: str) -> list[dict]:
        path = f"/session/{session_id}/message"
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=self._params(), timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Message list response was not valid JSON.") from exc
        if not isinstance(body, list):
            raise OpenCodeError("Message list response was not a JSON array.")
        return body

    async def get_last_assistant_message(
        self, session_id: str, *, directory: str | Path | None = None
    ) -> dict | None:
        path = f"/session/{session_id}/message"
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=self._params(directory), timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Message list response was not valid JSON.") from exc
        if not isinstance(body, list):
            raise OpenCodeError("Message list response was not a JSON array.")
        for message in reversed(body):
            if not isinstance(message, dict):
                continue
            info = message.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            parts = message.get("parts")
            if isinstance(parts, list) and parts:
                return message
        return None

    async def get_session_diff(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
        directory: str | Path | None = None,
    ) -> list[dict]:
        path = f"/session/{session_id}/diff"
        params = self._params(directory)
        if message_id is not None:
            params["messageID"] = str(message_id)
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=params, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Session diff response was not valid JSON.") from exc
        if not isinstance(body, list):
            raise OpenCodeError("Session diff response was not a JSON array.")
        return body

    async def get_last_activity(
        self, session_id: str, *, directory: str | Path | None = None
    ) -> dict | None:
        path = f"/session/{session_id}/message"
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=self._params(directory), timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Message list response was not valid JSON.") from exc
        if not isinstance(body, list):
            raise OpenCodeError("Message list response was not a JSON array.")
        for message in reversed(body):
            if not isinstance(message, dict):
                continue
            info = message.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            parts = message.get("parts")
            if not isinstance(parts, list) or not parts:
                continue
            for part in reversed(parts):
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"step-start", "step-finish"}:
                    continue
                return part
            return None
        return None

    async def list_models(self) -> list[dict]:
        providers_path = "/config/providers"
        provider_path = "/provider"
        async with self._http() as client:
            providers_response = await client.get(
                self._url(providers_path),
                params=self._params(),
                timeout=self._timeout,
            )
            provider_response = await client.get(
                self._url(provider_path),
                params=self._params(),
                timeout=self._timeout,
            )
        if providers_response.status_code >= 400:
            self._raise_for_status("GET", providers_path, providers_response)

        try:
            body = providers_response.json()
        except ValueError as exc:
            raise OpenCodeError("Providers response was not valid JSON.") from exc
        if not isinstance(body, dict):
            raise OpenCodeError("Providers response was not a JSON object.")
        providers = body.get("providers")
        if not isinstance(providers, list):
            raise OpenCodeError("Providers response did not contain a providers list.")

        connected: list | None = None
        if provider_response.status_code < 400:
            try:
                provider_body = provider_response.json()
            except ValueError:
                provider_body = None
            if isinstance(provider_body, dict):
                raw_connected = provider_body.get("connected")
                if isinstance(raw_connected, list) and raw_connected:
                    connected = raw_connected
        connected_ids = {str(item) for item in connected} if connected else None

        models: list[dict] = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            provider_id = str(provider.get("id") or "")
            if connected_ids is not None and provider_id not in connected_ids:
                continue
            raw_models = provider.get("models")
            if isinstance(raw_models, dict):
                entries = list(raw_models.items())
            elif isinstance(raw_models, list):
                entries = [
                    (entry.get("id") if isinstance(entry, dict) else "", entry)
                    for entry in raw_models
                ]
            else:
                entries = []
            for key, entry in entries:
                if not isinstance(entry, dict):
                    continue
                model_id = str(entry.get("modelID") or entry.get("id") or key or "")
                resolved_provider = str(entry.get("providerID") or provider_id)
                if not model_id or not resolved_provider:
                    continue
                name = str(entry.get("name") or model_id)
                models.append(
                    {
                        "providerID": resolved_provider,
                        "modelID": model_id,
                        "name": name,
                    }
                )
        models.sort(key=lambda item: (item["providerID"], item["modelID"]))
        return models

    async def list_sessions(
        self, *, directory: str | Path | None = None, limit: int = 50
    ) -> list[dict]:
        path = "/session"
        params = self._params(directory)
        params["limit"] = str(limit)
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=params, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Session list response was not valid JSON.") from exc
        if not isinstance(body, list):
            raise OpenCodeError("Session list response was not a JSON array.")
        return body

    async def get_session(self, session_id: str) -> dict:
        path = f"/session/{session_id}"
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=self._params(), timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Session response was not valid JSON.") from exc
        if not isinstance(body, dict):
            raise OpenCodeError("Session response was not a JSON object.")
        return body

    async def get_session_status(self) -> dict:
        path = "/session/status"
        async with self._http() as client:
            response = await client.get(self._url(path), timeout=self._timeout)
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Session status response was not valid JSON.") from exc
        if not isinstance(body, dict):
            raise OpenCodeError("Session status response was not a JSON object.")
        return body

    async def get_session_todos(
        self, session_id: str, *, directory: str | Path | None = None
    ) -> list[dict]:
        path = f"/session/{session_id}/todo"
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=self._params(directory), timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Session todo response was not valid JSON.") from exc
        if not isinstance(body, list):
            raise OpenCodeError("Session todo response was not a JSON array.")
        return body

    async def _session_model(self, session_id: str) -> tuple[str, str] | None:
        try:
            session = await self.get_session(session_id)
        except OpenCodeError:
            return None
        resolved = _normalize_model(session.get("model"))
        return resolved

    async def compact_session(
        self, session_id: str, *, model: dict | None = None
    ) -> None:
        path = f"/api/session/{session_id}/compact"
        try:
            async with self._http() as client:
                response = await client.post(self._url(path), timeout=self._timeout)
            if response.status_code >= 400:
                self._raise_for_status("POST", path, response)
            return
        except OpenCodeError as exc:
            original_error = exc

        resolved = _normalize_model(model)
        if resolved is None:
            resolved = await self._session_model(session_id)
        if resolved is None:
            raise original_error

        provider_id, model_id = resolved
        summarize_path = f"/session/{session_id}/summarize"
        async with self._http() as client:
            response = await client.post(
                self._url(summarize_path),
                params=self._params(),
                json={
                    "providerID": provider_id,
                    "modelID": model_id,
                    "auto": True,
                },
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", summarize_path, response)

    async def get_mcp_status(self, *, directory: str | Path | None = None) -> dict:
        path = "/mcp"
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=self._params(directory), timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("MCP response was not valid JSON.") from exc
        if not isinstance(body, dict):
            raise OpenCodeError("MCP response was not a JSON object.")
        return body

    async def list_tool_ids(self, *, directory: str | Path | None = None) -> list[str]:
        path = "/experimental/tool/ids"
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=self._params(directory), timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Tool id response was not valid JSON.") from exc
        if not isinstance(body, list):
            raise OpenCodeError("Tool id response was not a JSON array.")
        return [str(item) for item in body]

    async def reply_permission(self, request_id: str, reply: str) -> None:
        if reply not in VALID_PERMISSION_REPLIES:
            valid = ", ".join(sorted(VALID_PERMISSION_REPLIES))
            raise ValueError(f"reply must be one of {valid}, got {reply!r}.")
        path = f"/permission/{request_id}/reply"
        async with self._http() as client:
            response = await client.post(
                self._url(path),
                params=self._params(),
                json={"reply": reply},
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def list_questions(
        self, *, directory: str | Path | None = None
    ) -> list[dict]:
        path = "/question"
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=self._params(directory), timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Question list response was not valid JSON.") from exc
        if not isinstance(body, list):
            raise OpenCodeError("Question list response was not a JSON array.")
        return body

    async def reply_question(
        self,
        request_id: str,
        answers: list,
        *,
        directory: str | Path | None = None,
    ) -> None:
        path = f"/question/{request_id}/reply"
        async with self._http() as client:
            response = await client.post(
                self._url(path),
                params=self._params(directory),
                json={"answers": answers},
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def reject_question(
        self, request_id: str, *, directory: str | Path | None = None
    ) -> None:
        path = f"/question/{request_id}/reject"
        async with self._http() as client:
            response = await client.post(
                self._url(path),
                params=self._params(directory),
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def _iter_sse(self, response: httpx.Response) -> AsyncIterator[dict]:
        parser = _SSEParser()
        async for line in response.aiter_lines():
            event = parser.feed(line)
            if event is not None:
                yield event
        trailing = parser.flush()
        if trailing is not None:
            yield trailing

    async def stream_events(self) -> AsyncIterator[dict]:
        path = "/event"
        headers = {"Accept": "text/event-stream"}
        async with self._http() as client:
            async with client.stream(
                "GET",
                self._url(path),
                params=self._params(),
                headers=headers,
                timeout=None,
            ) as response:
                if response.status_code >= 400:
                    logger.warning(
                        "GET %s failed with status %s", path, response.status_code
                    )
                    return
                async for event in self._iter_sse(response):
                    yield event
