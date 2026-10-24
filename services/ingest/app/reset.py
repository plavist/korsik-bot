"""Сброс данных ingest: остаются только базовые документы из папки BASE_DOCS_DIR. Запуск: python -m app.reset"""
import asyncio

from . import base_docs, db


async def reset() -> dict:
    removed = []
    for d in db.list_all():
        if d.get("source") != "base":
            await base_docs.remove(d["id"])
            removed.append(d["filename"])
    synced = await base_docs.sync()          # докладываем недостающие базовые документы
    return {"removed": removed + synced["removed"], "added": synced["added"]}


if __name__ == "__main__":  # pragma: no cover
    print(asyncio.run(reset()))
