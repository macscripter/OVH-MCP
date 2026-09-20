"""What Simpl-Open is made of, where it comes from, and what it needs to run.

Two kinds of knowledge live here.

*Facts that change*: chart versions. Those are pinned to what has been verified, and every
pinned number can be checked against the live GitLab package registry with
`latest_versions()`, because code.europa.eu serves the Helm index anonymously.

*Facts that do not change often*: which GitLab project publishes which chart, what the
platform needs underneath, how much hardware it takes, and the handful of traps that have
cost real days. Those are written down once, here, instead of being rediscovered.

Sources: the Common Components and Governance Authority deployment guides, the programme's
Prerequisites page, and a reference k3d install completed on 12 September 2026.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

GITLAB = "https://code.europa.eu"
HELM_REPO_TEMPLATE = GITLAB + "/api/v4/projects/{project_id}/packages/helm/stable"
RELEASE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class ChartRef:
    """One Simpl-Open chart, published to a GitLab project's Helm registry."""

    key: str
    project_id: int
    chart: str | None
    verified_version: str | None
    kind: str  # "common" or "agent"
    description: str

    @property
    def repo_url(self) -> str:
        return HELM_REPO_TEMPLATE.format(project_id=self.project_id)

    @property
    def index_url(self) -> str:
        return f"{self.repo_url}/index.yaml"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "chart": self.chart,
            "project_id": self.project_id,
            "repo_url": self.repo_url,
            "verified_version": self.verified_version,
            "description": self.description,
        }


# The chart registry. Project ids are the ones the deployment guides use; the repository's
# own sample deployer once pointed `common_components` at project 1402, which publishes
# `authority-iaa` instead — that is the first trap below, and the reason these ids are
# written down rather than copied from a manifest.
CHARTS: dict[str, ChartRef] = {
    "common": ChartRef(
        key="common",
        project_id=951,
        chart="common_components",
        verified_version="4.0.9",
        kind="common",
        description="Shared services every agent uses: PostgreSQL, Kafka, OpenBao, ELK, "
        "notification. Installed once per cluster, before any agent.",
    ),
    "authority": ChartRef(
        key="authority",
        project_id=902,
        chart="authority",
        verified_version="4.0.4",
        kind="agent",
        description="Governance Authority agent: Keycloak, EJBCA, identity and onboarding, "
        "the catalogue, the schema manager — and the host of the DOME Bridge.",
    ),
    "consumer": ChartRef(
        key="consumer",
        project_id=903,
        chart="consumer",
        verified_version="4.0.5",
        kind="agent",
        description="Consumer agent: the side that searches the catalogue and consumes "
        "data or services.",
    ),
    "data-provider": ChartRef(
        key="data-provider",
        project_id=904,
        chart="data-provider",
        verified_version="4.0.6",
        kind="agent",
        description="Data Provider agent: publishes datasets and serves them through EDC.",
    ),
    "infrastructure-provider": ChartRef(
        key="infrastructure-provider",
        project_id=905,
        chart="infrastructure-provider",
        verified_version=None,
        kind="agent",
        description="Infrastructure Provider agent. No stable release published yet.",
    ),
    "application-provider": ChartRef(
        key="application-provider",
        project_id=906,
        chart="application-provider",
        verified_version=None,
        kind="agent",
        description="Application Provider agent. Nothing published to the Helm registry yet.",
    ),
}

AGENT_TYPES = tuple(k for k, c in CHARTS.items() if c.kind == "agent")


@dataclass(frozen=True)
class PlatformChart:
    """A chart that must exist before Simpl-Open is installed."""

    key: str
    release: str
    repo_name: str
    repo_url: str
    chart: str
    version: str
    namespace: str
    values: dict[str, Any] = field(default_factory=dict)
    why: str = ""


# Versions verified together on the reference install. The programme's Prerequisites page
# states minimums (Kubernetes 1.33, ArgoCD 3.2, ingress-nginx 1.13, cert-manager 1.19,
# nfs-provisioner 4.0); these are concrete versions that satisfy them.
PLATFORM: dict[str, PlatformChart] = {
    "ingress-nginx": PlatformChart(
        key="ingress-nginx",
        release="ingress-nginx",
        repo_name="ingress-nginx",
        repo_url="https://kubernetes.github.io/ingress-nginx",
        chart="ingress-nginx",
        version="4.15.1",
        namespace="ingress-nginx",
        why="Every Simpl-Open component is addressed by hostname, including by its "
        "neighbours inside the cluster. On OVH its Service of type LoadBalancer is what "
        "gives the deployment a public IP to point DNS at.",
    ),
    "cert-manager": PlatformChart(
        key="cert-manager",
        release="cert-manager",
        repo_name="jetstack",
        repo_url="https://charts.jetstack.io",
        chart="cert-manager",
        version="v1.21.2",
        namespace="cert-manager",
        values={"crds": {"enabled": True}},
        why="The charts ask for certificates by issuer name. Without cert-manager and the "
        "two issuers, ingresses come up without TLS and components refuse each other.",
    ),
    "argo-cd": PlatformChart(
        key="argo-cd",
        release="argocd",
        repo_name="argo",
        repo_url="https://argoproj.github.io/argo-helm",
        chart="argo-cd",
        version="10.9.0",
        namespace="argocd",
        why="Simpl-Open is deployed as an App-of-Apps. Argo CD 3.2 or newer is a hard "
        "requirement of the deployment guides.",
    ),
    "nfs-server-provisioner": PlatformChart(
        key="nfs-server-provisioner",
        release="nfs-server-provisioner",
        repo_name="nfs-ganesha",
        repo_url="https://kubernetes-sigs.github.io/nfs-ganesha-server-and-external-provisioner/",
        chart="nfs-server-provisioner",
        version="1.8.0",
        namespace="nfs-provisioner",
        values={
            "persistence": {"enabled": True, "size": "20Gi"},
            "storageClass": {"name": "nfs", "defaultClass": False, "reclaimPolicy": "Retain"},
        },
        why="OVH's default storage is Cinder, which is ReadWriteOnce only, and the OpenBao "
        "initialiser claims a ReadWriteMany volume. This provisioner answers that claim. "
        "The programme's own prerequisites list nfs-provisioner as mandatory.",
    ),
}

# Tool and platform minimums, quoted from the programme's Prerequisites page.
REQUIREMENTS = {
    "kubernetes": "1.33.x or newer",
    "argocd": "3.2.x or newer",
    "ingress-nginx": "1.13.x or newer (controller app version)",
    "cert-manager": "1.19.x or newer",
    "nfs-provisioner": "4.0.x or newer",
    "external-dns": "0.19.1 or newer (optional — OVH DNS is driven by this server instead)",
    "helm": "3.14.x or higher",
}

# Issuer names the charts look for. They are values, but every published example uses these
# two and the deployer fails quietly when they are missing, so they are treated as names.
ISSUERS = {"public": "dev-prod-http", "internal": "dev-selfsigned"}


@dataclass(frozen=True)
class Sizing:
    key: str
    summary: str
    nodes: int
    flavor: str
    vcpus_per_node: int
    ram_per_node_gb: int
    resource_preset: str
    ha: bool
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "summary": self.summary,
            "nodes": self.nodes,
            "ovh_flavor": self.flavor,
            "total_vcpus": self.nodes * self.vcpus_per_node,
            "total_ram_gb": self.nodes * self.ram_per_node_gb,
            "resource_preset": self.resource_preset,
            "high_availability": self.ha,
            "notes": self.notes,
        }


# Sizing options, from the documented minimums and from what a reference install actually
# consumed. The handbook's "experimental" row is included because it is the official
# number; the note says what happened when it was tested, which is the more useful fact.
SIZINGS: dict[str, Sizing] = {
    "demo": Sizing(
        key="demo",
        summary="Common components plus one Governance Authority, no HA, monitoring off.",
        nodes=2,
        flavor="b3-32",
        vcpus_per_node=8,
        ram_per_node_gb=32,
        resource_preset="low",
        ha=False,
        notes="Matches the reference install, which ran common + authority (53 of 60 pods) on "
        "6 vCPU and 62 GB with resourcePreset=low, Kafka/OpenBao/PostgreSQL single-replica and "
        "monitoring, redpanda, pg_admin and mailpit disabled. Two nodes rather than one so a "
        "node can be drained.",
    ),
    "experimental": Sizing(
        key="experimental",
        summary="The handbook's experimental minimum: one worker, everything single-instance.",
        nodes=1,
        flavor="b3-16",
        vcpus_per_node=4,
        ram_per_node_gb=16,
        resource_preset="low",
        ha=False,
        notes="The documented figure is 4 vCPU / 16 GB and 11 GB of ReadWriteOnce volumes. It "
        "is tight: the reference install of the same scope needed closer to 6 vCPU and well "
        "over 16 GB once ELK and Kafka were running. Use it for a single agent with monitoring "
        "off, and expect to scale up.",
    ),
    "production-like": Sizing(
        key="production-like",
        summary="The handbook's production-like environment for the common components.",
        nodes=3,
        flavor="b3-32",
        vcpus_per_node=8,
        ram_per_node_gb=32,
        resource_preset="default",
        ha=True,
        notes="3 workers of 8 vCPU / 32 GB and 300 GB of volumes for the common components, "
        "plus 1-3 workers per agent at 4-8 vCPU and 16-32 GB each. HA on for Kafka, PostgreSQL "
        "and OpenBao.",
    ),
}


@dataclass(frozen=True)
class Trap:
    """Something that has actually gone wrong, with the shape it takes when it does."""

    key: str
    symptom: str
    cause: str
    remedy: str
    severity: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "symptom": self.symptom,
            "cause": self.cause,
            "remedy": self.remedy,
            "severity": self.severity,
        }


TRAPS: tuple[Trap, ...] = (
    Trap(
        key="wrong-project-id",
        symptom="The common components deployer never syncs; ArgoCD reports that the chart "
        "cannot be found, or pulls a chart called authority-iaa.",
        cause="The sample deployer in the repository points `common_components` at GitLab "
        "project 1402. That project publishes authority-iaa. The chart is in project 951.",
        remedy="Use simpl_install_common, which builds the Application from this catalogue "
        "(project 951) rather than from the sample manifest.",
    ),
    Trap(
        key="rwx-openbao",
        symptom="simpl-notification and other pods crash-loop on "
        '"can\'t open /vault/secrets/config.txt"; the init-bao-<namespace>-pvc claim stays '
        "Pending.",
        cause="The OpenBao initialiser claims a ReadWriteMany volume. The hardware table says "
        "ReadWriteOnce is enough, which is wrong. OVH's default Cinder class and k3s local-path "
        "both refuse ReadWriteMany, so the init job never runs, OpenBao stays sealed, and every "
        "component that waits for an injected secret fails — several steps away from the cause.",
        remedy="Run simpl_setup_rwx_storage before installing the common components. It installs "
        "an NFS provisioner and pre-creates the claim against it.",
    ),
    Trap(
        key="domain-suffix-resolution",
        symptom="Pods are Running but cannot reach each other; front ends load and then fail "
        "every call; tokens are rejected because the issuer URL does not resolve.",
        cause="Components address each other by ingress hostname, not by Service name, so "
        "`domainSuffix` has to resolve to the ingress controller from INSIDE the cluster as "
        "well as outside. On a laptop that needs a CoreDNS override; on OVH it needs a real "
        "wildcard A record.",
        remedy="Point a wildcard at the ingress load balancer with ovh_dns_ensure_wildcard, then "
        "confirm both directions with simpl_verify_dns before installing anything.",
    ),
    Trap(
        key="missing-issuers",
        symptom="Ingresses have no certificate; the deployer looks healthy but nothing is "
        "reachable over HTTPS.",
        cause="The charts reference cluster issuers by name — dev-prod-http and dev-selfsigned — "
        "and fail quietly when they do not exist.",
        remedy="simpl_setup_issuers creates both: an ACME issuer for real certificates and a "
        "self-signed CA for internal ones.",
    ),
    Trap(
        key="authority-not-initialised",
        symptom="tier2-gateway, tier2-proxy and users-roles never become healthy; parts of the "
        "catalogue screens fail on data calls.",
        cause="The Governance Authority's tier-1 trust chain has to be created after the chart "
        "is deployed. Until then the tier-2 components have nothing to trust.",
        remedy="Run simpl_initialize_authority, or deploy with "
        "authority_iaa.extraValues.development.enableAutomaticInitialization=true, which runs "
        "the same six calls as a Kubernetes job.",
    ),
    Trap(
        key="agent-namespace-not-declared",
        symptom="Every agent pod that needs a secret stays Pending or crash-loops; their "
        "vault-env init logs say 'Code: 403 … namespace not authorized' against OpenBao; the "
        "initialisation job loops on 'EJBCA is not ready yet'; the postgres-operator cluster "
        "has no <agent>_* users.",
        cause="The common components chart authorises OpenBao's Kubernetes auth and creates "
        "PostgreSQL users ONLY for the namespaces in its agentList. When the common install "
        "was rendered with an empty list — a fresh profile, or one whose agents were cleared "
        "by a teardown — no agent namespace is authorised, and nothing later adds it: the "
        "agent chart assumes the platform already knows it.",
        remedy="Re-run simpl_install_common with agents={'authorities': ['authority01']} and "
        "force=True. That re-render adds the PostgreSQL users and re-runs the openbao-config "
        "job, but it does NOT add the per-agent key to the already-existing common Secret "
        "keycloak-secrets, and the job's 5-authority-secrets step then fails with \"couldn't "
        'find key authority01 in Secret common01/keycloak-secrets". Delete that Secret with '
        "k8s_delete(kind='Secret', name='keycloak-secrets', namespace=<common>) so the chart "
        "recreates it with the key, wait for the job to finish, and the agent recovers on its "
        "own. simpl_install_common now refuses an empty list and simpl_install_agent refuses "
        "an undeclared namespace, so this stays a one-time trap.",
    ),
    Trap(
        key="hyphen-in-agent-name",
        symptom="Hostnames come out malformed and the agent's ingresses never match.",
        cause="Agent names become part of an FQDN. A '-' in the name breaks it. The deployment "
        "guide states the rule in one line that is easy to miss.",
        remedy="Name agents like authority01, consumer01, provider01 — letters and digits only.",
    ),
    Trap(
        key="install-order",
        symptom="An agent's pods wait forever on secrets, databases or Kafka topics.",
        cause="Agents consume the common components. Installing an agent before the common "
        "components are healthy produces a namespace full of pods waiting on things that do "
        "not exist yet.",
        remedy="Install the common components first and wait for them; simpl_install_agent "
        "refuses to run before that unless forced.",
    ),
    Trap(
        key="bridge-needs-a-datasource",
        symptom='The Bridge crash-loops on start-up with "Unable to find datasource '
        "'<default>'\", or with an InactiveBeanException naming org.hibernate.Session and "
        "eu.europa.ec.simpl.bridge.outbox.OutboxRepository.",
        cause="Before GitLab main 9cbda97 the application required a datasource at boot even "
        "with publication off: the persistence unit started eagerly, and OutboxRepository "
        "injected the EntityManager directly, so Increment 2's outbox blocked Increment 1's read "
        "path. Images built from an older commit still behave that way.",
        remedy="Deploy a Bridge built from GitLab main at 9cbda97 or later: persistence now "
        "follows bridge.publication.enabled, and the outbox resolves its EntityManager lazily, so "
        "the read-only Bridge boots with no database at all. Only a deployment that switches "
        "publication on needs one — then bridge_deploy with extra_values carrying extraEnv for "
        "QUARKUS_DATASOURCE_DB_KIND, _JDBC_URL, _USERNAME and _PASSWORD, with the credentials "
        "copied into the agent's namespace because Secrets do not cross namespaces. The chart's "
        "own datasource block is still owed (T20).",
    ),
    Trap(
        key="postgres-password-divergence",
        symptom="After a cluster restart — a scale to zero and back, most obviously — Keycloak, "
        "identity-provider, authentication-provider, users-roles, security-attributes-provider "
        "and xsfc-service all crash-loop with "
        '"FATAL: password authentication failed for user <agent>_<component>", and the '
        "tier2 components stay Pending behind them.",
        cause="NOT ESTABLISHED. The PostgreSQL passwords have two holders — the postgres-"
        "operator's own Secrets, and a copy that openbao-config writes into OpenBao once at "
        "install (<agent>-postgres-passwords), which the components read — and after a scale-"
        "to-zero restart the components' copy was refused by the database. Which side changed, "
        "and why, was not determined: the operator has password rotation disabled, and re-"
        "running the openbao-config copy (argocd_app_sync on the common application) did NOT "
        "restore agreement when tried on 2026-09-19.",
        remedy="Nothing reliable short of a rebuild. Treat a Simpl-Open cluster as expendable: "
        "run it, delete it with simpl_teardown, rebuild it from the fixed profile when needed "
        "(about fifty minutes). Do not park it with a scale-to-zero expecting to resume. If you "
        "must investigate, compare the operator Secret, the OpenBao value and what the database "
        "accepts for one user before changing anything.",
    ),
    Trap(
        key="sync-rotates-credentials",
        symptom="After an ArgoCD sync of the common components application, Kafka, "
        "simpl-notification, the consumption monitoring service, Redis clients or Redpanda "
        "start failing authentication, on a platform that was healthy before the sync.",
        cause="PARTLY UNPROVEN. The common_components templates mint Kafka, Redis, pgAdmin and "
        "Redpanda passwords with randAlphaNum at render time, which is why a sync was "
        "suspected of rotating them. Observed on 2026-09-20: a re-render of the deployer with "
        "new values (agentList) changed none of the 28 Secrets in the common namespace — "
        "ArgoCD left every existing Secret untouched, including one that should have gained "
        "a key. So a plain re-render does not rotate; what an explicit argocd_app_sync with "
        "force or replace does has not been measured.",
        remedy="Nothing to run — the point is what not to run. Do not argocd_app_sync the common "
        "application with force or replace on a live deployment unless you intend to restart "
        "every dependent component afterwards and can afford the outage. Change values through "
        "the deployer Application (simpl_install_common with force=True) and let ArgoCD's own "
        "reconciliation apply them; that path was measured not to touch existing Secrets.",
    ),
    Trap(
        key="hpa-degraded-under-low-preset",
        symptom="An agent's ArgoCD application stays Degraded while every pod is Running and "
        "Ready. The degraded resources are all HorizontalPodAutoscalers reporting "
        '"failed to get cpu utilization: no metrics returned matched known pods".',
        cause="resourcePreset: low sets CPU and memory requests to 0. A utilization-based "
        "HPA computes a percentage OF the request, so a request of zero leaves it nothing to "
        "divide by. The charts also set those autoscalers to minReplicas 1 and maxReplicas 1, "
        "so they could not scale anything even if they worked.",
        remedy="Nothing, on a demonstration cluster: the workloads are healthy and the "
        "autoscalers are inert by configuration. To make the application report Healthy, give "
        "the deployments real CPU requests — resourcePreset: default, or explicit resources — "
        "which also means a bigger cluster.",
        severity="low",
    ),
    Trap(
        key="staging-acme-breaks-trust",
        symptom='openbao-config fails with "x509: certificate signed by unknown authority" '
        "and every component waiting on injected secrets stays Pending.",
        cause="Simpl-Open's components call each other over their public ingress hostnames and "
        "VERIFY the certificate against the system trust store. Let's Encrypt staging is not in "
        "it. The cost of staging is not that browsers warn — it is that components refuse each "
        "other and the install stops.",
        remedy="Use production ACME from the start: simpl_setup_issuers(mode='acme', "
        "acme_staging=False). If staging certificates were already issued, the Secrets have to "
        "be re-issued before the components will talk.",
    ),
    Trap(
        key="cluster-too-small",
        symptom="Pods stay Pending with FailedScheduling; the cluster looks healthy otherwise.",
        cause="The full platform is sixty pods including Elasticsearch, Kafka and PostgreSQL. "
        "The documented experimental minimum of 4 vCPU / 16 GB does not fit them once "
        "monitoring is on.",
        remedy="Size with simpl_plan, keep resourcePreset=low and monitoring off for a demo, or "
        "add a node pool with ovh_nodepool_create.",
    ),
)


async def latest_versions(chart_key: str, limit: int = 8, timeout: float = 30.0) -> dict[str, Any]:
    """Read the published releases of one chart from the GitLab Helm index.

    Anonymous: code.europa.eu serves both the container registry and the Helm index without
    credentials, which is what makes an unattended deployment possible at all.
    """
    ref = CHARTS.get(chart_key)
    if ref is None:
        return {"error": f"unknown chart '{chart_key}'", "known": sorted(CHARTS)}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(ref.index_url)
        if resp.status_code >= 400:
            return {
                "chart": ref.chart,
                "error": f"the Helm index answered {resp.status_code}",
                "index_url": ref.index_url,
            }
        text = resp.text

    charts = _parse_index_versions(text)
    out: dict[str, Any] = {"chart": ref.chart, "project_id": ref.project_id, "charts": {}}
    for name, versions in charts.items():
        releases = sorted(
            (v for v in versions if RELEASE_VERSION.match(v)),
            key=lambda s: [int(x) for x in s.split(".")],
            reverse=True,
        )
        out["charts"][name] = {
            "published_versions": len(versions),
            "latest_releases": releases[:limit],
            "newer_than_verified": [
                v
                for v in releases[:limit]
                if ref.verified_version
                and _tuple(v) > _tuple(ref.verified_version)
                and name == ref.chart
            ],
        }
    return out


def _parse_index_versions(text: str) -> dict[str, list[str]]:
    """Pull chart names and versions out of a Helm index without loading all of it as YAML.

    These indexes carry over a thousand entries per chart (every feature branch build), so a
    line scan is both faster and far less memory than yaml.safe_load.
    """
    charts: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        entry = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if entry:
            current = entry.group(1)
            charts.setdefault(current, [])
            continue
        version = re.match(r"^\s+version:\s*(\S+)\s*$", line)
        if version and current:
            charts[current].append(version.group(1))
    charts.pop("entries", None)
    return charts


def _tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return (0,)
