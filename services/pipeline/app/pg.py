"""Подключение к PostgreSQL: пул соединений и собственная схема сервиса ("pipeline").
Все сервисы используют одну базу (DATABASE_URL), у каждого — своя схема, поэтому имена таблиц не пересекаются.
Схема создаётся один раз (CREATE ... IF NOT EXISTS, идемпотентно), а не при каждом запросе."""
import atexit
import os
import threading
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = "pipeline"
DDL = """
CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY, user_id TEXT, channel TEXT, role TEXT, text TEXT,
  sources TEXT, grounded INTEGER, sentiment TEXT, sentiment_score DOUBLE PRECISION, topics TEXT,
  red_flag INTEGER DEFAULT 0, created_at DOUBLE PRECISION, kind TEXT, buttons TEXT, image TEXT);
CREATE INDEX IF NOT EXISTS ix_msg_user ON messages(user_id, id);
CREATE INDEX IF NOT EXISTS ix_msg_created ON messages(created_at);
CREATE TABLE IF NOT EXISTS escalations (
  id BIGSERIAL PRIMARY KEY, user_id TEXT, channel TEXT, reason TEXT, kind TEXT,
  status TEXT DEFAULT 'open', created_at DOUBLE PRECISION, repeats INTEGER DEFAULT 1, updated_at DOUBLE PRECISION);
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS repeats INTEGER DEFAULT 1;
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION;
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS assignee TEXT;
CREATE INDEX IF NOT EXISTS ix_esc_user ON escalations(user_id, status);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS employees (
  id BIGSERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, site TEXT, is_office INTEGER DEFAULT 1,
  hire_date TEXT, probation_end TEXT, recruiter_ok INTEGER DEFAULT 0, form TEXT, user_id TEXT, channel TEXT,
  state TEXT DEFAULT '{}', active INTEGER DEFAULT 1, created_at DOUBLE PRECISION, manager TEXT, manager_user TEXT);
CREATE INDEX IF NOT EXISTS ix_emp_user ON employees(user_id);
CREATE TABLE IF NOT EXISTS manager_notices (
  id BIGSERIAL PRIMARY KEY, employee_id BIGINT, kind TEXT, text TEXT, manager TEXT, delivered INTEGER DEFAULT 0,
  created_at DOUBLE PRECISION, UNIQUE(employee_id, kind));
CREATE TABLE IF NOT EXISTS sent_steps (
  employee_id BIGINT, step_id TEXT, status TEXT, at DOUBLE PRECISION, PRIMARY KEY (employee_id, step_id));
CREATE TABLE IF NOT EXISTS survey_answers (
  id BIGSERIAL PRIMARY KEY, employee_id BIGINT, survey TEXT, qkey TEXT, qtype TEXT,
  value TEXT, label TEXT, comment TEXT, created_at DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS ix_ans_emp ON survey_answers(employee_id);
CREATE TABLE IF NOT EXISTS bot_commands (name TEXT PRIMARY KEY, description TEXT, kind TEXT, reply TEXT, query TEXT,
  fallback TEXT, builtin INTEGER DEFAULT 0, sort INTEGER DEFAULT 0, updated_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS rewrites (key TEXT PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, name TEXT, channel TEXT, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS inbox (
  id BIGSERIAL PRIMARY KEY, user_id TEXT, payload TEXT, status TEXT DEFAULT 'processing', attempts INTEGER DEFAULT 1,
  error TEXT, created_at DOUBLE PRECISION, updated_at DOUBLE PRECISION);
"""

_pool: ConnectionPool | None = None
_lock = threading.Lock()


def url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://korsik:korsik@postgres:5432/korsik")


def _create_schema() -> None:
    with psycopg.connect(url(), autocommit=True) as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("SELECT pg_advisory_lock(727001)")  # несколько реплик сервиса стартуют одновременно — DDL по очереди
        try:
            c.execute(DDL)
        finally:
            c.execute("SELECT pg_advisory_unlock(727001)")


def pool() -> ConnectionPool:
    """Пул создаётся при первом обращении; схема и таблицы готовятся один раз."""
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _create_schema()
                _pool = ConnectionPool(url(), min_size=1, max_size=int(os.getenv("DB_POOL_MAX", "10")), open=True,
                                       kwargs={"row_factory": dict_row, "options": f"-c search_path={SCHEMA}"})
    return _pool


@contextmanager
def conn():
    """Соединение из пула: commit при успехе, rollback при ошибке."""
    with pool().connection() as c:
        yield c


def close() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


atexit.register(lambda: close())  # аккуратно закрываем пул при остановке процесса


def reset_schema() -> None:
    """Полностью пересоздаёт схему сервиса (тесты и сброс стенда)."""
    close()
    with psycopg.connect(url(), autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    pool()
