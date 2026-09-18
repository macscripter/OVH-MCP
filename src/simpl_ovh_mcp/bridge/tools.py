"""The DOME Bridge: deploy it into a Governance Authority agent, and prove it works.

The Bridge is a sub-chart of the Governance Authority (BRG-D-09): one Bridge per agent,
one agent-level technical identity towards DOME (BRG-D-03). Until it is merged into the
published authority chart it is installed as its own Helm release inside the agent's
namespace, which renders the same Deployment, Service and ConfigMap the sub-chart would.

Two things this module deliberately does not do:

* It never puts a credential in a values file. Southbound DOME credentials come from
  OpenBao (BRG-D-14) or from a Secret that already exists in the namespace; the chart
  refuses to render if it finds a literal secret.
* It does not report "the pod is running" as success. A Bridge is up when a search comes
  back with `degraded: false`, which is the only evidence that egress to DOME works.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastmcp import FastMCP

from ..errors import ConfigError, NotFound
from ..guard import get_guard
from ..helm.runner import runner_for
from ..kube.session import get_kube
from ..settings import Settings
from ..state import get_store
from ..toolkit import Toolkit

SERVICE_NAME = "bridge"
SERVICE_PORT = 8080
NORTHBOUND_BASE = "/bridge/v1"

# What the Bridge needs from the platform, from the deployment notes of 18 September 2026.
PLATFORM_NEEDS = [
    {
        "need": "Egress to DOME",
        "detail": "https://tmf.sbx.evidenceledger.eu for reads and writes, and the DOME "
        "verifier for tokens. In the Simpl-Open topology outbound traffic leaves the agent "
        "through the tier-2 proxy.",
        "if_absent": "Every DOME call times out and the search answers degraded — visible, "
        "by design, and not damaging.",
        "open_question": "Transparent egress, or explicit proxy settings through "
        "java.extraOpts? The code carries no proxy configuration of its own.",
    },
    {
        "need": "A PostgreSQL database (publication path only)",
        "detail": "The outbox: two tables created by Flyway at start-up, so the user needs DDL "
        "on its own schema. Increment 1's read path holds no state and needs none.",
        "if_absent": "The publication path cannot start. Search is unaffected.",
    },
    {
        "need": "Redis, or a decision not to cache",
        "detail": "Read cache (BRG-D-12), cache-aside with a TTL. Point it at the common "
        "components' Redis.",
        "if_absent": "Set cache.enabled=false and accept the extra load on DOME.",
    },
    {
        "need": "The OpenBao path and a role",
        "detail": "secret/data/simpl/<agent>/bridge/dome, role simpl-bridge, read-only.",
        "if_absent": "Increment 1 reads anonymously, so nothing breaks yet.",
    },
    {
        "need": "The image, and a registry to pull it from",
        "detail": "image.registry + image.repository, pinned by digest rather than tag. OVH's "
        "Managed Private Registry is one option; list yours with ovh_registry_list.",
        "if_absent": "The pod cannot start.",
    },
    {
        "need": "Keycloak clients (northbound authentication)",
        "detail": "A bearer-only client for the Bridge and a confidential client for the Query "
        "Mapper Adapter, with a bridge-caller role.",
        "if_absent": "The Bridge relies on network isolation inside the agent, which is what "
        "it does today.",
    },
    {
        "need": "Elasticsearch, if the audit trail is wanted",
        "detail": "The audit sink is off by default but the Micrometer registry is ON and "
        "pushes to elasticsearch.<common>. A cluster without that host gets a metrics "
        "exporter failing quietly in the background.",
        "if_absent": "Switch elk.metrics.enabled off, or point it at the agent's ELK.",
    },
]


def register(mcp: FastMCP, settings: Settings) -> Toolkit:
    tk = Toolkit(mcp, settings, "bridge")

    @tk.read
    async def bridge_chart_info() -> dict[str, Any]:
        """Where the Bridge chart is, what version it is, and what it needs from the platform.

        Read this before a first deployment: the `platform_needs` list is the set of
        questions a platform team has to answer, each with what happens if the answer is no.
        """
        path = Path(settings.bridge_chart_path or "")
        chart: dict[str, Any] = {}
        if (path / "Chart.yaml").exists():
            chart = yaml.safe_load((path / "Chart.yaml").read_text(encoding="utf-8")) or {}
        return {
            "chart_path": str(path),
            "available": bool(chart),
            "name": chart.get("name"),
            "version": chart.get("version"),
            "app_version": chart.get("appVersion"),
            "description": (chart.get("description") or "").strip(),
            "decisions": (chart.get("annotations") or {}).get("simpl.europa.eu/decisions"),
            "image_default": {
                "registry": settings.bridge_image_registry or "(cluster default)",
                "repository": settings.bridge_image_repository,
                "tag": settings.bridge_image_tag or "(chart appVersion)",
            },
            "platform_needs": PLATFORM_NEEDS,
            "deployment_shape": "One Helm release named 'bridge' inside the Governance "
            "Authority's namespace. It renders no ServiceAccount, Ingress, NetworkPolicy or "
            "Secret: those belong to the agent that hosts it.",
            "hint": None
            if chart
            else "No chart at that path. Point BRIDGE_CHART_PATH at charts/bridge from the "
            "Bridge repository, or run scripts/sync-bridge-chart.sh to vendor a copy.",
        }

    @tk.read
    async def bridge_render_values(
        profile: str | None = None,
        namespace: str | None = None,
        image_tag: str | None = None,
        image_registry: str | None = None,
        cache_enabled: bool = True,
        elk_metrics: bool = False,
        replicas: int = 2,
        resource_preset: str = "small",
        dome_base_url: str | None = None,
        northbound_auth: bool = False,
        extra_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the Bridge's values for this deployment, and check them for credentials.

        Redis and Elasticsearch hosts are pointed at the profile's common-components
        namespace, which is the part that is wrong in the chart's defaults for any namespace
        other than common01.

        `elk_metrics` defaults to off here, unlike the chart: the Micrometer registry is on
        by default in the chart and will fail quietly in the background on a cluster with no
        Elasticsearch at that address.
        """
        p = get_store().resolve(profile)
        ns = namespace or _authority_namespace(p)
        values = _build_values(
            common_namespace=p.common_namespace,
            image_tag=image_tag or settings.bridge_image_tag,
            image_registry=image_registry or settings.bridge_image_registry,
            image_repository=settings.bridge_image_repository,
            cache_enabled=cache_enabled,
            elk_metrics=elk_metrics,
            replicas=replicas,
            resource_preset=resource_preset,
            dome_base_url=dome_base_url or settings.dome_base_url,
            northbound_auth=northbound_auth,
            extra_values=extra_values,
        )
        leaks = _looks_like_secret(values)
        return {
            "namespace": ns,
            "values": values,
            "yaml": yaml.safe_dump(values, sort_keys=False),
            "secret_check": "clean" if not leaks else leaks,
            "note": "Nothing here is a credential. If you need one, put it in a Secret and "
            "reference it under openbao.existingSecret.",
        }

    @tk.write
    async def bridge_deploy(
        profile: str | None = None,
        namespace: str | None = None,
        image_tag: str | None = None,
        image_registry: str | None = None,
        cache_enabled: bool = True,
        elk_metrics: bool = False,
        replicas: int = 2,
        resource_preset: str = "small",
        dome_base_url: str | None = None,
        chart_path: str | None = None,
        dry_run: bool = False,
        extra_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Install or upgrade the Bridge inside a Governance Authority agent.

        The agent's namespace must already hold a running authority — the Bridge is part of
        it, not a neighbour of it. `dry_run=True` renders without applying.
        """
        p = get_store().resolve(profile)
        ns = namespace or _authority_namespace(p)
        path = Path(chart_path or settings.bridge_chart_path or "")
        if not (path / "Chart.yaml").exists():
            raise ConfigError(
                f"no Helm chart at {path}",
                "Set BRIDGE_CHART_PATH, pass chart_path=…, or vendor the chart with "
                "scripts/sync-bridge-chart.sh.",
            )
        kube = await get_kube(p.name)
        pods = await kube.list("Pod", ns, limit=5)
        if not pods and not dry_run:
            raise NotFound(
                f"namespace '{ns}' has no pods",
                "The Bridge is deployed inside a Governance Authority agent. Install the agent "
                "first with simpl_install_agent.",
            )

        values = _build_values(
            common_namespace=p.common_namespace,
            image_tag=image_tag or settings.bridge_image_tag,
            image_registry=image_registry or settings.bridge_image_registry,
            image_repository=settings.bridge_image_repository,
            cache_enabled=cache_enabled,
            elk_metrics=elk_metrics,
            replicas=replicas,
            resource_preset=resource_preset,
            dome_base_url=dome_base_url or settings.dome_base_url,
            extra_values=extra_values,
        )
        helm = await runner_for(p.name)
        result = await helm.upgrade_install(
            SERVICE_NAME, str(path), ns, values=values, wait=False, dry_run=dry_run
        )
        result.raise_for_status()
        if not dry_run:
            p.bridge = {
                "namespace": ns,
                "release": SERVICE_NAME,
                "image": f"{values['image'].get('registry') or ''}/"
                f"{values['image']['repository']}:{values['image'].get('tag') or 'appVersion'}".lstrip("/"),
                "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "dome_base_url": values["dome"]["baseUrl"],
            }
            get_store().save(p)
            get_guard().audit("bridge_deploy", f"{p.name}/{ns}", "deployed")
        return {
            "deployed": not dry_run,
            "namespace": ns,
            "release": SERVICE_NAME,
            "chart_path": str(path),
            "output": result.stdout[:3000],
            "next": "bridge_status, then bridge_search — a search with degraded:false is the "
            "only proof that egress to DOME works.",
        }

    @tk.read
    async def bridge_status(profile: str | None = None, namespace: str | None = None) -> dict[str, Any]:
        """Is the Bridge running, ready, and configured the way you think it is?

        Reads the Deployment, the pods, the health endpoints and the rendered configuration.
        The start-up log line about the token provider is worth checking on a real
        environment: `STATIC mode (dev only …)` there means something is wrong.
        """
        p = get_store().resolve(profile)
        ns = namespace or (p.bridge or {}).get("namespace") or _authority_namespace(p)
        kube = await get_kube(p.name)
        out: dict[str, Any] = {"namespace": ns}

        try:
            deployment = await kube.get("Deployment", SERVICE_NAME, ns)
            status = deployment.get("status", {})
            containers = (deployment.get("spec", {}).get("template", {}).get("spec", {}) or {}).get(
                "containers", []
            )
            out["deployment"] = {
                "ready": f"{status.get('readyReplicas', 0)}/{status.get('replicas', 0)}",
                "image": containers[0].get("image") if containers else None,
                "conditions": [
                    {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
                    for c in status.get("conditions", [])
                ],
            }
        except Exception as exc:  # noqa: BLE001
            out["deployment"] = {"error": str(exc)}
            out["verdict"] = "The Bridge is not deployed in this namespace."
            return out

        pods = await kube.list("Pod", ns, "app.kubernetes.io/name=bridge", limit=10)
        out["pods"] = [
            {
                "name": pod["metadata"]["name"],
                "phase": (pod.get("status") or {}).get("phase"),
                "restarts": sum(
                    c.get("restartCount", 0)
                    for c in ((pod.get("status") or {}).get("containerStatuses") or [])
                ),
            }
            for pod in pods
        ]

        health: dict[str, Any] = {}
        for name, path in (("live", "/q/health/live"), ("ready", "/q/health/ready")):
            try:
                code, body = await kube.service_proxy(ns, SERVICE_NAME, SERVICE_PORT, path)
                health[name] = {"status": code, "body": _short_json(body)}
            except Exception as exc:  # noqa: BLE001
                health[name] = {"error": str(exc)}
        out["health"] = health

        try:
            cm = await kube.get("ConfigMap", SERVICE_NAME, ns)
            properties = (cm.get("data") or {}).get("application.properties", "")
            out["configuration"] = {
                k: v
                for k, v in (
                    line.split("=", 1)
                    for line in properties.splitlines()
                    if "=" in line and not line.startswith("#")
                )
                if k.startswith(("bridge.dome", "bridge.cache", "bridge.search", "bridge.northbound"))
            }
        except Exception:  # noqa: BLE001
            out["configuration"] = "no ConfigMap found"

        ready_ok = (health.get("ready") or {}).get("status") == 200
        out["verdict"] = (
            "Running and ready. Prove the DOME path with bridge_search."
            if ready_ok
            else "Deployed but not ready — read the logs with k8s_logs(namespace, pod)."
        )
        return out

    @tk.read
    async def bridge_search(
        keyword: str | None = None,
        limit: int = 5,
        offset: int = 0,
        lifecycle_status: list[str] | None = None,
        category: list[str] | None = None,
        profile: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Run a federated DOME search through the deployed Bridge — the real smoke test.

        The northbound contract never fails because DOME failed: a timeout, an open circuit
        breaker or an unreachable marketplace all come back as HTTP 200 with `degraded:
        true` and a reason. So the thing to read in the answer is `degraded`, not the status
        code.

        Measured sandbox behaviour: latency tracks the OFFSET, not the page size. Offset 0
        answers in about 1.8 s, offset 127 in about 11 s. A degraded response at a large
        offset is the query's fault, not the platform's.
        """
        p = get_store().resolve(profile)
        ns = namespace or (p.bridge or {}).get("namespace") or _authority_namespace(p)
        kube = await get_kube(p.name)
        criteria: dict[str, Any] = {}
        if keyword:
            criteria["keyword"] = keyword
        if lifecycle_status:
            criteria["lifecycleStatus"] = lifecycle_status
        if category:
            criteria["category"] = category
        body = {"criteria": criteria, "paging": {"limit": limit, "offset": offset}}

        started = time.monotonic()
        status, text = await kube.service_proxy(
            ns,
            SERVICE_NAME,
            SERVICE_PORT,
            f"{NORTHBOUND_BASE}/search",
            method="POST",
            json_body=body,
            timeout=60,
        )
        elapsed = round(time.monotonic() - started, 2)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"raw": text[:2000]}
        results = payload.get("results") or []
        degraded = payload.get("degraded")
        return {
            "http_status": status,
            "elapsed_seconds": elapsed,
            "request": body,
            "origin": payload.get("origin"),
            "degraded": degraded,
            "degraded_reason": payload.get("degradedReason"),
            "has_more": payload.get("hasMore"),
            "result_count": len(results),
            "results": [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "lifecycleStatus": r.get("lifecycleStatus"),
                    "isBundle": r.get("isBundle"),
                }
                for r in results[:10]
            ],
            "verdict": "Egress to DOME works."
            if degraded is False
            else f"Degraded: {payload.get('degradedReason')}. Internal search is unaffected by "
            "design; check egress from the agent.",
        }

    @tk.read
    async def bridge_dome_probe(
        offsets: list[int] | None = None, base_url: str | None = None, limit: int = 5
    ) -> dict[str, Any]:
        """Probe the DOME sandbox directly from this server, to tell DOME's problems from ours.

        Calls TMF620 `GET /productOffering` at several offsets and reports status and
        latency for each. When the Bridge reports degraded and this probe is healthy, the
        problem is egress from the cluster; when both are slow, it is DOME.
        """
        url_base = (base_url or settings.dome_base_url).rstrip("/")
        probes = []
        async with httpx.AsyncClient(timeout=40.0) as client:
            for offset in offsets or [0, 20]:
                url = f"{url_base}/v4/productOffering"
                started = time.monotonic()
                try:
                    resp = await client.get(url, params={"limit": limit, "offset": offset})
                    elapsed = round(time.monotonic() - started, 2)
                    count = len(resp.json()) if resp.status_code == 200 else None
                    probes.append(
                        {
                            "offset": offset,
                            "status": resp.status_code,
                            "seconds": elapsed,
                            "returned": count,
                            "x_total_count": resp.headers.get("x-total-count"),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    probes.append(
                        {
                            "offset": offset,
                            "error": str(exc),
                            "seconds": round(time.monotonic() - started, 2),
                        }
                    )
        healthy = [p for p in probes if p.get("status") == 200]
        return {
            "base_url": url_base,
            "api": "TMF620 v4 / productOffering",
            "probes": probes,
            "verdict": "DOME answers."
            if healthy
            else "DOME did not answer this server either — the marketplace or the network, not "
            "the deployment.",
            "note": "Reads on the sandbox are anonymous; latency grows with the offset, not "
            "with the page size.",
        }

    @tk.destructive
    async def bridge_uninstall(
        profile: str | None = None, namespace: str | None = None, confirm: str | None = None
    ) -> dict[str, Any]:
        """Remove the Bridge release from an agent. The agent itself is untouched."""
        g = get_guard()
        g.require_destructive("bridge_uninstall")
        p = get_store().resolve(profile)
        ns = namespace or (p.bridge or {}).get("namespace") or _authority_namespace(p)
        target = f"{ns}/{SERVICE_NAME}"
        if not confirm:
            impact = (
                f"Uninstalls the Bridge from {ns}. The Governance Authority keeps running; only "
                "the DOME integration goes away."
            )
            return {
                "confirmation_required": True,
                "impact": impact,
                "confirm_token": g.issue_token(target, impact),
            }
        g.check_token("bridge_uninstall", target, confirm)
        helm = await runner_for(p.name)
        result = await helm.uninstall(SERVICE_NAME, ns)
        p.bridge = {}
        get_store().save(p)
        g.audit("bridge_uninstall", target, "ok" if result.ok else "failed")
        result.raise_for_status()
        return {"uninstalled": SERVICE_NAME, "namespace": ns}

    return tk


# ---------------------------------------------------------------------------- helpers --
def _authority_namespace(profile: Any) -> str:
    for ns, kind in (profile.agents or {}).items():
        if kind == "authority":
            return ns
    raise NotFound(
        "this profile has no Governance Authority agent",
        "The Bridge runs inside one. Install it with simpl_install_agent(agent_type="
        "'authority'), or pass namespace=… explicitly.",
    )


def _build_values(
    *,
    common_namespace: str,
    image_tag: str,
    image_registry: str,
    image_repository: str,
    cache_enabled: bool,
    elk_metrics: bool,
    replicas: int,
    resource_preset: str,
    dome_base_url: str,
    northbound_auth: bool = False,
    extra_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ..simpl.values import deep_merge

    values: dict[str, Any] = {
        "enabled": True,
        "replicaCount": replicas,
        "resourcePreset": resource_preset,
        "image": {
            "registry": image_registry,
            "repository": image_repository,
            "tag": image_tag,
            "pullPolicy": "IfNotPresent",
        },
        "dome": {"baseUrl": dome_base_url, "api": "TMF620", "apiVersion": "v4"},
        "cache": {"enabled": cache_enabled},
        "redis": {"hosts": f"redis://redis.{common_namespace}.svc.cluster.local:6379"},
        "elk": {
            "elasticsearch": {
                "hosts": f"elasticsearch.{common_namespace}.svc.cluster.local:9200"
            },
            "metrics": {
                "enabled": elk_metrics,
                "host": f"http://elasticsearch.{common_namespace}.svc.cluster.local:9200",
            },
        },
        "northbound": {"basePath": NORTHBOUND_BASE, "restDslEnabled": True},
        "logging": {"json": True, "level": "INFO"},
    }
    if northbound_auth:
        values.setdefault("config", {})["extraProperties"] = (
            "# northbound authentication is the agent's Keycloak; set the client here\n"
        )
    return deep_merge(values, extra_values)


SECRET_SHAPED = ("password", "secret", "token", "clientsecret", "apikey", "privatekey")


def _looks_like_secret(values: dict[str, Any], path: str = "") -> list[str]:
    """The chart refuses to render with a literal credential in it; catch it earlier."""
    found: list[str] = []
    for key, value in values.items():
        here = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            found += _looks_like_secret(value, here)
        elif isinstance(value, str) and value and any(
            marker in key.lower() for marker in SECRET_SHAPED
        ):
            found.append(f"{here} looks like a credential; use a Secret reference instead")
    return found


def _short_json(text: str, limit: int = 600) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return (text or "")[:limit]
