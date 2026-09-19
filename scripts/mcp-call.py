#!/usr/bin/env python3
"""Call a tool on the deployed simpl-ovh-mcp server from the command line.

A thin driver, for when the server is deployed but not attached to the client you happen
to be sitting in front of. The token is read from the environment or from a private file
and is never printed.

    scripts/mcp-call.py list
    scripts/mcp-call.py ovh_projects_list
    scripts/mcp-call.py simpl_plan '{"agents": ["authority"], "sizing": "demo"}'
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport

URL = os.environ.get("SIMPL_MCP_URL", "https://ovh-mcp-production.up.railway.app/mcp")
TOKEN_FILE = Path(os.environ.get("SIMPL_MCP_TOKEN_FILE", Path.home() / ".simpl-mcp-token"))


def _clean(value: str) -> str:
    """Accept either the bare token or the whole header value.

    Copying "Bearer <token>" out of a dashboard is the obvious thing to do, and sending it
    verbatim produces "Bearer Bearer <token>" and a 401 that blames the token.
    """
    value = value.strip().strip("\"'")
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def token() -> str:
    env = os.environ.get("SIMPL_MCP_BEARER_TOKEN")
    if env and env.strip():
        return _clean(env)
    if TOKEN_FILE.exists():
        value = _clean(TOKEN_FILE.read_text(encoding="utf-8"))
        if value:
            return value
    sys.exit(
        f"No token. Either export SIMPL_MCP_BEARER_TOKEN, or write it to {TOKEN_FILE} "
        "(chmod 600). It is never printed by this script."
    )


async def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    client = Client(StreamableHttpTransport(URL, auth=BearerAuth(token())), timeout=180)
    async with client:
        if tool == "list":
            for t in await client.list_tools():
                print(f"{t.name:32} {(t.description or '').splitlines()[0][:90]}")
            return
        result = await client.call_tool(tool, args)
        data = result.data if hasattr(result, "data") else result
        print(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
