#!/usr/bin/env python3
"""Переносит данные из старых SQLite-баз (pipeline.db, ingest.db, admin.db) в PostgreSQL.

Порядок:
  1. Запустите новую версию (docker compose up -d --build): сервисы сами создадут схемы и таблицы в PostgreSQL.
  2. Скопируйте файлы баз из старых томов:
       docker compose cp pipeline:/data/pipeline.db ./migrate/ ; docker compose cp ingest:/data/ingest.db ./migrate/ ;
       docker compose cp admin:/data/admin.db ./migrate/
  3. Запустите (нужен psycopg: pip install "psycopg[binary]"):
       python scripts/migrate_sqlite_to_postgres.py --dsn postgresql://korsik:ПАРОЛЬ@127.0.0.1:5432/korsik --dir ./migrate

Скрипт идемпотентен: уже перенесённые строки (по первичному ключу) пропускаются, счётчики id выравниваются.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import psycopg

PLAN = {  # файл SQLite → схема PostgreSQL → таблицы
    "pipeline.db": ("pipeline", ["settings", "users", "employees", "messages", "escalations", "manager_notices", "sent_steps",
                                 "survey_answers", "bot_commands", "rewrites"]),
    "ingest.db": ("ingest", ["documents"]),
    "admin.db": ("admin", ["users", "audit"]),
}


def pg_columns(c: psycopg.Connection, schema: str, table: str) -> list[str]:
    rows = c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                     (schema, table)).fetchall()
    return [r[0] for r in rows]


def migrate_table(src: sqlite3.Connection, dst: psycopg.Connection, schema: str, table: str) -> tuple[int, int]:
    """→ (строк в источнике, добавлено)."""
    target = pg_columns(dst, schema, table)
    if not target:
        raise SystemExit(f"В PostgreSQL нет таблицы {schema}.{table}: сначала запустите сервисы новой версии")
    have = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
    if not have:
        return 0, 0                                   # такой таблицы в старой базе не было
    cols = [c for c in target if c in have]
    rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    sql = f"INSERT INTO {schema}.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) ON CONFLICT DO NOTHING"
    added = 0
    with dst.cursor() as cur:
        for row in rows:
            cur.execute(sql, tuple(row))
            added += cur.rowcount
    seq = dst.execute("SELECT pg_get_serial_sequence(%s, 'id')", (f"{schema}.{table}",)).fetchone()[0] if "id" in target else None
    if seq:                                           # выровнять BIGSERIAL, чтобы новые id не конфликтовали с перенесёнными
        dst.execute(f"SELECT setval(pg_get_serial_sequence('{schema}.{table}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) "
                    f"FROM {schema}.{table}")
    return len(rows), added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", required=True, help="postgresql://user:password@host:5432/dbname")
    ap.add_argument("--dir", default="./migrate", help="папка с файлами pipeline.db, ingest.db, admin.db")
    args = ap.parse_args(argv)
    found = False
    with psycopg.connect(args.dsn) as dst:
        for fname, (schema, tables) in PLAN.items():
            path = Path(args.dir) / fname
            if not path.exists():
                print(f"{fname}: файла нет, пропускаю")
                continue
            found = True
            src = sqlite3.connect(str(path))
            for t in tables:
                total, added = migrate_table(src, dst, schema, t)
                print(f"{schema}.{t}: в источнике {total}, перенесено {added}")
            src.close()
        dst.commit()
    if not found:
        print("Ни одного файла SQLite не найдено", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
