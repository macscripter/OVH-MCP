"""ArgoCD tools.

Simpl-Open is deployed by ArgoCD from an App-of-Apps: two Applications you create by hand
(one for the common components, one per agent) generate dozens more. These tools read that
tree and drive it. They are also the right place to look when a chart "did not install" —
nine times out of ten ArgoCD has the reason in the application's conditions.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from ..guard import get_guard
from ..settings import Settings
from ..toolkit import Toolkit
from .client import get_argo


def register(mcp: FastMCP, settings: Settings) -> Toolkit:
    tk = Toolkit(mcp, settings, "argocd")

    @tk.read
    async def argocd_status(profile: str | None = None) -> dict[str, Any]:
        """Is ArgoCD reachable, which version, and how do its applications stand?"""
        argo = await get_argo(profile)
        version = await argo.version()
        apps = await argo.applications()
        rows = [_app_row(a) for a in apps]
        return {
            "reachable_via": f"kube-apiserver proxy → {argo.namespace}/{argo.service}:{argo.port}",
            "version": version.get("Version"),
            "application_count": len(rows),
            "unhealthy": [r for r in rows if r["health"] not in ("Healthy", "Progressing", None)],
            "out_of_sync": [r for r in rows if r["sync"] == "OutOfSync"],
            "applications": rows,
        }

    @tk.read
    async def argocd_apps(
        name_contains: str | None = None, profile: str | None = None
    ) -> dict[str, Any]:
        """List applications with their sync and health status.

        A Simpl-Open install produces one deployer application per agent and a dozen
        children under each; `name_contains` narrows the list to the ones you care about.
        """
        argo = await get_argo(profile)
        apps = await argo.applications()
        rows = [_app_row(a) for a in apps]
        if name_contains:
            rows = [r for r in rows if name_contains.lower() in (r["name"] or "").lower()]
        return {"count": len(rows), "applications": rows}

    @tk.read
    async def argocd_app_get(
        name: str, refresh: bool = False, profile: str | None = None
    ) -> dict[str, Any]:
        """One application in detail: source, sync state, conditions and last operation.

        `conditions` is where ArgoCD records what went wrong — a chart that could not be
        pulled, a values file that is not valid YAML, a namespace that does not exist.
        """
        argo = await get_argo(profile)
        app = await argo.application(name, refresh="normal" if refresh else None)
        spec = app.get("spec", {})
        status = app.get("status", {})
        sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
        op = status.get("operationState") or {}
        return {
            "name": app["metadata"]["name"],
            "namespace": spec.get("destination", {}).get("namespace"),
            "project": spec.get("project"),
            "sources": [
                {
                    "repo": s.get("repoURL"),
                    "chart": s.get("chart"),
                    "path": s.get("path"),
                    "target_revision": s.get("targetRevision"),
                }
                for s in sources
            ],
            "sync": (status.get("sync") or {}).get("status"),
            "revision": (status.get("sync") or {}).get("revision"),
            "health": (status.get("health") or {}).get("status"),
            "conditions": status.get("conditions") or [],
            "last_operation": {
                "phase": op.get("phase"),
                "message": op.get("message"),
                "started": op.get("startedAt"),
                "finished": op.get("finishedAt"),
            }
            if op
            else None,
            "resource_summary": _resource_summary(status.get("resources") or []),
        }

    @tk.read
    async def argocd_app_resources(
        name: str, unhealthy_only: bool = True, profile: str | None = None
    ) -> dict[str, Any]:
        """The resource tree of an application, with per-resource health.

        On an App-of-Apps such as `common01-deployer`, the children are themselves
        Applications; follow the unhealthy ones down with argocd_app_get.
        """
        argo = await get_argo(profile)
        tree = await argo.resource_tree(name)
        nodes = []
        for node in tree.get("nodes") or []:
            health = (node.get("health") or {}).get("status")
            if unhealthy_only and health in ("Healthy", None):
                continue
            nodes.append(
                {
                    "kind": node.get("kind"),
                    "name": node.get("name"),
                    "namespace": node.get("namespace"),
                    "health": health,
                    "message": (node.get("health") or {}).get("message"),
                    "created": node.get("createdAt"),
                }
            )
        return {"application": name, "count": len(nodes), "resources": nodes}

    @tk.write
    async def argocd_app_sync(
        name: str,
        prune: bool = False,
        dry_run: bool = False,
        force: bool = False,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Sync an application: make the cluster match what the chart says.

        `prune=True` deletes resources the chart no longer declares — the Simpl-Open
        deployers ship with prune off, and this tool keeps that default. `force=True` makes
        ArgoCD replace immutable fields rather than fail; use it only when a sync is stuck
        on one.

        The call returns as soon as the operation is accepted. Poll argocd_app_get.
        """
        argo = await get_argo(profile)
        result = await argo.sync(name, prune=prune, dry_run=dry_run, strategy_force=force)
        get_guard().audit("argocd_app_sync", name, "started", {"prune": prune, "dry_run": dry_run})
        status = (result or {}).get("status", {})
        return {
            "application": name,
            "sync_started": True,
            "prune": prune,
            "dry_run": dry_run,
            "phase": ((status.get("operationState") or {}).get("phase")),
            "next": "Poll argocd_app_get(name) until sync is Synced and health Healthy.",
        }

    @tk.write
    async def argocd_app_refresh(
        name: str, hard: bool = False, profile: str | None = None
    ) -> dict[str, Any]:
        """Re-read the application's source without syncing.

        `hard=True` also drops ArgoCD's manifest cache, which is what you need after a new
        chart version is published under a tag ArgoCD has already seen.
        """
        argo = await get_argo(profile)
        app = await argo.application(name, refresh="hard" if hard else "normal")
        status = app.get("status", {})
        return {
            "application": name,
            "sync": (status.get("sync") or {}).get("status"),
            "health": (status.get("health") or {}).get("status"),
            "revision": (status.get("sync") or {}).get("revision"),
        }

    @tk.write
    async def argocd_app_terminate(name: str, profile: str | None = None) -> dict[str, Any]:
        """Stop an operation that is stuck, so the application can be synced again."""
        argo = await get_argo(profile)
        await argo.terminate_operation(name)
        get_guard().audit("argocd_app_terminate", name, "terminated")
        return {"application": name, "terminated": True}

    @tk.destructive
    async def argocd_app_delete(
        name: str, cascade: bool = True, confirm: str | None = None, profile: str | None = None
    ) -> dict[str, Any]:
        """Delete an application. With `cascade`, everything it deployed goes with it.

        Deleting a Simpl-Open deployer application removes the whole agent: its
        Applications, its workloads, and — because the charts own them — its volumes.
        """
        g = get_guard()
        g.require_destructive("argocd_app_delete")
        target = f"argocd/{name}"
        argo = await get_argo(profile)
        if not confirm:
            app = await argo.application(name)
            resources = (app.get("status") or {}).get("resources") or []
            impact = (
                f"Deletes ArgoCD application '{name}' and, with cascade={cascade}, the "
                f"{len(resources)} resources it manages in namespace "
                f"{(app.get('spec') or {}).get('destination', {}).get('namespace')}."
            )
            return {
                "confirmation_required": True,
                "impact": impact,
                "confirm_token": g.issue_token(target, impact),
            }
        g.check_token("argocd_app_delete", target, confirm)
        await argo.delete(name, cascade=cascade)
        g.audit("argocd_app_delete", target, "deleted")
        return {"deleted": name, "cascade": cascade}

    return tk


def _app_row(app: dict[str, Any]) -> dict[str, Any]:
    status = app.get("status", {})
    spec = app.get("spec", {})
    return {
        "name": app.get("metadata", {}).get("name"),
        "namespace": (spec.get("destination") or {}).get("namespace"),
        "sync": (status.get("sync") or {}).get("status"),
        "health": (status.get("health") or {}).get("status"),
        "chart": (spec.get("source") or {}).get("chart")
        or ((spec.get("sources") or [{}])[0].get("chart")),
        "revision": (spec.get("source") or {}).get("targetRevision")
        or ((spec.get("sources") or [{}])[0].get("targetRevision")),
    }


def _resource_summary(resources: list[dict[str, Any]]) -> dict[str, Any]:
    by_health: dict[str, int] = {}
    problems = []
    for r in resources:
        health = (r.get("health") or {}).get("status") or "Unknown"
        by_health[health] = by_health.get(health, 0) + 1
        if health not in ("Healthy", "Progressing"):
            problems.append(
                {
                    "kind": r.get("kind"),
                    "name": r.get("name"),
                    "namespace": r.get("namespace"),
                    "health": health,
                    "sync": r.get("status"),
                    "message": (r.get("health") or {}).get("message"),
                }
            )
    return {"total": len(resources), "by_health": by_health, "problems": problems[:25]}
