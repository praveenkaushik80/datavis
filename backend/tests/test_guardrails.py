import pytest

from app.agent.guardrails import GuardrailViolation, validate_select

KNOWN = {"public.orders", "public.customers"}


def ok(sql, allowed=None, denied=frozenset(), limit=100):
    return validate_select(sql, "postgres", allowed, set(denied), KNOWN, limit)


def test_select_gets_limit():
    assert "LIMIT 100" in ok("SELECT id FROM orders")
    assert "LIMIT 5" in ok("SELECT id FROM orders LIMIT 5")
    assert "LIMIT 100" in ok("SELECT id FROM orders LIMIT 5000")


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders", "UPDATE orders SET amount=0", "DROP TABLE orders", "INSERT INTO orders VALUES (1)",
    "SELECT 1; DELETE FROM orders", "SELECT pg_sleep(10)", "SELECT * INTO backup FROM orders",
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x", "SELECT * FROM secrets",
])
def test_blocked(sql):
    with pytest.raises(GuardrailViolation):
        ok(sql)


def test_table_and_column_permissions():
    with pytest.raises(GuardrailViolation):
        ok("SELECT * FROM customers", allowed={"public.orders"})
    with pytest.raises(GuardrailViolation):
        ok("SELECT email FROM customers", denied={"public.customers.email"})
    with pytest.raises(GuardrailViolation):
        ok("SELECT * FROM customers", denied={"public.customers.email"})
    assert ok("SELECT name FROM customers", denied={"public.customers.email"})


def test_cte_and_union():
    assert "LIMIT" in ok("WITH p AS (SELECT * FROM orders) SELECT count(*) FROM p")
    assert "LIMIT 100" in ok("SELECT id FROM orders UNION SELECT id FROM customers")
