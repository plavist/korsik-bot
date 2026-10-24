"""Картинки и файлы (PDF) для сообщений бота. Ничего не поставляется заранее: HR загружает картинки через панель управления или API
(DATA_DIR/uploads) и назначает их сообщениям сценария (поле image сообщения)."""
import hashlib
import re
from pathlib import Path

from . import config, db

UPLOAD_RE = re.compile(r"^up_[0-9a-f]{12}\.(jpg|png|pdf)$")
MAX_UPLOAD = 8 * 1024 * 1024


def uploads_dir() -> Path:
    return Path(config.data_dir()) / "uploads"


def path(fname: str) -> Path | None:
    """Путь к загруженному файлу по имени (только известные имена — без выхода из папки)."""
    p = uploads_dir() / (fname or "")
    return p if UPLOAD_RE.match(fname or "") and p.exists() else None


def is_file(fname: str | None) -> bool:
    """PDF уходит вложением-файлом, остальное — картинкой."""
    return bool(fname) and fname.endswith(".pdf")


def media_type(fname: str) -> str:
    return "application/pdf" if is_file(fname) else "image/png" if fname.endswith(".png") else "image/jpeg"


def save_upload(data: bytes, name: str = "") -> str:
    """Сохраняет картинку (JPEG/PNG) или PDF до 8 МБ → имя файла. ValueError — если формат не подошёл."""
    if len(data) > MAX_UPLOAD:
        raise ValueError("файл больше 8 МБ")
    if data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif data.startswith(b"%PDF"):
        ext = "pdf"
    else:
        raise ValueError("нужна картинка JPEG/PNG или файл PDF")
    fname = f"up_{hashlib.sha256(data).hexdigest()[:12]}.{ext}"
    uploads_dir().mkdir(parents=True, exist_ok=True)
    (uploads_dir() / fname).write_bytes(data)
    if name.strip():
        db.set_setting(f"imgname:{fname}", name.strip()[:80])
    return fname


def library() -> list[dict]:
    """Загруженные картинки."""
    files = sorted(uploads_dir().glob("up_*")) if uploads_dir().exists() else []
    return [{"file": p.name, "label": db.get_setting(f"imgname:{p.name}") or p.name, "kind": "file" if is_file(p.name) else "image"}
            for p in files if UPLOAD_RE.match(p.name)]


def remove(fname: str) -> bool:
    p = path(fname)
    if not p:
        return False
    p.unlink()
    db.set_setting(f"imgname:{fname}", "")
    db.set_setting(f"maximg:{fname}", "")
    return True


def for_item(step_id: str, idx: int, it: dict) -> str | None:
    """Вложение сообщения — только то, что выбрал HR (и оно ещё есть в библиотеке)."""
    img = it.get("image")
    return img if img and path(img) else None
