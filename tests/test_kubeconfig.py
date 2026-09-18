"""Kubeconfig parsing, and the TLS context that httpx 0.28 actually honours."""

import base64
import ssl

import pytest

from simpl_ovh_mcp.errors import ConfigError
from simpl_ovh_mcp.kube.kubeconfig import parse_kubeconfig

# A throwaway self-signed pair, generated for these tests only.
CERT = """-----BEGIN CERTIFICATE-----
MIIBejCCASGgAwIBAgIUJ0CoVNPqmVrBJHBMmKr7KXjNPl4wCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJbG9jYWx0ZXN0MB4XDTI2MDkxODAwMDAwMFoXDTM2MDkxNTAw
MDAwMFowFDESMBAGA1UEAwwJbG9jYWx0ZXN0MFkwEwYHKoZIzj0CAQYIKoZIzj0D
AQcDQgAEo1J3d0z8kkAiC5tG6d1oPQ0m3hVQ0HcRxUeGRQ8BQ0m3hVQ0HcRxUeGR
Q8BQ0m3hVQ0HcRxUeGRQ8BQ0m3hVaNTMFEwHQYDVR0OBBYEFPqmVrBJHBMmKr7KXj
NPl4wHwYDVR0jBBgwFoAU+qZWsEkcEyYqvspeM0+XjAPBgNVHRMBAf8EBTADAQH/
MAoGCCqGSM49BAMCA0cAMEQCIA==
-----END CERTIFICATE-----
"""


def _kubeconfig(user_block: str, server: str = "https://1.2.3.4:6443") -> str:
    ca = base64.b64encode(CERT.encode()).decode()
    return f"""
apiVersion: v1
clusters:
- name: simpl
  cluster:
    server: {server}
    certificate-authority-data: {ca}
users:
- name: admin
{user_block}
contexts:
- name: ctx
  context:
    cluster: simpl
    user: admin
current-context: ctx
"""


def test_certificate_based_kubeconfig_is_parsed():
    data = base64.b64encode(CERT.encode()).decode()
    target = parse_kubeconfig(
        _kubeconfig(f"  user:\n    client-certificate-data: {data}\n    client-key-data: {data}")
    )
    assert target.server == "https://1.2.3.4:6443"
    assert target.context == "ctx"
    assert target.describe()["auth"] == "client-certificate"
    assert target.ca_path and target.client_cert_path and target.client_key_path


def test_token_kubeconfig_produces_an_authorization_header():
    target = parse_kubeconfig(_kubeconfig("  user:\n    token: abc123"))
    assert target.auth_headers() == {"Authorization": "Bearer abc123"}
    assert target.describe()["auth"] == "token"


def test_exec_plugins_are_refused_with_an_explanation():
    with pytest.raises(ConfigError) as exc:
        parse_kubeconfig(
            _kubeconfig("  user:\n    exec:\n      command: aws\n      apiVersion: v1")
        )
    assert "exec plugin" in str(exc.value)
    assert "ServiceAccount token" in str(exc.value)


def test_insecure_context_disables_verification():
    target = parse_kubeconfig(_kubeconfig("  user:\n    token: t"))
    object.__setattr__(target, "insecure", True)
    context = target.ssl_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_NONE


def test_a_kubeconfig_without_clusters_is_rejected():
    with pytest.raises(ConfigError):
        parse_kubeconfig("apiVersion: v1\nkind: Config\n")
