"""OVHcloud tools: account, Public Cloud project, Managed Kubernetes, DNS, registry.

These are the infrastructure layer. Nothing here knows what Simpl-Open is; they create and
inspect the cluster, the node pools, the DNS records and the registry that the Simpl layer
then builds on.
"""

from __future__ import annotations

import time
from typing import Any

from fastmcp import FastMCP

from ..errors import NotFound, UpstreamError
from ..guard import get_guard
from ..settings import Settings
from ..state import get_store
from ..toolkit import Toolkit
from .client import get_ovh

# Regions that carry Managed Kubernetes in the EU, offered as a hint when a user has not
# chosen one. The authoritative list is always ovh_kube_capabilities.
EU_REGION_HINT = ("GRA5", "GRA7", "GRA9", "GRA11", "SBG5", "SBG7", "DE1", "WAW1", "BHS5")


def register(mcp: FastMCP, settings: Settings) -> Toolkit:
    tk = Toolkit(mcp, settings, "ovh")
    ovh = get_ovh
    guard = get_guard
    store = get_store

    # ================================================================= account =========
    @tk.read
    async def ovh_whoami() -> dict[str, Any]:
        """Who the server authenticates as, and what it can actually reach.

        Start here when anything returns 403. The answer reports what each probe found
        rather than failing on the first refusal: a least-privilege credential is *expected*
        to be denied some of these, and which ones it is denied is the diagnosis.

        /me needs a right on the account resource, which a policy scoped to a cloud project
        and a DNS zone does not include — and does not need. A 403 there is reported and
        stepped over, because nothing else in this server reads it.
        """
        client = ovh()
        result: dict[str, Any] = {
            "auth_mode": client.settings.ovh_auth_mode,
            "endpoint": client.settings.ovh_endpoint,
            "api": client.base_url,
            "default_cloud_project": client.settings.ovh_cloud_project,
        }

        try:
            me = await client.get("/me")
            result["account"] = {
                "nichandle": me.get("nichandle"),
                "country": me.get("country"),
                "organisation": me.get("organisation"),
            }
        except UpstreamError as exc:
            result["account"] = {
                "readable": False,
                "detail": str(exc.status or exc),
                "note": "The credential has no right on the account resource. That is normal "
                "for a scoped IAM policy and affects nothing: no other tool reads /me. To "
                "make this line work, add urn:v1:eu:resource:account:<nic> to the policy.",
            }

        try:
            cred = await client.get("/auth/currentCredential")
            result["credential"] = {
                "status": cred.get("status"),
                "expiration": cred.get("expiration"),
                "allowed_ips": cred.get("allowedIPs"),
                "rules": cred.get("rules"),
            }
        except UpstreamError:
            # OAuth2 tokens have no /auth/currentCredential; absence is not a failure.
            result["credential"] = None

        # What the credential can actually do matters more than what it claims to be, so
        # probe the two paths this server lives on and report each verdict.
        reach: dict[str, Any] = {}
        try:
            projects = await client.get("/cloud/project")
            reach["cloud_projects"] = {"ok": True, "count": len(projects or [])}
        except UpstreamError as exc:
            reach["cloud_projects"] = {"ok": False, "detail": str(exc.status or exc)}
        try:
            zones = await client.get("/domain/zone")
            reach["dns_zones"] = {"ok": True, "zones": zones or []}
        except UpstreamError as exc:
            reach["dns_zones"] = {"ok": False, "detail": str(exc.status or exc)}
        result["reaches"] = reach
        result["verdict"] = (
            "Ready for the cluster and DNS steps."
            if reach.get("cloud_projects", {}).get("ok") and reach.get("dns_zones", {}).get("ok")
            else "Some paths are refused — see `reaches`. Add the missing resource to the IAM "
            "policy, or recreate the token with rights on /cloud/* and /domain/*."
        )
        return result

    @tk.read
    async def ovh_projects_list() -> dict[str, Any]:
        """List the Public Cloud projects (serviceName values) this credential can reach."""
        client = ovh()
        ids = await client.get("/cloud/project")
        projects = []
        for pid in ids[:20]:
            try:
                p = await client.get(f"/cloud/project/{pid}")
                projects.append(
                    {
                        "project_id": pid,
                        "description": p.get("description"),
                        "status": p.get("status"),
                        "plan": p.get("planCode"),
                    }
                )
            except UpstreamError as exc:
                projects.append({"project_id": pid, "error": str(exc)})
        return {"count": len(ids), "projects": projects}

    @tk.read
    async def ovh_project_status(project: str | None = None) -> dict[str, Any]:
        """Project description, current month's usage and the quotas that block a cluster.

        The quota block is the one to read before creating node pools: instance count,
        vCPU, RAM and volume space are per-region and a cluster that exceeds them fails
        with USER_QUOTA_ERROR rather than a clear message.
        """
        client = ovh()
        service = client.project(project)
        out: dict[str, Any] = {"project_id": service}
        out["project"] = await client.get(f"/cloud/project/{service}")
        try:
            out["quotas"] = await client.get(f"/cloud/project/{service}/quota")
        except UpstreamError as exc:
            out["quotas"] = {"error": str(exc)}
        try:
            usage = await client.get(f"/cloud/project/{service}/usage/current")
            out["usage_current"] = {
                "period": usage.get("period"),
                "total": (usage.get("total") or {}).get("total"),
            }
        except UpstreamError as exc:
            out["usage_current"] = {"error": str(exc)}
        return out

    # ================================================================= kubernetes ======
    @tk.read
    async def ovh_kube_capabilities(
        project: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Regions where Managed Kubernetes is available, and the node flavours on offer.

        `region` filters the flavour list. Flavours are what you pass as `flavor_name` when
        creating a node pool; the vCPU and RAM columns are what the Simpl-Open sizing is
        measured against.
        """
        client = ovh()
        service = client.project(project)
        regions = await client.get(f"/cloud/project/{service}/capabilities/kube/regions")
        flavors = await client.get(
            f"/cloud/project/{service}/capabilities/kube/flavors", region=region
        )
        return {
            "project_id": service,
            "regions": regions,
            "eu_regions_commonly_used": [r for r in EU_REGION_HINT if r in regions],
            "flavors": [
                {
                    "name": f.get("name"),
                    "vcpus": f.get("vCPUs"),
                    "ram_gb": f.get("ram"),
                    "gpus": f.get("gpus"),
                    "category": f.get("category"),
                    "state": f.get("state"),
                }
                for f in (flavors or [])
                if f.get("state") != "unavailable"
            ],
        }

    @tk.read
    async def ovh_kube_list(project: str | None = None) -> dict[str, Any]:
        """List Managed Kubernetes clusters in the project, with status and version."""
        client = ovh()
        service = client.project(project)
        ids = await client.get(f"/cloud/project/{service}/kube")
        clusters = []
        for kid in ids:
            c = await client.get(f"/cloud/project/{service}/kube/{kid}")
            clusters.append(_cluster_summary(c))
        return {"project_id": service, "count": len(clusters), "clusters": clusters}

    @tk.read
    async def ovh_kube_get(
        kube_id: str | None = None, project: str | None = None, profile: str | None = None
    ) -> dict[str, Any]:
        """One cluster in detail, including node pools and their current size.

        `status` is the field that matters while something is happening: INSTALLING,
        UPDATING, REDEPLOYING and RESETTING all mean "wait"; READY means the control plane
        is usable. USER_QUOTA_ERROR means the project hit a quota, not that OVH broke.
        """
        client = ovh()
        service, kid = _resolve_cluster(project, kube_id, profile)
        cluster = await client.get(f"/cloud/project/{service}/kube/{kid}")
        pools = await client.get(f"/cloud/project/{service}/kube/{kid}/nodepool")
        return {
            "cluster": _cluster_summary(cluster),
            "nodepools": [_pool_summary(p) for p in (pools or [])],
            "capacity": _capacity(pools or []),
        }

    @tk.write
    async def ovh_kube_create(
        name: str,
        region: str,
        flavor_name: str,
        desired_nodes: int = 3,
        version: str | None = None,
        min_nodes: int | None = None,
        max_nodes: int | None = None,
        autoscale: bool = False,
        anti_affinity: bool = False,
        private_network_id: str | None = None,
        update_policy: str = "MINIMAL_DOWNTIME",
        project: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Create a Managed Kubernetes cluster with one initial node pool.

        Returns as soon as OVH accepts the request; the cluster is INSTALLING for several
        minutes. Poll with ovh_kube_get until status is READY, then fetch credentials with
        ovh_kubeconfig_fetch.

        `version` defaults to OVH's current default. Simpl-Open requires 1.33 or newer.
        Pass `profile` to record the new cluster on a deployment profile.
        """
        client = ovh()
        service = client.project(project)
        pool: dict[str, Any] = {
            "name": f"{name}-pool",
            "flavorName": flavor_name,
            "desiredNodes": desired_nodes,
            "antiAffinity": anti_affinity,
            "autoscale": autoscale,
        }
        if min_nodes is not None:
            pool["minNodes"] = min_nodes
        if max_nodes is not None:
            pool["maxNodes"] = max_nodes
        body: dict[str, Any] = {
            "name": name,
            "region": region,
            "nodepools": [pool],
            "updatePolicy": update_policy,
        }
        if version:
            body["version"] = version
        if private_network_id:
            body["privateNetworkId"] = private_network_id

        guard().audit("ovh_kube_create", f"{service}/{name}", "attempt", body)
        created = await client.post(f"/cloud/project/{service}/kube", body)
        guard().audit("ovh_kube_create", f"{service}/{created.get('id')}", "accepted")

        if profile:
            p = store().get(profile)
            p.ovh_project = service
            p.kube_id = created.get("id")
            p.cluster_name = created.get("name")
            p.region = created.get("region")
            store().save(p)

        return {
            "cluster": _cluster_summary(created),
            "next": "Poll ovh_kube_get until status is READY (typically 3-8 minutes), then call "
            "ovh_kubeconfig_fetch.",
        }

    @tk.write
    async def ovh_nodepool_create(
        name: str,
        flavor_name: str,
        desired_nodes: int = 1,
        min_nodes: int | None = None,
        max_nodes: int | None = None,
        autoscale: bool = False,
        anti_affinity: bool = False,
        monthly_billed: bool = False,
        kube_id: str | None = None,
        project: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Add a node pool to an existing cluster.

        Use a second pool when one agent needs different hardware from the rest — for
        instance a memory-heavy pool for the common components' PostgreSQL, Kafka and
        Elasticsearch, and a smaller pool for the agent front ends.
        """
        client = ovh()
        service, kid = _resolve_cluster(project, kube_id, profile)
        body: dict[str, Any] = {
            "name": name,
            "flavorName": flavor_name,
            "desiredNodes": desired_nodes,
            "antiAffinity": anti_affinity,
            "autoscale": autoscale,
            "monthlyBilled": monthly_billed,
        }
        if min_nodes is not None:
            body["minNodes"] = min_nodes
        if max_nodes is not None:
            body["maxNodes"] = max_nodes
        guard().audit("ovh_nodepool_create", f"{service}/{kid}/{name}", "attempt", body)
        pool = await client.post(f"/cloud/project/{service}/kube/{kid}/nodepool", body)
        return {"nodepool": _pool_summary(pool)}

    @tk.write
    async def ovh_nodepool_resize(
        nodepool_id: str,
        desired_nodes: int | None = None,
        min_nodes: int | None = None,
        max_nodes: int | None = None,
        kube_id: str | None = None,
        project: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Change a node pool's size. Scaling down evicts pods; check PodDisruptionBudgets."""
        client = ovh()
        service, kid = _resolve_cluster(project, kube_id, profile)
        body = {
            k: v
            for k, v in (
                ("desiredNodes", desired_nodes),
                ("minNodes", min_nodes),
                ("maxNodes", max_nodes),
            )
            if v is not None
        }
        if not body:
            raise NotFound("nothing to change", "Pass at least one of desired/min/max nodes.")
        guard().audit("ovh_nodepool_resize", f"{service}/{kid}/{nodepool_id}", "attempt", body)
        await client.put(f"/cloud/project/{service}/kube/{kid}/nodepool/{nodepool_id}", body)
        pool = await client.get(f"/cloud/project/{service}/kube/{kid}/nodepool/{nodepool_id}")
        return {"nodepool": _pool_summary(pool)}

    @tk.read
    async def ovh_nodes_list(
        kube_id: str | None = None, project: str | None = None, profile: str | None = None
    ) -> dict[str, Any]:
        """Every node OVH knows about, with its flavour, version and status.

        This is the OVH view, not the Kubernetes one. A node that is READY here but absent
        from k8s_cluster_info has failed to join, which is a networking problem.
        """
        client = ovh()
        service, kid = _resolve_cluster(project, kube_id, profile)
        nodes = await client.get(f"/cloud/project/{service}/kube/{kid}/node")
        return {
            "count": len(nodes or []),
            "nodes": [
                {
                    "name": n.get("name"),
                    "flavor": n.get("flavor"),
                    "status": n.get("status"),
                    "version": n.get("version"),
                    "up_to_date": n.get("isUpToDate"),
                    "nodepool_id": n.get("nodePoolId"),
                }
                for n in (nodes or [])
            ],
        }

    @tk.write
    async def ovh_kubeconfig_fetch(
        profile: str,
        kube_id: str | None = None,
        project: str | None = None,
        reveal: bool = False,
    ) -> dict[str, Any]:
        """Fetch the cluster's admin credentials and store them on the profile.

        The kubeconfig is written to the state volume with mode 0600 and used by every
        Kubernetes, Helm and ArgoCD tool from then on. It is NOT returned: it contains an
        admin client certificate, and a transcript is not a place for one. `reveal=True`
        returns it anyway, and only works in admin mode — use it when you need the file on
        your own machine.
        """
        client = ovh()
        service, kid = _resolve_cluster(project, kube_id, profile)
        data = await client.post(f"/cloud/project/{service}/kube/{kid}/kubeconfig")
        content = data.get("content") if isinstance(data, dict) else None
        if not content:
            raise UpstreamError("OVH API", None, "kubeconfig response carried no content")

        path = store().write_kubeconfig(profile, content)
        p = store().get(profile)
        p.ovh_project = service
        p.kube_id = kid
        p.platform["kubeconfig_path"] = str(path)
        p.platform["kubeconfig_fetched_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        store().save(p)
        guard().audit("ovh_kubeconfig_fetch", f"{service}/{kid}", "stored", {"path": str(path)})

        out: dict[str, Any] = {
            "stored_at": str(path),
            "profile": profile,
            "server": _kubeconfig_server(content),
            "note": "Credentials are on the server's state volume; tools use them automatically.",
        }
        if reveal:
            if client.settings.mode != "admin":
                raise UpstreamError(
                    "simpl-ovh-mcp",
                    None,
                    "reveal=True needs admin mode",
                    "Run the server with SIMPL_MCP_MODE=admin, or copy the file from the volume.",
                )
            out["kubeconfig"] = content
        return out

    @tk.destructive
    async def ovh_kube_delete(
        kube_id: str | None = None,
        project: str | None = None,
        profile: str | None = None,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        """Delete a Managed Kubernetes cluster and everything running on it.

        Two-phase: call once without `confirm` for the impact and a token, then again with
        it. Persistent volumes provisioned by the cluster are destroyed with it; volumes
        you created by hand in the project are not.
        """
        client = ovh()
        service, kid = _resolve_cluster(project, kube_id, profile)
        target = f"{service}/{kid}"
        g = guard()
        g.require_destructive("ovh_kube_delete")
        if not confirm:
            cluster = await client.get(f"/cloud/project/{service}/kube/{kid}")
            pools = await client.get(f"/cloud/project/{service}/kube/{kid}/nodepool")
            impact = (
                f"Deletes cluster '{cluster.get('name')}' ({kid}) in {cluster.get('region')} with "
                f"{sum(p.get('currentNodes', 0) for p in pools or [])} nodes. Irreversible."
            )
            token = g.issue_token(target, impact)
            return {"confirmation_required": True, "impact": impact, "confirm_token": token}
        g.check_token("ovh_kube_delete", target, confirm)
        await client.delete(f"/cloud/project/{service}/kube/{kid}")
        g.audit("ovh_kube_delete", target, "deleted")
        return {"deleted": kid, "project_id": service}

    @tk.destructive
    async def ovh_nodepool_delete(
        nodepool_id: str,
        kube_id: str | None = None,
        project: str | None = None,
        profile: str | None = None,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        """Delete a node pool. Pods on it are evicted; unschedulable pods stay Pending."""
        client = ovh()
        service, kid = _resolve_cluster(project, kube_id, profile)
        target = f"{service}/{kid}/{nodepool_id}"
        g = guard()
        g.require_destructive("ovh_nodepool_delete")
        if not confirm:
            pool = await client.get(
                f"/cloud/project/{service}/kube/{kid}/nodepool/{nodepool_id}"
            )
            impact = (
                f"Deletes node pool '{pool.get('name')}' with {pool.get('currentNodes')} nodes "
                f"from cluster {kid}."
            )
            return {
                "confirmation_required": True,
                "impact": impact,
                "confirm_token": g.issue_token(target, impact),
            }
        g.check_token("ovh_nodepool_delete", target, confirm)
        await client.delete(f"/cloud/project/{service}/kube/{kid}/nodepool/{nodepool_id}")
        g.audit("ovh_nodepool_delete", target, "deleted")
        return {"deleted": nodepool_id}

    # ================================================================= dns =============
    @tk.read
    async def ovh_dns_zones() -> dict[str, Any]:
        """DNS zones hosted at OVH for this account.

        Simpl-Open addresses its own components by hostname, from inside the cluster as
        well as from a browser, so a real zone here is what replaces the CoreDNS override
        and /etc/hosts entries a laptop install needs.
        """
        zones = await ovh().get("/domain/zone")
        return {"count": len(zones or []), "zones": zones}

    @tk.read
    async def ovh_dns_records(
        zone: str, sub_domain: str | None = None, field_type: str | None = None
    ) -> dict[str, Any]:
        """Records in a zone, optionally filtered by subdomain or type (A, CNAME, TXT…)."""
        client = ovh()
        ids = await client.get(
            f"/domain/zone/{zone}/record", subDomain=sub_domain, fieldType=field_type
        )
        records = []
        for rid in (ids or [])[:200]:
            r = await client.get(f"/domain/zone/{zone}/record/{rid}")
            records.append(
                {
                    "id": r.get("id"),
                    "sub_domain": r.get("subDomain"),
                    "type": r.get("fieldType"),
                    "target": r.get("target"),
                    "ttl": r.get("ttl"),
                }
            )
        return {"zone": zone, "count": len(records), "records": records}

    @tk.write
    async def ovh_dns_ensure_wildcard(
        zone: str,
        sub_domain: str,
        target_ip: str,
        ttl: int = 60,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Point `*.<sub_domain>.<zone>` and `<sub_domain>.<zone>` at an IP, then refresh.

        This is the step that makes `domainSuffix` work. Every Simpl-Open component is
        reached at `<component>.<namespace>.<domainSuffix>`, so a wildcard one level above
        the namespace covers every present and future component in every agent.

        Example: zone 'example.eu', sub_domain 'simpl', target the ingress controller's
        load balancer IP, and `domainSuffix` becomes 'simpl.example.eu'.

        Existing records for those names are updated in place rather than duplicated.
        """
        client = ovh()
        g = guard()
        results = []
        # An empty sub_domain means the zone apex: the wildcard is then '*', not '*.', and
        # the second record is the apex itself. OVH rejects a subDomain of '*.'.
        names = ("*", "") if not sub_domain else (f"*.{sub_domain}", sub_domain)
        for name in names:
            existing_ids = await client.get(
                f"/domain/zone/{zone}/record", subDomain=name, fieldType="A"
            )
            if existing_ids:
                rid = existing_ids[0]
                await client.put(
                    f"/domain/zone/{zone}/record/{rid}", {"target": target_ip, "ttl": ttl}
                )
                results.append({"sub_domain": name or "@", "action": "updated", "id": rid})
            else:
                created = await client.post(
                    f"/domain/zone/{zone}/record",
                    {"fieldType": "A", "subDomain": name, "target": target_ip, "ttl": ttl},
                )
                results.append(
                    {"sub_domain": name or "@", "action": "created", "id": created.get("id")}
                )
        await client.post(f"/domain/zone/{zone}/refresh")
        g.audit("ovh_dns_ensure_wildcard", f"{sub_domain}.{zone}", "applied", {"ip": target_ip})

        domain_suffix = f"{sub_domain}.{zone}" if sub_domain else zone
        if profile:
            p = store().get(profile)
            p.dns_zone = zone
            p.domain_suffix = domain_suffix
            p.ingress_ip = target_ip
            store().save(p)
        return {
            "domain_suffix": domain_suffix,
            "records": results,
            "note": "DNS propagation with a 60s TTL is usually under two minutes. Verify with "
            "simpl_verify_dns before installing the charts.",
        }

    @tk.write
    async def ovh_dns_record_set(
        zone: str, sub_domain: str, field_type: str, target: str, ttl: int = 300
    ) -> dict[str, Any]:
        """Create or replace a single DNS record, then refresh the zone.

        Useful for the ACME DNS-01 challenge, for a CNAME to an external service, or for a
        TXT record a marketplace asks for.
        """
        client = ovh()
        existing = await client.get(
            f"/domain/zone/{zone}/record", subDomain=sub_domain, fieldType=field_type
        )
        if existing:
            rid = existing[0]
            await client.put(f"/domain/zone/{zone}/record/{rid}", {"target": target, "ttl": ttl})
            action, rid_out = "updated", rid
        else:
            created = await client.post(
                f"/domain/zone/{zone}/record",
                {"fieldType": field_type, "subDomain": sub_domain, "target": target, "ttl": ttl},
            )
            action, rid_out = "created", created.get("id")
        await client.post(f"/domain/zone/{zone}/refresh")
        guard().audit("ovh_dns_record_set", f"{sub_domain}.{zone}", action)
        return {"zone": zone, "sub_domain": sub_domain, "action": action, "id": rid_out}

    @tk.destructive
    async def ovh_dns_record_delete(
        zone: str, record_id: int, confirm: str | None = None
    ) -> dict[str, Any]:
        """Delete one DNS record by id, then refresh the zone.

        Deleting the wildcard that a running deployment resolves through takes the whole
        platform down within one TTL, so the impact line names the record before you agree
        to it.
        """
        client = ovh()
        g = guard()
        g.require_destructive("ovh_dns_record_delete")
        target = f"{zone}/{record_id}"
        if not confirm:
            record = await client.get(f"/domain/zone/{zone}/record/{record_id}")
            impact = (
                f"Deletes {record.get('fieldType')} record "
                f"'{record.get('subDomain') or '@'}.{zone}' -> {record.get('target')}."
            )
            return {
                "confirmation_required": True,
                "impact": impact,
                "confirm_token": g.issue_token(target, impact),
            }
        g.check_token("ovh_dns_record_delete", target, confirm)
        await client.delete(f"/domain/zone/{zone}/record/{record_id}")
        await client.post(f"/domain/zone/{zone}/refresh")
        g.audit("ovh_dns_record_delete", target, "deleted")
        return {"deleted": record_id, "zone": zone}

    # ================================================================= registry ========
    @tk.read
    async def ovh_registry_list(project: str | None = None) -> dict[str, Any]:
        """Managed Private Registries (Harbor) in the project — where the Bridge image lives.

        The URL in the result is what goes into `image.registry` in the Bridge values.
        """
        client = ovh()
        service = client.project(project)
        registries = await client.get(f"/cloud/project/{service}/containerRegistry")
        return {
            "project_id": service,
            "registries": [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "region": r.get("region"),
                    "url": r.get("url"),
                    "status": r.get("status"),
                    "size_gb": round((r.get("size") or 0) / 1e9, 2) if r.get("size") else None,
                }
                for r in (registries or [])
            ],
        }

    # ================================================================= escape hatch ====
    @tk.read
    async def ovh_api_get(path: str, params: dict[str, Any] | None = None) -> Any:
        """Call any OVH API GET endpoint directly, e.g. '/cloud/project/<id>/instance'.

        The typed tools above cover the Simpl-Open path; this covers everything else OVH
        exposes — storage, load balancers, vRack, billing — without waiting for a new tool.
        Browse the surface at https://api.ovh.com/console/.
        """
        return await ovh().request("GET", path, params=params)

    @tk.destructive
    async def ovh_api_call(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        confirm: str | None = None,
    ) -> Any:
        """Call any OVH API endpoint with any method. Admin mode, two-phase confirmation.

        The last resort when a typed tool does not exist. Because it can do anything the
        credential can, it is gated exactly like a deletion.
        """
        g = guard()
        g.require_destructive("ovh_api_call")
        target = f"{method.upper()} {path}"
        if not confirm:
            impact = f"Calls {target} on {ovh().base_url} with body: {body}"
            return {
                "confirmation_required": True,
                "impact": impact,
                "confirm_token": g.issue_token(target, impact),
            }
        g.check_token("ovh_api_call", target, confirm)
        g.audit("ovh_api_call", target, "executed", {"body": body})
        return await ovh().request(method, path, body=body)

    return tk


# ---------------------------------------------------------------------------- helpers --
def _resolve_cluster(
    project: str | None, kube_id: str | None, profile: str | None
) -> tuple[str, str]:
    """Cluster coordinates come either from the arguments or from a saved profile."""
    client = get_ovh()
    if profile and not (project and kube_id):
        p = get_store().get(profile)
        project = project or p.ovh_project
        kube_id = kube_id or p.kube_id
    service = client.project(project)
    if not kube_id:
        raise NotFound(
            "no cluster id given",
            "Pass kube_id, or profile=<name> for a profile that already has one, or list them "
            "with ovh_kube_list.",
        )
    return service, kube_id


def _cluster_summary(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c.get("id"),
        "name": c.get("name"),
        "region": c.get("region"),
        "status": c.get("status"),
        "version": c.get("version"),
        "up_to_date": c.get("isUpToDate"),
        "next_upgrade_versions": c.get("nextUpgradeVersions"),
        "url": c.get("url"),
        "private_network_id": c.get("privateNetworkId"),
        "update_policy": c.get("updatePolicy"),
        "created_at": c.get("createdAt"),
    }


def _pool_summary(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "flavor": p.get("flavor"),
        "status": p.get("status"),
        "desired_nodes": p.get("desiredNodes"),
        "current_nodes": p.get("currentNodes"),
        "available_nodes": p.get("availableNodes"),
        "min_nodes": p.get("minNodes"),
        "max_nodes": p.get("maxNodes"),
        "autoscale": p.get("autoscale"),
        "anti_affinity": p.get("antiAffinity"),
        "monthly_billed": p.get("monthlyBilled"),
    }


# Node flavour sizes, used to turn a pool list into "how much cluster do I actually have".
# Only the families Simpl-Open is plausibly deployed on; anything unknown is reported as
# unknown rather than guessed.
FLAVOR_SIZES: dict[str, tuple[int, int]] = {
    "b2-7": (2, 7), "b2-15": (4, 15), "b2-30": (8, 30), "b2-60": (16, 60), "b2-120": (32, 120),
    "b3-8": (2, 8), "b3-16": (4, 16), "b3-32": (8, 32), "b3-64": (16, 64), "b3-128": (32, 128),
    "b3-256": (64, 256),
    "c3-4": (2, 4), "c3-8": (4, 8), "c3-16": (8, 16), "c3-32": (16, 32), "c3-64": (32, 64),
    "d2-4": (1, 4), "d2-8": (2, 8), "r2-15": (2, 15), "r2-30": (2, 30), "r2-60": (4, 60),
    "r3-16": (2, 16), "r3-32": (4, 32), "r3-64": (8, 64), "r3-128": (16, 128),
}


def _capacity(pools: list[dict[str, Any]]) -> dict[str, Any]:
    vcpus = ram = 0
    unknown = []
    for p in pools:
        size = FLAVOR_SIZES.get(p.get("flavor", ""))
        nodes = p.get("currentNodes") or 0
        if size is None:
            unknown.append(p.get("flavor"))
            continue
        vcpus += size[0] * nodes
        ram += size[1] * nodes
    out: dict[str, Any] = {"total_vcpus": vcpus, "total_ram_gb": ram}
    if unknown:
        out["flavors_not_in_table"] = unknown
    return out


def _kubeconfig_server(content: str) -> str | None:
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("server:"):
            return line.split("server:", 1)[1].strip()
    return None
