"""ASGI entry point: ``uvicorn backend.main:app``."""

from backend.app import create_app

app = create_app()
