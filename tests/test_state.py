"""Profiles: the server's memory between conversations."""

import pytest

from simpl_ovh_mcp.errors import NotFound
from simpl_ovh_mcp.state import Profile, ProfileStore


def test_a_profile_round_trips(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(Profile(name="ovh-dev", domain_suffix="simpl.example.eu", region="GRA11"))
    loaded = store.get("ovh-dev")
    assert loaded.domain_suffix == "simpl.example.eu"
    assert loaded.region == "GRA11"
    assert store.list() == ["ovh-dev"]


def test_hostnames_follow_component_namespace_suffix(tmp_path):
    profile = Profile(name="p", domain_suffix="simpl.example.eu")
    assert profile.fqdn("authority.fe", "authority01") == "authority.fe.authority01.simpl.example.eu"


def test_a_profile_without_a_domain_explains_itself():
    with pytest.raises(NotFound) as exc:
        Profile(name="p").fqdn("authority.fe", "authority01")
    assert "domainSuffix" in str(exc.value)


def test_resolve_falls_back_to_the_active_profile(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(Profile(name="a"))
    store.save(Profile(name="b"))
    store.set_active("b")
    assert store.resolve(None).name == "b"
    assert store.resolve("a").name == "a"


def test_resolving_nothing_lists_what_exists(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(Profile(name="only-one"))
    with pytest.raises(NotFound) as exc:
        store.resolve(None)
    assert "only-one" in str(exc.value)


def test_kubeconfigs_are_written_private(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(Profile(name="p"))
    path = store.write_kubeconfig("p", "apiVersion: v1\n")
    assert path.stat().st_mode & 0o777 == 0o600


def test_deleting_a_profile_removes_its_credentials(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(Profile(name="p"))
    path = store.write_kubeconfig("p", "apiVersion: v1\n")
    store.delete("p")
    assert not path.exists()
    assert store.list() == []


def test_authority_namespaces_are_derived_from_the_agent_map():
    profile = Profile(name="p", agents={"authority01": "authority", "consumer01": "consumer"})
    assert profile.authority_namespaces() == ["authority01"]
