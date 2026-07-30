"""Start the backend: `python -m app`.

A thin wrapper around uvicorn that reads the host and port out of .env, so
PORT stays a single setting the dashboards, the card IDE webhook URL and the
README all agree on. Bind stays on loopback by default; put a tunnel in front
of it when the card IDE needs a public URL.
"""

from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
