"""Deployment profiles: the server's memory between conversations.

A *profile* is one Simpl-Open deployment — the OVH project and cluster it lives on, the
domain its components address each other by, the namespaces, the chart versions, and the
platform pieces that have been put in place. Tools take `profile` instead of a dozen
repeated arguments, and a new conversation can pick up where the last one stopped.

Stored as one JSON file per profile under the state directory (a Railway volume in
production). Kubeconfigs are written next to them with 0600 and are never returned by a
tool unless the operator explicitly asks for them in admin mode.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import NotFound
from .settings import get_settings


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Profile:
    """One deployment target. Every field is optional until the step that fills it runs."""

    name: str

    # OVH
    ovh_project: str | None = None
    region: str | None = None
    kube_id: str | None = None
    cluster_name: str | None = None

    # Naming and DNS. `domain_suffix` is the Simpl-Open `domainSuffix` value: the last part
    # of every component's FQDN, and the thing that must resolve identically inside and
    # outside the cluster.
    domain_suffix: str | None = None
    dns_zone: str | None = None
    ingress_ip: str | None = None

    # Namespaces. Simpl-Open forbids '-' in agent names, so these are checked on write.
    common_namespace: str = "common01"
    agents: dict[str, str] = field(default_factory=dict)  # namespace -> agent type

    # Versions actually deployed (as opposed to what the catalogue currently offers).
    chart_versions: dict[str, str] = field(default_factory=dict)  # component -> version

    # Platform state, set by the bootstrap steps.
    platform: dict[str, Any] = field(default_factory=dict)

    # Bridge
    bridge: dict[str, Any] = field(default_factory=dict)

    # Free-form, so a tool can record something the schema did not foresee.
    notes: dict[str, Any] = field(default_factory=dict)

    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # --- derived ----------------------------------------------------------------------
    def authority_namespaces(self) -> list[str]:
        return [ns for ns, kind in self.agents.items() if kind == "authority"]

    def fqdn(self, host: str, namespace: str) -> str:
        """Component hostnames follow <host>.<namespace>.<domainSuffix>."""
        if not self.domain_suffix:
            raise NotFound(
                f"profile '{self.name}' has no domain_suffix",
                "Set one with simpl_profile_update; it is the `domainSuffix` Helm value and "
                "every component URL is built from it.",
            )
        return f"{host}.{namespace}.{self.domain_suffix}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class ProfileStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or get_settings().state_dir)
        self.profiles_dir = self.root / "profiles"
        self.kubeconfig_dir = self.root / "kubeconfig"

    # --- paths ------------------------------------------------------------------------
    def _path(self, name: str) -> Path:
        safe = _safe_name(name)
        return self.profiles_dir / f"{safe}.json"

    def kubeconfig_path(self, name: str) -> Path:
        return self.kubeconfig_dir / f"{_safe_name(name)}.yaml"

    # --- CRUD -------------------------------------------------------------------------
    def list(self) -> list[str]:
        if not self.profiles_dir.exists():
            return []
        return sorted(p.stem for p in self.profiles_dir.glob("*.json"))

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def get(self, name: str) -> Profile:
        path = self._path(name)
        if not path.exists():
            known = ", ".join(self.list()) or "none yet"
            raise NotFound(
                f"no profile named '{name}'",
                f"Known profiles: {known}. Create one with simpl_profile_create.",
            )
        return Profile.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, profile: Profile) -> Profile:
        profile.updated_at = _now()
        path = self._path(profile.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(profile.to_dict(), indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash mid-write never leaves a half profile
        return profile

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)
        self.kubeconfig_path(name).unlink(missing_ok=True)
        if self.active() == _safe_name(name):
            (self.root / "active.json").unlink(missing_ok=True)

    # --- active profile ---------------------------------------------------------------
    def active(self) -> str | None:
        path = self.root / "active.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("active")
        except (json.JSONDecodeError, OSError):
            return None

    def set_active(self, name: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "active.json").write_text(
            json.dumps({"active": _safe_name(name)}), encoding="utf-8"
        )

    def resolve(self, name: str | None) -> Profile:
        """`profile=None` means 'the active one', which keeps most calls to one argument."""
        if name:
            return self.get(name)
        active = self.active()
        if not active:
            known = ", ".join(self.list()) or "none yet"
            raise NotFound(
                "no profile given and none is active",
                f"Pass profile=<name> or call simpl_profile_use. Known profiles: {known}.",
            )
        return self.get(active)

    # --- kubeconfig -------------------------------------------------------------------
    def write_kubeconfig(self, name: str, content: str) -> Path:
        path = self.kubeconfig_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written 0600 before any content lands in it.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path


def _safe_name(name: str) -> str:
    cleaned = "".join(c for c in name.strip() if c.isalnum() or c in "-_.")
    if not cleaned:
        raise NotFound("profile names must contain letters, digits, '-', '_' or '.'")
    return cleaned


_store: ProfileStore | None = None


def get_store() -> ProfileStore:
    global _store
    if _store is None:
        _store = ProfileStore()
    return _store


def reset_store_for_tests(root: Path | None = None) -> None:
    global _store
    _store = ProfileStore(root) if root else None
