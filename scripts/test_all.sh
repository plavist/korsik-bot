#!/usr/bin/env bash
# Прогон тестов всех сервисов с покрытием строк и ветвей: ./scripts/test_all.sh
# Нужен PostgreSQL: задайте TEST_DATABASE_URL (например, в CI это сервис postgres) — иначе скрипт сам поднимет
# временный контейнер postgres:16-alpine (нужен docker) и остановит его в конце.
# Зависимости ставит uv (pyproject.toml + uv.lock в каждом сервисе); установка: https://docs.astral.sh/uv/ (pip install uv)
set -e
cd "$(dirname "$0")/.."
UV=$(command -v uv || echo "$PWD/.venv/bin/uv")
if [ -z "${TEST_DATABASE_URL:-}" ]; then
  command -v docker >/dev/null || { echo "Нужен docker или TEST_DATABASE_URL с адресом PostgreSQL"; exit 2; }
  CID=$(docker run -d --rm -e POSTGRES_PASSWORD=test -e POSTGRES_USER=test -e POSTGRES_DB=korsik_test -p 127.0.0.1:54329:5432 postgres:16-alpine)
  trap 'docker stop "$CID" >/dev/null' EXIT
  until docker exec "$CID" pg_isready -U test -d korsik_test >/dev/null 2>&1; do sleep 1; done
  export TEST_DATABASE_URL=postgresql://test:test@127.0.0.1:54329/korsik_test
fi
for s in llm-gateway ingest pipeline admin; do
  echo "=== $s ==="
  (cd services/$s && "$UV" run --frozen pytest -q --cov=app --cov-branch --cov-report=term-missing:skip-covered 2>&1 | grep -v -i -E "warn|testclient|^$|Docs:|^Installed|^Using|^Creating|^Built|^Prepared")
done
