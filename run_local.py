"""Local dev launcher: runs the real app on SQLite (no Postgres required).

Maps Postgres JSONB columns to generic JSON so the schema works on SQLite,
then serves app.py with uvicorn. Use only for local testing.

    python run_local.py        # http://127.0.0.1:8000
"""
import os

os.environ.setdefault("POSTGRES_URL", "sqlite:///./local_dev.db")
os.environ.setdefault("PRICES_FILE", os.path.join(os.path.dirname(__file__), "prices.json"))
os.environ.setdefault("ENV", "development")

# Make the Postgres-specific JSONB type usable on SQLite.
import sqlalchemy
import sqlalchemy.dialects.postgresql as _pg
_pg.JSONB = sqlalchemy.JSON

import uvicorn
from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Local server on http://127.0.0.1:{port} (SQLite backend)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
