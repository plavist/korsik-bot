"""Пользователи панели управления и журнал действий (SQLite в DATA_DIR). Администратор из .env (ADMIN_USER/ADMIN_PASSWORD) — всегда есть."""
import hashlib
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import rbac

LOGIN_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(login TEXT PRIMARY KEY, name TEXT, role TEXT, salt TEXT, pw_hash TEXT, active INTEGER DEFAULT 1, created_at REAL, manager TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, login TEXT, action TEXT);
"""
_cache: dict[tuple[str, str], bool] = {}  # пароль проверяется на каждый запрос браузера — scrypt кэшируем


@contextmanager
def conn():
    d = Path(os.getenv("DATA_DIR", "/tmp/admin-data"))
    d.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(d / "admin.db"))
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    if "manager" not in {r["name"] for r in c.execute("PRAGMA table_info(users)")}:  # миграция старой базы
        c.execute("ALTER TABLE users ADD COLUMN manager TEXT")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()


def root_login() -> str:
    return os.getenv("ADMIN_USER", "hr")


def authenticate(login: str, password: str) -> dict | None:
    """→ {login, name, role} или None. Пользователь из .env — администратор."""
    root_ok = secrets.compare_digest(login.encode(), root_login().encode()) & secrets.compare_digest(
        password.encode(), os.getenv("ADMIN_PASSWORD", "admin").encode())
    if root_ok:
        return {"login": login, "name": "Главный администратор", "role": "admin", "root": True}
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE login=? AND active=1", (login,)).fetchone()
    if not r:
        return None
    key = (login, hashlib.sha256((r["pw_hash"] + password).encode()).hexdigest())
    if not _cache.get(key):
        if not secrets.compare_digest(_hash(password, r["salt"]), r["pw_hash"]):
            return None
        _cache[key] = True
    return {"login": r["login"], "name": r["name"] or r["login"], "role": r["role"], "root": False, "manager": r["manager"] or ""}


def _check(login: str | None, name: str, role: str, password: str | None, new: bool) -> None:
    if new and secrets.compare_digest((login or "").encode(), root_login().encode()):
        raise ValueError("этот логин занят главным администратором")
    if new and not LOGIN_RE.match(login or ""):
        raise ValueError("логин: 3–32 символа, латиница, цифры, _ . -")
    if role not in rbac.ROLES:
        raise ValueError("неизвестная роль")
    if (new or password) and len(password or "") < 8:
        raise ValueError("пароль: не короче 8 символов")


def list_users() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT login, name, role, active, created_at, manager FROM users ORDER BY login").fetchall()
    return [{**dict(r), "active": bool(r["active"])} for r in rows]


def create(login: str, name: str, role: str, password: str, manager: str = "") -> None:
    _check(login, name, role, password, True)
    if role == "manager" and not manager.strip():
        raise ValueError("для руководителя укажите имя, как в карточках сотрудников")
    salt = secrets.token_hex(16)
    with conn() as c:
        if c.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            raise ValueError("такой пользователь уже есть")
        c.execute("INSERT INTO users(login,name,role,salt,pw_hash,active,created_at,manager) VALUES(?,?,?,?,?,1,?,?)",
                  (login, name.strip(), role, salt, _hash(password, salt), time.time(), manager.strip()))


def update(login: str, name: str | None, role: str | None, active: bool | None, password: str | None, manager: str | None = None) -> bool:
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
        if not r:
            return False
        role = role or r["role"]
        _check(None, name or "", role, password, False)
        c.execute("UPDATE users SET name=?, role=?, active=? WHERE login=?",
                  (r["name"] if name is None else name.strip(), role, r["active"] if active is None else int(active), login))
        if manager is not None:
            c.execute("UPDATE users SET manager=? WHERE login=?", (manager.strip(), login))
        if password:
            salt = secrets.token_hex(16)
            c.execute("UPDATE users SET salt=?, pw_hash=? WHERE login=?", (salt, _hash(password, salt), login))
    _cache.clear()
    return True


def delete(login: str) -> bool:
    with conn() as c:
        n = c.execute("DELETE FROM users WHERE login=?", (login,)).rowcount
    _cache.clear()
    return bool(n)


def log(login: str, action: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO audit(ts,login,action) VALUES(?,?,?)", (time.time(), login, action[:300]))


def audit(limit: int = 20, offset: int = 0) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT ts, login, action FROM audit ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))]


def audit_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
