"""Базовые документы: всё, что лежит в папке BASE_DOCS_DIR, автоматически попадает в базу знаний (и убирается, когда файл удалён из папки)."""
import hashlib
import logging
import shutil
import uuid
from pathlib import Path

from . import config, db, extract, qdrant
from .pipeline import process, texts_dir

log = logging.getLogger("ingest")


def _files() -> list[Path]:
    root = Path(config.base_docs_dir())
    return sorted(p for p in root.iterdir() if p.is_file() and extract.supported(p.name) and not p.name.upper().startswith("README")) if root.is_dir() else []


async def remove(doc_id: str) -> None:
    """Полностью удаляет документ: векторы, текст, загруженный файл, запись."""
    await qdrant.delete_doc(doc_id)
    db.delete(doc_id)
    for p in [texts_dir() / f"{doc_id}.txt", *Path(config.data_dir(), "uploads").glob(f"{doc_id}.*")]:
        p.unlink(missing_ok=True)


async def sync() -> dict:
    """Синхронизирует базу знаний с папкой: новое — загружает, изменённое — перезагружает, исчезнувшее — удаляет. Идемпотентно."""
    added, removed = [], []
    docs = db.list_all()
    by_sha = {d["sha"]: d for d in docs if d.get("sha") and d.get("source") == "base"}
    by_name = {d["filename"]: d for d in docs if not d.get("sha") and d.get("source") != "base"}  # загруженные ранее вручную — «усыновляем»
    seen = set()
    for f in _files():
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        seen.add(sha)
        if sha in by_sha:
            continue
        old = by_name.get(f.name)
        if old and old["status"] == "ready":
            with db.conn() as c:
                c.execute("UPDATE documents SET source='base', sha=%s WHERE id=%s", (sha, old["id"]))
            continue
        doc_id = uuid.uuid4().hex
        uploads = Path(config.data_dir()) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / f"{doc_id}{f.suffix.lower()}"
        shutil.copyfile(f, path)
        db.create(doc_id, f.name, f.stem, source="base", sha=sha)
        await process(doc_id, str(path), f.name, f.stem)
        added.append(f.name)
    for d in db.list_all():
        if d.get("source") == "base" and d.get("sha") not in seen:
            await remove(d["id"])
            removed.append(d["filename"])
    log.info("base docs: добавлено %s, удалено %s", added, removed)
    return {"added": added, "removed": removed}
