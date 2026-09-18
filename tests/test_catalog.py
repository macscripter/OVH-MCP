"""The catalogue: what must stay true about the charts, and how the index is read."""

from simpl_ovh_mcp.simpl.catalog import (
    AGENT_TYPES,
    CHARTS,
    ISSUERS,
    PLATFORM,
    SIZINGS,
    TRAPS,
    _parse_index_versions,
)


def test_the_common_components_chart_is_project_951():
    # Project 1402 publishes authority-iaa. The repository's own sample deployer points
    # common_components at it, which is the first trap in the list.
    assert CHARTS["common"].project_id == 951
    assert CHARTS["common"].chart == "common_components"
    assert "951" in CHARTS["common"].repo_url


def test_every_agent_chart_has_a_project_and_a_description():
    for key in AGENT_TYPES:
        chart = CHARTS[key]
        assert chart.project_id > 0
        assert chart.description
        assert chart.repo_url.endswith("/packages/helm/stable")


def test_issuer_names_are_the_ones_the_charts_look_for():
    assert ISSUERS == {"public": "dev-prod-http", "internal": "dev-selfsigned"}


def test_platform_charts_carry_a_reason_each():
    for chart in PLATFORM.values():
        assert chart.why, f"{chart.key} has no explanation of why it is needed"
        assert chart.repo_url.startswith("https://")
        assert chart.version


def test_the_nfs_provisioner_publishes_the_class_the_claim_needs():
    nfs = PLATFORM["nfs-server-provisioner"]
    assert nfs.values["storageClass"]["name"] == "nfs"
    assert nfs.values["storageClass"]["defaultClass"] is False


def test_traps_are_actionable():
    for trap in TRAPS:
        assert trap.symptom and trap.cause and trap.remedy
        # A remedy that names no tool is a remedy nobody can follow.
        assert any(token in trap.remedy for token in ("simpl_", "ovh_", "k8s_", "argocd_", "Name "))


def test_sizings_are_internally_consistent():
    for sizing in SIZINGS.values():
        assert sizing.nodes >= 1
        assert sizing.to_dict()["total_vcpus"] == sizing.nodes * sizing.vcpus_per_node


def test_index_parsing_survives_a_thousand_feature_branch_builds():
    index = """
apiVersion: v1
entries:
  common_components:
  - name: common_components
    version: 4.0.9
    created: '2026-08-01T00:00:00Z'
  - name: common_components
    version: 0.0.1-F-feature-simpl-6539.1.76582634
  authority:
  - name: authority
    version: 4.0.4
"""
    parsed = _parse_index_versions(index)
    assert parsed["common_components"] == ["4.0.9", "0.0.1-F-feature-simpl-6539.1.76582634"]
    assert parsed["authority"] == ["4.0.4"]
