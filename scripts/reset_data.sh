#!/usr/bin/env bash
# Сброс рабочих данных до исходного состояния: ./scripts/reset_data.sh [--yes]
# Удаляется: сотрудники, переписки, обращения, аналитика, ответы опросов, напоминания, пользователи панели и журнал действий,
#            документы, загруженные вручную. Остаётся: сценарий (исходный, с выбранными вложениями), библиотека картинок и файлов,
#            площадки и команды по умолчанию, базовые документы из BASE_DOCS_DIR.
# Перед сбросом дамп базы PostgreSQL сохраняется в backups/<дата>/korsik.sql.
set -euo pipefail
cd "$(dirname "$0")/.."
DC=(docker compose -f compose.yml)
[ -f compose.proxy.yml ] && DC+=(-f compose.proxy.yml)
if [ "${1:-}" != "--yes" ]; then
  read -r -p "Удалить все рабочие данные (сотрудники, переписки, обращения, аналитика)? Копия будет в backups/. [y/N] " a
  [[ "$a" =~ ^[yYдД]$ ]] || { echo "Отменено"; exit 1; }
fi
B="backups/$(date +%Y%m%d-%H%M%S)"; mkdir -p "$B"
"${DC[@]}" exec -T postgres pg_dump -U korsik --no-owner korsik > "$B/korsik.sql" || true   # все схемы: pipeline, ingest, admin, gateway
echo "Копия базы: $B/korsik.sql (восстановить: docker compose exec -T postgres psql -U korsik korsik < $B/korsik.sql)"
"${DC[@]}" exec -T pipeline python -m app.reset </dev/null
"${DC[@]}" exec -T admin python -m app.reset </dev/null
"${DC[@]}" exec -T ingest python -m app.reset </dev/null
"${DC[@]}" restart pipeline admin ingest
echo "Готово."
