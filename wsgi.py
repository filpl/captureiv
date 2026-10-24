"""WSGI entrypoint for Gunicorn or another production server."""
from app import app

__all__ = ["app"]
