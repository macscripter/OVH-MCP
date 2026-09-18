"""Registration helpers.

A tool is registered through one of three decorators that say what it does to the world:
`read`, `write` or `destructive`. The decorator applies the matching tags and — this is
the point — leaves the tool unregistered when the deployment's mode does not allow it. A
read-only server does not merely refuse `ovh_kube_delete`; it never advertises it, so the
model does not spend a turn discovering the refusal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from .settings import Settings


class Toolkit:
    def __init__(self, mcp: FastMCP, settings: Settings, group: str) -> None:
        self.mcp = mcp
        self.settings = settings
        self.group = group
        self.registered: list[str] = []
        self.skipped: list[str] = []

    def _register(self, fn: Callable[..., Any], kind: str, **kwargs: Any) -> Callable[..., Any]:
        tags = {self.group, kind} | set(kwargs.pop("tags", set()))
        annotations = {
            "readOnlyHint": kind == "read",
            "destructiveHint": kind == "destructive",
            "idempotentHint": kwargs.pop("idempotent", kind == "read"),
            "openWorldHint": True,
        }
        self.mcp.tool(fn, tags=tags, annotations=annotations, **kwargs)
        self.registered.append(fn.__name__)
        return fn

    def read(self, fn: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
        def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
            return self._register(f, "read", **kwargs)

        return wrap(fn) if fn else wrap

    def write(self, fn: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
        def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
            if not self.settings.writes_allowed:
                self.skipped.append(f.__name__)
                return f
            return self._register(f, "write", **kwargs)

        return wrap(fn) if fn else wrap

    def destructive(self, fn: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
        def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
            if self.settings.mode != "admin":
                self.skipped.append(f.__name__)
                return f
            return self._register(f, "destructive", **kwargs)

        return wrap(fn) if fn else wrap
