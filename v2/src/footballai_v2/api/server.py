"""Bounded production Uvicorn launcher for the FootballAI API image."""

from __future__ import annotations

import os

import uvicorn


def _bounded_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def main() -> None:
    uvicorn.run(
        "footballai_v2.api.main:app",
        host=os.getenv("FOOTBALLAI_API_HOST", "0.0.0.0"),
        port=_bounded_integer("FOOTBALLAI_API_PORT", 8000, 1, 65535),
        # The implemented manifest adapter is local filesystem based. Keep one
        # API process until the PostgreSQL control-plane adapter is available.
        workers=_bounded_integer("FOOTBALLAI_API_WORKERS", 1, 1, 1),
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=_bounded_integer(
            "FOOTBALLAI_API_GRACEFUL_TIMEOUT_SECONDS", 30, 1, 300
        ),
    )


if __name__ == "__main__":
    main()
