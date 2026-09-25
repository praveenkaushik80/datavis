"""Vercel entrypoint. Vercel's Python runtime loads the ASGI app named `app` from this file."""
from app.main import app  # noqa: F401
