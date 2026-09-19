"""The Simpl-Open layer: plan, bootstrap, install, verify, diagnose, initialise, tear down.

These tools know what Simpl-Open is. They compose the OVH, Kubernetes, Helm and ArgoCD
layers into the handful of operations someone actually wants — "put a Governance Authority
on OVH" — and they refuse, with a reason, when a step's prerequisites are not in place.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import yaml
from fastmcp import FastMCP

from ..errors import NotFound
from ..guard import get_guard
from ..helm.runner import runner_for
from ..kube.session import get_kube
from ..kube.tools import _ingress_address
from ..settings import Settings
from ..state import Profile, get_store
from ..toolkit import Toolkit
from . import platform as plat
from .catalog import (
    AGENT_TYPES,
    CHARTS,
    ISSUERS,
    PLATFORM,
    REQUIREMENTS,
    SIZINGS,
    TRAPS,
    latest_versions,
)
from .diagnose import diagnose
from .initauthority import AuthorityInitialiser
from .values import agent_values, application_manifest, common_values, validate_agent_namespace

BOOTSTRAP_ORDER = ("ingress-nginx", "cert-manager", "argo-cd", "nfs-server-provisioner")


def register(mcp: FastMCP, settings: Settings) -> Toolkit:
    tk = Toolkit(mcp, settings, "simpl")
    store = get_store

    # =============================================================== knowledge =========
    @tk.read
    async def simpl_catalog(
        topic: str = "all", check_published_versions: bool = False
    ) -> dict[str, Any]:
        """What Simpl-Open is made of and what it needs.

        `topic` is one of: charts, platform, requirements, sizing, traps, all.

        With `check_published_versions=True` the chart versions are read live from the
        GitLab package registry at code.europa.eu, which serves them anonymously — so the
        answer reflects what is published today rather than what was verified when this
        server was written.
        """
        out: dict[str, Any] = {}
        if topic in ("charts", "all"):
            out["charts"] = {k: c.to_dict() for k, c in CHARTS.items()}
            if check_published_versions:
                live = await asyncio.gather(
                    *(latest_versions(k) for k in CHARTS), return_exceptions=True
                )
                out["published"] = {
                    k: (v if not isinstance(v, Exception) else {"error": str(v)})
                    for k, v in zip(CHARTS, live)
                }
        if topic in ("platform", "all"):
            out["platform"] = {
                k: {
                    "release": p.release,
                    "chart": f"{p.repo_name}/{p.chart}",
                    "repo_url": p.repo_url,
                    "version": p.version,
                    "namespace": p.namespace,
                    "why": p.why,
                }
                for k, p in PLATFORM.items()
            }
            out["issuers"] = ISSUERS
        if topic in ("requirements", "all"):
            out["requirements"] = REQUIREMENTS
        if topic in ("sizing", "all"):
            out["sizing"] = {k: s.to_dict() for k, s in SIZINGS.items()}
        if topic in ("traps", "all"):
            out["traps"] = [t.to_dict() for t in TRAPS]
        if not out:
            raise NotFound(
                f"unknown topic '{topic}'",
                "Use charts, platform, requirements, sizing, traps or all.",
            )
        return out

    @tk.read
    async def simpl_plan(
        agents: list[str] | None = None,
        sizing: str = "demo",
        domain_suffix: str | None = None,
        region: str = "GRA11",
        monitoring: bool = False,
    ) -> dict[str, Any]:
        """Plan a deployment before touching anything: hardware, DNS, storage, order.

        `agents` is a list of agent types — authority, consumer, data-provider. The plan
        names the OVH flavour and node count, the DNS records, the storage that has to
        exist, and the exact sequence of tools to call, with the traps each step avoids.
        """
        agents = agents or ["authority"]
        unknown = [a for a in agents if a not in AGENT_TYPES]
        if unknown:
            raise NotFound(
                f"unknown agent type(s): {', '.join(unknown)}",
                f"Known types: {', '.join(AGENT_TYPES)}.",
            )
        size = SIZINGS.get(sizing)
        if size is None:
            raise NotFound(f"unknown sizing '{sizing}'", f"Known: {', '.join(SIZINGS)}.")

        # One extra node per agent beyond the first: the common components dominate, but
        # each agent adds a Keycloak, an EDC and a set of front ends.
        nodes = size.nodes + max(0, len(agents) - 1)
        namespaces = {"common": "common01"}
        seen: dict[str, int] = {}
        for agent in agents:
            base = _default_namespace(agent)
            seen[base] = seen.get(base, 0) + 1
            namespaces[agent] = f"{base}{seen[base]:02d}"

        steps = [
            {
                "step": 1,
                "action": "Create the cluster",
                "tool": "ovh_kube_create",
                "arguments": {
                    "region": region,
                    "flavor_name": size.flavor,
                    "desired_nodes": nodes,
                },
                "note": f"{nodes} × {size.flavor} = {nodes * size.vcpus_per_node} vCPU / "
                f"{nodes * size.ram_per_node_gb} GB. {size.notes}",
            },
            {
                "step": 2,
                "action": "Fetch cluster credentials",
                "tool": "ovh_kubeconfig_fetch",
                "note": "Stored on the server's volume; never returned in the conversation.",
            },
            {
                "step": 3,
                "action": "Install the platform charts",
                "tool": "simpl_bootstrap_platform",
                "note": "ingress-nginx, cert-manager, ArgoCD and an NFS provisioner for RWX.",
            },
            {
                "step": 4,
                "action": "Point DNS at the ingress load balancer",
                "tool": "ovh_dns_ensure_wildcard",
                "note": "A wildcard one level above the namespaces. Without it, components "
                "cannot reach each other — the failure looks like everything else.",
            },
            {
                "step": 5,
                "action": "Create the two cluster issuers",
                "tool": "simpl_setup_issuers",
                "note": f"They must be named {ISSUERS['public']} and {ISSUERS['internal']}.",
            },
            {
                "step": 6,
                "action": "Prepare ReadWriteMany storage",
                "tool": "simpl_setup_rwx_storage",
                "note": "OVH's default Cinder class is RWO; the OpenBao initialiser needs RWX.",
            },
            {
                "step": 7,
                "action": "Install the common components",
                "tool": "simpl_install_common",
                "arguments": {"agents": namespaces},
                "note": f"Chart common_components {CHARTS['common'].verified_version} from "
                "project 951. Wait for it before installing any agent.",
            },
        ]
        for i, agent in enumerate(agents, start=8):
            steps.append(
                {
                    "step": i,
                    "action": f"Install the {agent} agent",
                    "tool": "simpl_install_agent",
                    "arguments": {"agent_type": agent, "namespace": namespaces[agent]},
                    "note": f"Chart {CHARTS[agent].chart} {CHARTS[agent].verified_version}.",
                }
            )
        if "authority" in agents:
            steps.append(
                {
                    "step": len(steps) + 1,
                    "action": "Initialise the authority's trust chain",
                    "tool": "simpl_initialize_authority",
                    "note": "Or deploy with automatic_initialization=True, which is the default "
                    "here. tier2-gateway, tier2-proxy and users-roles stay unhealthy until this "
                    "is done.",
                }
            )
        steps.append(
            {
                "step": len(steps) + 1,
                "action": "Verify",
                "tool": "simpl_status, then simpl_diagnose",
                "note": "Pods running is not the same as working; the diagnosis checks DNS, "
                "issuers, storage and the trust chain.",
            }
        )

        return {
            "agents": agents,
            "namespaces": namespaces,
            "domain_suffix": domain_suffix or "<zone>.<tld>, set with ovh_dns_ensure_wildcard",
            "hardware": {
                **size.to_dict(),
                "nodes": nodes,
                "total_vcpus": nodes * size.vcpus_per_node,
                "total_ram_gb": nodes * size.ram_per_node_gb,
            },
            "storage": {
                "default_class": "Cinder (ReadWriteOnce) — provided by OVH",
                "additional": "NFS provisioner for the one ReadWriteMany claim",
                "volumes": "≈11 GB for an experimental install; 300 GB for production-like",
            },
            "monitoring": monitoring,
            "charts": {
                "common": f"{CHARTS['common'].chart} {CHARTS['common'].verified_version}",
                **{a: f"{CHARTS[a].chart} {CHARTS[a].verified_version}" for a in agents},
            },
            "steps": steps,
            "traps_this_plan_avoids": [t.key for t in TRAPS],
        }

    # =============================================================== profiles ==========
    @tk.read
    async def simpl_profiles() -> dict[str, Any]:
        """Deployment profiles this server knows, and which one is active."""
        s = store()
        names = s.list()
        return {
            "active": s.active(),
            "count": len(names),
            "profiles": [
                {
                    "name": n,
                    **{
                        k: v
                        for k, v in s.get(n).to_dict().items()
                        if k in ("ovh_project", "region", "kube_id", "domain_suffix", "agents")
                    },
                }
                for n in names
            ],
        }

    @tk.read
    async def simpl_profile_get(name: str | None = None) -> dict[str, Any]:
        """One profile in full: cluster, domain, namespaces, versions, platform state."""
        return store().resolve(name).to_dict()

    @tk.write
    async def simpl_profile_create(
        name: str,
        ovh_project: str | None = None,
        region: str | None = None,
        kube_id: str | None = None,
        domain_suffix: str | None = None,
        dns_zone: str | None = None,
        common_namespace: str = "common01",
        make_active: bool = True,
    ) -> dict[str, Any]:
        """Create a deployment profile — the handle every other tool takes.

        A profile remembers the cluster, the domain and the namespaces, so later calls need
        one argument instead of six, and a new conversation can pick the deployment up
        where the last one left it.
        """
        s = store()
        if s.exists(name):
            raise NotFound(f"profile '{name}' already exists", "Use simpl_profile_update.")
        validate_agent_namespace(common_namespace)
        profile = Profile(
            name=name,
            ovh_project=ovh_project or settings.ovh_cloud_project,
            region=region,
            kube_id=kube_id,
            domain_suffix=domain_suffix,
            dns_zone=dns_zone,
            common_namespace=common_namespace,
        )
        s.save(profile)
        if make_active:
            s.set_active(name)
        return {"created": name, "active": s.active(), "profile": profile.to_dict()}

    @tk.write
    async def simpl_profile_update(
        name: str | None = None,
        ovh_project: str | None = None,
        region: str | None = None,
        kube_id: str | None = None,
        domain_suffix: str | None = None,
        dns_zone: str | None = None,
        common_namespace: str | None = None,
        notes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Change fields on a profile. Only the fields you pass are touched."""
        s = store()
        profile = s.resolve(name)
        for field_name, value in (
            ("ovh_project", ovh_project),
            ("region", region),
            ("kube_id", kube_id),
            ("domain_suffix", domain_suffix),
            ("dns_zone", dns_zone),
        ):
            if value is not None:
                setattr(profile, field_name, value)
        if common_namespace:
            profile.common_namespace = validate_agent_namespace(common_namespace)
        if notes:
            profile.notes.update(notes)
        s.save(profile)
        return profile.to_dict()

    @tk.write
    async def simpl_profile_use(name: str) -> dict[str, Any]:
        """Make a profile the default for tools called without one."""
        s = store()
        s.get(name)
        s.set_active(name)
        return {"active": name}

    @tk.destructive
    async def simpl_profile_delete(name: str, confirm: str | None = None) -> dict[str, Any]:
        """Forget a profile. Deletes the stored kubeconfig; leaves the cluster alone."""
        g = get_guard()
        g.require_destructive("simpl_profile_delete")
        if not confirm:
            impact = (
                f"Deletes the local profile '{name}' and its stored kubeconfig. The OVH cluster "
                "and everything on it are untouched."
            )
            return {
                "confirmation_required": True,
                "impact": impact,
                "confirm_token": g.issue_token(f"profile/{name}", impact),
            }
        g.check_token("simpl_profile_delete", f"profile/{name}", confirm)
        store().delete(name)
        return {"deleted": name}

    # =============================================================== bootstrap ========
    @tk.write
    async def simpl_bootstrap_platform(
        profile: str | None = None,
        charts: list[str] | None = None,
        wait: bool = False,
        ingress_values: dict[str, Any] | None = None,
        nfs_size: str = "20Gi",
    ) -> dict[str, Any]:
        """Install the platform charts Simpl-Open depends on.

        By default all four: ingress-nginx (which creates the OVH load balancer),
        cert-manager, ArgoCD, and an NFS provisioner for the one ReadWriteMany claim in the
        platform. Pass `charts` to install a subset.

        Returns without waiting. The load balancer's address appears a few minutes later —
        watch for it with simpl_status, then point DNS at it.
        """
        p = store().resolve(profile)
        helm = await runner_for(p.name)
        selected = charts or list(BOOTSTRAP_ORDER)
        installed = []
        for key in selected:
            overrides = None
            if key == "ingress-nginx" and ingress_values:
                overrides = ingress_values
            if key == "nfs-server-provisioner" and nfs_size:
                overrides = {"persistence": {"size": nfs_size}}
            installed.append(await plat.install_platform_chart(helm, key, overrides, wait=wait))
            p.platform.setdefault("charts", {})[key] = PLATFORM[key].version
        store().save(p)
        get_guard().audit("simpl_bootstrap_platform", p.name, "installed", {"charts": selected})
        return {
            "profile": p.name,
            "installed": installed,
            "next": "Wait for the ingress load balancer address (simpl_status), then "
            "ovh_dns_ensure_wildcard, simpl_setup_issuers and simpl_setup_rwx_storage.",
        }

    @tk.write
    async def simpl_setup_issuers(
        profile: str | None = None,
        mode: str = "acme",
        acme_email: str = "",
        acme_staging: bool = False,
        ingress_class: str = "nginx",
    ) -> dict[str, Any]:
        """Create the two cluster issuers the charts reference by name.

        `mode='acme'` issues real Let's Encrypt certificates over HTTP-01 and needs the
        ingress to be reachable from the internet and an email address. `mode='ca'` creates
        a private CA instead, for a deployment with no public DNS — browsers will warn, as
        they do on the reference laptop install.

        Let's Encrypt's production rate limit is 50 certificates per registered domain per
        week; a full Simpl-Open deployment asks for a few dozen, so `acme_staging=True` is
        worth one dry run if you expect to reinstall repeatedly.
        """
        p = store().resolve(profile)
        kube = await get_kube(p.name)
        manifests: list[dict[str, Any]] = [plat.self_signed_issuer(ISSUERS["internal"])]
        if mode == "acme":
            if not acme_email:
                raise NotFound(
                    "ACME needs an email address",
                    "Let's Encrypt sends expiry warnings to it. Pass acme_email=…, or use "
                    "mode='ca' for a private certificate authority.",
                )
            server = (
                "https://acme-staging-v02.api.letsencrypt.org/directory"
                if acme_staging
                else "https://acme-v02.api.letsencrypt.org/directory"
            )
            manifests.append(
                plat.acme_issuer(ISSUERS["public"], acme_email, server, ingress_class)
            )
        elif mode == "ca":
            manifests.extend(plat.ca_issuer_bundle(ISSUERS["public"]))
        else:
            raise NotFound(f"unknown issuer mode '{mode}'", "Use 'acme' or 'ca'.")

        applied = []
        for manifest in manifests:
            await kube.apply(manifest)
            applied.append(f"{manifest['kind']}/{manifest['metadata']['name']}")
        p.platform["issuers"] = {"mode": mode, "public": ISSUERS["public"], "internal": ISSUERS["internal"]}
        store().save(p)
        get_guard().audit("simpl_setup_issuers", p.name, mode)
        return {
            "applied": applied,
            "mode": mode,
            "note": "An ACME ClusterIssuer takes a few seconds to register its account. Check "
            "with k8s_list(kind='ClusterIssuer').",
        }

    @tk.write
    async def simpl_setup_rwx_storage(
        profile: str | None = None,
        size: str = "20Gi",
        make_default: bool = False,
        pre_create_openbao_claim: bool = True,
    ) -> dict[str, Any]:
        """Give the cluster ReadWriteMany storage, and bind the one claim that needs it.

        OVH's Cinder classes are ReadWriteOnce. Simpl-Open's OpenBao initialiser claims
        ReadWriteMany, and when that claim stays Pending the initialiser never runs, OpenBao
        stays sealed, no secrets are injected, and components fail with messages about
        missing files — several steps from the cause.

        This installs an NFS provisioner as storage class `nfs` and pre-creates
        `init-bao-<common-namespace>-pvc` against it. The chart's own claim declares no
        storage class, so its server-side apply leaves the class alone.

        `make_default=True` makes `nfs` the cluster default instead. Simpler, and worse:
        PostgreSQL, Kafka and Elasticsearch would all land on NFS.
        """
        p = store().resolve(profile)
        helm = await runner_for(p.name)
        kube = await get_kube(p.name)
        result = await plat.install_platform_chart(
            helm, "nfs-server-provisioner", {"persistence": {"size": size}}, wait=False
        )
        ready = await plat.wait_for_storage_class(kube, "nfs", timeout=180)
        out: dict[str, Any] = {"installed": result, "storage_class_ready": ready}
        if make_default:
            out["default_class"] = await plat.make_default_storage_class(kube, "nfs")
        if pre_create_openbao_claim:
            await kube.apply(
                {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": p.common_namespace}}
            )
            claim = plat.openbao_init_claim(p.common_namespace)
            await kube.apply(claim)
            out["openbao_claim"] = f"{p.common_namespace}/{claim['metadata']['name']}"
        p.platform["rwx"] = {"class": "nfs", "default": make_default}
        store().save(p)
        get_guard().audit("simpl_setup_rwx_storage", p.name, "ready" if ready else "pending")
        out["note"] = (
            "The NFS server pod itself takes a Cinder volume; give it a minute before the "
            "class can bind anything."
        )
        return out

    @tk.read
    async def simpl_verify_dns(
        profile: str | None = None, hostname: str | None = None, deep: bool = False
    ) -> dict[str, Any]:
        """Check that the deployment's hostnames resolve to the ingress — from both sides.

        `deep=True` runs a one-off Job in the cluster and resolves the name from inside it.
        That is the check worth doing once per deployment: a name that resolves from your
        laptop but not from a pod produces failures that all point somewhere else.
        """
        p = store().resolve(profile)
        kube = await get_kube(p.name)
        if not p.domain_suffix and not hostname:
            raise NotFound(
                "the profile has no domain_suffix",
                "Set one with ovh_dns_ensure_wildcard or simpl_profile_update.",
            )
        probe = hostname or f"authority.fe.{next(iter(p.agents), 'authority01')}.{p.domain_suffix}"
        ingress = await _ingress_address(kube)
        public = await plat.resolve_public(probe)
        out: dict[str, Any] = {
            "hostname": probe,
            "ingress": ingress,
            "public_resolution": public,
            "matches_ingress": bool(public) and bool(ingress.get("addresses"))
            and str(ingress["addresses"][0]) in public,
        }
        if deep:
            out["in_cluster_resolution"] = await plat.resolve_in_cluster(kube, probe)
        if not public:
            out["verdict"] = (
                "The name does not resolve. Components will not find each other. Run "
                "ovh_dns_ensure_wildcard."
            )
        elif not out["matches_ingress"]:
            out["verdict"] = "The name resolves somewhere other than this cluster's ingress."
        else:
            out["verdict"] = "Resolves to the ingress load balancer."
        return out

    # =============================================================== install ===========
    @tk.read
    async def simpl_render_manifest(
        kind: str,
        profile: str | None = None,
        agent_type: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
        resource_preset: str = "low",
        monitoring: bool = False,
        ha: bool = False,
        extra_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render the ArgoCD Application that an install would apply, without applying it.

        `kind` is 'common' or 'agent'. Read the YAML before a first install: it is the whole
        configuration of the deployment in one screen.
        """
        p = store().resolve(profile)
        manifest = _build_manifest(
            p,
            kind,
            agent_type=agent_type,
            namespace=namespace,
            version=version,
            resource_preset=resource_preset,
            monitoring=monitoring,
            ha=ha,
            extra_values=extra_values,
        )
        return {"manifest": yaml.safe_dump(manifest, sort_keys=False)}

    @tk.write
    async def simpl_install_common(
        profile: str | None = None,
        version: str | None = None,
        resource_preset: str = "low",
        monitoring: bool = False,
        ha: bool = False,
        agents: dict[str, list[str]] | None = None,
        extra_values: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Install the common components — the shared services every agent needs.

        `agents` lists the agent namespaces this cluster will host, as
        {"authorities": ["authority01"], "consumers": [], "providers": []}. The chart
        creates per-agent resources from it, so naming an agent here before installing it
        is normal and expected.

        Preflight checks run first: RWX storage, both issuers, ArgoCD, and DNS. Each of
        them has cost someone a day. `force=True` skips them.
        """
        p = store().resolve(profile)
        kube = await get_kube(p.name)
        blockers = await _preflight(kube, p)
        if blockers and not force:
            return {
                "installed": False,
                "blocked_by": blockers,
                "hint": "Fix these first, or pass force=True to install anyway. They are the "
                "documented ways this install fails hours later.",
            }
        manifest = _build_manifest(
            p,
            "common",
            version=version,
            resource_preset=resource_preset,
            monitoring=monitoring,
            ha=ha,
            agents=agents,
            extra_values=extra_values,
        )
        await kube.apply(manifest)
        p.chart_versions["common"] = manifest["spec"]["source"]["targetRevision"]
        p.platform["common_installed_at"] = _now()
        store().save(p)
        get_guard().audit("simpl_install_common", f"{p.name}/{p.common_namespace}", "applied")
        return {
            "installed": True,
            "application": manifest["metadata"]["name"],
            "namespace": p.common_namespace,
            "chart": f"{CHARTS['common'].chart} {manifest['spec']['source']['targetRevision']}",
            "warnings": blockers if force else [],
            "next": "Watch it with simpl_status. The common components take 10-20 minutes and "
            "OpenBao has to initialise before anything else settles. Install agents only "
            "after this namespace is healthy.",
        }

    @tk.write
    async def simpl_install_agent(
        agent_type: str,
        namespace: str | None = None,
        profile: str | None = None,
        version: str | None = None,
        resource_preset: str = "low",
        monitoring: bool = False,
        eidas: bool = False,
        automatic_initialization: bool = True,
        authority_namespace: str | None = None,
        identifier_number: str | None = None,
        extra_values: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Install one agent: authority, consumer or data-provider.

        The common components must be healthy first — an agent installed before them fills
        a namespace with pods waiting on databases, topics and secrets that do not exist.

        For an authority, `automatic_initialization=True` lets the chart run the tier-1
        trust chain setup as a job. Leave it on unless you intend to run
        simpl_initialize_authority by hand.

        A consumer or provider must be told which namespace holds the authority.
        """
        p = store().resolve(profile)
        kube = await get_kube(p.name)
        namespace = namespace or f"{_default_namespace(agent_type)}01"
        validate_agent_namespace(namespace)

        if not force:
            common_health = await _namespace_health(kube, p.common_namespace)
            if common_health["unhealthy"] or common_health["pods"] == 0:
                return {
                    "installed": False,
                    "blocked_by": [
                        (
                            f"The common components in '{p.common_namespace}' are not healthy "
                            f"({common_health['pods']} pods, {common_health['unhealthy']} "
                            "unhealthy). Agents consume them."
                        )
                    ],
                    "hint": "Watch with simpl_status; diagnose with simpl_diagnose; or pass "
                    "force=True if you know what you are doing.",
                }

        manifest = _build_manifest(
            p,
            "agent",
            agent_type=agent_type,
            namespace=namespace,
            version=version,
            resource_preset=resource_preset,
            monitoring=monitoring,
            eidas=eidas,
            automatic_initialization=automatic_initialization,
            authority_namespace=authority_namespace,
            identifier_number=identifier_number,
            extra_values=extra_values,
        )
        await kube.apply(manifest)
        p.agents[namespace] = agent_type
        p.chart_versions[namespace] = manifest["spec"]["source"]["targetRevision"]
        store().save(p)
        get_guard().audit("simpl_install_agent", f"{p.name}/{namespace}", agent_type)
        return {
            "installed": True,
            "application": manifest["metadata"]["name"],
            "agent_type": agent_type,
            "namespace": namespace,
            "chart": f"{CHARTS[agent_type].chart} {manifest['spec']['source']['targetRevision']}",
            "next": "Watch with simpl_status. The deployer generates an Application named "
            f"'{namespace}', and ArgoCD picks it up on its own reconciliation cycle — up to "
            "about three minutes, during which the namespace is empty and nothing appears to "
            f"be happening. argocd_app_sync('{namespace}') starts it immediately. For an "
            "authority, tier2-gateway, tier2-proxy and users-roles stay unhealthy until the "
            "trust chain is initialised.",
        }

    # =============================================================== operate ===========
    @tk.read
    async def simpl_status(profile: str | None = None, deep: bool = False) -> dict[str, Any]:
        """One view of the whole deployment: cluster, platform, ArgoCD, agents, endpoints.

        The fastest way to answer "where is it up to". `deep=True` adds per-pod detail for
        every namespace instead of counts.
        """
        p = store().resolve(profile)
        kube = await get_kube(p.name)
        out: dict[str, Any] = {"profile": p.name, "domain_suffix": p.domain_suffix}

        try:
            version = await kube.version()
            nodes = await kube.list("Node", limit=100)
            out["cluster"] = {
                "version": version.get("gitVersion"),
                "nodes": len(nodes),
                "nodes_ready": sum(
                    1
                    for n in nodes
                    if any(
                        c.get("type") == "Ready" and c.get("status") == "True"
                        for c in (n.get("status", {}).get("conditions") or [])
                    )
                ),
            }
        except Exception as exc:  # noqa: BLE001 — status must never fail as a whole
            out["cluster"] = {"error": str(exc)}
            return out

        out["ingress"] = await _ingress_address(kube)
        try:
            classes = await kube.list("StorageClass", limit=50)
            out["storage_classes"] = [c["metadata"]["name"] for c in classes]
        except Exception:  # noqa: BLE001
            out["storage_classes"] = []

        try:
            issuers = await kube.list("ClusterIssuer", limit=50)
            out["issuers"] = [i["metadata"]["name"] for i in issuers]
        except Exception:  # noqa: BLE001
            out["issuers"] = "cert-manager not installed"

        try:
            apps = await kube.list("Application", "argocd", limit=200)
            out["argocd"] = {
                "applications": len(apps),
                "by_health": _count(
                    ((a.get("status") or {}).get("health") or {}).get("status") for a in apps
                ),
                "by_sync": _count(
                    ((a.get("status") or {}).get("sync") or {}).get("status") for a in apps
                ),
                "not_healthy": [
                    {
                        "name": a["metadata"]["name"],
                        "health": ((a.get("status") or {}).get("health") or {}).get("status"),
                        "sync": ((a.get("status") or {}).get("sync") or {}).get("status"),
                    }
                    for a in apps
                    if ((a.get("status") or {}).get("health") or {}).get("status")
                    not in ("Healthy", None)
                ][:20],
            }
        except Exception:  # noqa: BLE001
            out["argocd"] = "not installed"

        namespaces = {p.common_namespace: "common", **p.agents}
        out["namespaces"] = {}
        for ns, kind in namespaces.items():
            health = await _namespace_health(kube, ns, detail=deep)
            out["namespaces"][ns] = {"role": kind, **health}

        out["endpoints"] = _endpoints(p)
        if p.bridge:
            out["bridge"] = p.bridge
        return out

    @tk.read
    async def simpl_diagnose(profile: str | None = None, deep_logs: bool = True) -> dict[str, Any]:
        """Find what is actually wrong, and say which tool fixes it.

        Runs every check this server knows: cluster and nodes, ReadWriteMany storage, the
        two issuers, DNS in both directions, ArgoCD's applications, and the workloads in
        each namespace — matching crash-loop logs against the failure signatures that have
        been seen before.
        """
        p = store().resolve(profile)
        kube = await get_kube(p.name)
        return await diagnose(kube, p, deep_logs=deep_logs)

    @tk.read
    async def simpl_endpoints(profile: str | None = None) -> dict[str, Any]:
        """The URLs this deployment publishes, and what each one is for."""
        p = store().resolve(profile)
        return {"domain_suffix": p.domain_suffix, "endpoints": _endpoints(p)}

    @tk.read
    async def simpl_wait(
        namespace: str,
        profile: str | None = None,
        timeout_seconds: int = 240,
        poll_seconds: int = 10,
    ) -> dict[str, Any]:
        """Poll a namespace until its pods settle, or the timeout runs out.

        Bounded on purpose: a Simpl-Open install takes 10-20 minutes, and an MCP call that
        blocks that long will be cut off by the client with the work still running and
        nobody watching. Call this repeatedly instead — each call reports progress.
        """
        p = store().resolve(profile)
        kube = await get_kube(p.name)
        deadline = time.monotonic() + min(timeout_seconds, 600)
        history: list[dict[str, Any]] = []
        while True:
            health = await _namespace_health(kube, namespace)
            history.append({"at": _now(), **health})
            if health["pods"] and not health["unhealthy"]:
                return {"namespace": namespace, "settled": True, "final": health, "history": history[-6:]}
            if time.monotonic() >= deadline:
                return {
                    "namespace": namespace,
                    "settled": False,
                    "final": health,
                    "history": history[-6:],
                    "next": "Call again to keep waiting, or simpl_diagnose if the numbers have "
                    "stopped moving.",
                }
            await asyncio.sleep(poll_seconds)

    @tk.write
    async def simpl_initialize_authority(
        organization: str,
        common_name: str,
        country: str,
        organizational_unit: str,
        namespace: str | None = None,
        profile: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Create the Governance Authority's tier-1 trust chain.

        Six HTTP calls against the identity and authentication providers, made through the
        Kubernetes API server's proxy rather than a port-forward. Until they are made,
        tier2-gateway, tier2-proxy and users-roles never become healthy.

        If the agent was installed with automatic initialisation — the default here — this
        has already happened, and the tool says so instead of creating a second authority
        participant. `force=True` runs it anyway.

        `country` is a two-letter ISO code; `common_name` is the authority's certificate
        common name.
        """
        p = store().resolve(profile)
        kube = await get_kube(p.name)
        ns = namespace or next(
            (n for n, kind in p.agents.items() if kind == "authority"), "authority01"
        )
        init = AuthorityInitialiser(kube, ns)
        reachable = await init.reachable()
        status = await init.status()
        if status.get("initialised") and not force:
            return {
                "initialised": True,
                "already": True,
                "namespace": ns,
                "detail": status,
                "note": "An authority participant already exists. Pass force=True to run the "
                "sequence again, which would create a second one.",
            }
        result = await init.run(
            organization=organization,
            common_name=common_name,
            country=country,
            organizational_unit=organizational_unit,
        )
        get_guard().audit(
            "simpl_initialize_authority",
            f"{p.name}/{ns}",
            "ok" if result.get("initialised") else "failed",
        )
        if result.get("initialised"):
            p.notes["authority_initialised"] = {"namespace": ns, "at": _now()}
            store().save(p)
        return {"namespace": ns, "reachability": reachable, **result}

    @tk.destructive
    async def simpl_teardown(
        scope: str = "agents",
        profile: str | None = None,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        """Remove a deployment, in stages.

        `scope`:
          * `agents` — delete the agent deployer applications and their namespaces.
          * `platform` — that, plus the common components.
          * `cluster` — delete the OVH cluster itself, which removes everything at once and
            is the cheapest way to start over.
        """
        g = get_guard()
        g.require_destructive("simpl_teardown")
        p = store().resolve(profile)
        kube = await get_kube(p.name)
        target = f"{p.name}/{scope}"
        if not confirm:
            impacts = {
                "agents": f"Deletes the agent applications and namespaces {list(p.agents)} in "
                f"cluster {p.kube_id}. Volumes in those namespaces are destroyed.",
                "platform": f"Deletes the agents above and the common components in "
                f"'{p.common_namespace}' — PostgreSQL, Kafka and OpenBao data with them.",
                "cluster": f"Deletes the OVH cluster {p.kube_id} in project {p.ovh_project} "
                "entirely, with every volume it provisioned.",
            }
            impact = impacts.get(scope)
            if impact is None:
                raise NotFound(f"unknown scope '{scope}'", "Use agents, platform or cluster.")
            return {
                "confirmation_required": True,
                "impact": impact,
                "confirm_token": g.issue_token(target, impact),
            }
        g.check_token("simpl_teardown", target, confirm)

        removed: list[str] = []
        skipped: list[dict[str, str]] = []

        async def remove(kind: str, name: str, ns: str | None = None) -> None:
            """Delete if present, and say so when it is not — a half-removed deployment is
            worth reporting rather than passing over in silence."""
            label = f"{ns}/{kind}/{name}" if ns else f"{kind}/{name}"
            try:
                await kube.delete(kind, name, ns)
                removed.append(label)
            except Exception as exc:  # noqa: BLE001 — teardown continues past every failure
                skipped.append({"target": label, "reason": str(exc)[:200]})

        if scope in ("agents", "platform"):
            for ns in list(p.agents):
                for app in (f"{ns}-deployer", ns):
                    await remove("Application", app, "argocd")
                await remove("Namespace", ns)
            p.agents = {}
        if scope == "platform":
            for app in (f"{p.common_namespace}-deployer", p.common_namespace):
                await remove("Application", app, "argocd")
            await remove("Namespace", p.common_namespace)
        if scope == "cluster":
            from ..ovh.client import get_ovh

            if not (p.ovh_project and p.kube_id):
                raise NotFound("the profile has no OVH cluster recorded")
            await get_ovh().delete(f"/cloud/project/{p.ovh_project}/kube/{p.kube_id}")
            removed.append(f"ovh-cluster/{p.kube_id}")
            p.kube_id = None
            p.agents = {}
        store().save(p)
        g.audit("simpl_teardown", target, "done", {"removed": removed, "skipped": skipped})
        return {"scope": scope, "removed": removed, "not_removed": skipped}

    return tk


# ---------------------------------------------------------------------------- helpers --
def _default_namespace(agent_type: str) -> str:
    return {
        "authority": "authority",
        "consumer": "consumer",
        "data-provider": "provider",
        "infrastructure-provider": "infraprovider",
        "application-provider": "appprovider",
    }.get(agent_type, agent_type.replace("-", ""))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        key = str(v)
        out[key] = out.get(key, 0) + 1
    return out


def _build_manifest(
    profile: Profile,
    kind: str,
    *,
    agent_type: str | None = None,
    namespace: str | None = None,
    version: str | None = None,
    resource_preset: str = "low",
    monitoring: bool = False,
    ha: bool = False,
    eidas: bool = False,
    automatic_initialization: bool = True,
    authority_namespace: str | None = None,
    identifier_number: str | None = None,
    agents: dict[str, list[str]] | None = None,
    extra_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not profile.domain_suffix:
        raise NotFound(
            "the profile has no domain_suffix",
            "Every component hostname is built from it. Set it with ovh_dns_ensure_wildcard "
            "or simpl_profile_update.",
        )
    issuer = (profile.platform.get("issuers") or {}).get("public", ISSUERS["public"])

    if kind == "common":
        chart_version = version or CHARTS["common"].verified_version
        if not chart_version:
            raise NotFound("no verified version for the common components chart")
        declared = agents or _agents_from_profile(profile)
        values = common_values(
            namespace=profile.common_namespace,
            domain_suffix=profile.domain_suffix,
            version=chart_version,
            agents=declared,
            resource_preset=resource_preset,
            issuer=issuer,
            monitoring=monitoring,
            ha=ha,
            extra_values=extra_values,
        )
        return application_manifest(
            name=f"{profile.common_namespace}-deployer",
            namespace=profile.common_namespace,
            chart_key="common",
            version=chart_version,
            values=values,
        )

    if kind != "agent" or not agent_type:
        raise NotFound("kind must be 'common' or 'agent' (with agent_type)")
    chart = CHARTS.get(agent_type)
    if chart is None or not chart.chart:
        raise NotFound(
            f"no chart published for agent type '{agent_type}'",
            f"Available: {', '.join(k for k, c in CHARTS.items() if c.kind == 'agent' and c.chart)}.",
        )
    chart_version = version or chart.verified_version
    if not chart_version:
        raise NotFound(
            f"the {agent_type} chart has no verified release",
            "Pass version=… explicitly, or check what is published with "
            "simpl_catalog(check_published_versions=True).",
        )
    ns = namespace or f"{_default_namespace(agent_type)}01"
    values = agent_values(
        agent_type=agent_type,
        namespace=ns,
        common_namespace=profile.common_namespace,
        domain_suffix=profile.domain_suffix,
        version=chart_version,
        authority_namespace=authority_namespace
        or next((n for n, k in profile.agents.items() if k == "authority"), None),
        resource_preset=resource_preset,
        issuer=issuer,
        monitoring=monitoring,
        eidas=eidas,
        automatic_initialization=automatic_initialization,
        identifier_number=identifier_number,
        extra_values=extra_values,
    )
    return application_manifest(
        name=f"{ns}-deployer",
        namespace=ns,
        chart_key=agent_type,
        version=chart_version,
        values=values,
    )


def _agents_from_profile(profile: Profile) -> dict[str, list[str]]:
    from .values import AGENT_LIST_KEY

    lists: dict[str, list[str]] = {"authorities": [], "consumers": [], "providers": []}
    for ns, kind in profile.agents.items():
        lists[AGENT_LIST_KEY.get(kind, "providers")].append(ns)
    return lists


async def _namespace_health(kube: Any, namespace: str, detail: bool = False) -> dict[str, Any]:
    try:
        pods = await kube.list("Pod", namespace, limit=300)
    except Exception as exc:  # noqa: BLE001
        return {"pods": 0, "unhealthy": 0, "error": str(exc)}
    from ..kube.tools import _container_problems

    unhealthy = []
    for pod in pods:
        status = pod.get("status") or {}
        containers = status.get("containerStatuses") or []
        ok = status.get("phase") == "Succeeded" or (
            status.get("phase") == "Running" and all(c.get("ready") for c in containers)
        )
        if not ok:
            unhealthy.append(
                {
                    "pod": pod["metadata"]["name"],
                    "phase": status.get("phase"),
                    "reasons": _container_problems(
                        containers + (status.get("initContainerStatuses") or [])
                    ),
                }
            )
    out: dict[str, Any] = {"pods": len(pods), "unhealthy": len(unhealthy)}
    if unhealthy:
        out["problems"] = unhealthy if detail else [u["pod"] for u in unhealthy][:12]
    return out


async def _preflight(kube: Any, profile: Profile) -> list[str]:
    """The four checks whose absence costs hours later, each phrased as what to do."""
    blockers: list[str] = []
    try:
        await kube.resolve("Application", "argoproj.io/v1alpha1")
    except Exception:  # noqa: BLE001 — any failure here means the CRD is not usable
        blockers.append(
            "ArgoCD is not installed — the deployer Application would have nothing to create "
            "it. Run simpl_bootstrap_platform."
        )
    try:
        from ..kube.tools import _rwx_capable

        classes = await kube.list("StorageClass", limit=50)
        if not any(_rwx_capable(c.get("provisioner", "")) for c in classes):
            blockers.append(
                "No ReadWriteMany storage class. The OpenBao initialiser's claim will stay "
                "Pending and the install will stall in a way that looks like something else. "
                "Run simpl_setup_rwx_storage."
            )
    except Exception:  # noqa: BLE001, S110 — a storage class we cannot read is not a blocker
        pass
    try:
        issuers = {i["metadata"]["name"] for i in await kube.list("ClusterIssuer", limit=50)}
        missing = [name for name in ISSUERS.values() if name not in issuers]
        if missing:
            blockers.append(
                f"Cluster issuer(s) missing: {', '.join(missing)}. The charts reference them by "
                "name. Run simpl_setup_issuers."
            )
    except Exception:  # noqa: BLE001
        blockers.append("cert-manager is not installed. Run simpl_bootstrap_platform.")
    if profile.domain_suffix:
        probe = f"authority.fe.{next(iter(profile.agents), 'authority01')}.{profile.domain_suffix}"
        if not await plat.resolve_public(probe):
            blockers.append(
                f"'{probe}' does not resolve. Components address each other by hostname, "
                "including inside the cluster. Run ovh_dns_ensure_wildcard."
            )
    else:
        blockers.append(
            "The profile has no domain_suffix. Set it with ovh_dns_ensure_wildcard."
        )
    return blockers


def _endpoints(profile: Profile) -> list[dict[str, str]]:
    """The hostnames the charts publish, per agent. Names follow <component>.<ns>.<suffix>."""
    if not profile.domain_suffix:
        return []
    out = []
    for ns, kind in profile.agents.items():
        if kind == "authority":
            out += [
                {
                    "what": "Governance Authority front end",
                    "url": f"https://authority.fe.{ns}.{profile.domain_suffix}",
                },
                {
                    "what": "Keycloak (agent realm)",
                    "url": f"https://authority.be.{ns}.{profile.domain_suffix}/auth",
                },
                {
                    "what": "Schema Manager",
                    "url": f"https://schema-manager-fe.{ns}.{profile.domain_suffix}",
                },
            ]
        elif kind == "consumer":
            out.append(
                {"what": "Consumer front end", "url": f"https://consumer.fe.{ns}.{profile.domain_suffix}"}
            )
        else:
            out.append(
                {"what": f"{kind} front end", "url": f"https://provider.fe.{ns}.{profile.domain_suffix}"}
            )
    return out
