"""Turning a kubeconfig into something httpx can use.

The server never shells out to kubectl, so a kubeconfig has to be decomposed into the
pieces an HTTP client needs: the API server URL, the CA to trust, and a client certificate
or bearer token to present. OVH issues certificate-based admin kubeconfigs; k3d and most
other clusters do the same, so certificates are the primary path and tokens are supported
for clusters that use them.

Exec-plugin kubeconfigs (`user.exec`, as AWS EKS and GKE produce) are refused with an
explanation rather than half-supported: running an arbitrary binary to mint credentials is
not something this server should do.
"""

from __future__ import annotations

import base64
import hashlib
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..errors import ConfigError
from ..settings import get_settings
from ..state import Profile, get_store


@dataclass
class KubeTarget:
    """Everything needed to talk to one cluster's API server."""

    server: str
    ca_path: str | None
    client_cert_path: str | None
    client_key_path: str | None
    token: str | None
    insecure: bool
    context: str | None
    source: str

    @property
    def cert(self) -> tuple[str, str] | None:
        if self.client_cert_path and self.client_key_path:
            return (self.client_cert_path, self.client_key_path)
        return None

    def ssl_context(self) -> ssl.SSLContext | bool:
        """Build the TLS context httpx should use.

        An explicit context rather than httpx's `verify=` / `cert=` pair: as of httpx 0.28
        the `cert` argument no longer reaches the handshake, so a certificate-based
        kubeconfig — which is every OVH and k3d one — authenticates as nobody and the API
        server answers 401. Building the context here makes the client certificate load
        where it is actually used.
        """
        if self.insecure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context(cafile=self.ca_path)
        if self.cert:
            context.load_cert_chain(*self.cert)
        return context

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def describe(self) -> dict[str, object]:
        return {
            "server": self.server,
            "context": self.context,
            "auth": "client-certificate" if self.cert else ("token" if self.token else "anonymous"),
            "tls_verification": "disabled" if self.insecure else "enabled",
            "source": self.source,
        }


def load_kubeconfig(profile: Profile | None = None, context: str | None = None) -> KubeTarget:
    """Resolve, in order: the profile's stored kubeconfig, then the server's own.

    A profile that has been through ovh_kubeconfig_fetch carries its own credentials, so
    several clusters can be operated from one server instance. Falling back to the
    environment's kubeconfig is what makes the same server useful against a local k3d
    cluster with no OVH account involved.
    """
    settings = get_settings()
    raw: str | None = None
    source = ""

    if profile is not None:
        path = Path(profile.platform.get("kubeconfig_path") or get_store().kubeconfig_path(profile.name))
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            source = f"profile '{profile.name}' ({path})"

    if raw is None and settings.kubeconfig_inline:
        try:
            raw = base64.b64decode(settings.kubeconfig_inline).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConfigError(
                "SIMPL_MCP_KUBECONFIG_B64 is not valid base64 UTF-8", str(exc)
            ) from exc
        source = "SIMPL_MCP_KUBECONFIG_B64"

    if raw is None and settings.kubeconfig_path:
        path = Path(settings.kubeconfig_path).expanduser()
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            source = str(path)

    if raw is None:
        raise ConfigError(
            "no kubeconfig available",
            "Fetch one with ovh_kubeconfig_fetch(profile=…) for an OVH cluster, or set "
            "SIMPL_MCP_KUBECONFIG (path) / SIMPL_MCP_KUBECONFIG_B64 (base64 of the file).",
        )

    return parse_kubeconfig(raw, context=context or (profile.platform.get("kube_context") if profile else None), source=source)


def parse_kubeconfig(raw: str, context: str | None = None, source: str = "inline") -> KubeTarget:
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError("kubeconfig is not valid YAML", str(exc)) from exc
    if not isinstance(doc, dict) or "clusters" not in doc:
        raise ConfigError("kubeconfig has no clusters block", "Is this really a kubeconfig file?")

    ctx_name = context or doc.get("current-context")
    contexts = {c["name"]: c.get("context", {}) for c in doc.get("contexts", [])}
    if ctx_name and ctx_name in contexts:
        ctx = contexts[ctx_name]
    elif contexts:
        ctx_name, ctx = next(iter(contexts.items()))
    else:
        ctx_name, ctx = None, {}

    clusters = {c["name"]: c.get("cluster", {}) for c in doc.get("clusters", [])}
    users = {u["name"]: u.get("user", {}) for u in doc.get("users", [])}

    cluster = clusters.get(ctx.get("cluster")) or (next(iter(clusters.values())) if clusters else {})
    user = users.get(ctx.get("user")) or (next(iter(users.values())) if users else {})

    server = cluster.get("server")
    if not server:
        raise ConfigError("kubeconfig context has no server URL")

    if "exec" in user:
        raise ConfigError(
            "this kubeconfig authenticates through an exec plugin, which is not supported",
            "Create a ServiceAccount token instead and put it in the kubeconfig as "
            "`user.token`, or use an OVH kubeconfig, which is certificate-based.",
        )

    workdir = Path(get_settings().state_dir) / ".certs"
    ca_path = _materialise(cluster.get("certificate-authority-data"), cluster.get("certificate-authority"), workdir, "ca")
    cert_path = _materialise(user.get("client-certificate-data"), user.get("client-certificate"), workdir, "cert")
    key_path = _materialise(user.get("client-key-data"), user.get("client-key"), workdir, "key")

    return KubeTarget(
        server=server.rstrip("/"),
        ca_path=ca_path,
        client_cert_path=cert_path,
        client_key_path=key_path,
        token=user.get("token"),
        insecure=bool(cluster.get("insecure-skip-tls-verify")) or get_settings().kube_insecure,
        context=ctx_name,
        source=source,
    )


def _materialise(b64_data: str | None, path_value: str | None, workdir: Path, kind: str) -> str | None:
    """Write embedded PEM data to a private file; pass through a path that already exists."""
    if path_value:
        p = Path(path_value).expanduser()
        return str(p) if p.exists() else None
    if not b64_data:
        return None
    try:
        data = base64.b64decode(b64_data)
    except ValueError as exc:
        raise ConfigError(f"kubeconfig {kind} data is not valid base64", str(exc)) from exc
    workdir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    target = workdir / f"{kind}-{digest}.pem"
    if not target.exists():
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    return str(target)
