"""ArgoCD API client that never needs ArgoCD to be exposed.

Every call goes through the Kubernetes API server's service proxy, so ArgoCD can stay a
ClusterIP service with no ingress, no port-forward and no public hostname. The session
token is obtained once from the `argocd-initial-admin-secret` (or from an explicitly
configured token), kept in memory, and never returned by a tool.

If the initial admin secret has been deleted — which ArgoCD's own documentation recommends
after the first login — set ARGOCD_AUTH_TOKEN to a project token instead.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from ..errors import ConfigError, NotFound, UpstreamError
from ..kube.client import KubeClient

DEFAULT_NAMESPACE = os.environ.get("ARGOCD_NAMESPACE", "argocd")
DEFAULT_SERVICE = os.environ.get("ARGOCD_SERVICE", "argocd-server")
DEFAULT_PORT = int(os.environ["ARGOCD_PORT"]) if os.environ.get("ARGOCD_PORT") else None
DEFAULT_SCHEME = os.environ.get("ARGOCD_SCHEME") or None
TOKEN_TTL = 12 * 3600

# How argocd-server is reached varies with how it was installed: with `server.insecure`
# set — which is the default of the chart this server installs — the pod speaks plain HTTP
# on both service ports, and proxying to the https port fails with a bare EOF. Rather than
# assume, probe: ask for /api/version over each candidate and keep the one that answers.
ENDPOINT_CANDIDATES = (("http", 80), ("https", 443))


class ArgoClient:
    def __init__(
        self,
        kube: KubeClient,
        namespace: str = DEFAULT_NAMESPACE,
        service: str = DEFAULT_SERVICE,
        port: int | None = DEFAULT_PORT,
        scheme: str | None = DEFAULT_SCHEME,
    ) -> None:
        self.kube = kube
        self.namespace = namespace
        self.service = service
        self.port = port or 80
        self.scheme = scheme or "http"
        self._endpoint_checked = bool(port and scheme)
        self._token: tuple[str, float] | None = None

    # --- endpoint -----------------------------------------------------------------------
    async def _ensure_endpoint(self) -> None:
        """Find the scheme and port that actually answer, once per client."""
        if self._endpoint_checked:
            return
        errors = []
        for scheme, port in ENDPOINT_CANDIDATES:
            try:
                status, text = await self.kube.service_proxy(
                    self.namespace, self.service, port, "/api/version", scheme=scheme
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{scheme}:{port} → {exc}")
                continue
            if status < 400 and "Version" in text:
                self.scheme, self.port = scheme, port
                self._endpoint_checked = True
                return
            errors.append(f"{scheme}:{port} → HTTP {status}")
        raise ConfigError(
            f"argocd-server in namespace '{self.namespace}' did not answer on any known port",
            "Tried " + "; ".join(errors) + ". Set ARGOCD_NAMESPACE, ARGOCD_SERVICE, "
            "ARGOCD_PORT and ARGOCD_SCHEME if the install is non-standard, or check that "
            "ArgoCD is running with k8s_pod_health(namespace='argocd').",
        )

    # --- auth ---------------------------------------------------------------------------
    async def token(self) -> str:
        env_token = os.environ.get("ARGOCD_AUTH_TOKEN")
        if env_token:
            return env_token
        if self._token and self._token[1] > time.time():
            return self._token[0]

        await self._ensure_endpoint()
        password = await self._admin_password()
        status, body = await self.kube.service_proxy(
            self.namespace,
            self.service,
            self.port,
            "/api/v1/session",
            method="POST",
            json_body={"username": "admin", "password": password},
            scheme=self.scheme,
        )
        if status >= 400:
            raise UpstreamError(
                "ArgoCD",
                status,
                _short(body),
                "The admin password from argocd-initial-admin-secret was refused. If it has been "
                "changed, set ARGOCD_AUTH_TOKEN to a project token instead.",
            )
        token = json.loads(body).get("token")
        if not token:
            raise UpstreamError("ArgoCD", status, "session response carried no token")
        self._token = (token, time.time() + TOKEN_TTL)
        return token

    async def _admin_password(self) -> str:
        try:
            secret = await self.kube.get("Secret", "argocd-initial-admin-secret", self.namespace)
        except UpstreamError as exc:
            raise ConfigError(
                "ArgoCD's initial admin secret is not in the cluster",
                "Either ArgoCD is not installed (run simpl_bootstrap_platform), or the secret was "
                "deleted after first login — in that case set ARGOCD_AUTH_TOKEN.",
            ) from exc
        raw = (secret.get("data") or {}).get("password")
        if not raw:
            raise ConfigError("argocd-initial-admin-secret has no 'password' key")
        return base64.b64decode(raw).decode("utf-8")

    # --- requests -----------------------------------------------------------------------
    async def call(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        await self._ensure_endpoint()
        if params:
            from urllib.parse import urlencode

            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                path = f"{path}?{urlencode(clean, doseq=True)}"
        status, text = await self.kube.service_proxy(
            self.namespace,
            self.service,
            self.port,
            path,
            method=method,
            json_body=body,
            headers={"Authorization": f"Bearer {await self.token()}"},
            scheme=self.scheme,
        )
        if status == 404 and "/applications/" in path:
            raise NotFound(
                f"ArgoCD has no application at {path.split('/applications/')[-1].split('?')[0]}",
                "List them with argocd_apps.",
            )
        if status >= 400:
            raise UpstreamError("ArgoCD", status, _short(text))
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # --- convenience --------------------------------------------------------------------
    async def version(self) -> dict[str, Any]:
        return await self.call("GET", "/api/version")

    async def applications(self) -> list[dict[str, Any]]:
        data = await self.call("GET", "/api/v1/applications")
        return (data or {}).get("items") or []

    async def application(self, name: str, refresh: str | None = None) -> dict[str, Any]:
        return await self.call("GET", f"/api/v1/applications/{name}", params={"refresh": refresh})

    async def resource_tree(self, name: str) -> dict[str, Any]:
        return await self.call("GET", f"/api/v1/applications/{name}/resource-tree")

    async def managed_resources(self, name: str) -> dict[str, Any]:
        return await self.call("GET", f"/api/v1/applications/{name}/managed-resources")

    async def sync(
        self,
        name: str,
        prune: bool = False,
        dry_run: bool = False,
        resources: list[dict[str, str]] | None = None,
        strategy_force: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "prune": prune, "dryRun": dry_run}
        if resources:
            body["resources"] = resources
        if strategy_force:
            body["strategy"] = {"apply": {"force": True}}
        return await self.call("POST", f"/api/v1/applications/{name}/sync", body)

    async def terminate_operation(self, name: str) -> Any:
        return await self.call("DELETE", f"/api/v1/applications/{name}/operation")

    async def delete(self, name: str, cascade: bool = True) -> Any:
        return await self.call(
            "DELETE", f"/api/v1/applications/{name}", params={"cascade": str(cascade).lower()}
        )


def _short(text: str, limit: int = 400) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


_clients: dict[str, ArgoClient] = {}


async def get_argo(profile_name: str | None = None) -> ArgoClient:
    from ..kube.session import get_kube

    kube = await get_kube(profile_name)
    key = profile_name or "__env__"
    client = _clients.get(key)
    if client is None or client.kube is not kube:
        client = ArgoClient(kube)
        _clients[key] = client
    return client
