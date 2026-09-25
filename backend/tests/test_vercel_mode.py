"""The same flows under Vercel conditions: VERCEL=1, no shared folder, in-process MCP, files over HTTP."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

HOP = {"Authorization": "Bearer hop-test-token"}


@pytest.fixture
def vercel_client(env, tmp_path, monkeypatch):
    from app import db
    from app.config import get_settings
    client_db = tmp_path / "crm.db"
    c = sqlite3.connect(client_db)
    c.executescript("""CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, tier TEXT);
                       INSERT INTO accounts VALUES (1,'Acme','gold'),(2,'Globex','silver');""")
    c.commit(); c.close()
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("APP_DB_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'vercel-app.db'}")
    monkeypatch.setenv("SHARED_DIR", "")
    monkeypatch.setenv("MCP_TRANSPORT", "auto")
    get_settings.cache_clear(); db.reset_engine()
    from app.main import app
    with TestClient(app) as tc:
        yield tc, str(client_db)
    get_settings.cache_clear(); db.reset_engine()


def test_settings_under_vercel(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("APP_DB_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@ep-x-pooler.neon.tech/neondb?sslmode=require")
    monkeypatch.setenv("SHARED_DIR", "")
    s = Settings()
    assert s.database_url == "postgresql+psycopg://u:p@ep-x-pooler.neon.tech/neondb?sslmode=require"
    assert s.effective_mcp_transport == "inprocess" and str(s.scratch_dir) == "/tmp/datafusion"


def test_flows_on_vercel(vercel_client):
    client, client_db = vercel_client
    from app.config import get_settings
    assert get_settings().serverless and get_settings().effective_mcp_transport == "inprocess"

    r = client.post("/api/connections", json={"name": "crm", "db_type": "sqlite", "database": client_db},
                    headers=HOP)
    assert r.status_code == 201, r.text

    # document as a raw body (what Hop sends); the shared-folder variant is refused
    r = client.post("/api/connections/crm/documents/raw?filename=tiers.txt", content=b"Gold tier = top 10% by revenue.",
                    headers={**HOP, "Content-Type": "application/octet-stream"})
    assert r.status_code == 201 and r.json()["characters"] > 10, r.text
    assert client.post("/api/connections/crm/documents/from-path", json={"path": "x.txt"},
                       headers=HOP).status_code == 400

    r = client.post("/api/connections/crm/research", json={}, headers=HOP)
    assert r.status_code == 200 and "review_file" not in r.json(), r.text

    # review round-trip over HTTP: GET review -> edit -> POST raw
    review = client.get("/api/connections/crm/versions/latest/review", headers=HOP).json()
    review["tables"]["accounts"]["columns"]["tier"]["business_meaning"] = "Commercial tier from the pricing policy"
    r = client.post("/api/connections/crm/versions/latest/edits/raw", content=json.dumps(review).encode(),
                    headers={**HOP, "Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    assert client.post("/api/connections/crm/versions/latest/edits/raw", content=b"not json",
                       headers=HOP).status_code == 400

    assert client.post("/api/connections/crm/versions/1/approve", json={}, headers=HOP).status_code == 200
    ctx = client.get("/api/context/crm", headers=HOP).json()
    assert ctx["metadata_layer"]["tables"]["accounts"]["columns"]["tier"]["business_meaning"].startswith("Commercial")

    # Flow B with in-process MCP servers (no subprocesses)
    r = client.post("/api/ask", json={"question": "list accounts"}, headers=HOP)
    assert r.status_code == 200, r.text
    ans = r.json()
    assert ans["sources"] and ans["table"]["rows"]
    events = client.get("/api/observability/events?kind=agent", headers=HOP).json()
    assert events[0]["detail"]["mcp_transport"] == "inprocess"
