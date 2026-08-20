"""Session service selection for durable local workflows."""

from typing import Any

from app.settings import settings


def build_session_service() -> Any:
    """Use a database locally by default; memory remains an explicit test mode."""
    if settings.session_backend == "memory":
        from google.adk.sessions import InMemorySessionService

        return InMemorySessionService()

    try:
        from google.adk.sessions import DatabaseSessionService
    except ImportError as exc:  # pragma: no cover - depends on installation extras
        raise RuntimeError(
            "Durable sessions require the database extra. Run `make dev-install`, "
            "or set ARMCL_SESSION_BACKEND=memory for a disposable run."
        ) from exc
    return DatabaseSessionService(settings.session_db_url)
