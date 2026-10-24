import os

import pytest

# Тесты идут в настоящем PostgreSQL (SQL-диалект важен): TEST_DATABASE_URL=postgresql://user:pass@host:port/db.
# Локально его поднимает scripts/test_all.sh (docker), в CI — сервис postgres.
if not os.environ.get("TEST_DATABASE_URL"):
    pytest.exit("Нужен PostgreSQL для тестов: задайте TEST_DATABASE_URL (см. scripts/test_all.sh)", returncode=2)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

import web_driver  # noqa: E402, F401  (тестовые маршруты диалога)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GATEWAY_URL", "http://gw")
    monkeypatch.setenv("INGEST_URL", "http://ing")
    monkeypatch.setenv("MAX_API_URL", "http://max")
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    monkeypatch.setenv("MAX_SEND_RETRY_DELAY", "0")
    monkeypatch.setenv("INSIGHTS_CACHE_TTL", "0")             # кэш дашборда выключен: тесты видят изменения сразу
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "test-secret")   # вебхук без секрета выключен; anon-клиент тестов шлёт этот секрет

    from app import pg, scenario
    pg.reset_schema()          # чистая схема для каждого теста
    scenario.reset()           # сценарий и площадки — из пустой базы теста (исходные значения)
