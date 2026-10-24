"""Проверка вводимых данных: понятные сообщения на русском и отказ от некорректных значений."""
from datetime import date, timedelta

import pytest

from app import validation as V
from test_api import anon, client
from test_onboarding import make_emp

TODAY = date.today()


def err(r):
    assert r.status_code == 400, (r.status_code, r.text)
    return r.json()["detail"]


# ---------- сотрудники ----------
@pytest.mark.parametrize("body,fragment", [
    ({"name": "", "site": "GrPP", "hire_date": "2026-11-02"}, "Имя: слишком короткое"),
    ({"name": "A", "site": "GrPP", "hire_date": "2026-11-02"}, "минимум 2"),
    ({"name": "Иван" * 40, "site": "GrPP", "hire_date": "2026-11-02"}, "максимум 100"),
    ({"name": "12345 67890", "site": "GrPP", "hire_date": "2026-11-02"}, "содержать буквы"),
    ({"name": "<b>Иван</b> Петров", "site": "GrPP", "hire_date": "2026-11-02"}, "недопустимые символы"),
    ({"name": "Иван Петров", "site": "Mars", "hire_date": "2026-11-02"}, "неизвестная площадка"),
    ({"name": "Иван Петров", "site": "GrPP", "hire_date": "02.11.2026"}, "ГГГГ-ММ-ДД"),
    ({"name": "Иван Петров", "site": "GrPP", "hire_date": "1990-01-01"}, "дата выхода должна быть"),
    ({"name": "Иван Петров", "site": "GrPP", "hire_date": (TODAY + timedelta(days=900)).isoformat()}, "дата выхода должна быть"),
    ({"name": "Иван Петров", "site": "GrPP", "hire_date": "2026-11-02", "probation_end": "2026-10-01"}, "конец испытательного срока"),
    ({"name": "Иван Петров", "site": "GrPP", "hire_date": "2026-11-02", "probation_end": "2028-01-01"}, "конец испытательного срока"),
    ({"name": "Иван Петров", "site": "GrPP", "hire_date": "2026-11-02", "manager": "X"}, "руководитель"),
    ({"name": "Иван", "site": "GrPP", "hire_date": "2026-11-02"}, "укажите имя и фамилию"),
    ({"name": "Иван Петров", "site": "GrPP", "hire_date": "2026-11-02", "manager": "Иванов"}, "руководитель: укажите имя и фамилию"),
    ({"name": "Иван Петров", "site": "GrPP", "hire_date": "2026-11-02", "manager": "Сергей Иванов", "manager_user": "id with spaces!"}, "ID чата руководителя"),
    ({"name": "Иван Петров", "site": "GrPP"}, "Дата выхода: обязательное поле"),
])
def test_employee_create_rejects_bad_input(body, fragment):
    assert fragment in err(client.post("/api/employees", json=body))


def test_employee_create_normalizes_input_and_patch_checks_dates_against_saved():
    e = client.post("/api/employees", json={"name": "  Иван   Петров ", "site": "GrPP", "hire_date": "2026-11-02", "manager": " Сергей  Иванов ", "manager_user": " 12345 "}).json()
    assert e["name"] == "Иван Петров" and e["manager"] == "Сергей Иванов" and e["manager_user"] == "12345"
    eid = e["id"]
    assert "после даты выхода" in err(client.patch(f"/api/employees/{eid}", json={"probation_end": "2026-10-01"}))
    assert client.patch(f"/api/employees/{eid}", json={"probation_end": "2027-01-15"}).json()["probation_end"] == "2027-01-15"
    assert "после даты выхода" in err(client.patch(f"/api/employees/{eid}", json={"hire_date": "2027-02-01"}))       # новая дата выхода после сохранённого конца
    assert "не может быть пустой" in err(client.patch(f"/api/employees/{eid}", json={"hire_date": ""}))
    assert client.patch(f"/api/employees/{eid}", json={"probation_end": ""}).json()["probation_end"] is None          # очистка допустима
    assert "ГГГГ-ММ-ДД" in err(client.patch(f"/api/employees/{eid}", json={"hire_date": "вчера"}))
    assert "площадка" in err(client.patch(f"/api/employees/{eid}", json={"site": "Mars"}))
    assert "имя" in err(client.patch(f"/api/employees/{eid}", json={"name": "999"})) and "фамилию" in err(client.patch(f"/api/employees/{eid}", json={"name": "Пётр"}))
    assert client.patch(f"/api/employees/{eid}", json={"manager": "", "manager_user": ""}).json()["manager"] in ("", None)
    assert "руководитель" in err(client.patch(f"/api/employees/{eid}", json={"manager": "Z"}))
    assert "ID" in err(client.patch(f"/api/employees/{eid}", json={"manager_user": "a b"}))
    assert client.patch(f"/api/employees/{eid}", json={"manager_user": "77", "name": "Пётр Сидоров"}).json()["name"] == "Пётр Сидоров"


# ---------- прочие поля ----------
def test_reply_status_subscribe_and_misc_validation():
    assert "максимум 4000" in err(client.post("/api/escalations/1/reply", json={"text": "я" * 4001}))
    assert "управляющие" in err(client.post("/api/escalations/1/reply", json={"text": "ok\x00"}))
    assert "неверный формат" in err(client.post("/api/escalations/1/status", json={"status": "bogus"}))
    assert "https" in err(client.post("/api/max/subscribe", json={"url": "http://example.com/hook"}))
    assert "https" in err(client.post("/api/max/subscribe", json={"url": "https://exa mple.com"}))
    assert "максимум 24" not in err(client.post("/api/max/subscribe", json={"url": "ftp://x.y/z"}))
    assert "неверные дата и время" in err(client.post("/api/scheduler/tick", json={"now": "завтра"}))
    emp = make_emp(user="v1")
    assert "больше" in err(client.post(f"/api/employees/{emp['id']}/age-pending", json={"hours": 0}))
    assert "не больше" in err(client.post(f"/api/employees/{emp['id']}/age-pending", json={"hours": 1000}))
    assert "нужно число" in err(client.post(f"/api/employees/{emp['id']}/age-pending", json={"hours": "много"}))
    assert "нужно «да» или «нет»" in err(client.patch(f"/api/employees/{emp['id']}", json={"active": "возможно"}))
    assert "нужен список" in err(client.post("/api/scenario/steps", json={"title": "T", "due": None, "sites": "GTP"}))
    assert "нужен объект" in err(client.post("/api/scenario/steps", json={"title": "T", "due": "завтра"}))
    assert "нужно целое число" in err(client.put("/api/scenario/text", json={"step_id": "d1_lunch", "idx": "x", "text": "t"}))
    assert "некорректный JSON" in err(client.post("/api/scenario/steps", content=b"{bad", headers={"content-type": "application/json"}))
    assert "не меньше 0" in err(client.put("/api/scenario/text", json={"step_id": "d1_lunch", "idx": -1, "text": "t"}))
    assert "не больше 500" in err(client.put("/api/scenario/text", json={"step_id": "d1_lunch", "idx": 9999, "text": "t"}))
    assert "слишком много" in err(client.post("/api/scenario/steps", json={"title": "T", "due": None, "items": [{"type": "msg", "text": "x"}] * 61}))


def test_bind_digest_filters_and_upload_validation():
    emp = make_emp(user=None)
    assert "user_id" in err(client.post(f"/api/employees/{emp['id']}/bind", json={"user_id": "bad id!"}))
    assert "получателей не больше 20" in err(client.put("/api/insights/digest/settings", json={"recipients": ",".join(str(i) for i in range(21))}))
    assert "получатели" in err(client.put("/api/insights/digest/settings", json={"recipients": "123, abc def"}))
    assert "максимум 500" in err(client.put("/api/insights/digest/settings", json={"recipients": "1," * 300}))
    assert "период" in err(client.get("/api/analytics?days=abc")) and "период" in err(client.get("/api/analytics?days=99999"))
    assert "неизвестная площадка" in err(client.get("/api/analytics?site=Mars"))
    assert "руководитель" in err(client.get("/api/insights/risk?manager=" + "я" * 101))
    assert client.get("/api/analytics?days=30&site=GTP").status_code == 200
    assert "неверный формат" in err(client.get("/api/analytics/drill?sentiment=happy")) and "неверный формат" in err(client.get("/api/analytics/drill?day=вчера"))
    assert "неизвестная тема" in err(client.get("/api/analytics/drill?topic=" + "т" * 81)) and "неизвестная тема" in err(client.get("/api/analytics?topic=Космос")) and "максимум 64" in err(client.get("/api/analytics/drill?user_id=" + "u" * 65))
    assert client.get("/api/analytics/drill?sentiment=negative&day=2026-10-24").status_code == 200
    assert "максимум 80" in err(client.post("/api/scenario/images?name=" + "н" * 81, content=b"\xff\xd8\xff123"))


def test_scenario_and_sites_validation_messages():
    assert "максимум 120" in err(client.post("/api/scenario/steps", json={"title": "Я" * 121, "due": None}))
    assert "минимум 1" in err(client.post("/api/scenario/steps", json={"title": "   ", "due": None}))
    assert "максимум 3500" in err(client.post("/api/scenario/steps/welcome/items", json={"text": "я" * 3501}))
    assert "неверный формат" in err(client.post("/api/scenario/steps/welcome/items", json={"type": "video", "text": "x"}))
    assert "максимум 60" in err(client.post("/api/sites", json={"code": "Ok1", "title": "Я" * 61}))
    assert "минимум 2" in err(client.post("/api/sites", json={"code": "x", "title": "T"}))
    assert "максимум 3500" in err(client.put("/api/scenario/steps/welcome/items/0", json={"text": "я" * 3501}))
    assert "максимум 8000" in err(client.post("/api/commands", json={"name": "abc", "reply": "я" * 8001}))


def test_validation_helpers_directly():
    assert V.person_name("  Анна-Мария   О'Нил ") == "Анна-Мария О'Нил"
    assert V.user_ref("  ", "x") is None and V.user_ref("12", "x") == "12"
    assert V.iso_date(None, "d") is None and V.iso_date("2026-01-01", "d") == date(2026, 1, 1)
    with pytest.raises(ValueError):
        V.employee_dates(None, None)
    assert V.no_control("текст\nс переводом\tстроки", "f") and V.no_control(None, "f") is None
