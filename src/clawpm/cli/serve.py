from __future__ import annotations

import sys

import click

from clawpm.cli.base import main

# ============================================================================
# Serve command
# ============================================================================

def _load_web_server():
    """Import the optional web-server deps (the ``web`` extra).

    Returns ``(create_app, uvicorn)``. Raises ``ImportError`` if fastapi /
    uvicorn aren't installed. Factored out so the graceful-degradation path
    is testable without uninstalling the deps.
    """
    import uvicorn
    from clawpm.serve import create_app

    return create_app, uvicorn


@main.command("serve")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
def serve(host: str, port: int) -> None:
    """Start the web UI server (read-only dashboard)."""
    try:
        create_app, uvicorn = _load_web_server()
    except ImportError:
        click.echo(
            "The ClawPM web UI requires the optional 'web' extra.\n"
            "Install it with:  pip install 'clawpm[web]'",
            err=True,
        )
        sys.exit(1)

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
