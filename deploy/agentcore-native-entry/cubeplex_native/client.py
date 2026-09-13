"""Small, strict HTTP client for the native CubePlex control-plane contract."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import httpx


class ControlPlaneError(RuntimeError):
    """A control-plane request was rejected or could not be decoded."""

    def __init__(self, code: str, *, status_code: int | None = None, detail: Any = None) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(code)


def _request_id() -> str:
    return str(uuid4())


class NativeControlPlaneClient:
    """Authenticate every request with the dispatch-scoped capability."""

    def __init__(
        self,
        *,
        base_url: str,
        dispatch_id: str,
        capability: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        try:
            UUID(dispatch_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("dispatch_id must be a UUID") from exc
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.host
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("control-plane URL must be an HTTPS origin without credentials")
        self.base_url = str(parsed).rstrip("/")
        self.dispatch_id = dispatch_id
        self.capability = capability
        self._owned_client = http_client is None
        self.http = http_client or httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=False,
        )

    @classmethod
    def from_environment(
        cls,
        *,
        dispatch_id: str,
        capability: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> "NativeControlPlaneClient":
        base_url = os.environ.get("CUBEPLEX_NATIVE_CONTROL_PLANE_URL", "")
        if not base_url:
            raise ControlPlaneError("control_plane_url_missing")
        if not capability:
            raise ControlPlaneError("capability_missing")
        return cls(
            base_url=base_url,
            dispatch_id=dispatch_id,
            capability=capability,
            http_client=http_client,
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/agentcore/tasks/{self.dispatch_id}{path}"

    def _headers(self, request_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.capability}",
            "X-AgentCore-Request-Id": request_id,
            "Accept": "application/json",
        }

    @staticmethod
    def _error_from_response(response: httpx.Response) -> ControlPlaneError:
        try:
            body: Any = response.json()
        except (ValueError, json.JSONDecodeError):
            body = None
        detail = body.get("detail") if isinstance(body, Mapping) else None
        if isinstance(detail, Mapping):
            code = detail.get("code")
        else:
            code = None
        return ControlPlaneError(
            str(code or f"control_plane_http_{response.status_code}"),
            status_code=response.status_code,
            detail=body,
        )

    async def _json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        request_id = request_id or _request_id()
        response = await self.http.request(
            method,
            self._url(path),
            headers=self._headers(request_id),
            json=dict(payload) if payload is not None else None,
        )
        if response.status_code >= 400:
            raise self._error_from_response(response)
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ControlPlaneError(
                "control_plane_json_invalid", status_code=response.status_code
            ) from exc
        if not isinstance(body, Mapping):
            raise ControlPlaneError(
                "control_plane_response_invalid", status_code=response.status_code
            )
        if body.get("version") != 1 or "result" not in body:
            raise ControlPlaneError(
                "control_plane_envelope_invalid", status_code=response.status_code
            )
        return body["result"]

    async def claim(self) -> dict[str, Any]:
        result = await self._json(
            "POST", "/claim", payload={"version": 1, "request_id": _request_id()}
        )
        if not isinstance(result, dict):
            raise ControlPlaneError("claim_response_invalid")
        return result

    async def control(self) -> dict[str, Any]:
        result = await self._json("GET", "/control")
        if not isinstance(result, dict):
            raise ControlPlaneError("control_response_invalid")
        return result

    async def checkpoint(self, op: str, args: Mapping[str, Any] | None = None) -> Any:
        return await self._json(
            "POST",
            "/checkpoint",
            payload={"version": 1, "request_id": _request_id(), "op": op, "args": dict(args or {})},
        )

    async def events(self, seq: int, event: Mapping[str, Any]) -> dict[str, Any]:
        result = await self._json(
            "POST",
            "/events",
            payload={"version": 1, "seq": seq, "event": dict(event)},
        )
        if not isinstance(result, dict):
            raise ControlPlaneError("events_response_invalid")
        return result

    async def workspace_get(self) -> dict[str, Any]:
        result = await self._json("GET", "/workspace")
        if not isinstance(result, dict):
            raise ControlPlaneError("workspace_response_invalid")
        return result

    async def workspace_put(self, files: list[Mapping[str, Any]]) -> dict[str, Any]:
        result = await self._json(
            "POST",
            "/workspace",
            payload={"version": 1, "request_id": _request_id(), "files": [dict(f) for f in files]},
        )
        if not isinstance(result, dict):
            raise ControlPlaneError("workspace_response_invalid")
        return result

    async def present(self, file: Mapping[str, Any]) -> dict[str, Any]:
        result = await self._json(
            "POST",
            "/present",
            payload={"version": 1, "request_id": _request_id(), **dict(file)},
        )
        if not isinstance(result, dict):
            raise ControlPlaneError("present_response_invalid")
        return result

    async def model_response(self, body: Mapping[str, Any], request_id: str) -> httpx.Response:
        headers = self._headers(request_id)
        headers["Accept"] = "text/event-stream"
        response = await self.http.post(
            self._url("/model/responses"),
            headers=headers,
            json=dict(body),
            timeout=httpx.Timeout(180.0, connect=5.0),
        )
        if response.status_code >= 400:
            raise self._error_from_response(response)
        return response

    async def finish(
        self,
        *,
        request_id: str,
        status: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "errored", "paused_hitl", "cancelled"}:
            raise ValueError("invalid finish status")
        result = await self._json(
            "POST",
            "/finish",
            payload={
                "version": 1,
                "request_id": request_id,
                "status": status,
                "error_code": error_code,
            },
        )
        if not isinstance(result, dict):
            raise ControlPlaneError("finish_response_invalid")
        return result

    async def aclose(self) -> None:
        if self._owned_client:
            await self.http.aclose()
