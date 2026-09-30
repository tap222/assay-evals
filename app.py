"""Vercel entry point. Vercel detects a FastAPI `app` in a root app.py and
sends every request to it with the original path (no rewrites needed).

Vercel functions have no persistent disk and no long-running process, so:
- the store is ASSAY_STORE_URL, else the DATABASE_URL a Postgres integration
  (Neon on the Vercel marketplace) sets, else SQLite in /tmp (lost with each
  instance, so every cold start reloads the demo);
- the demo tenant is loaded when the store is empty: once for Postgres, and
  on each fresh instance for /tmp;
- the in-process scheduler is off; Vercel Cron calls /v1/cron instead.
"""
import os


def store_url() -> str:
    url = os.environ.get("ASSAY_STORE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return "sqlite:////tmp/assay.db"
    for scheme in ("postgres://", "postgresql://"):  # the driver SQLAlchemy should use: psycopg 3
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url[len(scheme):]
    return url


os.environ["ASSAY_STORE_URL"] = store_url()
os.environ.setdefault("ASSAY_AUTO_DEMO", "1")
os.environ["ASSAY_SCHEDULE_MINUTES"] = "0"

from assay.api import create_app  # noqa: E402

app = create_app()
