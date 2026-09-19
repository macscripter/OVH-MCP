"""Runtime configuration, read once from the environment.

Everything the server needs to know about *where* it runs and *what it is allowed to do*
lives here. Nothing else in the code reads os.environ, so a deployment is fully described
by this one file plus the values set on Railway (or in .env locally).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Tool groups. Each tool is registered under exactly one of these, and an operator can
# narrow the surface with SIMPL_MCP_TOOL_GROUPS when a client's context budget is tight.
ALL_GROUPS = ("meta", "ovh", "k8s", "helm", "argocd", "simpl", "bridge")

# Three modes, in increasing order of what they permit. The mode is a property of the
# deployment, not of a request: a read-only instance cannot be talked into writing.
MODES = ("readonly", "operate", "admin")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _default_state_dir() -> Path:
    """Railway volumes are mounted at /data; anywhere else we keep state beside the code.

    /data is used only when it already exists — creating it would silently scatter state
    across the filesystem root on a developer machine.
    """
    candidate = Path("/data")
    if candidate.is_dir() and os.access(candidate, os.W_OK):
        return candidate
    local = Path("./state")
    local.mkdir(parents=True, exist_ok=True)
    return local


@dataclass(frozen=True)
class Settings:
    # --- OVHcloud API -----------------------------------------------------------------
    # Two authentication schemes are supported and either one is enough:
    #   * the legacy triplet (application key / secret / consumer key), signed per request;
    #   * an OAuth2 service account (client id / secret), which OVH now recommends.
    ovh_endpoint: str = "ovh-eu"
    ovh_application_key: str | None = None
    ovh_application_secret: str | None = None
    ovh_consumer_key: str | None = None
    ovh_client_id: str | None = None
    ovh_client_secret: str | None = None
    ovh_cloud_project: str | None = None

    # --- Permissions ------------------------------------------------------------------
    mode: str = "operate"
    allow_destructive: bool = False
    tool_groups: tuple[str, ...] = ALL_GROUPS

    # --- Transport --------------------------------------------------------------------
    transport: str = "stdio"
    # How the transport was decided, for the startup banner. Debugging a server that
    # "starts fine and answers nothing" is a great deal faster when it says why.
    transport_source: str = "default"
    host: str = "0.0.0.0"
    port: int = 8000
    http_path: str = "/mcp"
    bearer_token: str | None = None
    allowed_hosts: tuple[str, ...] = ()

    # --- Kubernetes -------------------------------------------------------------------
    # A kubeconfig given here wins over anything fetched from OVH, which is how the server
    # is pointed at a local k3d cluster or at a cluster that OVH does not manage.
    kubeconfig_path: str | None = None
    kubeconfig_inline: str | None = None
    kube_insecure: bool = False

    # --- Binaries ---------------------------------------------------------------------
    helm_bin: str = "helm"

    # --- Bridge -----------------------------------------------------------------------
    bridge_chart_path: str | None = None
    bridge_image_repository: str = "simpl-open/bridge"
    bridge_image_registry: str = ""
    bridge_image_tag: str = ""
    dome_base_url: str = "https://tmf.sbx.evidenceledger.eu/tmf-api/productCatalogManagement"

    # --- Housekeeping -----------------------------------------------------------------
    state_dir: Path = field(default_factory=lambda: Path("./state"))
    http_timeout: float = 30.0
    long_timeout: float = 600.0

    # ----------------------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> Settings:
        mode = os.environ.get("SIMPL_MCP_MODE", "operate").strip().lower()
        if mode not in MODES:
            mode = "operate"

        groups = _csv("SIMPL_MCP_TOOL_GROUPS", ALL_GROUPS)
        groups = tuple(g for g in groups if g in ALL_GROUPS) or ALL_GROUPS
        if "meta" not in groups:
            groups = ("meta",) + groups

        # Railway injects PORT; honour it before our own variable so a deploy needs no
        # extra configuration to be reachable.
        port = _int("PORT", _int("SIMPL_MCP_PORT", 8000))

        # A process with a port to serve on is not a stdio process. Defaulting to stdio
        # regardless produced a server that started cleanly, logged nothing alarming and
        # answered no request at all — the platform's 502 being the only symptom. An
        # explicit SIMPL_MCP_TRANSPORT still wins.
        hosted = bool(
            os.environ.get("PORT")
            or os.environ.get("RAILWAY_SERVICE_ID")
            or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
        )
        explicit_transport = os.environ.get("SIMPL_MCP_TRANSPORT")
        if explicit_transport:
            transport = explicit_transport.strip().lower()
            transport_source = "SIMPL_MCP_TRANSPORT"
        elif hosted:
            transport = "http"
            transport_source = "inferred (PORT or RAILWAY_* present)"
        else:
            transport = "stdio"
            transport_source = "inferred (no port in the environment)"

        state_dir = os.environ.get("SIMPL_MCP_STATE_DIR")
        resolved_state = Path(state_dir) if state_dir else _default_state_dir()
        if state_dir:
            Path(state_dir).mkdir(parents=True, exist_ok=True)

        default_chart = Path(__file__).resolve().parents[2] / "vendor" / "charts" / "bridge"

        return cls(
            ovh_endpoint=os.environ.get("OVH_ENDPOINT", "ovh-eu").strip(),
            ovh_application_key=os.environ.get("OVH_APPLICATION_KEY") or None,
            ovh_application_secret=os.environ.get("OVH_APPLICATION_SECRET") or None,
            ovh_consumer_key=os.environ.get("OVH_CONSUMER_KEY") or None,
            ovh_client_id=os.environ.get("OVH_CLIENT_ID") or None,
            ovh_client_secret=os.environ.get("OVH_CLIENT_SECRET") or None,
            ovh_cloud_project=os.environ.get("OVH_CLOUD_PROJECT") or None,
            mode=mode,
            allow_destructive=_bool("SIMPL_MCP_ALLOW_DESTRUCTIVE", False),
            tool_groups=groups,
            transport=transport,
            transport_source=transport_source,
            host=os.environ.get("SIMPL_MCP_HOST", "0.0.0.0"),
            port=port,
            http_path=os.environ.get("SIMPL_MCP_PATH", "/mcp"),
            bearer_token=os.environ.get("SIMPL_MCP_BEARER_TOKEN") or None,
            allowed_hosts=_csv("SIMPL_MCP_ALLOWED_HOSTS", ()),
            kubeconfig_path=os.environ.get("SIMPL_MCP_KUBECONFIG")
            or os.environ.get("KUBECONFIG")
            or None,
            kubeconfig_inline=os.environ.get("SIMPL_MCP_KUBECONFIG_B64") or None,
            kube_insecure=_bool("SIMPL_MCP_KUBE_INSECURE", False),
            helm_bin=os.environ.get("HELM_BIN", "helm"),
            bridge_chart_path=os.environ.get("BRIDGE_CHART_PATH") or str(default_chart),
            bridge_image_repository=os.environ.get("BRIDGE_IMAGE_REPOSITORY", "simpl-open/bridge"),
            bridge_image_registry=os.environ.get("BRIDGE_IMAGE_REGISTRY", ""),
            bridge_image_tag=os.environ.get("BRIDGE_IMAGE_TAG", ""),
            dome_base_url=os.environ.get(
                "DOME_BASE_URL",
                "https://tmf.sbx.evidenceledger.eu/tmf-api/productCatalogManagement",
            ),
            state_dir=resolved_state,
            http_timeout=float(_int("SIMPL_MCP_HTTP_TIMEOUT", 30)),
            long_timeout=float(_int("SIMPL_MCP_LONG_TIMEOUT", 600)),
        )

    # --- Derived ----------------------------------------------------------------------
    @property
    def looks_hosted(self) -> bool:
        """Is there a platform in front of this process expecting it to serve HTTP?"""
        return bool(
            os.environ.get("PORT")
            or os.environ.get("RAILWAY_SERVICE_ID")
            or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
        )

    @property
    def has_ovh_credentials(self) -> bool:
        legacy = all((self.ovh_application_key, self.ovh_application_secret, self.ovh_consumer_key))
        oauth = all((self.ovh_client_id, self.ovh_client_secret))
        return bool(legacy or oauth)

    @property
    def ovh_auth_mode(self) -> str:
        if self.ovh_client_id and self.ovh_client_secret:
            return "oauth2"
        if self.ovh_application_key and self.ovh_application_secret and self.ovh_consumer_key:
            return "signature"
        return "none"

    @property
    def writes_allowed(self) -> bool:
        return self.mode in ("operate", "admin")

    @property
    def destructive_allowed(self) -> bool:
        return self.mode == "admin" and self.allow_destructive


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings_for_tests(settings: Settings | None = None) -> None:
    """Test seam. Production code never calls this."""
    global _settings
    _settings = settings
