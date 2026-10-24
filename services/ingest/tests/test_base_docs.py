"""Базовые документы из папки BASE_DOCS_DIR: загрузка при старте, синхронизация, защита от удаления, сброс данных."""
import asyncio
import logging

import respx
from fastapi.testclient import TestClient

from app import base_docs, db, reset
from app.main import app
from test_api import mock_embed, mock_qdrant


def put(dir_, name, text):
    (dir_ / name).write_text(text, encoding="utf-8")


def names():
    return sorted((d["filename"], d["source"]) for d in db.list_all())


@respx.mock
def test_sync_adds_updates_and_removes(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("BASE_DOCS_DIR", str(base))
    mock_qdrant()
    mock_embed()
    put(base, "Правила.txt", "Рабочее время с 9 до 18.")
    put(base, "Отпуск.md", "Отпуск 28 дней.")
    (base / "картинка.exe").write_bytes(b"x")                       # неподдерживаемое — игнорируем
    put(base, "README.md", "описание папки, не документ")          # описание папки в базу знаний не попадает
    (base / "папка").mkdir()
    r = asyncio.run(base_docs.sync())
    assert sorted(r["added"]) == ["Отпуск.md", "Правила.txt"] and r["removed"] == []
    assert names() == [("Отпуск.md", "base"), ("Правила.txt", "base")] and all(d["status"] == "ready" for d in db.list_all())
    assert asyncio.run(base_docs.sync()) == {"added": [], "removed": []}                   # повтор ничего не меняет
    put(base, "Правила.txt", "Рабочее время с 8 до 17.")                                    # файл изменили → перезагружаем
    r = asyncio.run(base_docs.sync())
    assert r["added"] == ["Правила.txt"] and r["removed"] == ["Правила.txt"] and len(db.list_all()) == 2
    (base / "Отпуск.md").unlink()                                                           # файл убрали → документ удалён
    r = asyncio.run(base_docs.sync())
    assert r == {"added": [], "removed": ["Отпуск.md"]} and names() == [("Правила.txt", "base")]


@respx.mock
def test_existing_uploaded_document_is_adopted_not_duplicated(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("BASE_DOCS_DIR", str(base))
    mock_qdrant()
    mock_embed()
    db.create("old1", "ПВТР.txt", "ПВТР")                                                   # загружен раньше вручную (без хеша)
    db.update("old1", status="ready")
    db.create("old2", "Сломан.txt", "Сломан")                                               # в ошибке — заменяется загрузкой из папки
    db.update("old2", status="error")
    put(base, "ПВТР.txt", "текст")
    put(base, "Сломан.txt", "новый текст")
    r = asyncio.run(base_docs.sync())
    assert r["added"] == ["Сломан.txt"]
    docs = {d["id"]: d for d in db.list_all()}
    assert docs["old1"]["source"] == "base" and docs["old1"]["sha"]
    assert len(docs) == 3                                                                   # old1 усыновлён, для Сломан создан новый


@respx.mock
def test_base_document_cannot_be_deleted_from_panel_and_reset_keeps_it(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("BASE_DOCS_DIR", str(base))
    mock_qdrant()
    mock_embed()
    put(base, "Базовый.txt", "основной регламент")
    asyncio.run(base_docs.sync())
    client = TestClient(app)
    own = client.post("/documents", files={"file": ("Мой.txt", b"temporary text")}).json()["id"]
    base_id = next(d["id"] for d in db.list_all() if d["source"] == "base")
    r = client.delete(f"/documents/{base_id}")
    assert r.status_code == 400 and "BASE_DOCS_DIR" in r.json()["detail"]
    res = asyncio.run(reset.reset())
    assert res["removed"] == ["Мой.txt"] and client.get(f"/documents/{own}").status_code == 404
    assert names() == [("Базовый.txt", "base")]
    assert client.delete(f"/documents/{own}").status_code == 404


def test_missing_base_folder_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("BASE_DOCS_DIR", str(tmp_path / "нет-такой"))
    assert asyncio.run(base_docs.sync()) == {"added": [], "removed": []}


@respx.mock
def test_lifespan_syncs_in_background_and_survives_errors(tmp_path, monkeypatch, caplog):
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("BASE_DOCS_DIR", str(base))
    mock_qdrant()
    mock_embed()
    put(base, "А.txt", "текст")
    with TestClient(app):                                                                   # запуск приложения → синхронизация
        for _ in range(100):
            if db.list_all() and db.list_all()[0]["status"] == "ready":
                break
            import time
            time.sleep(0.05)
    assert names() == [("А.txt", "base")]

    async def boom():
        raise RuntimeError("qdrant недоступен")
    monkeypatch.setattr(base_docs, "sync", boom)
    with caplog.at_level(logging.ERROR, logger="ingest"):
        with TestClient(app):
            import time
            time.sleep(0.2)
    assert "base docs sync failed" in caplog.text
