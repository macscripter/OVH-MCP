"""The two manifests that install Simpl-Open. Their shape is fixed by the charts."""

import pytest
import yaml

from simpl_ovh_mcp.errors import NotFound
from simpl_ovh_mcp.simpl.values import (
    agent_values,
    application_manifest,
    common_values,
    deep_merge,
    validate_agent_namespace,
)


def test_agent_names_reject_hyphens():
    validate_agent_namespace("authority01")
    with pytest.raises(NotFound) as exc:
        validate_agent_namespace("authority-01")
    assert "hostname" in str(exc.value).lower() or "Hyphens" in str(exc.value)


def test_common_values_carry_the_branch_and_agent_lists():
    values = common_values(
        namespace="common01",
        domain_suffix="simpl.example.eu",
        version="4.0.9",
        agents={"authorities": ["authority01"]},
    )
    assert values["values"]["branch"] == "v4.0.9"
    assert values["namespaceTag"] == "common01"  # a string, not a map
    assert values["agentList"]["authorities"] == ["authority01"]
    assert values["agentList"]["consumers"] == []
    assert values["cluster"]["issuer"] == "dev-prod-http"
    assert values["cluster"]["internalIssuer"] == "dev-selfsigned"


def test_authority_values_use_a_namespace_tag_map():
    values = agent_values(
        agent_type="authority",
        namespace="authority01",
        common_namespace="common01",
        domain_suffix="simpl.example.eu",
        version="4.0.4",
    )
    assert values["namespaceTag"] == {"authority": "authority01", "common": "common01"}
    assert values["cluster"]["commonToolsNamespace"] == "common01"
    assert values["authority_iaa"]["extraValues"]["development"]["enableAutomaticInitialization"]


def test_data_provider_uses_its_own_tag_key_and_needs_an_authority():
    values = agent_values(
        agent_type="data-provider",
        namespace="provider01",
        common_namespace="common01",
        domain_suffix="simpl.example.eu",
        version="4.0.6",
        authority_namespace="authority01",
    )
    # The chart calls itself 'dataprovider', one word.
    assert values["namespaceTag"]["dataprovider"] == "provider01"
    assert values["namespaceTag"]["authority"] == "authority01"
    assert values["authorityDomainSuffix"] == "simpl.example.eu"


def test_consumer_without_an_authority_is_refused():
    with pytest.raises(NotFound):
        agent_values(
            agent_type="consumer",
            namespace="consumer01",
            common_namespace="common01",
            domain_suffix="simpl.example.eu",
            version="4.0.5",
        )


def test_application_points_at_the_right_gitlab_project():
    manifest = application_manifest(
        name="common01-deployer",
        namespace="common01",
        chart_key="common",
        version="4.0.9",
        values={"a": 1},
    )
    source = manifest["spec"]["source"]
    # Project 951, not 1402: 1402 publishes authority-iaa, and that mistake is in the
    # repository's own sample deployer.
    assert source["repoURL"].endswith("/projects/951/packages/helm/stable")
    assert source["chart"] == "common_components"
    assert manifest["spec"]["syncPolicy"]["automated"]["prune"] is False
    assert "ServerSideApply=true" in manifest["spec"]["syncPolicy"]["syncOptions"]
    assert yaml.safe_load(source["helm"]["values"]) == {"a": 1}


def test_deep_merge_replaces_leaves_and_recurses_into_maps():
    base = {"a": {"b": 1, "c": 2}, "d": [1]}
    deep_merge(base, {"a": {"c": 3}, "d": [2]})
    assert base == {"a": {"b": 1, "c": 3}, "d": [2]}


def test_agent_lists_merge_without_duplicates():
    from simpl_ovh_mcp.simpl.tools import _merge_agent_lists

    merged = _merge_agent_lists(
        {"authorities": ["authority01"], "consumers": [], "providers": []},
        {"authorities": ["authority01", "authority02"], "consumers": ["consumer01"]},
    )
    assert merged == {
        "authorities": ["authority01", "authority02"],
        "consumers": ["consumer01"],
        "providers": [],
    }
    assert _merge_agent_lists({"authorities": [], "consumers": [], "providers": []}, None) == {
        "authorities": [],
        "consumers": [],
        "providers": [],
    }
