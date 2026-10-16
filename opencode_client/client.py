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

MESSAGE_LIST_LIMIT = 200


class OpenCodeError(RuntimeError):
    pass


@dataclass
class PermissionRequest:
    """A pending opencode permission request (v2 ``permission.v2.asked``)."""

    id: str
    session_id: str
    action: str = ""
    resources: list[str] = field(default_factory=list)
    save: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)

    @classmethod
    def from_event(cls, event: dict) -> "PermissionRequest":
        properties = event.get("data") or event.get("properties") or {}
        if not isinstance(properties, dict):
            properties = {}
        return cls(
            id=str(properties.get("id", "")),
            session_id=str(properties.get("sessionID", "")),
            action=str(properties.get("action", "")),
            resources=[str(item) for item in (properties.get("resources") or [])],
            save=[str(item) for item in (properties.get("save") or [])],
            metadata=dict(properties.get("metadata") or {}),
            source=dict(properties.get("source") or {}),
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


def _model_ref(model: dict | None) -> dict | None:
    """Translate a model payload to the v2 ``Model.Ref`` shape."""
    if not isinstance(model, dict):
        return None
    model_id = model.get("id") or model.get("modelID") or model.get("modelId")
    provider_id = model.get("providerID") or model.get("providerId")
    if not model_id or not provider_id:
        return None
    ref: dict = {"id": str(model_id), "providerID": str(provider_id)}
    if model.get("variant") is not None:
        ref["variant"] = model["variant"]
    return ref


def _model_key(ref: dict | None) -> str | None:
    if not isinstance(ref, dict):
        return None
    return f"{ref.get('providerID', '')}/{ref.get('id', '')}"


def _created(message: dict) -> float:
    time = message.get("time") if isinstance(message, dict) else None
    if isinstance(time, dict):
        value = time.get("created")
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _assistant_text(message: dict) -> list[str]:
    if not isinstance(message, dict) or message.get("type") != "assistant":
        return []
    chunks: list[str] = []
    for part in message.get("content") or []:
        if (
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            chunks.append(part["text"])
    return chunks


def _assistant_parts(message: dict) -> list[dict]:
    if not isinstance(message, dict) or message.get("type") != "assistant":
        return []
    return [part for part in (message.get("content") or []) if isinstance(part, dict)]


def _mcp_status_label(status) -> str:
    if isinstance(status, str):
        return status
    if isinstance(status, dict):
        for key in ("type", "status", "state", "_tag"):
            value = status.get(key)
            if isinstance(value, str) and value:
                return value
        if len(status) == 1:
            key = next(iter(status))
            if isinstance(key, str):
                return key
    return "unknown"


class OpenCodeClient:
    def __init__(
        self,
        base_url: str | None = None,
        directory: str | Path | None = None,
        timeout: float | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or config.OPENCODE_BASE_URL).rstrip("/")
        if directory is None:
            directory = (
                config.OPENCODE_DIRECTORY
                if self._base_url == config.OPENCODE_BASE_URL.rstrip("/")
                else None
            )
        self._directory = Path(directory) if directory is not None else None
        self._timeout = (
            float(config.OPENCODE_TIMEOUT) if timeout is None else float(timeout)
        )
        # ``password`` and ``token`` are aliases for the secret; ``token`` is only
        # consulted when ``password`` is None. Credentials are never logged.
        secret = password if password is not None else token
        if username is None and not secret:
            # opencode v2 requires HTTP basic auth when OPENCODE_SERVER_PASSWORD
            # is set. Fall back to the configured credentials for the default
            # server (including localhost) so callers do not have to pass them.
            default_url = config.OPENCODE_BASE_URL.rstrip("/")
            credentials = (
                config.opencode_server_credentials()
                if self._base_url == default_url
                else None
            )
            if credentials is not None:
                username, secret = credentials
        self._auth = httpx.BasicAuth(username, secret or "") if username else None
        self._headers = (
            {"Authorization": f"Bearer {secret}"}
            if username is None and secret
            else None
        )
        self._client = client
        self._session_selection: dict[str, dict] = {}

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def directory(self) -> Path | None:
        return self._directory

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def auth(self) -> httpx.Auth | None:
        """Auth object for httpx, or ``None`` when the client is unauthenticated."""
        return self._auth

    @property
    def auth_headers(self) -> dict | None:
        """Extra headers (e.g. ``Authorization: Bearer``) or ``None``."""
        return self._headers

    @asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
        else:
            async with httpx.AsyncClient(
                timeout=self._timeout, auth=self._auth, headers=self._headers
            ) as client:
                yield client

    def _location_params(self, directory: str | Path | None = None) -> dict[str, str]:
        target = directory if directory is not None else self._directory
        if target is None:
            return {}
        return {"location[directory]": str(target)}

    def _params(self, directory: str | Path | None = None) -> dict[str, str]:
        return self._location_params(directory)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _raise_for_status(
        self, method: str, path: str, response: httpx.Response
    ) -> None:
        logger.warning(
            "%s %s failed with status %s", method, path, response.status_code
        )
        snippet = response.text[:500]
        raise OpenCodeError(
            f"{method} {path} failed with status {response.status_code}: {snippet}"
        )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    @staticmethod
    def _data(body: dict, default=None):
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return default if default is not None else body

    async def health(self) -> bool:
        path = "/api/info"
        async with self._http() as client:
            response = await client.get(self._url(path), timeout=self._timeout)
        if response.status_code >= 400:
            logger.warning("GET %s failed with status %s", path, response.status_code)
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict)

    # -- sessions --------------------------------------------------------

    async def create_session(
        self,
        *,
        title: str | None = None,
        agent: str | None = None,
        permission: list[dict] | None = None,
        model: dict | None = None,
        directory: str | Path | None = None,
    ) -> dict:
        path = "/api/session"
        payload: dict = {}
        if title is not None:
            payload["title"] = title
        if agent is not None:
            payload["agent"] = agent
        if permission is not None:
            payload["permissions"] = permission
        ref = _model_ref(model)
        if ref is not None:
            payload["model"] = ref
        if directory is not None:
            payload["location"] = {"directory": str(directory)}

        async with self._http() as client:
            response = await client.post(
                self._url(path), json=payload, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Session response was not valid JSON.") from exc
        session = self._data(body)
        if not isinstance(session, dict):
            raise OpenCodeError("Session response did not contain a session.")
        session_id = str(session.get("id") or "")
        if session_id:
            selection: dict = {}
            if agent:
                selection["agent"] = agent
            if ref:
                selection["model"] = _model_key(ref)
            self._session_selection[session_id] = selection
        return session

    async def list_sessions(
        self, *, directory: str | Path | None = None, limit: int = 50
    ) -> list[dict]:
        path = "/api/session"
        params: dict[str, str] = {"limit": str(limit)}
        target = directory if directory is not None else self._directory
        if target is not None:
            params["directory"] = str(target)
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
        data = self._data(body, [])
        if not isinstance(data, list):
            raise OpenCodeError("Session list response was not a JSON array.")
        return data

    async def get_session(self, session_id: str) -> dict:
        path = f"/api/session/{session_id}"
        async with self._http() as client:
            response = await client.get(self._url(path), timeout=self._timeout)
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Session response was not valid JSON.") from exc
        session = self._data(body)
        if not isinstance(session, dict):
            raise OpenCodeError("Session response was not a JSON object.")
        return session

    async def get_session_status(self) -> dict:
        """Return ``{sessionID: {"type": "busy"}}`` for active sessions."""
        path = "/api/session/active"
        async with self._http() as client:
            response = await client.get(self._url(path), timeout=self._timeout)
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Session status response was not valid JSON.") from exc
        active = self._data(body, {})
        if not isinstance(active, dict):
            raise OpenCodeError("Session status response was not a JSON object.")
        return {
            str(session_id): {"type": "busy", "status": entry}
            for session_id, entry in active.items()
        }

    async def get_session_todos(
        self, session_id: str, *, directory: str | Path | None = None
    ) -> list[dict]:
        # opencode v2 no longer exposes a per-session todo endpoint.
        return []

    # -- prompting -------------------------------------------------------

    async def _switch_model(self, session_id: str, ref: dict) -> None:
        path = f"/api/session/{session_id}/model"
        async with self._http() as client:
            response = await client.post(
                self._url(path), json={"model": ref}, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)
        self._session_selection.setdefault(session_id, {})["model"] = _model_key(ref)

    async def _switch_agent(self, session_id: str, agent: str) -> None:
        path = f"/api/session/{session_id}/agent"
        async with self._http() as client:
            response = await client.post(
                self._url(path), json={"agent": agent}, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)
        self._session_selection.setdefault(session_id, {})["agent"] = agent

    async def _ensure_selection(
        self, session_id: str, *, agent: str | None, model: dict | None
    ) -> None:
        selection = self._session_selection.setdefault(session_id, {})
        ref = _model_ref(model)
        if ref is not None and selection.get("model") != _model_key(ref):
            await self._switch_model(session_id, ref)
        if agent and selection.get("agent") != agent:
            await self._switch_agent(session_id, agent)

    async def _prompt(self, session_id: str, text: str) -> None:
        path = f"/api/session/{session_id}/prompt"
        async with self._http() as client:
            response = await client.post(
                self._url(path), json={"text": text}, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def _wait(self, session_id: str) -> None:
        path = f"/api/experimental/session/{session_id}/wait"
        async with self._http() as client:
            response = await client.post(self._url(path), timeout=self._timeout)
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def send_prompt(
        self,
        session_id: str,
        text: str,
        *,
        agent: str | None = None,
        model: dict | None = None,
        directory: str | Path | None = None,
    ) -> str:
        await self._ensure_selection(session_id, agent=agent, model=model)
        await self._prompt(session_id, text)
        await self._wait(session_id)
        return await self._latest_assistant_text(session_id)

    async def prompt_async(
        self, session_id: str, text: str, *, agent: str | None = None
    ) -> None:
        await self._ensure_selection(session_id, agent=agent, model=None)
        await self._prompt(session_id, text)

    async def abort(self, session_id: str) -> None:
        path = f"/api/session/{session_id}/interrupt"
        async with self._http() as client:
            response = await client.post(
                self._url(path), params={"resume": "false"}, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def compact_session(
        self, session_id: str, *, model: dict | None = None
    ) -> None:
        path = f"/api/session/{session_id}/compact"
        async with self._http() as client:
            response = await client.post(
                self._url(path), json={}, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    # -- messages --------------------------------------------------------

    async def list_messages(self, session_id: str) -> list[dict]:
        path = f"/api/session/{session_id}/message"
        params = {"limit": str(MESSAGE_LIST_LIMIT), "order": "asc"}
        async with self._http() as client:
            response = await client.get(
                self._url(path), params=params, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Message list response was not valid JSON.") from exc
        data = self._data(body, [])
        if not isinstance(data, list):
            raise OpenCodeError("Message list response was not a JSON array.")
        return data

    @staticmethod
    def _ordered(messages: list[dict]) -> list[dict]:
        return sorted(
            (message for message in messages if isinstance(message, dict)),
            key=_created,
        )

    async def _latest_assistant_text(self, session_id: str) -> str:
        messages = self._ordered(await self.list_messages(session_id))
        last_user = -1
        for index, message in enumerate(messages):
            if message.get("type") == "user":
                last_user = index
        chunks: list[str] = []
        for message in messages[last_user + 1 :]:
            chunks.extend(_assistant_text(message))
        return "\n".join(chunks).strip()

    async def get_turn_assistant_parts(
        self, session_id: str, *, directory: str | Path | None = None
    ) -> list[dict]:
        messages = self._ordered(await self.list_messages(session_id))
        last_user = -1
        for index, message in enumerate(messages):
            if message.get("type") == "user":
                last_user = index
        parts: list[dict] = []
        for message in messages[last_user + 1 :]:
            parts.extend(_assistant_parts(message))
        return parts

    async def get_last_assistant_message(
        self, session_id: str, *, directory: str | Path | None = None
    ) -> dict | None:
        messages = self._ordered(await self.list_messages(session_id))
        for message in reversed(messages):
            if message.get("type") == "assistant":
                return message
        return None

    async def get_last_activity(
        self, session_id: str, *, directory: str | Path | None = None
    ) -> dict | None:
        message = await self.get_last_assistant_message(session_id, directory=directory)
        if message is None:
            return None
        parts = _assistant_parts(message)
        for part in reversed(parts):
            if part.get("type") in {"text", "reasoning", "tool"}:
                return part
        return None

    async def get_session_diff(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
        directory: str | Path | None = None,
    ) -> list[dict]:
        path = f"/api/session/{session_id}/diff"
        params: dict[str, str] = {}
        if message_id is not None:
            params["from"] = str(message_id)
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
        data = self._data(body, [])
        if not isinstance(data, list):
            raise OpenCodeError("Session diff response was not a JSON array.")
        return data

    # -- location & filesystem ------------------------------------------

    async def get_path(self, *, directory: str | Path | None = None) -> dict:
        path = "/api/location"
        async with self._http() as client:
            response = await client.get(
                self._url(path),
                params=self._location_params(directory),
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Location response was not valid JSON.") from exc
        if not isinstance(body, dict):
            raise OpenCodeError("Location response was not a JSON object.")
        return body

    async def default_directory(self, *, directory: str | Path | None = None) -> str:
        try:
            body = await self.get_path(directory=directory)
        except Exception:
            logger.warning("Could not resolve the server default directory")
            return "/"
        for key in ("directory", "home"):
            value = body.get(key) if isinstance(body, dict) else None
            if isinstance(value, str) and value:
                return value
        return "/"

    @staticmethod
    def _join_path(base: str, name: str) -> str:
        cleaned = (name or "").rstrip("/\\")
        if not cleaned:
            return base
        if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
            return cleaned
        separator = "\\" if "\\" in base and "/" not in base else "/"
        trimmed = (base or "").rstrip("/\\")
        return f"{trimmed}{separator}{cleaned}" if trimmed else cleaned

    async def list_directory(
        self, path: str, *, directory: str | Path | None = None
    ) -> list[dict]:
        target = "/api/fs/list"
        base = str(path)
        params = {"path": base}
        params.update(self._location_params(directory if directory is not None else path))
        async with self._http() as client:
            response = await client.get(
                self._url(target), params=params, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", target, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Directory listing response was not valid JSON.") from exc
        data = self._data(body, [])
        if not isinstance(data, list):
            raise OpenCodeError("Directory listing response was not a JSON array.")
        entries: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            raw_path = str(entry.get("path") or "")
            entry_type = entry.get("type")
            absolute = self._join_path(base, raw_path)
            name = Path(raw_path.rstrip("/\\")).name if raw_path else base
            entries.append(
                {
                    "name": name,
                    "path": absolute,
                    "absolute": absolute,
                    "type": entry_type,
                    "ignored": bool(entry.get("ignored")),
                }
            )
        return entries

    # -- models, providers, agents --------------------------------------

    async def list_models(self) -> list[dict]:
        path = "/api/model"
        async with self._http() as client:
            response = await client.get(
                self._url(path),
                params=self._location_params(),
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Model response was not valid JSON.") from exc
        data = self._data(body, [])
        if not isinstance(data, list):
            raise OpenCodeError("Model response did not contain a model list.")
        models: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled") is False:
                continue
            model_id = str(entry.get("modelID") or entry.get("id") or "")
            provider_id = str(entry.get("providerID") or "")
            if not model_id or not provider_id:
                continue
            models.append(
                {
                    "providerID": provider_id,
                    "modelID": model_id,
                    "name": str(entry.get("name") or model_id),
                }
            )
        models.sort(key=lambda item: (item["providerID"], item["modelID"]))
        return models

    async def get_mcp_status(self, *, directory: str | Path | None = None) -> dict:
        path = "/api/mcp"
        async with self._http() as client:
            response = await client.get(
                self._url(path),
                params=self._location_params(directory),
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("MCP response was not valid JSON.") from exc
        data = self._data(body, [])
        if not isinstance(data, list):
            raise OpenCodeError("MCP response did not contain a server list.")
        servers: dict = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if not name:
                continue
            servers[name] = {"status": _mcp_status_label(entry.get("status"))}
        return servers

    async def list_tool_ids(self, *, directory: str | Path | None = None) -> list[str]:
        # opencode v2 no longer exposes a flat tool id list.
        return []

    # -- permissions -----------------------------------------------------

    async def reply_permission(
        self, request_id: str, reply: str, *, session_id: str
    ) -> None:
        if reply not in VALID_PERMISSION_REPLIES:
            valid = ", ".join(sorted(VALID_PERMISSION_REPLIES))
            raise ValueError(f"reply must be one of {valid}, got {reply!r}.")
        path = f"/api/session/{session_id}/permission/{request_id}/reply"
        async with self._http() as client:
            response = await client.post(
                self._url(path),
                json={"decision": reply},
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def list_permissions(
        self, *, directory: str | Path | None = None
    ) -> list[dict]:
        path = "/api/permission/request"
        async with self._http() as client:
            response = await client.get(
                self._url(path),
                params=self._location_params(directory),
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Permission list response was not valid JSON.") from exc
        data = self._data(body, [])
        if not isinstance(data, list):
            raise OpenCodeError("Permission list response was not a JSON array.")
        return data

    # -- forms (the v2 question mechanism) --------------------------------

    async def list_forms(self, *, directory: str | Path | None = None) -> list[dict]:
        path = "/api/form"
        async with self._http() as client:
            response = await client.get(
                self._url(path),
                params=self._location_params(directory),
                timeout=self._timeout,
            )
        if response.status_code >= 400:
            self._raise_for_status("GET", path, response)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenCodeError("Form list response was not valid JSON.") from exc
        data = self._data(body, [])
        if not isinstance(data, list):
            raise OpenCodeError("Form list response was not a JSON array.")
        return data

    async def reply_form(
        self, form_id: str, answer: dict, *, session_id: str
    ) -> None:
        path = f"/api/session/{session_id}/form/{form_id}/reply"
        async with self._http() as client:
            response = await client.post(
                self._url(path), json={"answer": answer}, timeout=self._timeout
            )
        if response.status_code >= 400:
            self._raise_for_status("POST", path, response)

    async def cancel_form(self, form_id: str, *, session_id: str) -> None:
        path = f"/api/session/{session_id}/form/{form_id}"
        async with self._http() as client:
            response = await client.delete(self._url(path), timeout=self._timeout)
        if response.status_code >= 400:
            self._raise_for_status("DELETE", path, response)

    # -- events ----------------------------------------------------------

    async def _iter_sse(self, response: httpx.Response) -> AsyncIterator[dict]:
        parser = _SSEParser()
        async for line in response.aiter_lines():
            event = parser.feed(line)
            if event is not None:
                yield event
        trailing = parser.flush()
        if trailing is not None:
            yield trailing

    async def stream_events(
        self, *, directory: str | Path | None = None
    ) -> AsyncIterator[dict]:
        path = "/api/event"
        headers = {"Accept": "text/event-stream"}
        async with self._http() as client:
            async with client.stream(
                "GET",
                self._url(path),
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
