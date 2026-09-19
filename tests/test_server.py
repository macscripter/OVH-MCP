"""Assembly: what the server advertises depends on what it is allowed to do."""

import pytest
from fastmcp import Client

from simpl_ovh_mcp import settings as settings_module
from simpl_ovh_mcp.server import build_server
from simpl_ovh_mcp.settings import Settings


async def _tool_names(settings: Settings) -> set[str]:
    settings_module.reset_settings_for_tests(settings)
    server = build_server(settings)
    async with Client(server) as client:
        return {tool.name for tool in await client.list_tools()}


@pytest.mark.asyncio
async def test_readonly_hides_writes_and_destruction(tmp_path):
    names = await _tool_names(Settings(mode="readonly", state_dir=tmp_path))
    assert "simpl_diagnose" in names
    assert "k8s_cluster_info" in names
    assert "ovh_kube_create" not in names
    assert "ovh_kube_delete" not in names


@pytest.mark.asyncio
async def test_operate_adds_writes_but_not_destruction(tmp_path):
    names = await _tool_names(Settings(mode="operate", state_dir=tmp_path))
    assert "ovh_kube_create" in names
    assert "simpl_install_common" in names
    assert "ovh_kube_delete" not in names
    assert "simpl_teardown" not in names


@pytest.mark.asyncio
async def test_admin_advertises_everything(tmp_path):
    names = await _tool_names(Settings(mode="admin", allow_destructive=True, state_dir=tmp_path))
    assert {"ovh_kube_delete", "simpl_teardown", "helm_uninstall", "k8s_delete"} <= names


@pytest.mark.asyncio
async def test_tool_groups_narrow_the_surface(tmp_path):
    names = await _tool_names(
        Settings(mode="operate", tool_groups=("meta", "simpl"), state_dir=tmp_path)
    )
    assert "simpl_plan" in names
    assert "mcp_info" in names
    assert not any(n.startswith("ovh_") for n in names)
    assert not any(n.startswith("helm_") for n in names)


@pytest.mark.asyncio
async def test_an_unauthenticated_http_server_that_can_write_refuses_to_start(tmp_path):
    with pytest.raises(SystemExit) as exc:
        build_server(Settings(transport="http", mode="operate", state_dir=tmp_path))
    assert "authentication" in str(exc.value)


@pytest.mark.asyncio
async def test_a_readonly_http_server_may_run_without_a_token(tmp_path):
    build_server(Settings(transport="http", mode="readonly", state_dir=tmp_path))


@pytest.mark.asyncio
async def test_every_tool_has_a_description(tmp_path):
    settings = Settings(mode="admin", allow_destructive=True, state_dir=tmp_path)
    settings_module.reset_settings_for_tests(settings)
    server = build_server(settings)
    async with Client(server) as client:
        for tool in await client.list_tools():
            assert tool.description, f"{tool.name} has no docstring"
            assert len(tool.description) > 40, f"{tool.name}'s description is too thin"


@pytest.mark.asyncio
async def test_a_port_in_the_environment_means_http_not_stdio(monkeypatch, tmp_path):
    """A hosted deployment that silently speaks stdio answers nothing at all.

    Railway sets PORT. When the image's own defaults are lost — a builder that ignores the
    Dockerfile, say — the transport has to be inferred rather than assumed.
    """
    from simpl_ovh_mcp.settings import Settings

    monkeypatch.delenv("SIMPL_MCP_TRANSPORT", raising=False)
    monkeypatch.setenv("SIMPL_MCP_STATE_DIR", str(tmp_path))

    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_ID", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    assert Settings.from_env().transport == "stdio"

    monkeypatch.setenv("PORT", "8000")
    assert Settings.from_env().transport == "http"

    monkeypatch.delenv("PORT")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc-123")
    assert Settings.from_env().transport == "http"

    monkeypatch.setenv("SIMPL_MCP_TRANSPORT", "stdio")
    assert Settings.from_env().transport == "stdio"


@pytest.mark.asyncio
async def test_blank_variables_are_treated_as_unset(monkeypatch, tmp_path):
    """A platform variable created and left empty must not defeat the default.

    Railway (and every other dashboard) happily stores an empty value, and
    os.environ.get(name, default) then hands back "" instead of the default — which turned
    a blank OVH_ENDPOINT into "unknown OVH endpoint ''" on every single call.
    """
    from simpl_ovh_mcp.settings import Settings

    monkeypatch.setenv("SIMPL_MCP_STATE_DIR", str(tmp_path))
    for name in (
        "OVH_ENDPOINT",
        "DOME_BASE_URL",
        "BRIDGE_IMAGE_REPOSITORY",
        "SIMPL_MCP_PATH",
        "SIMPL_MCP_MODE",
        "SIMPL_MCP_TRANSPORT",
        "HELM_BIN",
    ):
        monkeypatch.setenv(name, "")

    settings = Settings.from_env()
    assert settings.ovh_endpoint == "ovh-eu"
    assert settings.dome_base_url.startswith("https://tmf.sbx.evidenceledger.eu")
    assert settings.bridge_image_repository == "simpl-open/bridge"
    assert settings.http_path == "/mcp"
    assert settings.mode == "operate"
    assert settings.helm_bin == "helm"
    assert settings.transport in ("stdio", "http")
