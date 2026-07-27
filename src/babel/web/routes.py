"""Route handlers. Filled in by the next task."""

from fastapi import FastAPI


def register_routes(app: FastAPI) -> None:
    """Attach the list, article and image routes."""
