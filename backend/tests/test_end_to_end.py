"""Flow A (connect -> catalog -> notes -> document -> research -> review -> approve) and Flow B (ask via MCP)."""
import json

import pytest
from fastapi.testclient import TestClient

HOP = {"Authorization": "Bearer hop-test-token"}


@pytest.fixture(scope="module")
def client(env):
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_flow_a_and_b(client, env):
    body = {"name": "shop", "db_type": "sqlite", "database": env["client_db"]}

    r = client.post("/api/connections/verify", json=body, headers=HOP)
    assert r.status_code == 200, r.text
    assert r.json()["table_count"] == 2

    bad = client.post("/api/connections/verify", json={**body, "database": "/nope/x.db"}, headers=HOP)
    assert bad.status_code == 400

    r = client.post("/api/connections", json=body, headers=HOP)
    assert r.status_code == 201, r.text
    assert r.json()["catalog"]["standard_metadata"]["tables"] == 2
    assert r.json()["connection"]["password"] == ""          # nothing to mask for sqlite

    r = client.get("/api/connections/shop/tables/orders", headers=HOP)
    cols = {c["name"]: c for c in r.json()["columns"]}
    assert cols["id"]["pk"] and "null_ratio" in cols["status"]["profile"]

    r = client.post("/api/connections/shop/tables/orders/notes", headers=HOP,
                    json={"column": "amount", "description": "Order value in EUR incl. VAT",
                          "business_meaning": "Gross revenue before refunds"})
    assert r.status_code == 201, r.text

    r = client.post("/api/connections/shop/documents/from-path", json={"path": "glossary.txt"}, headers=HOP)
    assert r.status_code == 201, r.text
    assert client.post("/api/connections/shop/documents/from-path", json={"path": "../../etc/passwd"},
                       headers=HOP).status_code == 400

    r = client.post("/api/connections/shop/research", json={"web_search": False}, headers=HOP)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["version"] == 1 and out["status"] == "draft"

    # steward edits the review file and applies it
    review = json.loads(open(out["review_file"]).read())
    review["tables"]["customers"]["description"] = "One row per registered customer."
    open(out["review_file"], "w").write(json.dumps(review))
    r = client.post("/api/connections/shop/versions/1/edits/from-review-file", headers=HOP)
    assert r.status_code == 200, r.text

    v = client.get("/api/connections/shop/versions/1", headers=HOP).json()
    assert v["metadata_layer"]["tables"]["orders"]["columns"]["amount"]["description"] == "Order value in EUR incl. VAT"
    assert v["metadata_layer"]["tables"]["customers"]["columns"]["email"]["pii"] is True
    assert any(r["source"] == "foreign key" for r in v["metadata_layer"]["relationships"])

    r = client.post("/api/connections/shop/versions/1/approve", json={"note": "ok"}, headers=HOP)
    assert r.status_code == 200, r.text
    assert r.json()["cube_model"]["context_version"] == 1
    internal = {"X-Internal-Token": "internal-test"}
    assert client.get("/internal/cube/models").status_code == 401
    models = client.get("/internal/cube/models", headers=internal).json()
    cube_yaml = models[0]["content"]
    assert models[0]["fileName"] == "shop.yml"
    assert "data_source: shop" in cube_yaml and "sum_amount" in cube_yaml and "relationship: many_to_one" in cube_yaml
    assert client.get("/internal/cube/schema-version", headers=internal).json()["version"] == "shop:1"
    assert client.get("/internal/cube/datasource-types", headers=internal).json() == {"shop": "sqlite"}

    # Flow B: restricted user through the auto-generated MCP server
    admin = {"X-User-Email": "admin@example.com"}
    client.post("/api/admin/users", json={"email": "ana@example.com", "department": "finance", "role": "user"},
                headers=admin)
    client.post("/api/admin/permissions", headers=admin, json={
        "subject_type": "department", "subject": "finance", "connection": "shop", "tables": ["orders"]})

    from app.security.auth import verify_otp  # noqa: F401 - OTP path covered separately
    import jwt, time
    from app.config import get_settings
    token = jwt.encode({"sub": "ana@example.com", "exp": int(time.time()) + 600}, get_settings().jwt_secret,
                       algorithm="HS256")
    ana = {"Authorization": f"Bearer {token}"}

    ctx = client.get("/api/context/shop", headers=ana).json()
    assert list(ctx["metadata_layer"]["tables"]) == ["orders"]

    r = client.post("/api/ask", json={"question": "Show me recent orders"}, headers=ana)
    assert r.status_code == 200, r.text
    ans = r.json()
    assert ans["sources"] and ans["sources"][0]["sql"].upper().startswith("SELECT")
    assert ans["table"]["rows"] and ans["references"][0]["context_version"] == 1

    summary = client.get("/api/observability/summary", headers=HOP).json()
    assert summary["user_questions"] >= 1 and summary["tools_used"].get("run_readonly_query", 0) >= 1

    assert client.get("/api/context/shop", headers={"X-User-Email": ""}).status_code == 401


def test_mcp_server_enforces_permissions(client, env):
    import asyncio
    from app.mcp_servers.db_server import build_server
    server = build_server("shop", "ana@example.com")          # finance: orders only
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"list_tables", "run_readonly_query", "query_orders"} <= names and "query_customers" not in names
    blocks = asyncio.run(server.call_tool("run_readonly_query", {"sql": "SELECT name FROM customers"}))
    text = json.dumps(blocks, default=str)
    assert "Blocked by guardrails" in text          # restricted tables are not even revealed
    blocks = asyncio.run(server.call_tool("run_readonly_query", {"sql": "DELETE FROM orders"}))
    assert "Blocked by guardrails" in json.dumps(blocks, default=str)


def test_otp_login(client, caplog):
    admin = {"X-User-Email": "admin@example.com"}
    client.post("/api/admin/users", json={"email": "otp@example.com", "role": "user"}, headers=admin)
    with caplog.at_level("WARNING"):
        assert client.post("/api/auth/otp/request", json={"email": "otp@example.com"}).status_code == 200
    code = [r.getMessage() for r in caplog.records if "OTP for otp@example.com" in r.getMessage()][0].split()[-3]
    assert client.post("/api/auth/otp/verify", json={"email": "otp@example.com", "code": "000000"}).status_code == 401
    r = client.post("/api/auth/otp/verify", json={"email": "otp@example.com", "code": code})
    assert r.status_code == 200
    me = client.get("/api/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()
    assert me["email"] == "otp@example.com"


def test_openrouter_client_config(monkeypatch):
    from app.config import get_settings
    from app.llm import chat_model
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("LLM_MODEL_AGENT", "anthropic/claude-sonnet-5")
    get_settings.cache_clear()
    try:
        m = chat_model("agent")
        assert m.model_name == "anthropic/claude-sonnet-5" and "openrouter.ai" in str(m.openai_api_base)
        assert m.default_headers["X-Title"] == "DataFusion"
    finally:
        get_settings.cache_clear()
