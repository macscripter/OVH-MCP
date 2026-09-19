"""Entry point.

stdio by default, which is what a local client (Claude Code, Claude Desktop) speaks.
Set SIMPL_MCP_TRANSPORT=http for a hosted deployment — Railway sets PORT for us.
"""

from __future__ import annotations

import sys

from . import __version__
from .server import build_server
from .settings import get_settings


def _banner(settings) -> None:
    """One line saying what this process resolved, and from where.

    Written to stderr, which is free under both transports. Every deployment question so
    far — is it serving HTTP, on which port, with which credentials, is helm present —
    is answerable from this line without opening a shell.
    """
    import shutil

    print(
        f"simpl-ovh-mcp {__version__} | transport={settings.transport} "
        f"[{settings.transport_source}] | port={settings.port} | mode={settings.mode} "
        f"| state={settings.state_dir} | ovh-auth={settings.ovh_auth_mode} "
        f"| helm={'yes' if shutil.which(settings.helm_bin) else 'MISSING'}",
        file=sys.stderr,
    )
    if settings.transport == "stdio" and settings.looks_hosted:
        print(
            "WARNING: this looks like a hosted deployment (a platform port or RAILWAY_* "
            "variable is present) but the transport is stdio, so nothing will answer over "
            "HTTP and the platform will report 502. Unset SIMPL_MCP_TRANSPORT to let it "
            "infer, or set it to http.",
            file=sys.stderr,
        )


def main() -> None:
    settings = get_settings()
    _banner(settings)
    mcp = build_server(settings)

    if settings.transport in ("http", "streamable-http"):
        kwargs = {
            "transport": "http",
            "host": settings.host,
            "port": settings.port,
            "path": settings.http_path,
        }
        # Host/Origin checking is off by default in FastMCP, so naming allowed hosts has to
        # switch it on as well — otherwise the setting reads like a restriction and enforces
        # nothing. Behind Railway's proxy the Host header is the public domain, so that
        # domain is what belongs in SIMPL_MCP_ALLOWED_HOSTS.
        if settings.allowed_hosts:
            kwargs["allowed_hosts"] = list(settings.allowed_hosts)
            kwargs["host_origin_protection"] = True
        mcp.run(**kwargs)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
