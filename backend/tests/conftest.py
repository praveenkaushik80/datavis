import os
import sqlite3
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("df")
    client_db = tmp / "shop.db"
    c = sqlite3.connect(client_db)
    c.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, country TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                             amount NUMERIC NOT NULL, status TEXT, ordered_at DATE);
        INSERT INTO customers VALUES (1,'Asha','asha@example.com','IN'),(2,'Ben','ben@example.com','UK');
        INSERT INTO orders VALUES (1,1,120.5,'paid','2026-09-01'),(2,1,80,'refunded','2026-09-03'),
                                  (3,2,42,'paid','2026-09-04');
    """)
    c.commit()
    c.close()
    shared = tmp / "shared"
    (shared / "docs").mkdir(parents=True)
    (shared / "docs" / "glossary.txt").write_text("A refunded order is excluded from revenue.")
    os.environ.update({
        "APP_DB_URL": f"sqlite:///{tmp / 'app.db'}", "FERNET_KEY": Fernet.generate_key().decode(),
        "LLM_PROVIDER": "fake", "HOP_API_TOKEN": "hop-test-token", "JWT_SECRET": "test-secret-that-is-long-enough-for-hs256-xx", "AUTH_MODE": "dev",
        "SHARED_DIR": str(shared), "OM_URL": "", "CUBE_API_URL": "",
        "INTERNAL_TOKEN": "internal-test",
    })
    os.chdir(ROOT)
    return {"client_db": str(client_db), "shared": shared, "tmp": tmp}
