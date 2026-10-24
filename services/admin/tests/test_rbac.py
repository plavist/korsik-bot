import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import rbac, users
from app.main import app

client = TestClient(app)
ROOT = ("hr", "admin")


def mk(login, role, password="password1", name=""):
    r = client.post("/api/rbac/users", auth=ROOT, json={"login": login, "name": name, "role": role, "password": password})
    assert r.status_code == 200, r.text
    return (login, password)


def test_me_and_roles_for_root():
    me = client.get("/api/rbac/me", auth=ROOT).json()
    assert me["role"] == "admin" and me["root"] and "users" in me["perms"]
    roles = {r["role"]: r for r in client.get("/api/rbac/roles", auth=ROOT).json()}
    assert set(roles) == {"admin", "editor", "hr", "manager", "viewer"} and roles["viewer"]["perms"] == []


def test_user_lifecycle_and_login():
    u = mk("anna", "editor", name="Анна")
    me = client.get("/api/rbac/me", auth=u).json()
    assert me["role"] == "editor" and me["name"] == "Анна" and not me["root"]
    assert client.get("/api/rbac/me", auth=u).status_code == 200            # из кэша пароля
    assert client.get("/api/rbac/me", auth=("anna", "wrong-pass")).status_code == 401
    assert client.get("/api/rbac/me", auth=("ghost", "password1")).status_code == 401
    assert [x["login"] for x in client.get("/api/rbac/users", auth=ROOT).json()] == ["anna"]
    # смена роли и пароля админом; старый пароль перестаёт работать
    assert client.put("/api/rbac/users/anna", auth=ROOT, json={"role": "hr", "password": "newpassw0rd", "name": "Анна И."}).status_code == 200
    assert client.get("/api/rbac/me", auth=u).status_code == 401
    me = client.get("/api/rbac/me", auth=("anna", "newpassw0rd")).json()
    assert me["role"] == "hr" and me["name"] == "Анна И."
    # отключение
    client.put("/api/rbac/users/anna", auth=ROOT, json={"active": False})
    assert client.get("/api/rbac/me", auth=("anna", "newpassw0rd")).status_code == 401
    client.put("/api/rbac/users/anna", auth=ROOT, json={"active": True})
    assert client.get("/api/rbac/me", auth=("anna", "newpassw0rd")).status_code == 200
    assert client.delete("/api/rbac/users/anna", auth=ROOT).status_code == 200
    assert client.get("/api/rbac/me", auth=("anna", "newpassw0rd")).status_code == 401
    assert client.delete("/api/rbac/users/anna", auth=ROOT).status_code == 404
    assert client.put("/api/rbac/users/anna", auth=ROOT, json={"name": "Игорь"}).status_code == 404


@pytest.mark.parametrize("body,fragment", [
    ({"login": "a", "role": "hr", "password": "password1"}, "логин"),
    ({"login": "hr", "role": "hr", "password": "password1"}, "занят"),
    ({"login": "bob", "role": "god", "password": "password1"}, "роль"),
    ({"login": "bob", "role": "hr", "password": "short"}, "пароль"),
])
def test_user_validation(body, fragment):
    r = client.post("/api/rbac/users", auth=ROOT, json=body)
    assert r.status_code == 400 and fragment in r.json()["detail"]


def test_duplicate_user_and_bad_update():
    mk("dup", "viewer")
    assert client.post("/api/rbac/users", auth=ROOT, json={"login": "dup", "role": "hr", "password": "password1"}).status_code == 400
    assert client.put("/api/rbac/users/dup", auth=ROOT, json={"role": "god"}).status_code == 400
    assert client.put("/api/rbac/users/dup", auth=ROOT, json={"password": "x"}).status_code == 400


def test_only_admin_manages_users_and_audit():
    for role in ("editor", "hr", "viewer"):
        u = mk(f"u_{role}", role)
        assert client.get("/api/rbac/users", auth=u).status_code == 403
        assert client.post("/api/rbac/users", auth=u, json={"login": "zzz", "role": "admin", "password": "password1"}).status_code == 403
        assert client.put("/api/rbac/users/u_hr", auth=u, json={"role": "admin"}).status_code == 403
        assert client.delete("/api/rbac/users/u_hr", auth=u).status_code == 403
        assert client.get("/api/rbac/audit", auth=u).status_code == 403
        assert client.get("/api/rbac/me", auth=u).status_code == 200           # свой профиль и роли видны всем
    second_admin = mk("boss2", "admin")
    assert client.get("/api/rbac/users", auth=second_admin).status_code == 200


def test_cannot_lock_yourself_out():
    boss = mk("boss2", "admin")
    assert client.put("/api/rbac/users/boss2", auth=boss, json={"active": False}).status_code == 400
    assert client.put("/api/rbac/users/boss2", auth=boss, json={"role": "viewer"}).status_code == 400
    assert client.put("/api/rbac/users/boss2", auth=boss, json={"role": "admin", "name": "Босс"}).status_code == 200
    assert client.delete("/api/rbac/users/boss2", auth=boss).status_code == 400


def test_change_own_password():
    u = mk("pwuser", "viewer")
    assert client.post("/api/rbac/password", auth=u, json={"old": "bad", "new": "newpassw0rd"}).status_code == 400
    assert client.post("/api/rbac/password", auth=u, json={"old": "password1", "new": "short"}).status_code == 400
    assert client.post("/api/rbac/password", auth=u, json={"old": "password1", "new": "newpassw0rd"}).status_code == 200
    assert client.get("/api/rbac/me", auth=("pwuser", "newpassw0rd")).status_code == 200
    assert client.post("/api/rbac/password", auth=ROOT, json={"old": "admin", "new": "newpassw0rd"}).status_code == 400  # root — через .env


# ---------- права на проксируемые маршруты ----------
WRITES = {  # (метод, сервис, путь) → право
    ("POST", "ingest", "documents"): "docs", ("DELETE", "ingest", "documents/1"): "docs",
    ("PUT", "pipeline", "api/scenario/text"): "scenario", ("PUT", "pipeline", "api/scenario/image"): "scenario",
    ("POST", "pipeline", "api/scenario/images"): "scenario",
    ("POST", "pipeline", "api/commands"): "commands", ("PUT", "pipeline", "api/commands/x"): "commands",
    ("DELETE", "pipeline", "api/commands/x"): "commands",
    ("POST", "pipeline", "api/employees"): "employees", ("POST", "pipeline", "api/employees/1/send-next"): "employees",
    ("POST", "pipeline", "api/scheduler/tick"): "employees",
    ("POST", "pipeline", "api/escalations/1/reply"): "escalations", ("POST", "pipeline", "api/escalations/1/status"): "escalations",
    ("POST", "pipeline", "api/mode"): "channel", ("POST", "pipeline", "api/max/subscribe"): "channel",
    ("POST", "pipeline", "api/max/commands"): "channel",
    ("POST", "pipeline", "api/sites"): "scenario", ("PUT", "pipeline", "api/sites/Z"): "scenario", ("DELETE", "pipeline", "api/sites/Z"): "scenario",
    ("POST", "pipeline", "api/scenario/steps"): "scenario", ("DELETE", "pipeline", "api/scenario/steps/x"): "scenario",
    ("POST", "pipeline", "api/scenario/steps/x/items/0/move"): "scenario", ("POST", "pipeline", "api/scenario/restore"): "scenario",
    ("POST", "pipeline", "api/unknown"): "users",
}


def test_required_permission_table():
    for (m, svc, p), perm in WRITES.items():
        assert rbac.required(m, svc, p) == perm, (m, svc, p)
    assert rbac.required("GET", "pipeline", "api/analytics") is None
    assert rbac.required("GET", "pipeline", "api/max/debug") == "channel"
    assert rbac.required("POST", "pipeline", "api/commands/x/preview") is None
    assert rbac.can("viewer", None) and not rbac.can("viewer", "docs") and not rbac.can("nope", "docs") and rbac.can("admin", "users")


@respx.mock
@pytest.mark.parametrize("role,allowed", [
    ("admin", {"docs", "scenario", "commands", "employees", "escalations", "channel", "users"}),
    ("editor", {"docs", "scenario", "commands"}),
    ("hr", {"employees", "escalations"}),
    ("viewer", set()),
])
def test_proxy_enforces_roles(role, allowed):
    respx.route(host="pipeline").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.route(host="ingest").mock(return_value=httpx.Response(200, json={"ok": True}))
    u = mk(f"t_{role}", role)
    for (m, svc, p), perm in WRITES.items():
        r = client.request(m, f"/api/{svc}/{p}", auth=u, json={})
        assert (r.status_code == 200) == (perm in allowed), (role, m, svc, p, r.status_code)
        if r.status_code == 403:
            assert "Недостаточно прав" in r.json()["detail"]
    assert client.get("/api/pipeline/api/analytics", auth=u).status_code == 200            # чтение — всем
    assert client.get("/api/pipeline/api/max/debug", auth=u).status_code == (200 if "channel" in allowed else 403)
    assert client.post("/api/pipeline/api/commands/x/preview", auth=u, json={}).status_code == 200


@respx.mock
def test_forbidden_write_never_reaches_upstream_and_audit_records_success():
    route = respx.put("http://pipeline:8000/api/scenario/text").mock(return_value=httpx.Response(200, json={"ok": True}))
    viewer, editor = mk("viewer1", "viewer"), mk("editor1", "editor", name="Ed")
    assert client.put("/api/pipeline/api/scenario/text", auth=viewer, json={}).status_code == 403 and not route.called
    assert client.put("/api/pipeline/api/scenario/text", auth=editor, json={}).status_code == 200 and route.called
    respx.put("http://pipeline:8000/api/scenario/image").mock(return_value=httpx.Response(400, json={"detail": "bad"}))
    client.put("/api/pipeline/api/scenario/image", auth=editor, json={})              # ошибка апстрима — в журнал не пишем
    log = client.get("/api/rbac/audit", auth=ROOT).json()["items"]
    actions = [(a["login"], a["action"]) for a in log]
    assert ("editor1", "PUT pipeline/api/scenario/text") in actions and not any("scenario/image" in a for _, a in actions)
    assert any(a.startswith("создал пользователя editor1") for _, a in actions)


def test_users_module_edge_cases():
    users.create("solo", "", "viewer", "password1")
    assert users.authenticate("solo", "password1")["name"] == "solo"      # без имени — логин


# ---------- роль «Руководитель подразделения» ----------
@respx.mock
def test_manager_role_is_scoped_and_read_only():
    route = respx.get("http://pipeline:8000/api/analytics").mock(return_value=httpx.Response(200, json={}))
    emp = respx.get("http://pipeline:8000/api/employees/5").mock(return_value=httpx.Response(200, json={}))
    other = respx.get("http://pipeline:8000/api/escalations").mock(return_value=httpx.Response(200, json=[]))
    r = client.post("/api/rbac/users", auth=ROOT, json={"login": "boss_ivanov", "name": "Иванов", "role": "manager", "password": "password1"})
    assert r.status_code == 400 and "руководителя" in r.json()["detail"]                    # нужно ФИО руководителя
    assert client.post("/api/rbac/users", auth=ROOT, json={"login": "boss_ivanov", "name": "Иванов", "role": "manager",
                                                           "password": "password1", "manager": " Сергей Иванов "}).status_code == 200
    u = ("boss_ivanov", "password1")
    assert client.get("/api/rbac/me", auth=u).json()["manager"] == "Сергей Иванов"
    assert client.get("/api/rbac/users", auth=ROOT).json()[0]["manager"] == "Сергей Иванов"
    # чтение аналитики и сотрудников — всегда с принудительным фильтром по руководителю (подмена из запроса не работает)
    client.get("/api/pipeline/api/analytics?manager=Чужой&days=7", auth=u)
    assert "manager=%D0%A1%D0%B5%D1%80%D0%B3%D0%B5%D0%B9" in str(route.calls[0].request.url) and "days=7" in str(route.calls[0].request.url)
    assert "%D0%A7%D1%83%D0%B6%D0%BE%D0%B9" not in str(route.calls[0].request.url)
    assert client.get("/api/pipeline/api/employees/5", auth=u).status_code == 200 and emp.called
    # всё остальное — запрещено: обращения, диалоги, дайджест, запись
    for method, path in (("GET", "api/escalations"), ("GET", "api/users/x/history"), ("GET", "api/insights/digest"),
                         ("GET", "api/scenario"), ("POST", "api/employees"), ("PUT", "api/scenario/text"), ("GET", "api/max/status")):
        assert client.request(method, f"/api/pipeline/{path}", auth=u, json={}).status_code == 403, path
    assert not other.called
    assert client.get("/api/ingest/documents", auth=u).status_code == 403
    # руководитель без привязанного ФИО ничего не увидит
    client.put("/api/rbac/users/boss_ivanov", auth=ROOT, json={"manager": ""})
    assert client.get("/api/pipeline/api/analytics", auth=u).status_code == 403


def test_manager_permissions_table_and_insights_writes():
    assert rbac.required("POST", "pipeline", "api/insights/digest/send") == "employees"
    assert rbac.required("PUT", "pipeline", "api/insights/digest/settings") == "employees"
    assert rbac.required("GET", "pipeline", "api/insights/digest") is None
    assert rbac.manager_can_read("pipeline", "api/analytics/drill") and rbac.manager_can_read("pipeline", "api/employees")
    assert not rbac.manager_can_read("ingest", "api/analytics") and not rbac.manager_can_read("pipeline", "api/analyticsX")
    assert "manager" in {r["role"] for r in client.get("/api/rbac/roles", auth=ROOT).json()}


def test_users_migration_adds_manager_column(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    c = sqlite3.connect(str(tmp_path / "admin.db"))
    c.execute("CREATE TABLE users(login TEXT PRIMARY KEY, name TEXT, role TEXT, salt TEXT, pw_hash TEXT, active INTEGER DEFAULT 1, created_at REAL)")
    c.commit()
    c.close()
    assert users.list_users() == []


# ---------- пароли хранятся только в виде хеша ----------
def test_passwords_are_stored_hashed_not_plain():
    import sqlite3
    import os
    secret = "Sup3r-Secret-Pass"
    mk("hashed", "viewer", password=secret)
    db = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "admin.db"))
    row = db.execute("SELECT salt, pw_hash FROM users WHERE login='hashed'").fetchone()
    raw = open(os.path.join(os.environ["DATA_DIR"], "admin.db"), "rb").read()
    assert secret.encode() not in raw and row[1] != secret and len(row[1]) == 128 and len(row[0]) == 32     # scrypt: 64 байта, соль 16 байт
    mk("hashed2", "viewer", password=secret)
    assert db.execute("SELECT pw_hash FROM users WHERE login='hashed2'").fetchone()[0] != row[1]           # у одинаковых паролей разная соль → разный хеш
    assert users._hash(secret, row[0]) == row[1] and users._hash("другой", row[0]) != row[1]


@respx.mock
def test_sites_are_readable_by_manager_role():
    respx.get("http://pipeline:8000/api/sites").mock(return_value=httpx.Response(200, json=[]))
    client.post("/api/rbac/users", auth=ROOT, json={"login": "mgr_sites", "name": "Михаил", "role": "manager", "password": "password1", "manager": "Сергей Иванов"})
    assert client.get("/api/pipeline/api/sites", auth=("mgr_sites", "password1")).status_code == 200


# ---------- проверка вводимых данных ----------
@pytest.mark.parametrize("body,fragment", [
    ({"login": "ivan", "role": "hr", "password": "pass"}, "от 8 до 128"),
    ({"login": "ivan", "role": "hr", "password": "onlyletters"}, "буква и одна цифра"),
    ({"login": "ivan", "role": "hr", "password": "12345678"}, "буква и одна цифра"),
    ({"login": "ivan", "role": "hr", "password": " password1 "}, "пробелы"),
    ({"login": "ivan1234", "role": "hr", "password": "IVAN1234"}, "совпадать с логином"),
    ({"login": "ivan", "role": "hr", "password": "a1" * 70}, "максимум 256" if False else "от 8 до 128"),
    ({"login": "ivan", "role": "hr", "password": "a1" * 200}, "максимум 256"),
    ({"login": "bad login!", "role": "hr", "password": "password1"}, "логин"),
    ({"login": "ivan", "role": "hr", "password": "password1", "name": "12"}, "имя"),
    ({"login": "ivan", "role": "hr", "password": "password1", "name": "<b>x</b>"}, "имя"),
    ({"login": "ivan", "role": "manager", "password": "password1", "manager": "###"}, "ФИО руководителя"),
    ({"login": "ivan", "role": "hr"}, "Пароль: обязательное поле"),
])
def test_user_input_validation_messages(body, fragment):
    r = client.post("/api/rbac/users", auth=ROOT, json=body)
    assert r.status_code == 400 and fragment in r.json()["detail"], r.text


def test_user_patch_and_password_change_validation_and_normalisation():
    mk("normal", "viewer")
    assert client.put("/api/rbac/users/normal", auth=ROOT, json={"password": "short1"}).status_code == 400
    assert client.put("/api/rbac/users/normal", auth=ROOT, json={"name": "1"}).status_code == 400
    assert client.put("/api/rbac/users/normal", auth=ROOT, json={"manager": "!!"}).status_code == 400
    assert client.put("/api/rbac/users/normal", auth=ROOT, json={"name": "  Иван   Петров "}).status_code == 200
    assert client.get("/api/rbac/users", auth=ROOT).json()[0]["name"] == "Иван Петров"
    assert client.post("/api/rbac/password", auth=("normal", "password1"), json={"old": "password1", "new": "alllettersnodigit"}).status_code == 400
    assert client.post("/api/rbac/password", auth=("normal", "password1"), json={"old": "password1"}).status_code == 400


def test_users_module_rejects_short_password_when_called_directly():
    with pytest.raises(ValueError):
        users.create("direct", "", "viewer", "short")


def test_audit_log_is_paginated():
    for i in range(23):
        users.log("admin", f"действие {i}")
    p1 = client.get("/api/rbac/audit?size=10", auth=ROOT).json()
    assert p1["total"] == 23 and p1["pages"] == 3 and p1["page"] == 1 and len(p1["items"]) == 10 and p1["items"][0]["action"] == "действие 22"
    p3 = client.get("/api/rbac/audit?size=10&page=3", auth=ROOT).json()
    assert len(p3["items"]) == 3 and p3["items"][-1]["action"] == "действие 0"
    assert client.get("/api/rbac/audit?size=10&page=99", auth=ROOT).json()["page"] == 3             # за пределами — последняя страница
    assert client.get("/api/rbac/audit", auth=ROOT).json()["size"] == 20
    for bad in ("page=0", "size=1", "size=500", "page=x"):
        assert client.get(f"/api/rbac/audit?{bad}", auth=ROOT).status_code == 400
    assert client.get("/api/rbac/audit?size=10", auth=ROOT).json()["pages"] == 3


def test_manager_name_needs_name_and_surname():
    for manager in ("Сергей", "12 34", "###"):
        r = client.post("/api/rbac/users", auth=ROOT, json={"login": "mgr_bad", "role": "manager", "password": "password1", "manager": manager})
        assert r.status_code == 400 and "ФИО руководителя" in r.json()["detail"], manager
    assert client.put("/api/rbac/users/ghost", auth=ROOT, json={"manager": "Один"}).status_code == 400


def test_reset_removes_panel_users_and_audit_but_not_root():
    from app import reset
    mk("tempuser", "viewer")
    users.log("admin", "что-то сделал")
    res = reset.reset()
    assert res["users"] == 1 and res["audit"] >= 2
    assert client.get("/api/rbac/me", auth=("tempuser", "password1")).status_code == 401       # пользователь удалён
    assert client.get("/api/rbac/me", auth=ROOT).status_code == 200                           # главный администратор остался
    assert client.get("/api/rbac/users", auth=ROOT).json() == []
