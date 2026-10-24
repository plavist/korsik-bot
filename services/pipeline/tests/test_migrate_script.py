"""Скрипт переноса данных из старых SQLite-баз в PostgreSQL (scripts/migrate_sqlite_to_postgres.py)."""
import importlib.util
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app import db

spec = importlib.util.spec_from_file_location("migrate", Path(__file__).resolve().parents[3] / "scripts" / "migrate_sqlite_to_postgres.py")
mig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mig)


def old_pipeline_db(path):
    c = sqlite3.connect(str(path))
    c.executescript("""
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, channel TEXT, role TEXT, text TEXT, sources TEXT, grounded INTEGER,
      sentiment TEXT, sentiment_score REAL, topics TEXT, red_flag INTEGER DEFAULT 0, created_at REAL);
    CREATE TABLE escalations (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, channel TEXT, reason TEXT, kind TEXT, status TEXT DEFAULT 'open', created_at REAL);
    CREATE TABLE employees (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, name TEXT, site TEXT, is_office INTEGER DEFAULT 1, hire_date TEXT,
      probation_end TEXT, recruiter_ok INTEGER DEFAULT 0, form TEXT, user_id TEXT, channel TEXT, state TEXT DEFAULT '{}', active INTEGER DEFAULT 1, created_at REAL);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    c.execute("INSERT INTO messages(id,user_id,channel,role,text,created_at) VALUES (5,'u1','max','user','Привет',?)", (time.time(),))
    c.execute("INSERT INTO escalations(id,user_id,channel,reason,kind,created_at) VALUES (3,'u1','max','Вопрос','agent',?)", (time.time(),))
    c.execute("INSERT INTO employees(id,code,name,site,hire_date,created_at) VALUES (2,'ABC123','Иван Петров','GrPP','2026-10-20',?)", (time.time(),))
    c.execute("INSERT INTO settings VALUES ('max_marker','777')")
    c.commit()
    c.close()


def test_migration_copies_rows_is_idempotent_and_aligns_sequences(tmp_path, capsys):
    old_pipeline_db(tmp_path / "pipeline.db")
    dsn = os.environ["TEST_DATABASE_URL"]
    assert mig.main(["--dsn", dsn, "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "pipeline.messages: в источнике 1, перенесено 1" in out and "ingest.db: файла нет" in out
    assert db.get_setting("max_marker") == "777" and db.employee_by_code("abc123")["name"] == "Иван Петров"
    esc = db.get_escalation(3)
    assert esc["kind"] == "agent" and esc["repeats"] == 1                       # новые колонки получили значения по умолчанию
    assert mig.main(["--dsn", dsn, "--dir", str(tmp_path)]) == 0                # повторный запуск ничего не дублирует
    assert "перенесено 0" in capsys.readouterr().out and len(db.history("u1")) == 1
    assert db.add_message("u1", "max", "user", "Новое") == 6                    # счётчик id продолжился после перенесённых
    assert db.add_escalation("u1", "max", "Другое", "contact") == 4


def test_migration_without_files_and_without_tables(tmp_path, capsys):
    dsn = os.environ["TEST_DATABASE_URL"]
    assert mig.main(["--dsn", dsn, "--dir", str(tmp_path)]) == 1                # нечего переносить
    sqlite3.connect(str(tmp_path / "pipeline.db")).close()                      # пустой файл: таблиц нет — пропускаем без ошибок
    assert mig.main(["--dsn", dsn, "--dir", str(tmp_path)]) == 0
    assert "перенесено 0" in capsys.readouterr().out


def test_migration_demands_new_schema_to_exist(tmp_path):
    import psycopg
    old_pipeline_db(tmp_path / "pipeline.db")
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS pipeline CASCADE")
    from app import pg
    pg.close()
    with pytest.raises(SystemExit, match="сначала запустите сервисы"):
        mig.main(["--dsn", os.environ["TEST_DATABASE_URL"], "--dir", str(tmp_path)])
