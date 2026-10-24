"""Подключение к PostgreSQL: пул соединений и собственная схема сервиса ("gateway").
Все сервисы используют одну базу (DATABASE_URL), у каждого — своя схема, поэтому имена таблиц не пересекаются.
Схема создаётся один раз (CREATE ... IF NOT EXISTS, идемпотентно), а не при каждом запросе."""
import atexit
import os
import threading
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = "gateway"
DDL = """
CREATE TABLE IF NOT EXISTS llm_usage (
  day TEXT, kind TEXT, model TEXT, requests BIGINT DEFAULT 0, prompt_tokens BIGINT DEFAULT 0, completion_tokens BIGINT DEFAULT 0,
  PRIMARY KEY (day, kind, model));
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
