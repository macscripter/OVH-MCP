"""Building the two manifests that install Simpl-Open.

Everything the platform deploys comes from two kinds of ArgoCD Application: one for the
common components and one per agent. Each carries a Helm values block, and the shape of
that block is fixed by the charts — `namespaceTag` is a string for the common components
and a map for an agent, the key inside that map is the agent's own word for itself
('authority', 'consumer', 'dataprovider'), and a consumer or provider also needs to know
the authority's domain.

Getting those three details wrong is the difference between a platform that comes up and a
namespace of pods waiting on hostnames that do not exist, so they are encoded here once,
from the chart sources of authority 4.0.4, consumer 4.0.5 and data-provider 4.0.6.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import NotFound
from .catalog import CHARTS, ISSUERS

AGENT_NAME = re.compile(r"^[a-z0-9]+$")

# The key each agent chart uses for itself inside `namespaceTag`.
NAMESPACE_TAG_KEY = {
    "authority": "authority",
    "consumer": "consumer",
    "data-provider": "dataprovider",
    "infrastructure-provider": "infrastructureprovider",
    "application-provider": "applicationprovider",
}

# Which agents belong in which list of the common chart's `agentList`.
AGENT_LIST_KEY = {
    "authority": "authorities",
    "consumer": "consumers",
    "data-provider": "providers",
    "infrastructure-provider": "providers",
    "application-provider": "providers",
}

SYNC_POLICY = {
    "automated": {"prune": False, "selfHeal": False},
    "syncOptions": [
        "ApplyOutOfSyncOnly=true",
        "CreateNamespace=true",
        "ServerSideApply=true",
    ],
}

KUBE_API = "https://kubernetes.default.svc"


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Merge override into base, recursing into dicts and replacing everything else."""
    if not override:
        return base
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def validate_agent_namespace(name: str) -> str:
    """Agent names become part of every hostname the agent publishes.

    Lower-case letters and digits only: a hyphen produces FQDNs the charts' own templates
    then fail to match, and the deployment guide states the rule in a single line that is
    easy to read past.
    """
    if not AGENT_NAME.match(name or ""):
        raise NotFound(
            f"'{name}' is not a valid Simpl-Open namespace tag",
            "Use lower-case letters and digits only — authority01, consumer01, provider01. "
            "Hyphens break the generated hostnames.",
        )
    return name


def common_values(
    *,
    namespace: str,
    domain_suffix: str,
    version: str,
    agents: dict[str, list[str]] | None = None,
    resource_preset: str = "low",
    issuer: str = ISSUERS["public"],
    internal_issuer: str = ISSUERS["internal"],
    secret_engine: str = "integrated",
    secret_role: str = "integrated-role",
    argocd_namespace: str = "argocd",
    project: str = "default",
    monitoring: bool = False,
    ha: bool = False,
    mailpit: bool = True,
    redpanda: bool = False,
    pg_admin: bool = False,
    pg_volume_size: str = "30Gi",
    kube_state_host: str | None = None,
    smtp: dict[str, Any] | None = None,
    extra_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The Helm values for the common components deployer."""
    validate_agent_namespace(namespace)
    lists = {"authorities": [], "consumers": [], "providers": []}
    deep_merge(lists, agents or {})
    for group in lists.values():
        for name in group:
            validate_agent_namespace(name)

    values: dict[str, Any] = {
        "values": {"branch": f"v{version}"},
        "resourcePreset": resource_preset,
        "agentList": lists,
        "project": project,
        "namespaceTag": namespace,
        "domainSuffix": domain_suffix,
        "argocd": {"appname": namespace, "namespace": argocd_namespace},
        "cluster": {
            "address": KUBE_API,
            "namespace": namespace,
            "issuer": issuer,
            "internalIssuer": internal_issuer,
        },
        "secrets": {"secretEngine": secret_engine, "role": secret_role},
        "monitoring": {"enabled": monitoring},
        "redpanda": {"enabled": redpanda},
        "pg_admin": {"enabled": pg_admin},
        "mailpit": {"enabled": mailpit},
        "kafka": {"ha": ha, "topic": {"autocreate": True}},
        "openbao": {"ha": ha, "replicas": 3 if ha else 1},
        "pg_cluster": {"ha": ha, "volumeSize": pg_volume_size},
    }
    if kube_state_host:
        values["cluster"]["kubeStateHost"] = kube_state_host
    if smtp:
        # Only meaningful with mailpit disabled; the password itself lives in OpenBao, in
        # the <namespace>-notifications secret, and never in these values.
        values["notification"] = {"mail": smtp}
    return deep_merge(values, extra_values)


def agent_values(
    *,
    agent_type: str,
    namespace: str,
    common_namespace: str,
    domain_suffix: str,
    version: str,
    authority_namespace: str | None = None,
    authority_domain_suffix: str | None = None,
    resource_preset: str = "low",
    issuer: str = ISSUERS["public"],
    secret_engine: str = "integrated",
    secret_role: str = "integrated-role",
    argocd_namespace: str = "argocd",
    project: str = "default",
    monitoring: bool = False,
    eidas: bool = False,
    automatic_initialization: bool = True,
    identifier_number: str | None = None,
    extra_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The Helm values for one agent deployer."""
    if agent_type not in NAMESPACE_TAG_KEY:
        raise NotFound(
            f"unknown agent type '{agent_type}'",
            f"Known types: {', '.join(sorted(NAMESPACE_TAG_KEY))}.",
        )
    validate_agent_namespace(namespace)
    validate_agent_namespace(common_namespace)

    tag_key = NAMESPACE_TAG_KEY[agent_type]
    namespace_tag: dict[str, str] = {tag_key: namespace, "common": common_namespace}
    values: dict[str, Any] = {
        "values": {"branch": f"v{version}"},
        "project": project,
        "namespaceTag": namespace_tag,
        "domainSuffix": domain_suffix,
        "resourcePreset": resource_preset,
        "argocd": {"appname": namespace, "namespace": argocd_namespace},
        "cluster": {
            "address": KUBE_API,
            "namespace": namespace,
            "commonToolsNamespace": common_namespace,
            "issuer": issuer,
        },
        "secrets": {"role": secret_role, "secretEngine": secret_engine},
    }

    if agent_type == "authority":
        if identifier_number:
            values["identifierNumber"] = identifier_number
        values["authority_monitoring"] = {"enabled": monitoring}
        values["authority_iaa"] = {
            "extraValues": {
                "eidas": {"enabled": eidas},
                "development": {"enableAutomaticInitialization": automatic_initialization},
            }
        }
    else:
        # Consumers and providers must be told where the authority lives: they fetch their
        # trust material and their catalogue from it, by hostname.
        if not authority_namespace:
            raise NotFound(
                f"a {agent_type} agent needs the authority's namespace",
                "Pass authority_namespace=… (for example authority01).",
            )
        validate_agent_namespace(authority_namespace)
        namespace_tag["authority"] = authority_namespace
        values["authorityDomainSuffix"] = authority_domain_suffix or domain_suffix
        values[f"{tag_key}_monitoring"] = {"enabled": monitoring}

    return deep_merge(values, extra_values)


def application_manifest(
    *,
    name: str,
    namespace: str,
    chart_key: str,
    version: str,
    values: dict[str, Any],
    argocd_namespace: str = "argocd",
    project: str = "default",
) -> dict[str, Any]:
    """Wrap a values block in the ArgoCD Application that deploys it.

    The sync policy is the one the working deployers use: automated, but with prune and
    self-heal off. Simpl-Open's App-of-Apps generates resources that a prune would fight
    with, and self-heal makes a manual fix impossible to keep.
    """
    ref = CHARTS.get(chart_key)
    if ref is None or not ref.chart:
        raise NotFound(
            f"no chart published for '{chart_key}'",
            f"Known charts: {', '.join(k for k, c in CHARTS.items() if c.chart)}.",
        )
    import yaml

    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": name, "namespace": argocd_namespace},
        "spec": {
            "project": project,
            "destination": {"server": KUBE_API, "namespace": namespace},
            "syncPolicy": SYNC_POLICY,
            "source": {
                "repoURL": ref.repo_url,
                "chart": ref.chart,
                "targetRevision": version,
                "helm": {"values": yaml.safe_dump(values, sort_keys=False)},
            },
        },
    }
