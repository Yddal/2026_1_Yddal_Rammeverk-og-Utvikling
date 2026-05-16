"""
Entry-point for MCP-serveren.

Kjøring:
    py src/__init__.py http     # Streamable-HTTP transport (default port 8000).
    py src/__init__.py stdio    # Stdio-transport (for direkte MCP-klienter).

Env-vars:
    LOG_LEVEL              : DEBUG | INFO (default) | WARNING | ERROR
    PORT                   : HTTP-port (default 8000)
    SHOW_DEVELOPER_INFO    : 0/false skjuler get_developer_info-toolet
"""

import logging
import os
import sys
from pathlib import Path

# La modulene i samme mappe (server.py, ssb_client.py) importeres uten pakkenavn.
# Vurdert å pakke som ekte Python-pakke; utsatt til vi vil pip-distribuere.
sys.path.insert(0, str(Path(__file__).parent))

from server import server  # noqa: E402  (sys.path-hack må kjøre først)


def _configure_logging() -> None:
    """Sett root-logger basert på LOG_LEVEL. Default INFO i stedet for DEBUG."""
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Synk FastMCP-server settings til samme nivå.
    server.settings.log_level = log_level_name


def main() -> None:
    transport_type = sys.argv[1] if len(sys.argv) > 1 else None
    _configure_logging()

    if transport_type == "http":
        port = int(os.environ.get("PORT", 8000))
        server.settings.port = port
        server.settings.host = "127.0.0.1"
        server.run(transport="streamable-http")
    elif transport_type == "stdio":
        server.run(transport="stdio")
    else:
        logging.error(
            "Ugyldig transport-type: %r. Bruk 'http' eller 'stdio'.",
            transport_type,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
