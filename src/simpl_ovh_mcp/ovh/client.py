"""OVHcloud API client.

Supports both authentication schemes OVH offers, because accounts in the wild use both:

* **signature** — the classic application key / application secret / consumer key triplet.
  Every request carries `X-Ovh-Signature`, the SHA-1 of the secret, the consumer key, the
  method, the full URL, the body and a timestamp. The timestamp must match OVH's clock, so
  the client learns the drift once from `/auth/time` and applies it to every call.
* **oauth2** — a service account (client id / secret) exchanged for a one-hour bearer
  token at the region's token endpoint. Newer, and the one OVH recommends for automation.

Configure either. If both are present, OAuth2 wins.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ..errors import OVH_HINTS, ConfigError, UpstreamError
from ..settings import Settings, get_settings

ENDPOINTS: dict[str, dict[str, str]] = {
    "ovh-eu": {
        "api": "https://eu.api.ovh.com/1.0",
        "oauth": "https://www.ovh.com/auth/oauth2/token",
    },
    "ovh-ca": {
        "api": "https://ca.api.ovh.com/1.0",
        "oauth": "https://ca.ovh.com/auth/oauth2/token",
    },
    "ovh-us": {
        "api": "https://api.us.ovhcloud.com/1.0",
        "oauth": "https://us.ovhcloud.com/auth/oauth2/token",
    },
    "kimsufi-eu": {"api": "https://eu.api.kimsufi.com/1.0", "oauth": ""},
    "kimsufi-ca": {"api": "https://ca.api.kimsufi.com/1.0", "oauth": ""},
    "soyoustart-eu": {"api": "https://eu.api.soyoustart.com/1.0", "oauth": ""},
    "soyoustart-ca": {"api": "https://ca.api.soyoustart.com/1.0", "oauth": ""},
}

RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 3


class OvhClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        endpoint = ENDPOINTS.get(self.settings.ovh_endpoint)
        if endpoint is None:
            raise ConfigError(
                f"unknown OVH endpoint '{self.settings.ovh_endpoint}'",
                f"Use one of: {', '.join(ENDPOINTS)}.",
            )
        self.base_url = endpoint["api"]
        self.oauth_url = endpoint["oauth"]
        self._time_delta: int | None = None
        self._token: tuple[str, float] | None = None  # (access_token, expires_at)
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # --- lifecycle --------------------------------------------------------------------
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.http_timeout,
                headers={"User-Agent": "simpl-ovh-mcp/0.1"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- auth -------------------------------------------------------------------------
    def _require_credentials(self) -> str:
        mode = self.settings.ovh_auth_mode
        if mode == "none":
            raise ConfigError(
                "no OVH credentials configured",
                "Set OVH_APPLICATION_KEY, OVH_APPLICATION_SECRET and OVH_CONSUMER_KEY, or an "
                "OAuth2 service account as OVH_CLIENT_ID and OVH_CLIENT_SECRET. Create either at "
                "https://api.ovh.com/createToken/ or in the OVH control panel.",
            )
        return mode

    async def _server_time_delta(self) -> int:
        if self._time_delta is None:
            client = await self._http()
            resp = await client.get(f"{self.base_url}/auth/time")
            resp.raise_for_status()
            self._time_delta = int(resp.text) - int(time.time())
        return self._time_delta

    async def _oauth_token(self) -> str:
        now = time.time()
        if self._token and self._token[1] > now + 30:
            return self._token[0]
        async with self._lock:
            if self._token and self._token[1] > time.time() + 30:
                return self._token[0]
            if not self.oauth_url:
                raise ConfigError(
                    f"endpoint '{self.settings.ovh_endpoint}' has no OAuth2 token service",
                    "Use the application key / consumer key credentials for this endpoint.",
                )
            client = await self._http()
            resp = await client.post(
                self.oauth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.ovh_client_id,
                    "client_secret": self.settings.ovh_client_secret,
                    "scope": "all",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code >= 400:
                raise UpstreamError(
                    "OVH OAuth2",
                    resp.status_code,
                    _short(resp.text),
                    "Check OVH_CLIENT_ID / OVH_CLIENT_SECRET and that the service account has "
                    "the right policies attached in the OVH IAM console.",
                )
            payload = resp.json()
            token = payload["access_token"]
            self._token = (token, time.time() + int(payload.get("expires_in", 3600)))
            return token

    async def _headers(self, method: str, url: str, body: str) -> dict[str, str]:
        mode = self._require_credentials()
        headers = {"Content-Type": "application/json"}
        if mode == "oauth2":
            headers["Authorization"] = f"Bearer {await self._oauth_token()}"
            return headers
        delta = await self._server_time_delta()
        timestamp = str(int(time.time()) + delta)
        raw = "+".join(
            [
                self.settings.ovh_application_secret or "",
                self.settings.ovh_consumer_key or "",
                method.upper(),
                url,
                body,
                timestamp,
            ]
        )
        headers.update(
            {
                "X-Ovh-Application": self.settings.ovh_application_key or "",
                "X-Ovh-Consumer": self.settings.ovh_consumer_key or "",
                "X-Ovh-Timestamp": timestamp,
                "X-Ovh-Signature": "$1$" + hashlib.sha1(raw.encode("utf-8")).hexdigest(),
            }
        )
        return headers

    # --- requests ---------------------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        if params:
            clean = {k: _qs(v) for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urlencode(clean)}"
        payload = "" if body is None else json.dumps(body)
        client = await self._http()

        last: httpx.Response | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = await self._headers(method, url, payload)
            resp = await client.request(
                method.upper(),
                url,
                headers=headers,
                content=payload.encode("utf-8") if payload else None,
            )
            last = resp
            if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
                continue
            break

        assert last is not None
        if last.status_code >= 400:
            raise _to_error(last)
        if not last.content:
            return None
        try:
            return last.json()
        except json.JSONDecodeError:
            return last.text

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params or None)

    async def post(self, path: str, body: Any | None = None, **params: Any) -> Any:
        return await self.request("POST", path, params=params or None, body=body)

    async def put(self, path: str, body: Any | None = None, **params: Any) -> Any:
        return await self.request("PUT", path, params=params or None, body=body)

    async def delete(self, path: str, **params: Any) -> Any:
        return await self.request("DELETE", path, params=params or None)

    # --- convenience ------------------------------------------------------------------
    def project(self, explicit: str | None = None) -> str:
        service = explicit or self.settings.ovh_cloud_project
        if not service:
            raise ConfigError(
                "no OVH cloud project given",
                "Pass project=<serviceName> or set OVH_CLOUD_PROJECT. List them with "
                "ovh_projects_list.",
            )
        return service


def _qs(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _short(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _to_error(resp: httpx.Response) -> UpstreamError:
    message = _short(resp.text)
    detail: Any = None
    try:
        payload = resp.json()
        detail = payload
        if isinstance(payload, dict):
            message = payload.get("message") or message
            if payload.get("class"):
                message = f"{message} ({payload['class']})"
    except (json.JSONDecodeError, ValueError):
        pass
    return UpstreamError(
        "OVH API", resp.status_code, message, OVH_HINTS.get(resp.status_code), detail
    )


_client: OvhClient | None = None


def get_ovh() -> OvhClient:
    global _client
    if _client is None:
        _client = OvhClient()
    return _client
