"""Permission model, two-phase confirmation and the audit trail.

The rules, in one place:

* `readonly` may call anything tagged read. `operate` adds writes that create or change
  things. `admin` is the only mode that can destroy, and only when the operator has also
  set SIMPL_MCP_ALLOW_DESTRUCTIVE — two independent switches, because an agent can talk
  its way past one sentence in a prompt but not past an environment variable.
* Destructive tools are two-phase. Called without `confirm`, they return an impact
  description and a token. Called again with that token, they act. The token is bound to
  the exact target, expires, and is single-use, so "delete the test cluster" cannot be
  replayed onto the production one.
* Every write attempt, allowed or refused, is appended to an audit log on the state
  volume. It is the only record that survives a conversation.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfirmationRequired, PermissionError_
from .settings import Settings, get_settings

TOKEN_TTL_SECONDS = 600


@dataclass
class _Pending:
    target: str
    impact: str
    issued_at: float


class Guard:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._pending: dict[str, _Pending] = {}

    # --- permissions ------------------------------------------------------------------
    def require_write(self, tool: str) -> None:
        if not self.settings.writes_allowed:
            raise PermissionError_(
                f"{tool} changes state and this server runs in '{self.settings.mode}' mode",
                "Set SIMPL_MCP_MODE=operate (or admin) on the deployment to allow writes.",
            )

    def require_destructive(self, tool: str) -> None:
        if self.settings.mode != "admin":
            raise PermissionError_(
                f"{tool} destroys resources and this server runs in '{self.settings.mode}' mode",
                "Destructive tools need SIMPL_MCP_MODE=admin.",
            )
        if not self.settings.allow_destructive:
            raise PermissionError_(
                f"{tool} destroys resources and destructive operations are switched off",
                "Set SIMPL_MCP_ALLOW_DESTRUCTIVE=true on the deployment, then retry. Turning it "
                "back off afterwards is the cheapest safety net this server has.",
            )

    # --- two-phase confirmation -------------------------------------------------------
    def issue_token(self, target: str, impact: str) -> str:
        token = f"confirm-{secrets.token_hex(6)}"
        self._pending[token] = _Pending(target=target, impact=impact, issued_at=time.time())
        self._sweep()
        return token

    def check_token(self, tool: str, target: str, confirm: str | None) -> None:
        if not confirm:
            raise ConfirmationRequired(
                f"{tool} needs confirmation",
                "Call it once without `confirm` to see the impact and receive a token, then "
                "call it again passing that token as `confirm`.",
            )
        self._sweep()
        pending = self._pending.get(confirm)
        if pending is None:
            raise ConfirmationRequired(
                "That confirmation token is unknown or has expired",
                f"Tokens last {TOKEN_TTL_SECONDS // 60} minutes and are single-use. Re-run the "
                "tool without `confirm` to get a fresh one.",
            )
        if pending.target != target:
            del self._pending[confirm]
            raise ConfirmationRequired(
                f"That token was issued for '{pending.target}', not for '{target}'",
                "A token is bound to one exact target. Re-run without `confirm` for this target.",
            )
        del self._pending[confirm]

    def _sweep(self) -> None:
        cutoff = time.time() - TOKEN_TTL_SECONDS
        for token, pending in list(self._pending.items()):
            if pending.issued_at < cutoff:
                del self._pending[token]

    # --- audit ------------------------------------------------------------------------
    def audit(self, tool: str, target: str, outcome: str, detail: dict[str, Any] | None = None) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool,
            "target": target,
            "outcome": outcome,
            "mode": self.settings.mode,
        }
        if detail:
            record["detail"] = _redact(detail)
        path = Path(self.settings.state_dir) / "audit.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            # An unwritable volume must not stop an operation that is otherwise fine.
            pass

    def read_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        path = Path(self.settings.state_dir) / "audit.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


SECRET_HINTS = ("password", "secret", "token", "key", "credential", "cert", "auth")


def _redact(value: Any, _depth: int = 0) -> Any:
    """Best-effort scrub before anything reaches a log or a transcript.

    It is deliberately blunt: a key whose *name* suggests a credential loses its value,
    whatever that value is. False positives cost a reader nothing; a false negative is a
    leaked secret in someone's chat history.
    """
    if _depth > 8:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and any(h in k.lower() for h in SECRET_HINTS):
                out[k] = "«redacted»" if v not in (None, "", {}, []) else v
            else:
                out[k] = _redact(v, _depth + 1)
        return out
    if isinstance(value, list):
        return [_redact(v, _depth + 1) for v in value]
    return value


_guard: Guard | None = None


def get_guard() -> Guard:
    global _guard
    if _guard is None:
        _guard = Guard()
    return _guard


def reset_guard_for_tests() -> None:
    global _guard
    _guard = None


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")
