"""A small Kubernetes API client, built on httpx.

Why not the official client library: this server needs four things — read objects, apply
manifests, read logs, and reach services inside the cluster — and it needs them to work
identically for core resources and for CRDs like `argoproj.io/Application`, which is where
every Simpl-Open deployment actually lives. Discovery plus raw HTTP does that in a couple
of hundred lines and adds no dependency that has to track Kubernetes releases.

Two capabilities carry most of the weight:

* **Server-side apply.** `PATCH` with `application/apply-patch+yaml` is `kubectl apply`
  without kubectl, and it is how every manifest this server creates is written, so
  repeated calls converge instead of conflicting.
* **The API server's service proxy.** `/api/v1/namespaces/{ns}/services/{scheme}:{name}:{port}/proxy/…`
  reaches any HTTP service in the cluster through the API server, which is what lets a
  server running on Railway talk to ArgoCD and to the Governance Authority's internal
  endpoints with no ingress, no port-forward and nothing published to the internet.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
import yaml

from ..errors import KUBE_HINTS, NotFound, UpstreamError
from ..settings import get_settings
from .kubeconfig import KubeTarget

FIELD_MANAGER = "simpl-ovh-mcp"


@dataclass(frozen=True)
class ResourceRef:
    group_version: str
    kind: str
    plural: str
    namespaced: bool

    def path(self, namespace: str | None = None, name: str | None = None) -> str:
        base = (
            f"/api/{self.group_version}"
            if "/" not in self.group_version
            else f"/apis/{self.group_version}"
        )
        if self.namespaced:
            if not namespace:
                raise NotFound(
                    f"{self.kind} is namespaced and no namespace was given",
                    "Pass namespace=…",
                )
            base = f"{base}/namespaces/{namespace}"
        base = f"{base}/{self.plural}"
        return f"{base}/{name}" if name else base


class KubeClient:
    def __init__(self, target: KubeTarget) -> None:
        self.target = target
        self._client: httpx.AsyncClient | None = None
        self._resources: dict[str, ResourceRef] = {}
        self._discovery_lock = asyncio.Lock()

    async def http(self) -> httpx.AsyncClient:
        if self._client is None:
            settings = get_settings()
            self._client = httpx.AsyncClient(
                base_url=self.target.server,
                verify=self.target.ssl_context(),
                timeout=httpx.Timeout(settings.http_timeout, read=settings.http_timeout),
                headers={
                    "Accept": "application/json",
                    "User-Agent": "simpl-ovh-mcp/0.1",
                    **self.target.auth_headers(),
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- raw ---------------------------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
        raw_text: bool = False,
    ) -> Any:
        client = await self.http()
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        data = content
        if body is not None and content is None:
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        clean = {k: _qs(v) for k, v in (params or {}).items() if v is not None}
        resp = await client.request(
            method.upper(),
            path,
            params=clean or None,
            content=data,
            headers=headers or None,
            timeout=timeout,
        )
        if resp.status_code >= 400:
            raise _kube_error(resp)
        if raw_text:
            return resp.text
        if not resp.content:
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            return resp.text

    # --- discovery ---------------------------------------------------------------------
    async def discover(self, refresh: bool = False) -> dict[str, ResourceRef]:
        """Map kind names to API paths, including every CRD the cluster has."""
        if self._resources and not refresh:
            return self._resources
        async with self._discovery_lock:
            if self._resources and not refresh:
                return self._resources
            found: dict[str, ResourceRef] = {}
            group_versions = ["v1"]
            groups = await self.request("GET", "/apis")
            for group in groups.get("groups", []):
                preferred = (group.get("preferredVersion") or {}).get("groupVersion")
                if preferred:
                    group_versions.append(preferred)

            async def load(gv: str) -> None:
                path = f"/api/{gv}" if "/" not in gv else f"/apis/{gv}"
                try:
                    listing = await self.request("GET", path)
                except UpstreamError:
                    return
                for res in listing.get("resources", []):
                    name = res.get("name", "")
                    if "/" in name:  # subresources: status, log, scale…
                        continue
                    ref = ResourceRef(
                        group_version=gv,
                        kind=res.get("kind", ""),
                        plural=name,
                        namespaced=bool(res.get("namespaced")),
                    )
                    for key in _keys_for(ref):
                        found.setdefault(key, ref)

            await asyncio.gather(*(load(gv) for gv in group_versions))
            self._resources = found
            return found

    async def resolve(self, kind: str, api_version: str | None = None) -> ResourceRef:
        """Accepts 'Pod', 'pods', 'deployment', 'applications.argoproj.io', …"""
        key = kind.lower()
        resources = await self.discover()
        if api_version:
            direct = resources.get(f"{api_version.lower()}/{key}")
            if direct:
                return direct
        ref = resources.get(key)
        if ref is None:
            resources = await self.discover(refresh=True)
            ref = resources.get(key)
        if ref is None:
            raise NotFound(
                f"no Kubernetes resource matches '{kind}'",
                "Use the kind (Pod, Deployment, Application) or the plural (pods, "
                "applications). A CRD that is not installed yet cannot be resolved.",
            )
        return ref

    # --- objects -----------------------------------------------------------------------
    async def list(
        self,
        kind: str,
        namespace: str | None = None,
        label_selector: str | None = None,
        field_selector: str | None = None,
        limit: int = 200,
        api_version: str | None = None,
    ) -> list[dict[str, Any]]:
        ref = await self.resolve(kind, api_version)
        path = ref.path(namespace) if ref.namespaced and namespace else _cluster_wide_path(ref)
        data = await self.request(
            "GET",
            path,
            params={
                "labelSelector": label_selector,
                "fieldSelector": field_selector,
                "limit": limit,
            },
        )
        return data.get("items", [])

    async def get(
        self,
        kind: str,
        name: str,
        namespace: str | None = None,
        api_version: str | None = None,
    ) -> dict[str, Any]:
        ref = await self.resolve(kind, api_version)
        return await self.request("GET", ref.path(namespace, name))

    async def apply(self, manifest: dict[str, Any], force: bool = True) -> dict[str, Any]:
        """Server-side apply one object. Creates it if absent, converges it if present."""
        api_version = manifest.get("apiVersion")
        kind = manifest.get("kind")
        if not api_version or not kind:
            raise NotFound("manifest needs apiVersion and kind")
        meta = manifest.get("metadata") or {}
        name = meta.get("name")
        namespace = meta.get("namespace")
        if not name:
            raise NotFound("manifest needs metadata.name")
        ref = await self.resolve(kind, api_version)
        if ref.namespaced and not namespace:
            raise NotFound(
                f"{kind} is namespaced but the manifest has no metadata.namespace",
                "Add one, or pass the namespace through the calling tool.",
            )
        return await self.request(
            "PATCH",
            ref.path(namespace, name),
            params={"fieldManager": FIELD_MANAGER, "force": force},
            content=yaml.safe_dump(manifest).encode("utf-8"),
            content_type="application/apply-patch+yaml",
        )

    async def patch(
        self,
        kind: str,
        name: str,
        patch: dict[str, Any],
        namespace: str | None = None,
        strategy: str = "merge",
    ) -> dict[str, Any]:
        ref = await self.resolve(kind)
        content_types = {
            "merge": "application/merge-patch+json",
            "strategic": "application/strategic-merge-patch+json",
            "json": "application/json-patch+json",
        }
        return await self.request(
            "PATCH",
            ref.path(namespace, name),
            content=json.dumps(patch).encode("utf-8"),
            content_type=content_types.get(strategy, content_types["merge"]),
        )

    async def delete(
        self,
        kind: str,
        name: str,
        namespace: str | None = None,
        propagation: str = "Foreground",
    ) -> Any:
        ref = await self.resolve(kind)
        return await self.request(
            "DELETE",
            ref.path(namespace, name),
            body={"propagationPolicy": propagation},
        )

    # --- pods --------------------------------------------------------------------------
    async def logs(
        self,
        namespace: str,
        pod: str,
        container: str | None = None,
        tail_lines: int = 200,
        since_seconds: int | None = None,
        previous: bool = False,
    ) -> str:
        return await self.request(
            "GET",
            f"/api/v1/namespaces/{namespace}/pods/{pod}/log",
            params={
                "container": container,
                "tailLines": tail_lines,
                "sinceSeconds": since_seconds,
                "previous": previous,
                "timestamps": True,
            },
            raw_text=True,
        )

    async def events(
        self, namespace: str | None = None, field_selector: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        path = f"/api/v1/namespaces/{namespace}/events" if namespace else "/api/v1/events"
        data = await self.request(
            "GET", path, params={"fieldSelector": field_selector, "limit": limit}
        )
        return data.get("items", [])

    # --- cluster -----------------------------------------------------------------------
    async def version(self) -> dict[str, Any]:
        return await self.request("GET", "/version")

    async def node_metrics(self) -> list[dict[str, Any]]:
        try:
            data = await self.request("GET", "/apis/metrics.k8s.io/v1beta1/nodes")
            return data.get("items", [])
        except UpstreamError:
            return []

    # --- service proxy -----------------------------------------------------------------
    async def service_proxy(
        self,
        namespace: str,
        service: str,
        port: int | str,
        path: str = "/",
        method: str = "GET",
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        scheme: str = "http",
        timeout: float | None = None,
    ) -> tuple[int, str]:
        """Call an in-cluster HTTP service through the API server. Returns (status, body).

        `scheme` is part of the proxy path, not a transport detail: 'https:argocd-server:443'
        makes the API server speak TLS to the backend and skip verification, which is what
        ArgoCD's self-signed serving certificate needs.
        """
        target = f"{scheme}:{service}:{port}" if scheme != "http" else f"{service}:{port}"
        if not path.startswith("/"):
            path = "/" + path
        proxy_path = f"/api/v1/namespaces/{namespace}/services/{target}/proxy{path}"
        client = await self.http()
        content = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        hdrs = dict(headers or {})
        if content is not None:
            hdrs.setdefault("Content-Type", "application/json")
        resp = await client.request(
            method.upper(), proxy_path, content=content, headers=hdrs or None, timeout=timeout
        )
        return resp.status_code, resp.text


def _cluster_wide_path(ref: ResourceRef) -> str:
    base = (
        f"/api/{ref.group_version}"
        if "/" not in ref.group_version
        else f"/apis/{ref.group_version}"
    )
    return f"{base}/{ref.plural}"


def _keys_for(ref: ResourceRef) -> list[str]:
    keys = [ref.kind.lower(), ref.plural.lower()]
    keys += [f"{ref.group_version.lower()}/{k}" for k in list(keys)]
    if "/" in ref.group_version:
        group = ref.group_version.split("/")[0]
        keys.append(f"{ref.plural.lower()}.{group.lower()}")
    return keys


def _qs(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _kube_error(resp: httpx.Response) -> UpstreamError:
    message = (resp.text or "").strip()
    detail: Any = None
    try:
        payload = resp.json()
        detail = payload
        if isinstance(payload, dict):
            message = payload.get("message") or message
    except (json.JSONDecodeError, ValueError):
        pass
    if len(message) > 400:
        message = message[:400] + "…"
    return UpstreamError(
        "Kubernetes API", resp.status_code, message, KUBE_HINTS.get(resp.status_code), detail
    )
