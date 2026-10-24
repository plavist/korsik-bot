import time
from contextlib import contextmanager

from . import pg

COLUMNS = {"filename", "title", "status", "error", "pages", "chunks", "ocr_pages", "source", "sha"}


@contextmanager
def conn():
    """Соединение с PostgreSQL (схема ingest); commit при успехе, rollback при ошибке."""
    with pg.conn() as c:
        yield c


def create(doc_id: str, filename: str, title: str, source: str = "upload", sha: str | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO documents (id, filename, title, status, created_at, updated_at, source, sha) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (doc_id, filename, title, "processing", time.time(), time.time(), source, sha),
        )


def update(doc_id: str, **fields) -> None:
    unknown = set(fields) - COLUMNS
    if unknown:  # имена колонок попадают в SQL — только из белого списка
        raise ValueError(f"неизвестные поля документа: {sorted(unknown)}")
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=%s" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE documents SET {cols} WHERE id=%s", (*fields.values(), doc_id))


def get(doc_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM documents WHERE id=%s", (doc_id,)).fetchone()
        return dict(r) if r else None


def list_all() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM documents ORDER BY created_at DESC")]


def delete(doc_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
