"""Helm, run as a subprocess.

Helm is used for exactly one job: the four platform charts that have to exist before
ArgoCD can take over — ingress-nginx, cert-manager, ArgoCD itself, and an RWX storage
provisioner — plus the Bridge's own chart. Everything Simpl-Open publishes is deployed as
an ArgoCD Application through the Kubernetes API instead, because that is how the platform
is designed to be operated and because a Helm release that ArgoCD does not know about will
be reverted the moment someone syncs.

Values are always written to a temporary file rather than passed as `--set`: `--set` has
its own escaping rules for dots and commas, and Simpl-Open's values are full of both.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError, UpstreamError
from ..settings import get_settings
from ..state import Profile, get_store


@dataclass
class HelmResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def raise_for_status(self) -> HelmResult:
        if not self.ok:
            raise UpstreamError(
                "helm",
                self.returncode,
                (self.stderr or self.stdout or "").strip()[:800],
                _hint_for(self.stderr or self.stdout or ""),
            )
        return self


class HelmRunner:
    def __init__(self, profile: Profile | None) -> None:
        self.settings = get_settings()
        self.profile = profile
        self.binary = shutil.which(self.settings.helm_bin) or self.settings.helm_bin

    # --- environment -------------------------------------------------------------------
    def kubeconfig(self) -> str:
        if self.profile is not None:
            stored = self.profile.platform.get("kubeconfig_path") or get_store().kubeconfig_path(
                self.profile.name
            )
            if Path(stored).exists():
                return str(stored)
        if self.settings.kubeconfig_path and Path(self.settings.kubeconfig_path).exists():
            return self.settings.kubeconfig_path
        if self.settings.kubeconfig_inline:
            import base64

            path = Path(self.settings.state_dir) / "kubeconfig" / "env.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(base64.b64decode(self.settings.kubeconfig_inline))
            return str(path)
        raise ConfigError(
            "helm needs a kubeconfig file and none is available",
            "Run ovh_kubeconfig_fetch(profile=…) first, or set SIMPL_MCP_KUBECONFIG.",
        )

    def _env(self) -> dict[str, str]:
        home = Path(self.settings.state_dir) / "helm"
        (home / "cache").mkdir(parents=True, exist_ok=True)
        (home / "config").mkdir(parents=True, exist_ok=True)
        (home / "data").mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "KUBECONFIG": self.kubeconfig(),
                "HELM_CACHE_HOME": str(home / "cache"),
                "HELM_CONFIG_HOME": str(home / "config"),
                "HELM_DATA_HOME": str(home / "data"),
            }
        )
        return env

    # --- execution ---------------------------------------------------------------------
    async def run(self, *args: str, timeout: float | None = None) -> HelmResult:
        if not shutil.which(self.binary) and not Path(self.binary).exists():
            raise ConfigError(
                f"helm binary '{self.settings.helm_bin}' not found",
                "The container image ships helm; locally, install it and/or set HELM_BIN.",
            )
        cmd = [self.binary, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout or self.settings.long_timeout
            )
        except TimeoutError:
            proc.kill()
            raise UpstreamError(
                "helm",
                None,
                f"'{' '.join(args[:3])}' exceeded {timeout or self.settings.long_timeout}s",
                "Re-run with wait=false and watch the rollout with k8s_pod_health instead.",
            ) from None
        return HelmResult(
            command=cmd,
            returncode=proc.returncode or 0,
            stdout=stdout.decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
        )

    # --- operations --------------------------------------------------------------------
    async def repo_add(self, name: str, url: str) -> HelmResult:
        return (await self.run("repo", "add", name, url, "--force-update", timeout=120)).raise_for_status()

    async def repo_update(self) -> HelmResult:
        return (await self.run("repo", "update", timeout=180)).raise_for_status()

    async def list_releases(self, namespace: str | None = None) -> list[dict[str, Any]]:
        args = ["list", "--output", "json"]
        args += ["--namespace", namespace] if namespace else ["--all-namespaces"]
        result = (await self.run(*args, timeout=120)).raise_for_status()
        import json

        return json.loads(result.stdout or "[]")

    async def status(self, release: str, namespace: str) -> dict[str, Any]:
        import json

        result = (
            await self.run("status", release, "--namespace", namespace, "--output", "json", timeout=120)
        ).raise_for_status()
        return json.loads(result.stdout or "{}")

    async def get_values(self, release: str, namespace: str) -> dict[str, Any]:
        import json

        result = (
            await self.run(
                "get", "values", release, "--namespace", namespace, "--output", "json", timeout=120
            )
        ).raise_for_status()
        return json.loads(result.stdout or "{}")

    async def upgrade_install(
        self,
        release: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
        version: str | None = None,
        repo: str | None = None,
        create_namespace: bool = True,
        wait: bool = False,
        timeout_seconds: int = 600,
        dry_run: bool = False,
        extra_args: list[str] | None = None,
    ) -> HelmResult:
        args = ["upgrade", "--install", release, chart, "--namespace", namespace]
        if create_namespace:
            args.append("--create-namespace")
        if version:
            args += ["--version", version]
        if repo:
            args += ["--repo", repo]
        if wait:
            args += ["--wait", "--timeout", f"{timeout_seconds}s"]
        if dry_run:
            args += ["--dry-run"]
        args += ["--output", "json"] if not dry_run else []
        values_file: str | None = None
        if values:
            fd, values_file = tempfile.mkstemp(suffix=".yaml", prefix="helm-values-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(values, fh, sort_keys=False)
            args += ["--values", values_file]
        if extra_args:
            args += extra_args
        try:
            return await self.run(*args, timeout=timeout_seconds + 60 if wait else 300)
        finally:
            if values_file:
                Path(values_file).unlink(missing_ok=True)

    async def uninstall(self, release: str, namespace: str, wait: bool = False) -> HelmResult:
        args = ["uninstall", release, "--namespace", namespace]
        if wait:
            args.append("--wait")
        return await self.run(*args, timeout=300)

    async def template(
        self,
        release: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
        version: str | None = None,
    ) -> HelmResult:
        args = ["template", release, chart, "--namespace", namespace]
        if version:
            args += ["--version", version]
        values_file: str | None = None
        if values:
            fd, values_file = tempfile.mkstemp(suffix=".yaml", prefix="helm-values-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(values, fh, sort_keys=False)
            args += ["--values", values_file]
        try:
            return await self.run(*args, timeout=180)
        finally:
            if values_file:
                Path(values_file).unlink(missing_ok=True)


def _hint_for(output: str) -> str | None:
    lowered = output.lower()
    if "another operation" in lowered and "in progress" in lowered:
        return (
            "A previous helm operation on this release is stuck. Check with helm_list, then "
            "roll back or delete the pending release secret."
        )
    if "cannot re-use a name" in lowered:
        return "A release with that name exists in another namespace. Pass the right namespace."
    if "connection refused" in lowered or "no such host" in lowered:
        return "The cluster is unreachable. Confirm with k8s_cluster_info."
    if "unauthorized" in lowered or "forbidden" in lowered:
        return "The kubeconfig lacks rights. Re-fetch it with ovh_kubeconfig_fetch."
    if "timed out waiting for the condition" in lowered:
        return (
            "The chart installed but its pods did not become ready in time. Inspect with "
            "k8s_pod_health; the release itself is deployed."
        )
    return None


async def runner_for(profile_name: str | None) -> HelmRunner:
    try:
        profile = get_store().resolve(profile_name)
    except Exception:
        if profile_name:
            raise
        profile = None
    return HelmRunner(profile)
