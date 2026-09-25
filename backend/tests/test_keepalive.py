"""Keep-alive streaming (Railway: public connections close after 5 minutes without data)."""
import json

from fastapi.testclient import TestClient

HOP = {"Authorization": "Bearer hop-test-token"}


def test_keepalive_success_and_error(env, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("KEEPALIVE_STREAMING", "true")
    monkeypatch.setenv("KEEPALIVE_INTERVAL_SECONDS", "0.05")
    monkeypatch.setenv("DATAFUSION_FAKE_LLM_DELAY", "0.1")
    get_settings.cache_clear()
    try:
        from app.main import app
        with TestClient(app) as client:
            # a connection with no catalogue: research fails *after* the 200 status was sent
            r = client.post("/api/connections", headers=HOP, json={
                "name": "ka_empty", "db_type": "sqlite", "database": env["client_db"], "run_catalog": False})
            assert r.status_code == 201, r.text
            r = client.post("/api/connections/ka_empty/research", json={}, headers=HOP)
            assert r.status_code == 200
            body = json.loads(r.text)
            assert body["datafusion_error"] is True and body["status_code"] == 400
            assert "catalog pipeline" in body["detail"]

            client.post("/api/connections/ka_empty/catalog/run", headers=HOP)
            r = client.post("/api/connections/ka_empty/research", json={}, headers=HOP)
            assert r.text.startswith(" "), "expected keep-alive whitespace before the JSON"
            out = json.loads(r.text)
            assert out["status"] == "draft" and "datafusion_error" not in out
    finally:
        get_settings.cache_clear()
