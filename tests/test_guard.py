"""The permission model is the part of this server that must not be clever."""

import pytest

from simpl_ovh_mcp.errors import ConfirmationRequired, PermissionError_
from simpl_ovh_mcp.guard import Guard, _redact
from simpl_ovh_mcp.settings import Settings


def _settings(**over) -> Settings:
    base = dict(mode="admin", allow_destructive=True)
    base.update(over)
    return Settings(**base)


def test_readonly_refuses_writes():
    guard = Guard(_settings(mode="readonly", allow_destructive=False))
    with pytest.raises(PermissionError_):
        guard.require_write("ovh_kube_create")


def test_operate_allows_writes_but_not_destruction():
    guard = Guard(_settings(mode="operate", allow_destructive=True))
    guard.require_write("ovh_kube_create")
    with pytest.raises(PermissionError_):
        guard.require_destructive("ovh_kube_delete")


def test_admin_without_the_flag_still_refuses():
    guard = Guard(_settings(mode="admin", allow_destructive=False))
    with pytest.raises(PermissionError_) as exc:
        guard.require_destructive("ovh_kube_delete")
    assert "SIMPL_MCP_ALLOW_DESTRUCTIVE" in str(exc.value)


def test_token_is_single_use():
    guard = Guard(_settings())
    token = guard.issue_token("cluster/abc", "deletes everything")
    guard.check_token("ovh_kube_delete", "cluster/abc", token)
    with pytest.raises(ConfirmationRequired):
        guard.check_token("ovh_kube_delete", "cluster/abc", token)


def test_token_is_bound_to_its_target():
    guard = Guard(_settings())
    token = guard.issue_token("cluster/test", "deletes the test cluster")
    with pytest.raises(ConfirmationRequired) as exc:
        guard.check_token("ovh_kube_delete", "cluster/production", token)
    assert "cluster/test" in str(exc.value)


def test_missing_token_explains_the_two_phase_flow():
    guard = Guard(_settings())
    with pytest.raises(ConfirmationRequired) as exc:
        guard.check_token("ovh_kube_delete", "cluster/abc", None)
    assert "without `confirm`" in str(exc.value)


def test_audit_survives_and_redacts():
    guard = Guard(_settings())
    guard.audit("helm_install", "ns/release", "ok", {"password": "hunter2", "chart": "authority"})
    entries = guard.read_audit()
    assert entries[-1]["tool"] == "helm_install"
    assert entries[-1]["detail"]["password"] == "«redacted»"
    assert entries[-1]["detail"]["chart"] == "authority"


def test_redaction_is_recursive_and_keeps_shape():
    scrubbed = _redact({"a": {"clientSecret": "x", "list": [{"token": "y", "name": "n"}]}})
    assert scrubbed["a"]["clientSecret"] == "«redacted»"
    assert scrubbed["a"]["list"][0]["token"] == "«redacted»"
    assert scrubbed["a"]["list"][0]["name"] == "n"
