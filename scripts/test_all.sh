#!/usr/bin/env bash
# Прогон тестов всех сервисов с покрытием: ./scripts/test_all.sh
# Зависимости ставит uv (pyproject.toml + uv.lock в каждом сервисе); установка: https://docs.astral.sh/uv/ (pip install uv)
set -e
cd "$(dirname "$0")/.."
UV=$(command -v uv || echo "$PWD/.venv/bin/uv")
for s in llm-gateway ingest pipeline admin; do
  echo "=== $s ==="
  (cd services/$s && "$UV" run --frozen pytest -q --cov=app --cov-report=term-missing:skip-covered 2>&1 | grep -v -i -E "warn|testclient|^$|Docs:|^Installed|^Using|^Creating|^Built|^Prepared")
done
