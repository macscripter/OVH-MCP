"""The server: assembly, instructions, resources, prompts, transport.

Everything else in this package is a layer; this is where the layers become one MCP
server, with the tool surface trimmed to what the deployment's mode allows.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP

from . import __version__
from .guard import get_guard
from .settings import Settings, get_settings
from .toolkit import Toolkit

INSTRUCTIONS = """
Deploy and operate Simpl-Open — and the DOME Bridge — on OVHcloud Managed Kubernetes.

Four layers, lowest first:

* `ovh_*`  — the account, Public Cloud projects, Managed Kubernetes clusters and node
  pools, DNS zones, registries. Anything OVH exposes is reachable through ovh_api_get.
* `k8s_*`  — the cluster itself: objects, logs, events, storage, and a proxy that reaches
  services inside the cluster without an ingress or a port-forward.
* `helm_*` / `argocd_*` — the platform charts, and the App-of-Apps that installs everything
  Simpl-Open publishes.
* `simpl_*` / `bridge_*` — the deployment itself: plan, bootstrap, install, verify,
  diagnose, initialise, tear down.

Working order for a new deployment: `simpl_plan` → `simpl_profile_create` →
`ovh_kube_create` → `ovh_kubeconfig_fetch` → `simpl_bootstrap_platform` →
`ovh_dns_ensure_wildcard` → `simpl_setup_issuers` → `simpl_setup_rwx_storage` →
`simpl_install_common` → `simpl_install_agent` → `simpl_status` / `simpl_diagnose`.

Four things about this platform that are not obvious and cost days when missed:

1. Components address each other by **ingress hostname**, including inside the cluster, so
   `domainSuffix` must resolve from a pod as well as from a browser.
2. The OpenBao initialiser claims a **ReadWriteMany** volume. OVH's default storage class
   is ReadWriteOnce, and the resulting failures appear in unrelated components.
3. The charts reference two cluster issuers **by name** — dev-prod-http and dev-selfsigned
   — and fail quietly without them.
4. A Governance Authority needs its **tier-1 trust chain initialised** before tier2-gateway,
   tier2-proxy and users-roles will ever be healthy.

`simpl_diagnose` checks all four and names the tool that fixes each one.

Destructive tools are two-phase: call once to see the impact and get a token, then again
with the token. Secrets are never returned — not kubeconfigs, not Secret values, not the
ArgoCD password. Long operations return immediately and are watched with `simpl_status`,
`simpl_wait` or `k8s_pod_health`, because an MCP call that blocks for fifteen minutes gets
cut off by the client with the work still running.
""".strip()


def build_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or get_settings()
    auth = _build_auth(settings)
    mcp = FastMCP(
        name="simpl-ovh-mcp",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=auth,
    )

    toolkits: list[Toolkit] = []
    if "meta" in settings.tool_groups:
        toolkits.append(_register_meta(mcp, settings, toolkits))
    if "ovh" in settings.tool_groups:
        from .ovh import tools as ovh_tools

        toolkits.append(ovh_tools.register(mcp, settings))
    if "k8s" in settings.tool_groups:
        from .kube import tools as kube_tools

        toolkits.append(kube_tools.register(mcp, settings))
    if "helm" in settings.tool_groups:
        from .helm import tools as helm_tools

        toolkits.append(helm_tools.register(mcp, settings))
    if "argocd" in settings.tool_groups:
        from .argo import tools as argo_tools

        toolkits.append(argo_tools.register(mcp, settings))
    if "simpl" in settings.tool_groups:
        from .simpl import tools as simpl_tools

        toolkits.append(simpl_tools.register(mcp, settings))
    if "bridge" in settings.tool_groups:
        from .bridge import tools as bridge_tools

        toolkits.append(bridge_tools.register(mcp, settings))

    _register_resources(mcp, settings)
    _register_prompts(mcp)
    _register_health_route(mcp, settings, toolkits)
    return mcp


# ------------------------------------------------------------------------------ auth ---
def _build_auth(settings: Settings) -> Any:
    """Bearer-token auth when a token is configured; otherwise none.

    A public HTTP deployment with no token is refused rather than quietly served: this
    server holds credentials for a cloud account and a Kubernetes cluster.
    """
    if settings.transport == "stdio":
        return None
    if not settings.bearer_token:
        if settings.mode == "readonly":
            return None
        raise SystemExit(
            "Refusing to start an HTTP server that can change things without authentication.\n"
            "Set SIMPL_MCP_BEARER_TOKEN, or run with SIMPL_MCP_MODE=readonly."
        )
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    return StaticTokenVerifier(
        tokens={
            settings.bearer_token: {
                "client_id": "simpl-ovh-mcp-operator",
                "scopes": ["operate"],
            }
        }
    )


# ------------------------------------------------------------------------------ meta ---
def _register_meta(mcp: FastMCP, settings: Settings, toolkits: list[Toolkit]) -> Toolkit:
    tk = Toolkit(mcp, settings, "meta")

    @tk.read
    async def mcp_info() -> dict[str, Any]:
        """What this server is configured to do, and what it can reach.

        Call it first in a new conversation: it reports the permission mode, which tool
        groups are loaded, whether OVH credentials and a kubeconfig are present, and which
        deployment profile is active.
        """
        from .state import get_store

        store = get_store()
        kube_source = "none"
        if settings.kubeconfig_path:
            kube_source = f"env: {settings.kubeconfig_path}"
        elif settings.kubeconfig_inline:
            kube_source = "env: inline (base64)"
        active = store.active()
        if active:
            try:
                profile = store.get(active)
                if profile.platform.get("kubeconfig_path"):
                    kube_source = f"profile '{active}'"
            except Exception:  # noqa: BLE001, S110 — a damaged profile must not break mcp_info
                pass

        import shutil

        return {
            "server": "simpl-ovh-mcp",
            "version": __version__,
            "mode": settings.mode,
            "writes_allowed": settings.writes_allowed,
            "destructive_allowed": settings.destructive_allowed,
            "tool_groups": list(settings.tool_groups),
            # `toolkits` includes this one by the time a tool is called, so no extra sum.
            "tools_registered": sum(len(t.registered) for t in toolkits),
            "tools_hidden_by_mode": sorted(name for t in toolkits for name in t.skipped),
            "ovh": {
                "auth_mode": settings.ovh_auth_mode,
                "endpoint": settings.ovh_endpoint,
                "default_project": settings.ovh_cloud_project,
            },
            "kubernetes": {"credentials": kube_source},
            "helm": shutil.which(settings.helm_bin) or "not found",
            "state_dir": str(settings.state_dir),
            "state_dir_writable": settings.state_dir_writable,
            "profiles": store.list(),
            "active_profile": active,
            "dome_base_url": settings.dome_base_url,
        }

    @tk.read
    async def mcp_audit(limit: int = 30) -> dict[str, Any]:
        """The server's own record of every write it attempted, oldest to newest.

        Stored on the state volume, so it survives restarts and outlives the conversation
        that produced it. Credential-shaped values are redacted before they are written.
        """
        records = get_guard().read_audit(limit)
        return {"count": len(records), "entries": records}

    return tk


# ------------------------------------------------------------------------- resources ---
def _register_resources(mcp: FastMCP, settings: Settings) -> None:
    @mcp.resource("simpl://catalog", mime_type="application/json")
    def catalog_resource() -> str:
        """The Simpl-Open chart catalogue: charts, projects, verified versions."""
        from .simpl.catalog import CHARTS, PLATFORM, REQUIREMENTS

        return json.dumps(
            {
                "charts": {k: c.to_dict() for k, c in CHARTS.items()},
                "platform": {
                    k: {"chart": p.chart, "version": p.version, "namespace": p.namespace}
                    for k, p in PLATFORM.items()
                },
                "requirements": REQUIREMENTS,
            },
            indent=2,
        )

    @mcp.resource("simpl://traps", mime_type="application/json")
    def traps_resource() -> str:
        """Known failure modes: symptom, cause, remedy."""
        from .simpl.catalog import TRAPS

        return json.dumps([t.to_dict() for t in TRAPS], indent=2)

    @mcp.resource("simpl://profiles/{name}", mime_type="application/json")
    def profile_resource(name: str) -> str:
        """One deployment profile as stored on the volume."""
        from .state import get_store

        return json.dumps(get_store().get(name).to_dict(), indent=2, default=str)

    @mcp.resource("simpl://runbook", mime_type="text/markdown")
    def runbook_resource() -> str:
        """The deployment order, with the reason for each step."""
        return RUNBOOK


# --------------------------------------------------------------------------- prompts ---
def _register_prompts(mcp: FastMCP) -> None:
    @mcp.prompt
    def deploy_simpl_open(
        agents: str = "authority", domain: str = "", sizing: str = "demo"
    ) -> str:
        """Walk through a full deployment on OVH, in order, checking as you go."""
        return (
            f"Deploy Simpl-Open on OVHcloud with these agents: {agents}. "
            f"Domain: {domain or '(ask me)'} . Sizing: {sizing}.\n\n"
            "Work in this order and stop at anything unexpected:\n"
            "1. simpl_plan to agree the shape, then show me the plan before spending money.\n"
            "2. simpl_profile_create, ovh_kube_create, and poll ovh_kube_get until READY.\n"
            "3. ovh_kubeconfig_fetch, then simpl_bootstrap_platform.\n"
            "4. Wait for the ingress load balancer address, then ovh_dns_ensure_wildcard.\n"
            "5. simpl_setup_issuers and simpl_setup_rwx_storage.\n"
            "6. simpl_verify_dns with deep=True — both directions must resolve.\n"
            "7. simpl_install_common, then wait until the namespace is healthy.\n"
            "8. simpl_install_agent for each agent.\n"
            "9. simpl_status and simpl_diagnose; fix what they name.\n\n"
            "Report cost-bearing steps before taking them."
        )

    @mcp.prompt
    def diagnose_deployment(symptom: str = "") -> str:
        """Diagnose a deployment that is not behaving, from symptom to cause."""
        return (
            f"Something is wrong with the Simpl-Open deployment: {symptom or 'unspecified'}.\n\n"
            "Run simpl_diagnose first, then follow its findings. Read the evidence rather than "
            "assuming: check DNS resolution from inside the cluster, whether the ReadWriteMany "
            "claim is bound, whether both cluster issuers are Ready, and whether the authority's "
            "trust chain was initialised. Tell me the cause before proposing a fix."
        )

    @mcp.prompt
    def deploy_bridge(namespace: str = "") -> str:
        """Deploy the DOME Bridge into a Governance Authority and prove it works."""
        return (
            f"Deploy the DOME Bridge into the Governance Authority agent"
            f"{' in namespace ' + namespace if namespace else ''}.\n\n"
            "Start with bridge_chart_info and tell me which of the platform needs are not yet "
            "answered. Then bridge_render_values so I can read the configuration, then "
            "bridge_deploy. Prove it with bridge_status and bridge_search — a search with "
            "degraded:false is the only evidence that egress to DOME works. If it is degraded, "
            "run bridge_dome_probe to tell DOME's problem from ours."
        )


# ---------------------------------------------------------------------------- health ---
def _register_health_route(mcp: FastMCP, settings: Settings, toolkits: list[Toolkit]) -> None:
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "server": "simpl-ovh-mcp",
                "version": __version__,
                "mode": settings.mode,
                "tools": sum(len(t.registered) for t in toolkits),
            }
        )


RUNBOOK = """
# Deploying Simpl-Open on OVHcloud

## 0. Decide the shape
`simpl_plan(agents=["authority"], sizing="demo")`. It returns the node count and flavour,
the namespaces, the DNS records and the install order. Nothing is created.

## 1. The cluster
`simpl_profile_create` then `ovh_kube_create`. Poll `ovh_kube_get` until READY (3-8 min),
then `ovh_kubeconfig_fetch`, which stores the credentials on the server's volume.

## 2. The platform
`simpl_bootstrap_platform` installs ingress-nginx, cert-manager, ArgoCD and an NFS
provisioner. The OVH load balancer appears a few minutes later; `simpl_status` shows its
address as soon as it does.

## 3. DNS — the step everything else depends on
`ovh_dns_ensure_wildcard(zone, sub_domain, target_ip=<load balancer>)`. Components address
each other by hostname from inside the cluster, so this is not only for browsers. Confirm
with `simpl_verify_dns(deep=True)`, which resolves the name from inside a pod.

## 4. Issuers and storage
`simpl_setup_issuers` creates dev-prod-http and dev-selfsigned — the charts reference those
names. `simpl_setup_rwx_storage` installs the RWX class and pre-creates the OpenBao
initialiser's claim against it.

## 5. The common components
`simpl_install_common`. Ten to twenty minutes. Watch with `simpl_status`; do not install an
agent until this namespace is healthy.

## 6. The agents
`simpl_install_agent(agent_type="authority")`. For an authority, leave
`automatic_initialization=True` or run `simpl_initialize_authority` afterwards; without it
tier2-gateway, tier2-proxy and users-roles never go healthy.

## 7. The Bridge
`bridge_chart_info`, `bridge_render_values`, `bridge_deploy`, then `bridge_search`. A search
with `degraded: false` is the proof that egress to DOME works. "The pod is running" is not.

## 8. When something is wrong
`simpl_diagnose`. It checks the cluster, RWX storage, both issuers, DNS in both directions,
ArgoCD's applications and every namespace's workloads, matching crash-loop logs against
known failure signatures, and names the tool that fixes what it finds.
""".strip()
