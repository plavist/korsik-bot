"""Пользователи панели управления и журнал действий (PostgreSQL, схема admin). Администратор из .env (ADMIN_USER/ADMIN_PASSWORD) — всегда есть."""
import hashlib
import os
import re
import secrets
import time
from contextlib import contextmanager

from . import pg, rbac

LOGIN_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_cache: dict[tuple[str, str], bool] = {}  # пароль проверяется на каждый запрос браузера — scrypt кэшируем


@contextmanager
def conn():
    """Соединение с PostgreSQL (схема admin); commit при успехе, rollback при ошибке."""
    with pg.conn() as c:
        yield c


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
        r = c.execute("SELECT * FROM users WHERE login=%s AND active=1", (login,)).fetchone()
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
        if c.execute("SELECT 1 FROM users WHERE login=%s", (login,)).fetchone():
            raise ValueError("такой пользователь уже есть")
        c.execute("INSERT INTO users(login,name,role,salt,pw_hash,active,created_at,manager) VALUES(%s,%s,%s,%s,%s,1,%s,%s)",
                  (login, name.strip(), role, salt, _hash(password, salt), time.time(), manager.strip()))


def update(login: str, name: str | None, role: str | None, active: bool | None, password: str | None, manager: str | None = None) -> bool:
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE login=%s", (login,)).fetchone()
        if not r:
            return False
        role = role or r["role"]
        _check(None, name or "", role, password, False)
        c.execute("UPDATE users SET name=%s, role=%s, active=%s WHERE login=%s",
                  (r["name"] if name is None else name.strip(), role, r["active"] if active is None else int(active), login))
        if manager is not None:
            c.execute("UPDATE users SET manager=%s WHERE login=%s", (manager.strip(), login))
        if password:
            salt = secrets.token_hex(16)
            c.execute("UPDATE users SET salt=%s, pw_hash=%s WHERE login=%s", (salt, _hash(password, salt), login))
    _cache.clear()
    return True


def delete(login: str) -> bool:
    with conn() as c:
        n = c.execute("DELETE FROM users WHERE login=%s", (login,)).rowcount
    _cache.clear()
    return bool(n)


def log(login: str, action: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO audit(ts,login,action) VALUES(%s,%s,%s)", (time.time(), login, action[:300]))


def audit(limit: int = 20, offset: int = 0) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT ts, login, action FROM audit ORDER BY id DESC LIMIT %s OFFSET %s", (limit, offset))]


def audit_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM audit").fetchone()["n"]
