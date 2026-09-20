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

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastmcp import FastMCP

from ..errors import ConfigError, NotFound, SimplMcpError
from ..guard import get_guard
from ..helm.runner import runner_for
from ..kube.session import get_kube
from ..settings import Settings
from ..state import get_store
from ..toolkit import Toolkit

SERVICE_NAME = "bridge"
SERVICE_PORT = 8080
NORTHBOUND_BASE = "/bridge/v1"

# The publication path needs two Secrets in the agent's namespace. Neither ever holds a
# value that came through a tool argument or a tool result.
DATABASE_SECRET = "bridge-database"
DOME_SECRET = "bridge-dome"
PG_CLUSTER = "pg-cluster"
SAMPLE_SD_NAME = "governance-authority-ai-service-sample.json"

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
        "on its own schema. Increment 1's read path holds no state and needs none. "
        "bridge_database_ensure asks the platform's postgres-operator for the role and the "
        "database and hands the credentials to the agent's namespace as a Secret.",
        "if_absent": "The publication path cannot start. Search is unaffected.",
    },
    {
        "need": "A DOME token (publication path only)",
        "detail": "Writes to the sandbox carry a static bearer token (bridge.dome.auth.mode="
        "STATIC). Set DOME_DEV_TOKEN on this server and run bridge_dome_credentials_ensure; "
        "the value goes straight into a Secret.",
        "if_absent": "Registrations are accepted (202) but every delivery to DOME is refused.",
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
        publication: bool = False,
        database_secret: str = DATABASE_SECRET,
        dome_secret: str | None = None,
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
            agent_namespace=ns,
            image_tag=image_tag or settings.bridge_image_tag,
            image_registry=image_registry or settings.bridge_image_registry,
            image_repository=settings.bridge_image_repository,
            cache_enabled=cache_enabled,
            elk_metrics=elk_metrics,
            replicas=replicas,
            resource_preset=resource_preset,
            dome_base_url=dome_base_url or settings.dome_base_url,
            northbound_auth=northbound_auth,
            publication_secret=database_secret if publication else None,
            dome_secret=dome_secret,
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
        publication: bool = False,
        database_secret: str = DATABASE_SECRET,
        dome_secret: str | None = None,
        extra_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Install or upgrade the Bridge inside a Governance Authority agent.

        The agent's namespace must already hold a running authority — the Bridge is part of
        it, not a neighbour of it. `dry_run=True` renders without applying.

        `publication=True` switches the write path on: the pod gets its datasource from
        `database_secret` (made by bridge_database_ensure) and, when `dome_secret` is given
        (made by bridge_dome_credentials_ensure), the DOME token from there. Without
        `dome_secret` registrations are accepted but deliveries are refused by DOME.
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

        if not dry_run:
            names = {
                (x.get("metadata") or {}).get("name") or x.get("name")
                for x in await kube.list("Secret", ns, limit=500)
            }
            if publication and database_secret not in names:
                raise NotFound(
                    f"Secret '{database_secret}' not found in {ns}",
                    "Run bridge_database_ensure first: it asks the postgres-operator for the "
                    "Bridge's database and writes the credentials into that Secret.",
                )
            if dome_secret and dome_secret not in names:
                raise NotFound(
                    f"Secret '{dome_secret}' not found in {ns}",
                    "Run bridge_dome_credentials_ensure first (DOME_DEV_TOKEN must be set on "
                    "the server).",
                )
        values = _build_values(
            common_namespace=p.common_namespace,
            agent_namespace=ns,
            image_tag=image_tag or settings.bridge_image_tag,
            image_registry=image_registry or settings.bridge_image_registry,
            image_repository=settings.bridge_image_repository,
            cache_enabled=cache_enabled,
            elk_metrics=elk_metrics,
            replicas=replicas,
            resource_preset=resource_preset,
            dome_base_url=dome_base_url or settings.dome_base_url,
            publication_secret=database_secret if publication else None,
            dome_secret=dome_secret,
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
                f"{values['image']['repository']}:{values['image'].get('tag') or 'appVersion'}".lstrip(
                    "/"
                ),
                "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "dome_base_url": values["dome"]["baseUrl"],
                "publication": publication,
                "dome_secret": dome_secret,
            }
            get_store().save(p)
            get_guard().audit("bridge_deploy", f"{p.name}/{ns}", "deployed")
        return {
            "deployed": not dry_run,
            "namespace": ns,
            "release": SERVICE_NAME,
            "chart_path": str(path),
            "output": result.stdout[:3000],
            "publication": publication,
            "next": "bridge_status, then bridge_search — a search with degraded:false is the "
            "only proof that egress to DOME works."
            + (" Then bridge_publish_sample to prove the write path." if publication else ""),
        }

    @tk.read
    async def bridge_status(
        profile: str | None = None, namespace: str | None = None
    ) -> dict[str, Any]:
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
                if k.startswith(
                    ("bridge.dome", "bridge.cache", "bridge.search", "bridge.northbound")
                )
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

    @tk.write
    async def bridge_database_ensure(
        profile: str | None = None,
        namespace: str | None = None,
        secret_name: str = DATABASE_SECRET,
        wait_seconds: int = 180,
    ) -> dict[str, Any]:
        """Give the Bridge its PostgreSQL database, the way the platform gives every component one.

        Adds a role and a database named `<agent>_bridge` to the common components'
        postgres-operator cluster, waits for the operator to mint the credentials, and copies
        them into the agent's namespace as a Secret shaped for Quarkus
        (QUARKUS_DATASOURCE_JDBC_URL / _USERNAME / _PASSWORD). Idempotent. The password is
        read from one Secret and written into another; it is never returned or logged.
        """
        p = get_store().resolve(profile)
        ns = namespace or _authority_namespace(p)
        common = p.common_namespace
        kube = await get_kube(p.name)
        user = _database_user(ns)
        operator_secret = _operator_secret_name(user)

        cluster = await kube.get("postgresql", PG_CLUSTER, common)
        spec = cluster.get("spec") or {}
        patched = False
        if user not in (spec.get("users") or {}) or user not in (spec.get("databases") or {}):
            await kube.patch(
                "postgresql",
                PG_CLUSTER,
                {"spec": {"users": {user: ["createdb"]}, "databases": {user: user}}},
                common,
            )
            patched = True

        deadline = time.monotonic() + wait_seconds
        source: dict[str, Any] | None = None
        while True:
            try:
                source = await kube.get("Secret", operator_secret, common)
            except SimplMcpError:
                source = None
            data = (source or {}).get("data") or {}
            if data.get("username") and data.get("password"):
                break
            if time.monotonic() > deadline:
                raise NotFound(
                    f"the postgres-operator has not created {common}/{operator_secret} "
                    f"after {wait_seconds}s",
                    "Check the operator with k8s_logs on pg-operator and the cluster object "
                    f"with k8s_get(kind='postgresql', name='{PG_CLUSTER}', namespace='{common}').",
                )
            await asyncio.sleep(5)

        jdbc_url = f"jdbc:postgresql://{PG_CLUSTER}.{common}.svc.cluster.local:5432/{user}"
        manifest = _database_secret_manifest(
            secret_name, ns, jdbc_url, data["username"], data["password"]
        )
        await kube.apply(manifest)
        get_guard().audit("bridge_database_ensure", f"{p.name}/{ns}/{secret_name}", "ok")
        return {
            "namespace": ns,
            "secret": secret_name,
            "database": user,
            "role": user,
            "jdbc_url": jdbc_url,
            "operator_secret": f"{common}/{operator_secret}",
            "cluster_patched": patched,
            "note": "The password went from the operator's Secret into this one and nowhere "
            "else. Deploy with bridge_deploy(publication=True).",
        }

    @tk.write
    async def bridge_dome_credentials_ensure(
        profile: str | None = None,
        namespace: str | None = None,
        secret_name: str = DOME_SECRET,
    ) -> dict[str, Any]:
        """Write the DOME sandbox token into a Secret in the agent's namespace.

        The token is read from this server's DOME_DEV_TOKEN environment variable — set it
        as a deployment variable, not as a tool argument — and is never returned. The
        chart then exposes it to the pod as DOME_DEV_TOKEN, which is what
        `bridge.dome.auth.staticToken` reads.
        """
        token = settings.dome_dev_token
        if not token:
            raise ConfigError(
                "DOME_DEV_TOKEN is not set on this server",
                "Set it as an environment variable of the server (a Railway variable) and "
                "redeploy; it is written into a Secret and never surfaces in a tool result.",
            )
        p = get_store().resolve(profile)
        ns = namespace or _authority_namespace(p)
        kube = await get_kube(p.name)
        await kube.apply(_dome_secret_manifest(secret_name, ns, token))
        get_guard().audit("bridge_dome_credentials_ensure", f"{p.name}/{ns}/{secret_name}", "ok")
        return {
            "namespace": ns,
            "secret": secret_name,
            "key": "token",
            "bytes": len(token.encode("utf-8")),
            "note": f"Deploy with bridge_deploy(publication=True, dome_secret='{secret_name}').",
        }

    @tk.write
    async def bridge_publish_sample(
        asset_id: str | None = None,
        wait_seconds: int = 90,
        retire_after: bool = False,
        profile: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Prove the write path: register a sample AI-service self-description and watch it reach DOME.

        Registration answers 202 at once (BRG-D-06: registration is not delivery); the
        dispatcher claims the row a second later and delivers to the sandbox. This tool
        polls the operations ledger until the operation is finished or `wait_seconds` pass.
        `retire_after=True` registers a retirement afterwards so the sandbox is left as it
        was found.
        """
        p = get_store().resolve(profile)
        ns = namespace or (p.bridge or {}).get("namespace") or _authority_namespace(p)
        kube = await get_kube(p.name)
        sample = _sample_sd_path(settings)
        if sample is None:
            raise ConfigError(
                f"no {SAMPLE_SD_NAME} next to the vendored chart",
                "Vendor it with scripts/sync-bridge-chart.sh, or pass a self-description of "
                "your own once bridge_publish accepts one.",
            )
        sd = json.loads(sample.read_text(encoding="utf-8"))
        asset = asset_id or f"simpl-ovh-mcp-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        body = {"assetId": asset, "selfDescription": sd, "requestedBy": "simpl-ovh-mcp"}
        status, text = await kube.service_proxy(
            ns,
            SERVICE_NAME,
            SERVICE_PORT,
            f"{NORTHBOUND_BASE}/publications",
            method="POST",
            json_body=body,
            timeout=60,
        )
        registration = {"http_status": status, "body": _short_json(text)}
        if status != 202:
            return {
                "asset_id": asset,
                "registration": registration,
                "verdict": "Registration refused. 404 means the publication routes are off "
                "(deploy with publication=True); 503 means the database is unreachable.",
            }
        operations: list[Any] = []
        deadline = time.monotonic() + wait_seconds
        while True:
            _s2, t2 = await kube.service_proxy(
                ns,
                SERVICE_NAME,
                SERVICE_PORT,
                f"{NORTHBOUND_BASE}/operations?assetId={asset}",
                timeout=30,
            )
            try:
                parsed = json.loads(t2)
            except json.JSONDecodeError:
                parsed = []
            operations = (
                parsed
                if isinstance(parsed, list)
                else parsed.get("operations") or parsed.get("results") or [parsed]
            )
            done = [
                o
                for o in operations
                if isinstance(o, dict)
                and str(o.get("status", "")).upper()
                in (
                    "DELIVERED",
                    "SUCCEEDED",
                    "DONE",
                    "COMPLETED",
                    "FAILED",
                    "REJECTED",
                    "EXHAUSTED",
                )
            ]
            if done or time.monotonic() > deadline:
                break
            await asyncio.sleep(5)
        retirement = None
        if retire_after:
            s3, t3 = await kube.service_proxy(
                ns,
                SERVICE_NAME,
                SERVICE_PORT,
                f"{NORTHBOUND_BASE}/publications/{asset}",
                method="DELETE",
                timeout=60,
            )
            retirement = {"http_status": s3, "body": _short_json(t3)}
        statuses = [str(o.get("status")) for o in operations if isinstance(o, dict)]
        delivered = any(
            x.upper() in ("DELIVERED", "SUCCEEDED", "DONE", "COMPLETED") for x in statuses
        )
        get_guard().audit(
            "bridge_publish_sample",
            f"{p.name}/{ns}/{asset}",
            "delivered" if delivered else "pending",
        )
        return {
            "asset_id": asset,
            "registration": registration,
            "operations": operations[:10],
            "retirement": retirement,
            "verdict": "Delivered to DOME."
            if delivered
            else "Registered, not (yet) delivered — read the operations' status and lastError; "
            "a refused token shows there.",
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
def _sample_sd_path(settings: Settings) -> Path | None:
    """Where the sample self-description lives, in a checkout and in the image alike.

    The chart is vendored at <root>/vendor/charts/bridge and the samples beside it at
    <root>/vendor/bridge-samples; <root> is the repository locally and /app in the image,
    so derive it from the chart path rather than from this file's position, which differs
    between an editable install and a site-packages one.
    """
    candidates: list[Path] = []
    chart = settings.bridge_chart_path
    if chart:
        candidates.append(Path(chart).resolve().parents[1] / "bridge-samples" / SAMPLE_SD_NAME)
    candidates.append(
        Path(__file__).resolve().parents[3] / "vendor" / "bridge-samples" / SAMPLE_SD_NAME
    )
    candidates.append(Path("/app/vendor/bridge-samples") / SAMPLE_SD_NAME)
    for path in candidates:
        if path.exists():
            return path
    return None


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
    agent_namespace: str = "authority01",
    image_tag: str,
    image_registry: str,
    image_repository: str,
    cache_enabled: bool,
    elk_metrics: bool,
    replicas: int,
    resource_preset: str,
    dome_base_url: str,
    northbound_auth: bool = False,
    publication_secret: str | None = None,
    dome_secret: str | None = None,
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
        # The chart's default points at redis.<common>, which does not exist in a real
        # Simpl-Open deployment: the common components ship no Redis, and each agent gets its
        # own `redis-master` with a password in the `redis-secrets` Secret of its namespace.
        # Pointed at the wrong host the cache silently never hits — every search then costs a
        # round trip to the marketplace and `cacheHit` stays false.
        "redis": {
            "hosts": f"redis://redis-master.{agent_namespace}.svc.cluster.local:6379",
            "existingSecret": {"name": "redis-secrets", "passwordKey": "redis"},
        },
        "elk": {
            "elasticsearch": {"hosts": f"elasticsearch.{common_namespace}.svc.cluster.local:9200"},
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
    values = deep_merge(values, _publication_values(publication_secret, dome_secret))
    return deep_merge(values, extra_values)


def _publication_values(database_secret: str | None, dome_secret: str | None) -> dict[str, Any]:
    """The write path, as chart values. Only Secret NAMES appear here.

    The datasource arrives as environment (envFrom the database Secret), which sits above
    the mounted properties file in Quarkus' ordinal order; the switches go into
    extraProperties because the chart does not model them yet.
    """
    out: dict[str, Any] = {}
    if database_secret:
        out["extraEnvFrom"] = [{"secretRef": {"name": database_secret}}]
        out["config"] = {
            "extraProperties": "bridge.publication.enabled=true\n"
            "bridge.publication.dispatcherEnabled=true\n"
        }
    if dome_secret:
        out["openbao"] = {
            "enabled": True,
            "mode": "existingSecret",
            "existingSecret": {"name": dome_secret, "keys": {"DOME_DEV_TOKEN": "token"}},
        }
    return out


def _database_user(namespace: str) -> str:
    """The postgres-operator's naming: one role per agent component, agent prefix, underscores."""
    return f"{namespace.replace('-', '_')}_bridge"


def _operator_secret_name(user: str, cluster: str = PG_CLUSTER) -> str:
    """Where the Zalando operator puts the credentials it mints (underscores become dashes)."""
    return f"{user.replace('_', '-')}.{cluster}.credentials.postgresql.acid.zalan.do"


def _database_secret_manifest(
    name: str, namespace: str, jdbc_url: str, username_b64: str, password_b64: str
) -> dict[str, Any]:
    """A Secret that Quarkus reads through envFrom. Username and password are passed on
    exactly as the operator encoded them; only the URL is encoded here."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": SERVICE_NAME,
                "app.kubernetes.io/managed-by": "simpl-ovh-mcp",
            },
        },
        "type": "Opaque",
        "data": {
            "QUARKUS_DATASOURCE_JDBC_URL": base64.b64encode(jdbc_url.encode()).decode(),
            "QUARKUS_DATASOURCE_USERNAME": username_b64,
            "QUARKUS_DATASOURCE_PASSWORD": password_b64,
        },
    }


def _dome_secret_manifest(name: str, namespace: str, token: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": SERVICE_NAME,
                "app.kubernetes.io/managed-by": "simpl-ovh-mcp",
            },
        },
        "type": "Opaque",
        "data": {"token": base64.b64encode(token.encode("utf-8")).decode()},
    }


SECRET_SHAPED = ("password", "secret", "token", "clientsecret", "apikey", "privatekey")


def _looks_like_secret(values: dict[str, Any], path: str = "") -> list[str]:
    """The chart refuses to render with a literal credential in it; catch it earlier."""
    found: list[str] = []
    for key, value in values.items():
        here = f"{path}.{key}" if path else key
        if "existingSecret" in path.split("."):
            # Everything under existingSecret names something — the Secret, the key inside it,
            # the environment variable it becomes — and never holds a value, by the chart's
            # own contract. `passwordKey: redis` is a key name, not a password.
            continue
        if isinstance(value, dict):
            found += _looks_like_secret(value, here)
        elif (
            isinstance(value, str)
            and value
            and any(marker in key.lower() for marker in SECRET_SHAPED)
        ):
            found.append(f"{here} looks like a credential; use a Secret reference instead")
    return found


def _short_json(text: str, limit: int = 600) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return (text or "")[:limit]
