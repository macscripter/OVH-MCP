"""One Kubernetes client per profile, kept alive between tool calls.

Connections are pooled because a diagnosis makes dozens of calls, and discovery is cached
because it costs a request per API group. The cache is invalidated when the profile's
kubeconfig file changes on disk, which is what happens after ovh_kubeconfig_fetch.
"""

from __future__ import annotations

from pathlib import Path

from ..state import Profile, get_store
from .client import KubeClient
from .kubeconfig import load_kubeconfig

_clients: dict[str, tuple[float, KubeClient]] = {}


def _stamp(profile: Profile | None) -> float:
    if profile is None:
        return 0.0
    raw = profile.platform.get("kubeconfig_path")
    if not raw:
        return 0.0
    path = Path(raw)
    return path.stat().st_mtime if path.exists() else 0.0


async def get_kube(profile_name: str | None = None) -> KubeClient:
    profile: Profile | None
    try:
        profile = get_store().resolve(profile_name)
    except Exception:
        # No profile at all is legitimate: the server may be pointed at a cluster purely
        # through SIMPL_MCP_KUBECONFIG, which is how it operates a local k3d install.
        if profile_name:
            raise
        profile = None

    key = profile.name if profile else "__env__"
    stamp = _stamp(profile)
    cached = _clients.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    if cached:
        await cached[1].aclose()

    client = KubeClient(load_kubeconfig(profile))
    _clients[key] = (stamp, client)
    return client


async def close_all() -> None:
    for _, client in _clients.values():
        await client.aclose()
    _clients.clear()
