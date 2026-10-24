"""Редактор сценария и площадки: этапы, сроки, порядок, сообщения, разделение по площадкам."""
from datetime import date, datetime

import pytest

from app import db, engine, scenario_edit
from app import scenario as sc
from test_api import client
from test_onboarding import make_emp


# ---------- срок отправки ----------
def test_due_spec_validation_and_computation():
    d = {"hire": date(2026, 11, 2), "end": date(2027, 1, 31)}
    cases = [({"anchor": "hire", "time": "10:00"}, datetime(2026, 11, 2, 10, 0), "В день выхода, 10:00"),
             ({"anchor": "hire", "days": -1, "time": "10:00"}, datetime(2026, 11, 1, 10, 0), "За 1 дн. до выхода, 10:00"),
             ({"anchor": "hire", "days": 2, "workdays": True, "time": "09:30"}, datetime(2026, 11, 4, 9, 30), "Через 2 раб. дн. после выхода, 09:30"),
             ({"anchor": "hire", "months": 1, "days": 14, "time": "10:00"}, datetime(2026, 12, 16, 10, 0), "Через 1 мес. 14 дн. после выхода, 10:00"),
             ({"anchor": "end", "time": "09:00"}, datetime(2027, 1, 31, 9, 0), "В последний день испытательного срока, 09:00"),
             ({"anchor": "end", "days": -7, "time": "09:00"}, datetime(2027, 1, 24, 9, 0), "За 7 дн. до конца испытательного срока, 09:00")]
    for spec, when, text in cases:
        norm = sc.validate_due(spec)
        assert sc.due_datetime(norm, d) == when and sc.describe_due(norm) == text
    assert sc.validate_due(None) is None and "привязк" in sc.describe_due(None)
    for bad in ({"anchor": "x", "time": "10:00"}, {"anchor": "hire", "time": "25:00"}, {"anchor": "hire", "time": "9:00"},
                {"anchor": "hire", "time": "ab"}, {"anchor": "hire", "days": "z", "time": "10:00"}, {"anchor": "hire", "days": -1, "workdays": True, "time": "10:00"},
                {"anchor": "hire", "days": 9999, "time": "10:00"}, "строка"):
        with pytest.raises(ValueError):
            sc.validate_due(bad)


def test_default_scenario_has_declarative_due_and_welcome_event_based():
    assert sc.STEP_BY_ID["welcome"]["due"] is None and sc.STEP_BY_ID["d1_congrats"]["due"]["time"] == "10:00"
    emp = {"hire_date": "2026-11-02", "probation_end": None}
    assert sc.due_at(sc.STEP_BY_ID["d3_survey"], emp) == datetime(2026, 11, 4, 17, 30) and sc.due_at(sc.STEP_BY_ID["welcome"], emp) is None


# ---------- этапы ----------
def test_create_update_move_delete_step():
    n = len(sc.STEPS)
    sid = client.post("/api/scenario/steps", json={
        "title": "Встреча с наставником", "due": {"anchor": "hire", "days": 4, "workdays": True, "time": "14:00"}, "sites": ["GTP"], "gate": True,
        "survey": True, "after": "d1_end", "items": [{"type": "msg", "text": "Привет, {name}! Сегодня встреча"}, {"type": "scale", "text": "Оцените встречу"},
                                                     {"type": "yesno", "text": "Всё понятно?"}, {"type": "text", "text": "Что улучшить?"}]}).json()["id"]
    assert len(sc.STEPS) == n + 1 and [s["id"] for s in sc.STEPS].index(sid) == [s["id"] for s in sc.STEPS].index("d1_end") + 1
    st = sc.STEP_BY_ID[sid]
    assert st["survey"] == sid and st["gate"] == "recruiter" and st["sites"] == ["GTP"] and "Через 4 раб. дн." in st["when"]
    assert [i["t"] for i in st["items"]] == ["msg", "scale", "yesno", "text"] and len({i.get("key") for i in st["items"][1:]}) == 3
    assert client.put(f"/api/scenario/steps/{sid}", json={"title": "Встреча 2", "due": {"anchor": "end", "days": -3, "time": "10:15"},
                                                          "sites": None, "office": True, "gate": False, "survey": False}).status_code == 200
    st = sc.STEP_BY_ID[sid]
    assert st["title"] == "Встреча 2" and st["sites"] is None and st["office"] is True and st["gate"] is None and st["survey"] is None and "за 3 дн." in st["when"].lower()
    assert client.put(f"/api/scenario/steps/{sid}", json={"survey": True}).status_code == 200 and sc.STEP_BY_ID[sid]["survey"] == sid
    ids = lambda: [s["id"] for s in sc.STEPS]  # noqa: E731
    pos = ids().index(sid)
    client.post(f"/api/scenario/steps/{sid}/move", json={"direction": "up"})
    assert ids().index(sid) == pos - 1
    client.post(f"/api/scenario/steps/{sid}/move", json={"direction": "down"})
    client.post(f"/api/scenario/steps/{sid}/move", json={"direction": "down"})
    assert ids().index(sid) == pos + 1
    first = ids()[0]
    client.post(f"/api/scenario/steps/{first}/move", json={"direction": "up"})            # за границу — без изменений
    assert ids()[0] == first
    assert client.post(f"/api/scenario/steps/{sid}/move", json={"direction": "sideways"}).status_code == 400
    assert client.delete(f"/api/scenario/steps/{sid}").json() == {"deleted": sid} and len(sc.STEPS) == n
    assert client.delete("/api/scenario/steps/welcome").status_code == 400
    assert client.delete(f"/api/scenario/steps/{sid}").status_code == 404
    assert client.put("/api/scenario/steps/nope", json={"title": "x"}).status_code == 404


def test_step_validation_errors():
    ok = {"title": "T", "due": {"anchor": "hire", "time": "10:00"}}
    for bad in ({**ok, "title": "  "}, {**ok, "due": {"anchor": "hire", "time": "99:99"}}, {**ok, "sites": ["Mars"]},
                {**ok, "items": [{"type": "msg", "text": ""}]}, {**ok, "items": [{"type": "video", "text": "x"}]}):
        assert client.post("/api/scenario/steps", json=bad).status_code == 400, bad
    assert client.post("/api/scenario/steps", json={**ok, "after": "ghost"}).status_code == 404
    assert client.put("/api/scenario/steps/welcome", json={"due": {"anchor": "hire", "time": "10:00"}}).status_code == 400   # у знакомства срока нет
    assert client.put("/api/scenario/steps/welcome", json={"title": "Знакомство"}).status_code == 200


# ---------- сообщения внутри этапа ----------
def test_add_edit_move_delete_items():
    sid = client.post("/api/scenario/steps", json={"title": "Свой", "due": {"anchor": "hire", "time": "10:00"}, "items": [{"type": "msg", "text": "один"}]}).json()["id"]
    assert client.post(f"/api/scenario/steps/{sid}/items", json={"type": "msg", "text": "два"}).json()["idx"] == 1
    assert client.post(f"/api/scenario/steps/{sid}/items", json={"type": "msg", "text": "ноль", "index": 0, "sites": ["GTP"]}).json()["idx"] == 0
    assert client.post(f"/api/scenario/steps/{sid}/items", json={"type": "msg", "text": "x", "image": "up_zzzzzzzzzzzz.jpg"}).status_code == 400
    texts = lambda: [i["text"] for i in sc.STEP_BY_ID[sid]["items"]]  # noqa: E731
    assert texts() == ["ноль", "один", "два"]
    client.post(f"/api/scenario/steps/{sid}/items/2/move", json={"direction": "up"})
    assert texts() == ["ноль", "два", "один"]
    client.post(f"/api/scenario/steps/{sid}/items/0/move", json={"direction": "up"})
    assert texts() == ["ноль", "два", "один"]
    assert client.post(f"/api/scenario/steps/{sid}/items/0/move", json={"direction": "x"}).status_code == 400
    assert client.put(f"/api/scenario/steps/{sid}/items/1", json={"text": "двойка", "sites": ["GrPP"], "office": True}).status_code == 200
    assert texts()[1] == "двойка" and sc.STEP_BY_ID[sid]["items"][1]["sites"] == ["GrPP"]
    assert client.put(f"/api/scenario/steps/{sid}/items/1", json={"text": "  "}).status_code == 400            # у своего сообщения исходного текста нет
    assert client.put(f"/api/scenario/steps/{sid}/items/9", json={"text": "x"}).status_code == 404
    assert client.put(f"/api/scenario/steps/{sid}/items/1", json={"sites": ["Mars"]}).status_code == 400
    assert client.delete(f"/api/scenario/steps/{sid}/items/0").json() == {"deleted": 0} and texts() == ["двойка", "один"]
    assert client.delete(f"/api/scenario/steps/{sid}/items/5").status_code == 404
    # вопрос нельзя снабдить вложением, у ask-вопросов без текста нет «text»
    q = client.post(f"/api/scenario/steps/{sid}/items", json={"type": "yesno", "text": "Ок?"}).json()["idx"]
    img = client.post("/api/scenario/images", content=b"\xff\xd8\xff" + b"7" * 20).json()["file"]
    assert client.put(f"/api/scenario/steps/{sid}/items/{q}", json={"image": img}).status_code == 400
    assert client.put(f"/api/scenario/steps/{sid}/items/0", json={"image": "ghost.jpg"}).status_code == 400
    steps = [dict(s_) for s_ in sc.STEPS]
    next(s_ for s_ in steps if s_["id"] == sid)["items"].append(sc.KB("q", "fallback"))      # элемент без текста (ответ из базы знаний)
    sc.save(steps)
    assert client.put(f"/api/scenario/steps/{sid}/items/{len(sc.STEP_BY_ID[sid]['items']) - 1}", json={"text": "x"}).status_code == 400


def test_empty_text_restores_original_for_default_items():
    orig = sc.STEP_BY_ID["d1_lunch"]["items"][0]["text"]
    client.put("/api/scenario/text", json={"step_id": "d1_lunch", "idx": 0, "text": "Пообедаем?"})
    view = next(i for s in client.get("/api/scenario").json()["steps"] if s["id"] == "d1_lunch" for i in s["items"] if i["idx"] == 0)
    assert view["text"] == "Пообедаем?" and view["edited"]
    client.put("/api/scenario/text", json={"step_id": "d1_lunch", "idx": 0, "text": ""})
    assert sc.STEP_BY_ID["d1_lunch"]["items"][0]["text"] == orig
    long = client.put("/api/scenario/text", json={"step_id": "d1_lunch", "idx": 0, "text": "я" * 4000})
    assert long.status_code == 400 and "3500" in long.json()["detail"]                        # слишком длинный текст отклоняется, а не обрезается


def test_restore_defaults_and_persistence_across_reload():
    client.post("/api/scenario/steps", json={"title": "Свой", "due": None})
    n = len(sc.STEPS)
    sc.reset()                                                        # «перезапуск»: сценарий читается из базы
    assert len(sc.STEPS) == n and any(s["title"] == "Свой" for s in sc.STEPS)
    assert client.post("/api/scenario/restore").status_code == 200
    assert len(sc.STEPS) == n - 1 and not any(s["title"] == "Свой" for s in sc.STEPS)


@pytest.mark.asyncio
async def test_engine_sends_custom_step_on_time_and_respects_site():
    from test_onboarding import llm_router
    import respx
    with respx.mock:
        llm_router()
        sid = client.post("/api/scenario/steps", json={"title": "Свой", "due": {"anchor": "hire", "days": 1, "time": "08:00"}, "sites": ["GTP"],
                                                       "items": [{"type": "msg", "text": "Привет из своего этапа"}]}).json()["id"]
        gtp, grpp = make_emp(site="GTP", hire="2026-11-02", user="g1"), make_emp(site="GrPP", hire="2026-11-02", user="g2")
        sent = []

        async def deliver(emp, msgs):
            sent.append((emp["user_id"], [m["text"] for m in msgs]))
        await engine.tick(deliver, datetime(2026, 11, 3, 8, 5))
        assert ("g1", ["Привет из своего этапа"]) in sent and not any(u == "g2" and "Привет из своего этапа" in t for u, ts in sent for t in ts)
        assert db.step_status(gtp["id"])[sid]["status"] == "done"
        assert [t["status"] for t in engine.timeline(grpp) if t["id"] == sid] == ["n/a"]


# ---------- площадки ----------
def test_sites_crud_and_copy_from():
    assert [s["code"] for s in client.get("/api/sites").json()] == ["GrPP", "GTP", "Luzhniki"]
    marker = [(s["id"], i) for s in sc.STEPS for i, it in enumerate(s["items"]) if it.get("sites") and "GTP" in it["sites"]]
    steps_gtp = [s["id"] for s in sc.STEPS if s.get("sites") and "GTP" in s["sites"]]
    r = client.post("/api/sites", json={"code": "Zavod2", "title": "Завод №2", "office": False, "copy_from": "GTP"})
    assert r.status_code == 200 and r.json()[-1] == {"code": "Zavod2", "title": "Завод №2", "office": False}
    assert "Zavod2" in sc.ALL_SITES and sc.SITE_TITLES["Zavod2"] == "Завод №2" and sc.SITE_OFFICE["Zavod2"] is False
    assert all("Zavod2" in sc.STEP_BY_ID[i]["sites"] for i in steps_gtp)                            # содержание «как у ГТП»
    assert all("Zavod2" in sc.STEP_BY_ID[s]["items"][i]["sites"] for s, i in marker)
    emp = client.post("/api/employees", json={"name": "Новый Сотрудник", "site": "Zavod2", "hire_date": "2026-11-02"}).json()
    assert emp["is_office"] is False and client.patch(f"/api/employees/{emp['id']}", json={"site": "Zavod2"}).status_code == 200
    assert client.put("/api/sites/Zavod2", json={"title": "Завод 2-й", "office": True}).json()[-1]["title"] == "Завод 2-й"
    assert client.put("/api/sites/Zavod2", json={}).status_code == 200 and client.put("/api/sites/ghost", json={"title": "x"}).status_code == 404
    assert client.delete("/api/sites/Zavod2").status_code == 400                                     # есть сотрудники
    client.delete(f"/api/employees/{emp['id']}")
    sc.reset()                                                                                         # площадки переживают перезапуск
    assert "Zavod2" in sc.ALL_SITES
    assert client.delete("/api/sites/Zavod2").status_code == 200 and "Zavod2" not in sc.ALL_SITES
    assert all("Zavod2" not in (s.get("sites") or []) for s in sc.STEPS)
    assert client.delete("/api/sites/Zavod2").status_code == 404


def test_site_validation_and_prune_site_only_content():
    for body in ({"code": "x", "title": "T"}, {"code": "GTP", "title": "T"}, {"code": "Ok1", "title": " "}, {"code": "Ok1", "title": "T", "copy_from": "Mars"},
                 {"code": "bad code!", "title": "T"}):
        assert client.post("/api/sites", json=body).status_code == 400, body
    client.post("/api/sites", json={"code": "Only1", "title": "Один"})
    sid = client.post("/api/scenario/steps", json={"title": "Только для Only1", "sites": ["Only1"], "due": None,
                                                   "items": [{"type": "msg", "text": "a", "sites": ["Only1"]}, {"type": "msg", "text": "b"}]}).json()["id"]
    both = client.post("/api/scenario/steps", json={"title": "Для двух", "sites": ["Only1", "GTP"], "due": None,
                                                    "items": [{"type": "msg", "text": "a", "sites": ["Only1"]}, {"type": "msg", "text": "b", "sites": ["Only1", "GTP"]}]}).json()["id"]
    client.delete("/api/sites/Only1")
    assert sid not in sc.STEP_BY_ID                                   # этап был только для удалённой площадки — удалён, а не стал «для всех»
    assert sc.STEP_BY_ID[both]["sites"] == ["GTP"] and [i["text"] for i in sc.STEP_BY_ID[both]["items"]] == ["b"] and sc.STEP_BY_ID[both]["items"][0]["sites"] == ["GTP"]
    only = [s for s in sc.site_list()]
    assert len(only) == 3
    for code in ("GTP", "Luzhniki"):
        client.delete(f"/api/sites/{code}")
    assert client.delete("/api/sites/GrPP").status_code == 400         # последнюю площадку удалить нельзя
    assert [x["t"] for x in scenario_edit._walk([{"t": "x", "then": [{"t": "y"}]}])] == ["x", "y"]


def test_editor_rejects_bad_input_when_called_directly():
    with pytest.raises(ValueError):
        scenario_edit.create_step("   ", None)
    sid = scenario_edit.create_step("Для проверки", None, items=[{"type": "msg", "text": "а"}])
    with pytest.raises(ValueError):
        scenario_edit.move_step(sid, "sideways")
    with pytest.raises(ValueError):
        scenario_edit.move_item(sid, 0, "sideways")
    with pytest.raises(ValueError):
        scenario_edit.add_site("Ok_1", "  ")


def test_place_step_for_drag_and_drop():
    ids = lambda: [s["id"] for s in sc.STEPS]  # noqa: E731
    a, b, c = ids()[1], ids()[2], ids()[3]
    assert client.post(f"/api/scenario/steps/{c}/place", json={"before": a}).status_code == 200
    assert ids().index(c) == ids().index(a) - 1
    assert client.post(f"/api/scenario/steps/{c}/place", json={"after": b}).status_code == 200
    assert ids().index(c) == ids().index(b) + 1
    before = ids()
    assert client.post(f"/api/scenario/steps/{a}/place", json={"before": a}).status_code == 200 and ids() == before   # на самого себя — без изменений
    assert client.post(f"/api/scenario/steps/{a}/place", json={}).status_code == 400
    assert client.post(f"/api/scenario/steps/{a}/place", json={"before": b, "after": c}).status_code == 400
    assert client.post(f"/api/scenario/steps/{a}/place", json={"before": "ghost"}).status_code == 404
    assert client.post("/api/scenario/steps/ghost/place", json={"before": a}).status_code == 404
    first = ids()[0]
    client.post(f"/api/scenario/steps/{c}/place", json={"before": first})
    assert ids()[0] == c
