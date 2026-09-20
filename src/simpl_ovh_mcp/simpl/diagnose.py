"""Diagnosis: from what is visible to what is actually wrong.

Simpl-Open's failures rarely announce themselves where they happen. A crash-looping
notification pod means no ReadWriteMany volume. A front end that loads and then fails every
call means a hostname that does not resolve inside the cluster. Three components that never
go healthy mean nobody ran the authority initialisation.

Each check below looks for evidence and, when it finds it, names the cause and the tool
that fixes it. Findings are ordered by severity, and the absence of findings is reported as
such rather than left as silence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..errors import SimplMcpError
from ..kube.client import KubeClient
from ..state import Profile
from .catalog import ISSUERS, TRAPS
from .platform import resolve_public

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Log fragments that identify a known cause, mapped to the trap that explains them.
LOG_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("/vault/secrets/config.txt", "rwx-openbao"),
    ("vault is sealed", "rwx-openbao"),
    ("bao is sealed", "rwx-openbao"),
    ("connection refused", "domain-suffix-resolution"),
    ("no such host", "domain-suffix-resolution"),
    ("certificate signed by unknown authority", "missing-issuers"),
    ("x509: certificate", "missing-issuers"),
    ("password authentication failed for user", "postgres-password-divergence"),
    ("failed to start quarkus", "bridge-needs-a-datasource"),
    ("unable to find datasource", "bridge-needs-a-datasource"),
)

TRAPS_BY_KEY = {t.key: t for t in TRAPS}


@dataclass
class Finding:
    severity: str
    title: str
    evidence: str
    remedy: str
    trap: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "severity": self.severity,
            "title": self.title,
            "evidence": self.evidence,
            "remedy": self.remedy,
        }
        if self.trap:
            out["known_trap"] = self.trap
        return out


@dataclass
class Diagnosis:
    findings: list[Finding] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self, severity: str, title: str, evidence: str, remedy: str, trap: str | None = None
    ) -> None:
        self.findings.append(Finding(severity, title, evidence, remedy, trap))

    def note(self, name: str, ok: bool, detail: Any = None) -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail})

    def to_dict(self) -> dict[str, Any]:
        ordered = sorted(self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
        by_severity: dict[str, int] = {}
        for f in ordered:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        return {
            "healthy": not [f for f in ordered if f.severity in ("critical", "high")],
            "summary": by_severity or {"none": 0},
            "findings": [f.to_dict() for f in ordered],
            "checks_run": self.checks,
        }


async def diagnose(
    kube: KubeClient,
    profile: Profile,
    deep_logs: bool = True,
) -> dict[str, Any]:
    d = Diagnosis()

    # --- cluster ------------------------------------------------------------------------
    try:
        version = await kube.version()
        nodes = await kube.list("Node", limit=100)
    except SimplMcpError as exc:
        d.add(
            "critical",
            "The cluster is unreachable",
            str(exc),
            "Re-fetch credentials with ovh_kubeconfig_fetch, and check the cluster's status "
            "with ovh_kube_get.",
        )
        return d.to_dict()

    minor = "".join(ch for ch in str(version.get("minor", "")) if ch.isdigit())
    if minor and int(minor) < 33:
        d.add(
            "medium",
            f"Kubernetes {version.get('major')}.{minor} is below the documented minimum",
            "Simpl-Open's prerequisites ask for 1.33 or newer.",
            "Upgrade with ovh_kube_update, or accept the risk knowingly.",
        )
    not_ready = [
        n["metadata"]["name"]
        for n in nodes
        if not any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in (n.get("status", {}).get("conditions") or [])
        )
    ]
    if not_ready:
        d.add(
            "high",
            f"{len(not_ready)} node(s) are not Ready",
            ", ".join(not_ready),
            "Check the node pool with ovh_kube_get; OVH replaces failed nodes automatically "
            "but a quota error stops it.",
        )
    d.note("cluster", True, {"nodes": len(nodes), "version": version.get("gitVersion")})

    # --- storage ------------------------------------------------------------------------
    classes = await kube.list("StorageClass", limit=50)
    from ..kube.tools import _rwx_capable  # single source of truth for the provisioner list

    rwx = [c["metadata"]["name"] for c in classes if _rwx_capable(c.get("provisioner", ""))]
    claims = await kube.list("PersistentVolumeClaim", limit=300)
    pending = [c for c in claims if (c.get("status") or {}).get("phase") != "Bound"]
    pending_rwx = [
        c for c in pending if "ReadWriteMany" in ((c.get("spec") or {}).get("accessModes") or [])
    ]
    if not rwx:
        d.add(
            "high" if pending_rwx else "medium",
            "No storage class in this cluster can serve ReadWriteMany",
            f"Classes present: {', '.join(c['metadata']['name'] for c in classes) or 'none'}. "
            "OVH's Cinder classes are ReadWriteOnce.",
            "Run simpl_setup_rwx_storage. Without it the OpenBao initialiser never runs.",
            trap="rwx-openbao",
        )
    for claim in pending_rwx:
        d.add(
            "critical",
            f"Claim {claim['metadata']['namespace']}/{claim['metadata']['name']} is not bound",
            f"It asks for ReadWriteMany from class "
            f"'{(claim.get('spec') or {}).get('storageClassName') or '(default)'}'.",
            "simpl_setup_rwx_storage installs an NFS provisioner and binds this claim.",
            trap="rwx-openbao",
        )
    d.note("storage", not pending_rwx, {"rwx_classes": rwx, "pending_claims": len(pending)})

    # --- issuers ------------------------------------------------------------------------
    try:
        issuers = await kube.list("ClusterIssuer", limit=50)
        names = {i["metadata"]["name"]: i for i in issuers}
        for role, expected in ISSUERS.items():
            issuer = names.get(expected)
            if issuer is None:
                d.add(
                    "high",
                    f"Cluster issuer '{expected}' does not exist",
                    f"The charts reference it by name for {role} certificates.",
                    "Run simpl_setup_issuers.",
                    trap="missing-issuers",
                )
                continue
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in ((issuer.get("status") or {}).get("conditions") or [])
            )
            if not ready:
                d.add(
                    "high",
                    f"Cluster issuer '{expected}' is not Ready",
                    str((issuer.get("status") or {}).get("conditions")),
                    "For an ACME issuer this is usually the account registration failing: check "
                    "that the email is valid and that the ingress is reachable from the internet.",
                    trap="missing-issuers",
                )
        d.note("issuers", True, sorted(names))
    except SimplMcpError:
        d.add(
            "high",
            "cert-manager is not installed",
            "The ClusterIssuer CRD is not present in this cluster.",
            "Run simpl_bootstrap_platform, which installs cert-manager and the two issuers.",
            trap="missing-issuers",
        )
        d.note("issuers", False)

    # --- dns ----------------------------------------------------------------------------
    if profile.domain_suffix:
        from ..kube.tools import _ingress_address

        ingress = await _ingress_address(kube)
        addresses = ingress.get("addresses") or []
        if not addresses:
            d.add(
                "critical",
                "The ingress controller has no external address",
                f"Service {ingress.get('service') or '(none found)'} has no load balancer IP.",
                "On OVH the load balancer takes a few minutes to appear. If it never does, check "
                "the project's quota with ovh_project_status.",
            )
        else:
            probe = (
                f"authority.fe.{next(iter(profile.agents), 'authority01')}.{profile.domain_suffix}"
            )
            resolved = await resolve_public(probe)
            if not resolved:
                d.add(
                    "critical",
                    f"'{probe}' does not resolve",
                    f"The ingress is at {', '.join(str(a) for a in addresses)} but the wildcard "
                    "record is missing.",
                    "Run ovh_dns_ensure_wildcard, then simpl_verify_dns.",
                    trap="domain-suffix-resolution",
                )
            elif addresses and resolved and str(addresses[0]) not in resolved:
                d.add(
                    "high",
                    f"'{probe}' resolves somewhere other than the ingress",
                    f"DNS says {', '.join(resolved)}; the ingress is at {addresses[0]}.",
                    "Update the wildcard with ovh_dns_ensure_wildcard.",
                    trap="domain-suffix-resolution",
                )
            d.note("dns", True, {"probe": probe, "resolved": resolved, "ingress": addresses})

    # --- argocd -------------------------------------------------------------------------
    argo_apps: list[dict[str, Any]] = []
    try:
        argo_apps = await kube.list("Application", "argocd", limit=200)
        degraded = [
            a
            for a in argo_apps
            if ((a.get("status") or {}).get("health") or {}).get("status")
            not in ("Healthy", "Progressing", None)
        ]
        for app in degraded[:10]:
            status = app.get("status") or {}
            conditions = "; ".join(
                c.get("message", "")[:200] for c in (status.get("conditions") or [])
            )
            unhealthy_kinds = {
                r.get("kind")
                for r in (status.get("resources") or [])
                if (r.get("health") or {}).get("status") not in ("Healthy", "Progressing", None)
            }
            if unhealthy_kinds == {"HorizontalPodAutoscaler"}:
                # resourcePreset: low sets CPU requests to 0, and a utilization-based HPA has
                # nothing to divide by. The autoscalers are min=max=1, so they are inert
                # anyway. Reporting this as a fault buries the findings that matter.
                d.add(
                    "low",
                    f"ArgoCD application '{app['metadata']['name']}' is Degraded, but only its "
                    "autoscalers are",
                    "Every degraded resource is a HorizontalPodAutoscaler that cannot compute "
                    "CPU utilisation, because resourcePreset: low sets the requests to zero. "
                    "The workloads themselves are healthy.",
                    "Nothing, unless you want the application to read Healthy — that needs real "
                    "CPU requests, and a cluster large enough to honour them.",
                    trap="hpa-degraded-under-low-preset",
                )
                continue
            d.add(
                "high",
                f"ArgoCD application '{app['metadata']['name']}' is "
                f"{((status.get('health') or {}).get('status'))}",
                conditions or str((status.get("operationState") or {}).get("message", ""))[:300],
                "Inspect with argocd_app_get and argocd_app_resources; sync again with "
                "argocd_app_sync once the cause is fixed.",
            )
        d.note("argocd", True, {"applications": len(argo_apps), "degraded": len(degraded)})
    except SimplMcpError:
        d.add(
            "critical",
            "ArgoCD is not installed",
            "The argoproj.io/Application CRD is not present.",
            "Run simpl_bootstrap_platform.",
        )
        d.note("argocd", False)

    # --- namespaces ---------------------------------------------------------------------
    namespaces = [profile.common_namespace, *profile.agents.keys()]
    existing = {n["metadata"]["name"] for n in await kube.list("Namespace", limit=300)}
    for ns in namespaces:
        if ns not in existing:
            d.note(f"namespace:{ns}", False, "not created yet")
            continue
        await _check_namespace(kube, d, ns, profile, deep_logs)

    if not d.findings:
        d.add(
            "info",
            "No problem found",
            "Every check passed: cluster, storage, issuers, DNS, ArgoCD and the workloads.",
            "Nothing to do.",
        )
    return d.to_dict()


async def _check_namespace(
    kube: KubeClient, d: Diagnosis, namespace: str, profile: Profile, deep_logs: bool
) -> None:
    pods = await kube.list("Pod", namespace, limit=300)
    unhealthy = []
    for pod in pods:
        status = pod.get("status") or {}
        containers = (status.get("containerStatuses") or []) + (
            status.get("initContainerStatuses") or []
        )
        waiting = [
            (c.get("name"), (c.get("state") or {}).get("waiting", {}).get("reason"))
            for c in containers
            if (c.get("state") or {}).get("waiting", {}).get("reason")
            not in (None, "ContainerCreating", "PodInitializing")
        ]
        # A Completed init container is how a healthy pod looks, not a fault.
        phase = status.get("phase")
        ready = all(c.get("ready") for c in (status.get("containerStatuses") or []))
        # A Succeeded pod is a finished Job, not a fault: its containers are terminated and
        # therefore never "ready". Treating it as unhealthy buries the pods that are.
        if phase == "Succeeded" or (phase == "Running" and ready and not waiting):
            continue
        unhealthy.append({"pod": pod["metadata"]["name"], "phase": phase, "waiting": waiting})

    d.note(
        f"namespace:{namespace}", not unhealthy, {"pods": len(pods), "unhealthy": len(unhealthy)}
    )
    if not unhealthy:
        return

    # Scheduling pressure is a cluster-shaped problem, not a workload one.
    events = await kube.events(namespace, field_selector="type=Warning", limit=80)
    scheduling = [e for e in events if e.get("reason") == "FailedScheduling"]
    if scheduling:
        d.add(
            "high",
            f"Pods in {namespace} cannot be scheduled",
            scheduling[0].get("message", "")[:300],
            "The cluster is too small for this configuration. Add capacity with "
            "ovh_nodepool_create or ovh_nodepool_resize, or lower it with resourcePreset=low "
            "and monitoring off.",
            trap="cluster-too-small",
        )

    # The authority's tier-2 components are the signature of a missing initialisation.
    tier2 = [u for u in unhealthy if any(k in u["pod"] for k in ("tier2", "users-roles"))]
    if tier2 and namespace in profile.agents and profile.agents[namespace] == "authority":
        # Before blaming the initialisation, ask the identity provider whether the trust
        # chain exists. The two causes look identical from the outside and the remedies
        # have nothing in common.
        from .initauthority import AuthorityInitialiser

        chain = await AuthorityInitialiser(kube, namespace).status()
        if chain.get("initialised"):
            d.add(
                "high",
                f"The tier-2 components in {namespace} are not healthy, but the trust chain exists",
                f"{', '.join(u['pod'] for u in tier2[:5])}. The identity provider already holds "
                f"{chain.get('participant_count')} participant(s), so initialisation is not the "
                "cause.",
                "Read the failing pod's own log — k8s_logs(previous=True) — and look for a "
                "database, Keycloak or certificate error rather than a missing trust chain.",
            )
        else:
            d.add(
                "high",
                f"The tier-2 components in {namespace} are not healthy",
                f"{', '.join(u['pod'] for u in tier2[:5])}. The identity provider reports no "
                "authority participant.",
                "This is what an uninitialised authority looks like. Run "
                f"simpl_initialize_authority(namespace='{namespace}').",
                trap="authority-not-initialised",
            )

    config_errors = [
        u
        for u in unhealthy
        if any(r in ("CreateContainerConfigError", "CreateContainerError") for _, r in u["waiting"])
    ]
    if config_errors:
        d.add(
            "high",
            f"{len(config_errors)} pod(s) in {namespace} cannot build their container",
            ", ".join(u["pod"] for u in config_errors[:5]),
            "A Secret or ConfigMap the pod mounts does not exist. In this platform that is "
            "usually OpenBao not having injected it — check the RWX volume first.",
            trap="rwx-openbao",
        )

    pull_errors = [
        u
        for u in unhealthy
        if any("ImagePull" in (r or "") or r == "ErrImagePull" for _, r in u["waiting"])
    ]
    if pull_errors:
        d.add(
            "high",
            f"{len(pull_errors)} pod(s) in {namespace} cannot pull their image",
            ", ".join(u["pod"] for u in pull_errors[:5]),
            "Simpl-Open's images pull anonymously from code.europa.eu:4567. A failure here is "
            "either egress being blocked or a private image with no pull secret.",
        )

    explained = {u["pod"] for u in config_errors + pull_errors}
    explained |= {u["pod"] for u in tier2}
    crash_loops = [u for u in unhealthy if any(r == "CrashLoopBackOff" for _, r in u["waiting"])]
    explained |= {u["pod"] for u in crash_loops}
    if scheduling:
        explained |= {u["pod"] for u in unhealthy if u["phase"] == "Pending"}
    remaining = [u for u in unhealthy if u["pod"] not in explained]
    if remaining:
        d.add(
            "medium",
            f"{len(remaining)} pod(s) in {namespace} are not healthy for another reason",
            ", ".join(
                f"{u['pod']} ({u['phase']}"
                + (f", {u['waiting'][0][1]}" if u["waiting"] else "")
                + ")"
                for u in remaining[:6]
            ),
            f"Look at them directly: k8s_pod_health(namespace='{namespace}', "
            "only_problems=True) and k8s_logs.",
        )
    if crash_loops and deep_logs:
        for entry in crash_loops[:3]:
            try:
                logs = await kube.logs(namespace, entry["pod"], tail_lines=60, previous=True)
            except SimplMcpError:
                try:
                    logs = await kube.logs(namespace, entry["pod"], tail_lines=60)
                except SimplMcpError:
                    continue
            lowered = logs.lower()
            matched = None
            for fragment, trap_key in LOG_SIGNATURES:
                if fragment in lowered:
                    matched = trap_key
                    break
            trap = TRAPS_BY_KEY.get(matched or "")
            d.add(
                "critical" if trap else "high",
                f"{namespace}/{entry['pod']} is crash-looping",
                _last_meaningful_line(logs),
                trap.remedy if trap else "Read the full log with k8s_logs(previous=True).",
                trap=matched,
            )
    elif crash_loops:
        d.add(
            "high",
            f"{len(crash_loops)} pod(s) in {namespace} are crash-looping",
            ", ".join(u["pod"] for u in crash_loops[:5]),
            "Re-run with deep_logs=True, or read them with k8s_logs(previous=True).",
        )


def _last_meaningful_line(logs: str, limit: int = 300) -> str:
    """The most informative line of a log, not merely the last one.

    Simpl-Open's services log structured JSON, so the useful part is the `message` field
    and whatever exception came with it — the raw line is mostly timestamps and thread
    names. Errors are preferred over the final line, which is usually a shutdown notice.
    """
    lines = [ln.strip() for ln in (logs or "").splitlines() if ln.strip()]
    if not lines:
        return "(no log output)"
    errors = [ln for ln in lines if _is_error(ln)]
    chosen = errors[-3:] if errors else lines[-1:]
    rendered = [_render_log_line(ln) for ln in chosen]
    text = " | ".join(r for r in rendered if r)
    return text[:limit] if text else lines[-1][:limit]


def _is_error(line: str) -> bool:
    lowered = line.lower()
    return any(
        marker in lowered
        for marker in ('"level":"error"', " error ", "exception", "caused by", "fatal")
    )


def _render_log_line(line: str) -> str:
    """Pull the message out of a JSON log line; leave a plain one alone."""
    start = line.find("{")
    if start >= 0:
        import json as _json

        try:
            payload = _json.loads(line[start:])
        except (ValueError, TypeError):
            return line.strip()
        if isinstance(payload, dict):
            parts = [
                str(payload.get(key))
                for key in ("message", "error", "exception", "stack_trace", "throwable")
                if payload.get(key)
            ]
            if parts:
                return " :: ".join(p.replace("\n", " ")[:200] for p in parts[:2])
    return line.strip()


async def gather(*coros: Any) -> list[Any]:
    return await asyncio.gather(*coros, return_exceptions=True)
