"""Errors that say what to do next.

Every failure a tool can produce is one of these. They all carry a `hint`: the operator
reading the transcript should learn the remedy from the error itself, not from a later
round-trip. FastMCP turns ToolError messages into the tool's visible output, so these
strings are part of the product.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError


class SimplMcpError(ToolError):
    """Base class. Rendered as '<message> — <hint>'."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        self.hint = hint
        super().__init__(f"{message} — {hint}" if hint else message)


class ConfigError(SimplMcpError):
    """The server is missing configuration it cannot invent."""


class PermissionError_(SimplMcpError):
    """The deployment's mode forbids this class of operation."""


class ConfirmationRequired(SimplMcpError):
    """A destructive tool was called without the token from its dry run."""


class NotFound(SimplMcpError):
    """A named thing (profile, cluster, release, application) does not exist."""


class UpstreamError(SimplMcpError):
    """An API we depend on refused or failed."""

    def __init__(
        self,
        service: str,
        status: int | None,
        message: str,
        hint: str | None = None,
        detail: object | None = None,
    ) -> None:
        self.service = service
        self.status = status
        self.detail = detail
        prefix = f"{service} returned {status}" if status else f"{service} failed"
        super().__init__(f"{prefix}: {message}", hint)


# Hints for upstream statuses that mean something specific in this context. Kept here so
# that the OVH, Kubernetes and ArgoCD clients all explain themselves the same way.
OVH_HINTS = {
    401: "The credentials are refused. Check OVH_APPLICATION_KEY/SECRET/CONSUMER_KEY (or "
    "OVH_CLIENT_ID/SECRET) and that the endpoint matches the account region.",
    403: "Authenticated but not entitled. The consumer key or service account needs rights on "
    "this path — most often GET/POST/PUT/DELETE on /cloud/project/* and /domain/zone/*.",
    404: "No such resource. Check the cloud project id (OVH_CLOUD_PROJECT) and the cluster id.",
    409: "The resource is busy — usually another operation is still running on the cluster. "
    "Poll ovh_kube_get until status is READY.",
    429: "Rate limited by the OVH API. Wait a few seconds and retry.",
}

KUBE_HINTS = {
    401: "The kubeconfig is rejected. Fetch a fresh one with ovh_kubeconfig_fetch; OVH rotates "
    "the admin certificate when the cluster is reset.",
    403: "The kubeconfig's identity lacks RBAC rights for this resource.",
    404: "No such object, or the API group is not installed in this cluster (a CRD like "
    "argoproj.io/Application only exists once ArgoCD is installed).",
}
