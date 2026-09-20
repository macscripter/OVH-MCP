"""Kubernetes tools: inspect, apply, restart, read logs, reach in-cluster services.

Deliberately general. The Simpl-specific reasoning lives one layer up; these tools are the
ones you fall back to when something happens that no workflow anticipated — which, on a
platform of sixty pods, is most interesting days.
"""

from __future__ import annotations

from typing import Any

import yaml
from fastmcp import FastMCP

from ..errors import NotFound, SimplMcpError
from ..guard import get_guard
from ..settings import Settings
from ..toolkit import Toolkit
from .session import get_kube

# Kinds whose full objects are enormous and whose interesting parts are few.
SUMMARY_LIMIT = 60


def register(mcp: FastMCP, settings: Settings) -> Toolkit:
    tk = Toolkit(mcp, settings, "k8s")

    @tk.read
    async def k8s_cluster_info(profile: str | None = None) -> dict[str, Any]:
        """Cluster version, node capacity, storage classes and the ingress load balancer.

        Read the `storage` block carefully on a fresh OVH cluster: the default class is
        Cinder, which is ReadWriteOnce only, and Simpl-Open's OpenBao initialiser claims a
        ReadWriteMany volume. `rwx_capable` says whether anything in the cluster can answer
        that claim.
        """
        kube = await get_kube(profile)
        version = await kube.version()
        nodes = await kube.list("Node", limit=100)
        classes = await kube.list("StorageClass", limit=50)
        namespaces = [n["metadata"]["name"] for n in await kube.list("Namespace", limit=200)]

        node_rows = []
        total_cpu = total_mem = 0.0
        for n in nodes:
            status = n.get("status", {})
            cap = status.get("allocatable", {})
            cpu = _cpu_cores(cap.get("cpu", "0"))
            mem = _mem_gib(cap.get("memory", "0"))
            total_cpu += cpu
            total_mem += mem
            conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
            node_rows.append(
                {
                    "name": n["metadata"]["name"],
                    "ready": conditions.get("Ready") == "True",
                    "allocatable_cpu": round(cpu, 2),
                    "allocatable_memory_gib": round(mem, 1),
                    "kubelet": (status.get("nodeInfo") or {}).get("kubeletVersion"),
                    "pool": (n["metadata"].get("labels") or {}).get("nodepool"),
                    "pressure": [k for k, v in conditions.items() if k != "Ready" and v == "True"],
                }
            )

        class_rows = []
        for sc in classes:
            ann = sc["metadata"].get("annotations", {}) or {}
            provisioner = sc.get("provisioner", "")
            class_rows.append(
                {
                    "name": sc["metadata"]["name"],
                    "provisioner": provisioner,
                    "default": ann.get("storageclass.kubernetes.io/is-default-class") == "true",
                    "rwx_capable": _rwx_capable(provisioner),
                    "reclaim_policy": sc.get("reclaimPolicy"),
                    "volume_binding_mode": sc.get("volumeBindingMode"),
                }
            )

        ingress_ip = await _ingress_address(kube)
        return {
            "server": kube.target.describe(),
            "version": f"{version.get('major')}.{version.get('minor')}".replace("+", ""),
            "git_version": version.get("gitVersion"),
            "nodes": node_rows,
            "capacity": {
                "node_count": len(node_rows),
                "allocatable_cpu": round(total_cpu, 1),
                "allocatable_memory_gib": round(total_mem, 1),
            },
            "storage": {
                "classes": class_rows,
                "rwx_capable": any(c["rwx_capable"] for c in class_rows),
                "default_class": next((c["name"] for c in class_rows if c["default"]), None),
            },
            "ingress": ingress_ip,
            "namespaces": namespaces,
        }

    @tk.read
    async def k8s_list(
        kind: str,
        namespace: str | None = None,
        label_selector: str | None = None,
        limit: int = SUMMARY_LIMIT,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """List objects of any kind, including CRDs, as compact summaries.

        `kind` accepts 'Pod', 'pods', 'Application', 'applications.argoproj.io'. Omit
        `namespace` for cluster-scoped kinds or to list across all namespaces.
        """
        kube = await get_kube(profile)
        items = await kube.list(kind, namespace, label_selector, limit=limit)
        return {
            "kind": kind,
            "namespace": namespace or "(all)",
            "count": len(items),
            "items": [_summarise(i) for i in items],
        }

    @tk.read
    async def k8s_get(
        kind: str,
        name: str,
        namespace: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one object in full, minus managedFields — and minus Secret values.

        A Secret comes back with its key names and byte counts and nothing else. That is a
        deliberate hole in this tool: the value of a credential has no business in a chat
        transcript, and everything you legitimately need to know (does it exist, does it
        have the key the chart expects) is in the key list.
        """
        kube = await get_kube(profile)
        obj = await kube.get(kind, name, namespace)
        return _redact_object(obj)

    @tk.read
    async def k8s_pod_health(
        namespace: str, only_problems: bool = True, profile: str | None = None
    ) -> dict[str, Any]:
        """Pod-by-pod health for a namespace, with the reason each unhealthy pod gives.

        This is the first call for "what is broken in common01". Restart counts and
        waiting reasons (CrashLoopBackOff, ImagePullBackOff, CreateContainerConfigError)
        are the ones that matter; the last one usually means a Secret or ConfigMap the pod
        mounts does not exist yet.
        """
        kube = await get_kube(profile)
        pods = await kube.list("Pod", namespace, limit=300)
        rows = []
        counts = {"Running": 0, "Succeeded": 0, "Pending": 0, "Failed": 0, "Unknown": 0}
        for pod in pods:
            status = pod.get("status", {})
            phase = status.get("phase", "Unknown")
            counts[phase] = counts.get(phase, 0) + 1
            containers = status.get("containerStatuses") or []
            ready = sum(1 for c in containers if c.get("ready"))
            restarts = sum(c.get("restartCount", 0) for c in containers)
            reasons = _container_problems(containers + (status.get("initContainerStatuses") or []))
            healthy = phase == "Succeeded" or (
                phase == "Running" and ready == len(containers) and not reasons
            )
            if only_problems and healthy:
                continue
            rows.append(
                {
                    "pod": pod["metadata"]["name"],
                    "phase": phase,
                    "ready": f"{ready}/{len(containers)}",
                    "restarts": restarts,
                    "reasons": reasons,
                    "node": (pod.get("spec") or {}).get("nodeName"),
                    "message": status.get("message"),
                }
            )
        return {
            "namespace": namespace,
            "totals": {k: v for k, v in counts.items() if v},
            "pod_count": len(pods),
            "problems": rows,
            # An empty namespace has no problems and no health either. Reporting it as
            # healthy makes "wait until healthy" return immediately on a namespace whose
            # workloads have not been created yet, which reads as success.
            "healthy": bool(pods) and not rows,
            "empty": not pods,
        }

    @tk.read
    async def k8s_logs(
        namespace: str,
        pod: str | None = None,
        label_selector: str | None = None,
        container: str | None = None,
        tail_lines: int = 120,
        previous: bool = False,
        contains: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Container logs, by pod name or by label selector.

        `previous=True` reads the log of the *last* container, which is the only way to see
        why a CrashLoopBackOff pod died. `contains` filters lines server-side of the model's
        context, so a long log can be searched without pulling it all into the answer.
        """
        kube = await get_kube(profile)
        pods = (
            [pod]
            if pod
            else [
                p["metadata"]["name"]
                for p in await kube.list("Pod", namespace, label_selector, limit=5)
            ]
        )
        if not pods:
            raise NotFound(
                f"no pods matched in {namespace}",
                "Check the name with k8s_list(kind='Pod', namespace=…).",
            )
        out = {}
        for name in pods[:5]:
            try:
                text = await kube.logs(
                    namespace, name, container, tail_lines=tail_lines, previous=previous
                )
            except SimplMcpError as exc:
                out[name] = f"«{exc}»"
                continue
            if contains:
                lines = [ln for ln in text.splitlines() if contains.lower() in ln.lower()]
                text = "\n".join(lines[-tail_lines:]) or f"«no line contains '{contains}'»"
            out[name] = text
        return {"namespace": namespace, "logs": out}

    @tk.read
    async def k8s_events(
        namespace: str | None = None,
        warnings_only: bool = True,
        limit: int = 60,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Recent cluster events. Warnings first — they name the cause of most failures."""
        kube = await get_kube(profile)
        items = await kube.events(
            namespace, field_selector="type=Warning" if warnings_only else None, limit=limit
        )
        events = [
            {
                "time": e.get("lastTimestamp") or e.get("eventTime"),
                "type": e.get("type"),
                "reason": e.get("reason"),
                "object": f"{(e.get('involvedObject') or {}).get('kind')}/"
                f"{(e.get('involvedObject') or {}).get('name')}",
                "message": (e.get("message") or "")[:300],
                "count": e.get("count"),
            }
            for e in items
        ]
        events.sort(key=lambda e: e["time"] or "", reverse=True)
        return {"namespace": namespace or "(all)", "count": len(events), "events": events}

    @tk.read
    async def k8s_storage_status(
        namespace: str | None = None, profile: str | None = None
    ) -> dict[str, Any]:
        """Persistent volume claims and why the unbound ones are unbound.

        A Pending claim with `ReadWriteMany` in its access modes on a cluster whose only
        provisioner is Cinder is the single most common way a Simpl-Open install stalls:
        the OpenBao initialiser never runs, OpenBao stays sealed, and every component that
        waits for an injected secret crashes with a message about a missing file.
        """
        kube = await get_kube(profile)
        claims = await kube.list("PersistentVolumeClaim", namespace, limit=200)
        rows = []
        for c in claims:
            spec = c.get("spec", {})
            status = c.get("status", {})
            rows.append(
                {
                    "namespace": c["metadata"]["namespace"],
                    "name": c["metadata"]["name"],
                    "phase": status.get("phase"),
                    "access_modes": spec.get("accessModes"),
                    "storage_class": spec.get("storageClassName"),
                    "requested": (spec.get("resources") or {}).get("requests", {}).get("storage"),
                    "volume": spec.get("volumeName"),
                }
            )
        pending = [r for r in rows if r["phase"] != "Bound"]
        findings = []
        for r in pending:
            if "ReadWriteMany" in (r["access_modes"] or []):
                findings.append(
                    f"{r['namespace']}/{r['name']} wants ReadWriteMany from storage class "
                    f"'{r['storage_class'] or '(default)'}'. Run simpl_setup_rwx_storage."
                )
        return {"claims": rows, "pending": pending, "findings": findings}

    @tk.read
    async def k8s_secret_info(
        namespace: str, name: str, profile: str | None = None
    ) -> dict[str, Any]:
        """Whether a Secret exists and which keys it holds. Never its values."""
        kube = await get_kube(profile)
        secret = await kube.get("Secret", name, namespace)
        data = secret.get("data") or {}
        return {
            "namespace": namespace,
            "name": name,
            "type": secret.get("type"),
            "keys": sorted(data.keys()),
            "sizes_bytes": {k: len(v or "") * 3 // 4 for k, v in data.items()},
            "created": secret["metadata"].get("creationTimestamp"),
        }

    @tk.read
    async def k8s_service_get(
        namespace: str,
        service: str,
        port: int | str,
        path: str = "/",
        scheme: str = "http",
        profile: str | None = None,
    ) -> dict[str, Any]:
        """GET an in-cluster HTTP endpoint through the Kubernetes API server proxy.

        No ingress and no port-forward: the API server relays the request, so this reaches
        ClusterIP-only services such as the Governance Authority's internal APIs, the
        Bridge's health endpoints, or anything else inside the agent network.
        """
        kube = await get_kube(profile)
        status, body = await kube.service_proxy(namespace, service, port, path, scheme=scheme)
        return {"status": status, "body": _clip(body)}

    @tk.write
    async def k8s_apply(
        manifest: str, namespace: str | None = None, profile: str | None = None
    ) -> dict[str, Any]:
        """Server-side apply one or several YAML documents (`---` separated).

        Equivalent to `kubectl apply --server-side --force-conflicts`. Applying the same
        manifest twice converges rather than conflicting, so a workflow can re-run its own
        steps safely.
        """
        kube = await get_kube(profile)
        docs = [d for d in yaml.safe_load_all(manifest) if d]
        if not docs:
            raise NotFound("the manifest is empty")
        results = []
        for doc in docs:
            if namespace:
                doc.setdefault("metadata", {})["namespace"] = namespace
            applied = await kube.apply(doc)
            meta = applied.get("metadata", {})
            results.append(
                {
                    "kind": applied.get("kind"),
                    "name": meta.get("name"),
                    "namespace": meta.get("namespace"),
                    "resource_version": meta.get("resourceVersion"),
                }
            )
        get_guard().audit("k8s_apply", ",".join(r["name"] or "?" for r in results), "applied")
        return {"applied": results}

    @tk.write
    async def k8s_secret_generate_key(
        namespace: str,
        name: str,
        key: str,
        length: int = 32,
        overwrite: bool = False,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Put a freshly generated random value under `key` in a Secret, creating the Secret if needed.

        For the Simpl-Open case where a chart expects a per-agent key in a Secret it only
        populates at creation time (keycloak-secrets/<agent>). The value is generated on the
        server, written straight into the cluster, and never returned or logged. Existing
        keys are kept; an existing value under `key` is kept too unless `overwrite=True`.
        """
        import base64 as _b64
        import secrets as _secrets
        import string as _string

        kube = await get_kube(profile)
        try:
            existing = await kube.get("Secret", name, namespace)
        except SimplMcpError:
            existing = None
        data = dict((existing or {}).get("data") or {})
        if key in data and not overwrite:
            action = "kept"
        else:
            alphabet = _string.ascii_letters + _string.digits
            value = "".join(_secrets.choice(alphabet) for _ in range(max(8, length)))
            data[key] = _b64.b64encode(value.encode("utf-8")).decode("ascii")
            action = "generated"
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "type": (existing or {}).get("type") or "Opaque",
            "data": data,
        }
        applied = await kube.apply(manifest)
        get_guard().audit("k8s_secret_generate_key", f"{namespace}/{name}/{key}", action)
        return {
            "namespace": namespace,
            "name": name,
            "key": key,
            "action": action,
            "created": existing is None,
            "keys": sorted(data.keys()),
            "resource_version": (applied.get("metadata") or {}).get("resourceVersion"),
            "note": "The value was generated on the server and is not returned.",
        }

    @tk.write
    async def k8s_namespace_create(name: str, profile: str | None = None) -> dict[str, Any]:
        """Create a namespace if it does not exist.

        Simpl-Open forbids '-' in agent namespace names: the charts build hostnames from
        them and a hyphen breaks the resulting FQDN. Names are checked here.
        """
        if "-" in name:
            raise NotFound(
                f"'{name}' contains a hyphen",
                "Simpl-Open agent names cannot contain '-'. Use e.g. 'authority01'.",
            )
        kube = await get_kube(profile)
        await kube.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}})
        return {"namespace": name, "created": True}

    @tk.write
    async def k8s_rollout_restart(
        kind: str, name: str, namespace: str, profile: str | None = None
    ) -> dict[str, Any]:
        """Restart a Deployment, StatefulSet or DaemonSet by touching its pod template."""
        import time as _time

        kube = await get_kube(profile)
        stamp = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        await kube.patch(
            kind,
            name,
            {
                "spec": {
                    "template": {"metadata": {"annotations": {"simpl-ovh-mcp/restartedAt": stamp}}}
                }
            },
            namespace,
            strategy="strategic",
        )
        get_guard().audit("k8s_rollout_restart", f"{namespace}/{kind}/{name}", "restarted")
        return {"restarted": f"{kind}/{name}", "namespace": namespace, "at": stamp}

    @tk.write
    async def k8s_scale(
        kind: str, name: str, namespace: str, replicas: int, profile: str | None = None
    ) -> dict[str, Any]:
        """Set the replica count of a Deployment or StatefulSet.

        Scaling something ArgoCD manages is a temporary measure: the next sync restores
        what the chart declares, which is usually what you want after an experiment.
        """
        kube = await get_kube(profile)
        await kube.patch(kind, name, {"spec": {"replicas": replicas}}, namespace)
        get_guard().audit("k8s_scale", f"{namespace}/{kind}/{name}", f"replicas={replicas}")
        return {"scaled": f"{kind}/{name}", "namespace": namespace, "replicas": replicas}

    @tk.write
    async def k8s_service_call(
        namespace: str,
        service: str,
        port: int | str,
        path: str,
        method: str = "POST",
        body: dict[str, Any] | None = None,
        scheme: str = "http",
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Call an in-cluster HTTP endpoint with a method other than GET.

        The write-side twin of k8s_service_get. Used by the authority initialisation flow
        and available for any component API that has no dedicated tool.
        """
        kube = await get_kube(profile)
        status, text = await kube.service_proxy(
            namespace, service, port, path, method=method, json_body=body, scheme=scheme
        )
        get_guard().audit("k8s_service_call", f"{namespace}/{service}{path}", str(status))
        return {"status": status, "body": _clip(text)}

    @tk.destructive
    async def k8s_delete(
        kind: str,
        name: str,
        namespace: str | None = None,
        confirm: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Delete a Kubernetes object. Two-phase, like every destructive tool.

        Deleting a namespace deletes everything in it, including PersistentVolumeClaims and
        therefore the data on dynamically provisioned volumes.
        """
        g = get_guard()
        g.require_destructive("k8s_delete")
        target = f"{namespace or '-'}/{kind}/{name}"
        kube = await get_kube(profile)
        if not confirm:
            impact = f"Deletes {kind}/{name}" + (f" in namespace {namespace}" if namespace else "")
            if kind.lower() in ("namespace", "namespaces"):
                pods = await kube.list("Pod", name, limit=300)
                pvcs = await kube.list("PersistentVolumeClaim", name, limit=100)
                impact += f", with {len(pods)} pods and {len(pvcs)} volume claims. Data is lost."
            return {
                "confirmation_required": True,
                "impact": impact,
                "confirm_token": g.issue_token(target, impact),
            }
        g.check_token("k8s_delete", target, confirm)
        await kube.delete(kind, name, namespace)
        g.audit("k8s_delete", target, "deleted")
        return {"deleted": target}

    return tk


# ---------------------------------------------------------------------------- helpers --
def _summarise(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata", {})
    out: dict[str, Any] = {"name": meta.get("name")}
    if meta.get("namespace"):
        out["namespace"] = meta["namespace"]
    status = obj.get("status") or {}
    kind = obj.get("kind", "")
    if kind == "Pod":
        containers = status.get("containerStatuses") or []
        out["phase"] = status.get("phase")
        out["ready"] = f"{sum(1 for c in containers if c.get('ready'))}/{len(containers)}"
        out["restarts"] = sum(c.get("restartCount", 0) for c in containers)
    elif kind in ("Deployment", "StatefulSet"):
        out["ready"] = f"{status.get('readyReplicas', 0)}/{status.get('replicas', 0)}"
    elif kind == "Application":  # ArgoCD
        out["sync"] = (status.get("sync") or {}).get("status")
        out["health"] = (status.get("health") or {}).get("status")
    elif kind == "Certificate":
        conds = {c["type"]: c["status"] for c in status.get("conditions", [])}
        out["ready"] = conds.get("Ready")
    elif kind in ("PersistentVolumeClaim",):
        out["phase"] = status.get("phase")
    elif status.get("phase"):
        out["phase"] = status["phase"]
    out["age"] = meta.get("creationTimestamp")
    return out


def _redact_object(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata", {})
    meta.pop("managedFields", None)
    if obj.get("kind") == "Secret":
        data = obj.pop("data", {}) or {}
        obj.pop("stringData", None)
        obj["data_keys"] = sorted(data.keys())
        obj["note"] = "Secret values are never returned by this server."
    return obj


# States that are not problems: a container that is starting, and an init container that
# has finished its job. Reporting those as faults buries the real one — on a healthy
# Simpl-Open agent almost every pod has a Completed `copy-vault-env` init container.
BENIGN_WAITING = ("ContainerCreating", "PodInitializing")


def _container_problems(containers: list[dict[str, Any]]) -> list[str]:
    problems = []
    for c in containers:
        state = c.get("state") or {}
        waiting = state.get("waiting") or {}
        terminated = state.get("terminated") or {}
        if waiting.get("reason") and waiting["reason"] not in BENIGN_WAITING:
            problems.append(f"{c.get('name')}: {waiting['reason']}")
        elif terminated.get("reason") and terminated.get("exitCode", 0) != 0:
            problems.append(
                f"{c.get('name')}: {terminated['reason']} (exit {terminated.get('exitCode')})"
            )
    return problems


def _clip(text: str, limit: int = 8000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n… «{len(text) - limit} more bytes»"


def _cpu_cores(value: str) -> float:
    value = str(value)
    if value.endswith("m"):
        return int(value[:-1]) / 1000
    try:
        return float(value)
    except ValueError:
        return 0.0


def _mem_gib(value: str) -> float:
    value = str(value)
    units = {
        "Ki": 1 / 1024 / 1024,
        "Mi": 1 / 1024,
        "Gi": 1.0,
        "Ti": 1024.0,
        "K": 1e-6,
        "M": 1e-3,
        "G": 1.0,
    }
    for suffix, factor in units.items():
        if value.endswith(suffix):
            try:
                return float(value[: -len(suffix)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(value) / (1024**3)
    except ValueError:
        return 0.0


# Provisioners known to serve ReadWriteMany. Anything else is reported as not RWX-capable,
# which is the safe direction to be wrong in: the check exists to catch a stalled install.
RWX_PROVISIONERS = (
    "nfs",
    "cluster.local/nfs",
    "csi-driver-nfs",
    "nfs.csi.k8s.io",
    "cephfs",
    "manila",
    "efs",
    "azurefile",
    "longhorn",
    "glusterfs",
    "quobyte",
)


def _rwx_capable(provisioner: str) -> bool:
    p = (provisioner or "").lower()
    return any(marker in p for marker in RWX_PROVISIONERS)


async def _ingress_address(kube: Any) -> dict[str, Any]:
    """Find the ingress controller's external address, whichever namespace it lives in."""
    services = await kube.list("Service", limit=200)
    for svc in services:
        if svc.get("spec", {}).get("type") != "LoadBalancer":
            continue
        name = svc["metadata"]["name"]
        if "ingress" not in name and "nginx" not in name:
            continue
        ingress = ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        addresses = [i.get("ip") or i.get("hostname") for i in ingress if i]
        return {
            "service": f"{svc['metadata']['namespace']}/{name}",
            "addresses": addresses,
            "pending": not addresses,
        }
    return {"service": None, "addresses": [], "pending": True}
