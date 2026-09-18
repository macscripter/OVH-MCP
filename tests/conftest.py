import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from simpl_ovh_mcp import guard, state
from simpl_ovh_mcp import settings as settings_module


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets its own state directory and a known configuration."""
    monkeypatch.setenv("SIMPL_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIMPL_MCP_MODE", "admin")
    monkeypatch.setenv("SIMPL_MCP_ALLOW_DESTRUCTIVE", "true")
    monkeypatch.delenv("SIMPL_MCP_TOOL_GROUPS", raising=False)
    monkeypatch.delenv("SIMPL_MCP_BEARER_TOKEN", raising=False)
    settings_module.reset_settings_for_tests(settings_module.Settings.from_env())
    guard.reset_guard_for_tests()
    state.reset_store_for_tests(tmp_path)
    yield
    settings_module.reset_settings_for_tests(None)
    guard.reset_guard_for_tests()
    state.reset_store_for_tests(None)
