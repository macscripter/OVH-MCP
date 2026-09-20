"""OVH request signing, which is unforgiving about exactly what is hashed."""

import hashlib

import pytest

from simpl_ovh_mcp.errors import ConfigError
from simpl_ovh_mcp.ovh.client import ENDPOINTS, OvhClient
from simpl_ovh_mcp.settings import Settings


def _client(**over) -> OvhClient:
    base = dict(
        ovh_endpoint="ovh-eu",
        ovh_application_key="AK",
        ovh_application_secret="AS",
        ovh_consumer_key="CK",
    )
    base.update(over)
    return OvhClient(Settings(**base))


@pytest.mark.asyncio
async def test_signature_matches_the_documented_formula(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_server_time_delta", _const(0))
    monkeypatch.setattr("time.time", lambda: 1_700_000_000)

    url = "https://eu.api.ovh.com/1.0/cloud/project"
    headers = await client._headers("GET", url, "")

    expected = hashlib.sha1(
        "+".join(["AS", "CK", "GET", url, "", "1700000000"]).encode()
    ).hexdigest()
    assert headers["X-Ovh-Signature"] == f"$1${expected}"
    assert headers["X-Ovh-Application"] == "AK"
    assert headers["X-Ovh-Consumer"] == "CK"
    assert headers["X-Ovh-Timestamp"] == "1700000000"


@pytest.mark.asyncio
async def test_the_body_is_part_of_the_signature(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_server_time_delta", _const(0))
    monkeypatch.setattr("time.time", lambda: 1_700_000_000)
    url = "https://eu.api.ovh.com/1.0/cloud/project/x/kube"
    empty = await client._headers("POST", url, "")
    with_body = await client._headers("POST", url, '{"region":"GRA11"}')
    assert empty["X-Ovh-Signature"] != with_body["X-Ovh-Signature"]


@pytest.mark.asyncio
async def test_clock_drift_is_applied(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_server_time_delta", _const(45))
    monkeypatch.setattr("time.time", lambda: 1_700_000_000)
    headers = await client._headers("GET", "https://eu.api.ovh.com/1.0/me", "")
    assert headers["X-Ovh-Timestamp"] == "1700000045"


@pytest.mark.asyncio
async def test_oauth2_wins_when_both_are_configured():
    client = _client(ovh_client_id="id", ovh_client_secret="secret")
    assert client.settings.ovh_auth_mode == "oauth2"


def test_missing_credentials_name_the_variables():
    client = OvhClient(Settings())
    with pytest.raises(ConfigError) as exc:
        client._require_credentials()
    assert "OVH_APPLICATION_KEY" in str(exc.value)


def test_unknown_endpoint_is_refused():
    with pytest.raises(ConfigError):
        OvhClient(Settings(ovh_endpoint="ovh-mars"))


def test_every_endpoint_has_an_api_url():
    assert all(e["api"].startswith("https://") for e in ENDPOINTS.values())


def test_project_requires_a_service_name():
    client = _client()
    with pytest.raises(ConfigError) as exc:
        client.project(None)
    assert "OVH_CLOUD_PROJECT" in str(exc.value)
    assert client.project("abc123") == "abc123"


def _const(value):
    async def _inner():
        return value

    return _inner


def test_pull_secret_manifest_is_a_valid_dockerconfigjson():
    """The one place a registry password is handled must produce what kubelet expects."""
    import base64
    import json

    from simpl_ovh_mcp.ovh.tools import _pull_secret_manifest

    m = _pull_secret_manifest(
        "ovh-registry-pull",
        "authority01",
        "abc.c1.gra.container-registry.ovh.net",
        "simpl-pull",
        "s3cr3t",
    )
    assert m["type"] == "kubernetes.io/dockerconfigjson"
    assert m["metadata"] == {
        "name": "ovh-registry-pull",
        "namespace": "authority01",
        "annotations": {"simpl-ovh-mcp/registry": "abc.c1.gra.container-registry.ovh.net"},
    }
    config = json.loads(base64.b64decode(m["data"][".dockerconfigjson"]))
    entry = config["auths"]["abc.c1.gra.container-registry.ovh.net"]
    assert entry["username"] == "simpl-pull"
    assert entry["password"] == "s3cr3t"
    assert base64.b64decode(entry["auth"]).decode() == "simpl-pull:s3cr3t"
