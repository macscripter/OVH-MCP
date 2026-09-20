"""The Bridge's values and Secrets for the publication path: names in, never values out."""

import base64

from simpl_ovh_mcp.bridge.tools import (
    _build_values,
    _database_secret_manifest,
    _database_user,
    _dome_secret_manifest,
    _looks_like_secret,
    _operator_secret_name,
    _publication_values,
)


def _values(**kw):
    base = dict(
        common_namespace="common01",
        image_tag="1.0.0",
        image_registry="reg.example",
        image_repository="simpl-open/bridge",
        cache_enabled=True,
        elk_metrics=False,
        replicas=1,
        resource_preset="small",
        dome_base_url="https://dome.example/tmf-api/productCatalogManagement",
    )
    base.update(kw)
    return _build_values(**base)


def test_read_only_values_carry_no_publication_wiring():
    v = _values()
    assert "extraEnvFrom" not in v
    assert "openbao" not in v
    assert "publication" not in (v.get("config") or {}).get("extraProperties", "")


def test_publication_wires_datasource_and_switches():
    v = _values(publication_secret="bridge-database")
    assert v["extraEnvFrom"] == [{"secretRef": {"name": "bridge-database"}}]
    props = v["config"]["extraProperties"]
    assert "bridge.publication.enabled=true" in props
    assert "bridge.publication.dispatcherEnabled=true" in props
    assert _looks_like_secret(v) == []


def test_dome_secret_is_referenced_by_name_only():
    v = _values(publication_secret="bridge-database", dome_secret="bridge-dome")
    ob = v["openbao"]
    assert ob["enabled"] is True and ob["mode"] == "existingSecret"
    assert ob["existingSecret"] == {"name": "bridge-dome", "keys": {"DOME_DEV_TOKEN": "token"}}
    # the ENV -> key map names things; it must not trip the credential check
    assert _looks_like_secret(v) == []


def test_publication_values_alone():
    assert _publication_values(None, None) == {}
    assert "openbao" not in _publication_values("db", None)
    assert "extraEnvFrom" not in _publication_values(None, "dome")


def test_operator_naming_follows_zalando():
    assert _database_user("authority01") == "authority01_bridge"
    assert _database_user("data-provider01") == "data_provider01_bridge"
    assert (
        _operator_secret_name("authority01_bridge")
        == "authority01-bridge.pg-cluster.credentials.postgresql.acid.zalan.do"
    )


def test_database_secret_passes_operator_encoding_through():
    u = base64.b64encode(b"authority01_bridge").decode()
    pw = base64.b64encode(b"s3cr3t").decode()
    m = _database_secret_manifest("bridge-database", "authority01", "jdbc:postgresql://h/db", u, pw)
    assert m["kind"] == "Secret" and m["metadata"]["namespace"] == "authority01"
    d = m["data"]
    assert base64.b64decode(d["QUARKUS_DATASOURCE_JDBC_URL"]) == b"jdbc:postgresql://h/db"
    assert d["QUARKUS_DATASOURCE_USERNAME"] == u and d["QUARKUS_DATASOURCE_PASSWORD"] == pw
    assert "s3cr3t" not in str(m)  # only ever base64, as the API expects


def test_dome_secret_manifest():
    m = _dome_secret_manifest("bridge-dome", "authority01", "tok-123")
    assert base64.b64decode(m["data"]["token"]) == b"tok-123"
    assert "tok-123" not in str(m)


def test_credential_check_still_catches_literal_values():
    v = _values(extra_values={"dome": {"auth": {"staticToken": "eyJ..."}}})
    assert any("staticToken" in x for x in _looks_like_secret(v))
