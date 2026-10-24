import os

import pytest

# Тесты идут в настоящем PostgreSQL (SQL-диалект важен): TEST_DATABASE_URL=postgresql://user:pass@host:port/db.
# Локально его поднимает scripts/test_all.sh (docker), в CI — сервис postgres.
if not os.environ.get("TEST_DATABASE_URL"):
    pytest.exit("Нужен PostgreSQL для тестов: задайте TEST_DATABASE_URL (см. scripts/test_all.sh)", returncode=2)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]


@pytest.fixture(autouse=True)
def clean_db():
    from app import pg
    pg.reset_schema()
