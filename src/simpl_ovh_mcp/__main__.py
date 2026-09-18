"""Entry point.

stdio by default, which is what a local client (Claude Code, Claude Desktop) speaks.
Set SIMPL_MCP_TRANSPORT=http for a hosted deployment — Railway sets PORT for us.
"""

from __future__ import annotations

import sys

from .server import build_server
from .settings import get_settings


def main() -> None:
    settings = get_settings()
    mcp = build_server(settings)

    if settings.transport in ("http", "streamable-http"):
        kwargs = {
            "transport": "http",
            "host": settings.host,
            "port": settings.port,
            "path": settings.http_path,
        }
        # Behind Railway's proxy the Host header is the public domain, which the
        # DNS-rebinding protection rejects unless it is named here.
        if settings.allowed_hosts:
            kwargs["allowed_hosts"] = list(settings.allowed_hosts)
        print(
            f"simpl-ovh-mcp listening on {settings.host}:{settings.port}{settings.http_path} "
            f"(mode={settings.mode})",
            file=sys.stderr,
        )
        mcp.run(**kwargs)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
