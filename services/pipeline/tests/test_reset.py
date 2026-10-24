"""Сброс рабочих данных: остаётся только заложенное изначально."""
from app import assets, commands, db, reset
from app import scenario as sc
from test_api import client
from test_onboarding import make_emp

JPG = b"\xff\xd8\xff" + b"5" * 20


def test_reset_clears_runtime_data_and_keeps_initial_content():
    img = client.post("/api/scenario/images?name=Приветствие", content=JPG).json()["file"]
    client.put("/api/scenario/image", json={"step_id": "welcome", "idx": 1, "image": img})
    # рабочие данные
    emp = make_emp(user="u1", name="Иван Петров", manager="Сергей Иванов")
    db.mark_step(emp["id"], "welcome", "done")
    db.add_answer(emp["id"], "d3_survey", "q1", "scale", "5", "5", comment="плохо")
    mid = db.add_message("u1", "max", "user", "Хочу уволиться")
    db.set_sentiment(mid, "negative", -0.9, ["увольнение"], True)
    db.add_escalation("u1", "max", "Красный флаг", "sentiment")
    db.add_notice(emp["id"], "mgr_day3", "напоминание", "Сергей Иванов")
    db.set_setting("digest_recipients", "123")
    db.set_setting("mode", "both")
    db.set_setting("max_marker", "555")
    # правки, которых изначально не было
    client.post("/api/scenario/steps", json={"title": "Свой этап", "due": None})
    client.post("/api/sites", json={"code": "Zavod2", "title": "Завод №2"})
    client.put("/api/scenario/text", json={"step_id": "d1_lunch", "idx": 0, "text": "Пообедаем?"})
    client.post("/api/commands", json={"name": "docs", "description": "Документы", "kind": "static", "reply": "Привет"})
    client.put("/api/commands/hr", json={"description": "Изменено", "reply": "x"})

    res = reset.reset()

    assert res["employees"] == 1 and res["messages"] == 1 and res["escalations"] == 1 and res["images_kept"] == 1
    for t in reset.RUNTIME_TABLES:
        with db.conn() as c:
            assert c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] == 0, t
    assert db.get_setting("digest_recipients") is None and db.get_setting("mode") is None and db.get_setting("max_marker") == "555"
    # заложенное осталось: сценарий исходный, вложение на месте, площадки по умолчанию, библиотека, команды по умолчанию
    assert len(sc.STEPS) == 21 and not any(s["title"] == "Свой этап" for s in sc.STEPS)
    assert sc.STEP_BY_ID["welcome"]["items"][1]["image"] == img and assets.path(img)
    assert sc.STEP_BY_ID["d1_lunch"]["items"][0]["text"] == "Хочешь, подскажу, где пообедать?"
    assert sc.ALL_SITES == ["GrPP", "GTP", "Luzhniki"]
    assert [c["name"] for c in db.list_commands()] == [d["name"] for d in commands.DEFAULTS]
    assert next(c for c in db.list_commands() if c["name"] == "hr")["description"] == commands.DEFAULTS[1]["description"]
    assert any(x["file"] == img and x["label"] == "Приветствие" for x in client.get("/api/scenario/images").json())
    assert client.get("/api/employees").json() == [] and client.get("/api/escalations").json() == []
    assert client.get("/api/analytics").json()["user_messages"] == 0


def test_reset_on_empty_database_and_removed_images_are_not_assigned():
    img = client.post("/api/scenario/images", content=JPG).json()["file"]
    client.put("/api/scenario/image", json={"step_id": "welcome", "idx": 1, "image": img})
    client.delete(f"/api/scenario/images/{img}")                       # файла уже нет — вложение не восстанавливается
    res = reset.reset()
    assert res["images_kept"] == 0 and sc.STEP_BY_ID["welcome"]["items"][1].get("image") is None
    assert reset.reset()["employees"] == 0                               # повторный запуск безопасен


def test_reset_keeps_images_from_legacy_settings_when_scenario_was_saved_without_them():
    img = client.post("/api/scenario/images", content=JPG).json()["file"]
    client.post("/api/scenario/restore")                                  # сценарий сохранён (без вложений) ...
    db.set_setting("image:welcome:1", img)                                # ... а вложение осталось только в старой настройке
    db.set_setting("image:welcome:3", "none")
    db.set_setting("image:welcome:4", "up_000000000000.jpg")              # файла нет
    res = reset.reset()
    assert res["images_kept"] == 1 and sc.STEP_BY_ID["welcome"]["items"][1]["image"] == img
    assert db.get_setting("image:welcome:1") is None                      # старые настройки очищены
