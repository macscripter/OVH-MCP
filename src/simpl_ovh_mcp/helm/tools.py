"""Helm tools: the platform charts and anything else that is a plain Helm release."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from ..guard import _redact, get_guard
from ..settings import Settings
from ..toolkit import Toolkit
from .runner import runner_for


def register(mcp: FastMCP, settings: Settings) -> Toolkit:
    tk = Toolkit(mcp, settings, "helm")

    @tk.read
    async def helm_list(namespace: str | None = None, profile: str | None = None) -> dict[str, Any]:
        """Helm releases in a namespace, or across the cluster when none is given."""
        helm = await runner_for(profile)
        releases = await helm.list_releases(namespace)
        return {
            "count": len(releases),
            "releases": [
                {
                    "name": r.get("name"),
                    "namespace": r.get("namespace"),
                    "revision": r.get("revision"),
                    "status": r.get("status"),
                    "chart": r.get("chart"),
                    "app_version": r.get("app_version"),
                    "updated": r.get("updated"),
                }
                for r in releases
            ],
        }

    @tk.read
    async def helm_status(release: str, namespace: str, profile: str | None = None) -> dict[str, Any]:
        """A release's status, revision, chart version and the notes the chart printed.

        The notes are where a platform chart tells you what it created — ingress-nginx
        names its Service, ArgoCD explains how to reach its UI.
        """
        helm = await runner_for(profile)
        status = await helm.status(release, namespace)
        info = status.get("info", {})
        return {
            "name": status.get("name"),
            "namespace": status.get("namespace"),
            "version": status.get("version"),
            "chart": (status.get("chart") or {}).get("metadata", {}).get("version"),
            "status": info.get("status"),
            "first_deployed": info.get("first_deployed"),
            "last_deployed": info.get("last_deployed"),
            "description": info.get("description"),
            "notes": (info.get("notes") or "")[:2000],
        }

    @tk.read
    async def helm_values(release: str, namespace: str, profile: str | None = None) -> dict[str, Any]:
        """The user-supplied values of a release, with credential-shaped keys redacted."""
        helm = await runner_for(profile)
        values = await helm.get_values(release, namespace)
        return {"release": release, "namespace": namespace, "values": _redact(values)}

    @tk.read
    async def helm_render(
        release: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
        version: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Render a chart without installing it — `helm template`.

        Use it to see exactly what a set of values produces before anything is applied,
        which is the cheapest way to check a Bridge configuration.
        """
        helm = await runner_for(profile)
        result = await helm.template(release, chart, namespace, values, version)
        result.raise_for_status()
        return {"manifest": result.stdout[:60000], "truncated": len(result.stdout) > 60000}

    @tk.write
    async def helm_repo_add(name: str, url: str, profile: str | None = None) -> dict[str, Any]:
        """Add or refresh a chart repository, then update the local index."""
        helm = await runner_for(profile)
        await helm.repo_add(name, url)
        await helm.repo_update()
        return {"repo": name, "url": url, "updated": True}

    @tk.write
    async def helm_install(
        release: str,
        chart: str,
        namespace: str,
        repo: str | None = None,
        version: str | None = None,
        values: dict[str, Any] | None = None,
        wait: bool = False,
        timeout_seconds: int = 600,
        dry_run: bool = False,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Install or upgrade a Helm release (`helm upgrade --install`).

        `chart` is either a repository reference ('ingress-nginx/ingress-nginx'), a chart
        name with `repo` set to its URL, a local path, or an OCI reference.

        `wait=False` is the default on purpose: the call returns as soon as the manifests
        are applied, and the rollout is then watched with k8s_pod_health. Blocking an MCP
        call for ten minutes tends to end in a client timeout with the install still
        running and nobody watching it.
        """
        helm = await runner_for(profile)
        result = await helm.upgrade_install(
            release,
            chart,
            namespace,
            values=values,
            version=version,
            repo=repo,
            wait=wait,
            timeout_seconds=timeout_seconds,
            dry_run=dry_run,
        )
        get_guard().audit(
            "helm_install",
            f"{namespace}/{release}",
            "ok" if result.ok else "failed",
            {"chart": chart, "version": version},
        )
        result.raise_for_status()
        return {
            "release": release,
            "namespace": namespace,
            "chart": chart,
            "version": version,
            "dry_run": dry_run,
            "waited": wait,
            "output": result.stdout[:4000],
            "next": None if wait else f"Watch with k8s_pod_health(namespace='{namespace}').",
        }

    @tk.destructive
    async def helm_uninstall(
        release: str, namespace: str, confirm: str | None = None, profile: str | None = None
    ) -> dict[str, Any]:
        """Uninstall a Helm release and every object it owns.

        Only for releases this server installed with Helm — the platform charts and the
        Bridge. Anything Simpl-Open publishes belongs to ArgoCD; remove those with
        argocd_app_delete or simpl_teardown, or ArgoCD will simply recreate them.
        """
        g = get_guard()
        g.require_destructive("helm_uninstall")
        target = f"{namespace}/{release}"
        helm = await runner_for(profile)
        if not confirm:
            status = await helm.status(release, namespace)
            impact = (
                f"Uninstalls release '{release}' in {namespace} "
                f"(chart {(status.get('chart') or {}).get('metadata', {}).get('version')}, "
                f"revision {status.get('version')}). PersistentVolumeClaims created by the chart "
                "are usually kept, but StatefulSet data can be lost."
            )
            return {
                "confirmation_required": True,
                "impact": impact,
                "confirm_token": g.issue_token(target, impact),
            }
        g.check_token("helm_uninstall", target, confirm)
        result = await helm.uninstall(release, namespace)
        g.audit("helm_uninstall", target, "ok" if result.ok else "failed")
        result.raise_for_status()
        return {"uninstalled": release, "namespace": namespace, "output": result.stdout[:2000]}

    return tk
