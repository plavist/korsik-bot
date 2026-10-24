"""Интерактивная аналитика: фильтры, детализация, этапы, риск, дайджест, напоминания руководителям."""
import json
import time
from datetime import date, datetime

import httpx
import pytest
import respx

from app import channels, db, engine, insights
from app import main as m
from helpers import GW, llm_reply
from test_api import client
from test_onboarding import llm_router, make_emp


def add(uid, text, sentiment="neutral", score=0.0, topics=("другое",), red=False, ts=None):
    mid = db.add_message(uid, "web", "user", text)
    db.set_sentiment(mid, sentiment, score, list(topics), red)
    if ts:
        with db.conn() as c:
            c.execute("UPDATE messages SET created_at=? WHERE id=?", (ts, mid))
    return mid


def seed():
    a = make_emp(user="ua", name="Анна Петрова", site="GrPP", hire="2026-10-20", manager="Иванов")
    b = make_emp(user="ub", name="Борис Сидоров", site="GTP", hire="2026-09-01", manager="Смирнов")
    db.upsert_user("ua", "Анна", "web")
    db.upsert_user("ub", "Борис", "web")
    add("ua", "Как попасть на портал?", "positive", 0.6, ["доступы и ИТ"])
    add("ub", "Меня обижает наставник", "negative", -0.8, ["конфликт или токсичность"], red=True)
    add("ub", "Хочу уволиться", "negative", -0.9, ["увольнение"], red=True)
    old = add("ua", "Старый вопрос", "neutral", 0.0, ["график и отпуск"], ts=time.time() - 40 * 86400)
    db.add_escalation("ub", "web", "Красный флаг", "sentiment")
    return a, b, old


# ---------- фильтры и детализация ----------
def test_analytics_filters_by_period_site_and_manager():
    seed()
    allp = db.analytics()
    assert allp["user_messages"] == 4 and allp["filters"] == {"days": None, "site": None, "manager": None, "topic": None, "sentiment": None}
    assert db.analytics(days=7)["user_messages"] == 3                              # «Старый вопрос» вне периода
    assert db.analytics(site="GTP")["user_messages"] == 2 and db.analytics(site="GTP")["users"] == 1
    a = db.analytics(manager="Иванов")
    assert a["user_messages"] == 2 and a["escalations"] == {} and a["red_flags"] == []
    b = db.analytics(site="GTP", manager="Смирнов", days=30)
    assert b["escalations"] == {"open": 1} and b["escalation_kinds"] == {"sentiment": 1} and len(b["red_flags"]) == 2
    d = {x["topic"]: x for x in b["topics_detail"]}
    assert d["увольнение"] == {"topic": "увольнение", "n": 1, "neg": 1} and b["top_topics"][0][1] == 1
    day = b["daily"][-1]
    assert day["neg"] == 2 and day["avg"] == -0.85 and day["n"] == 2


def test_drill_filters_and_user_history():
    seed()
    assert len(db.messages_drill()) == 4
    assert [r["text"] for r in db.messages_drill(sentiment="negative", site="GTP")] == ["Хочу уволиться", "Меня обижает наставник"]
    assert len(db.messages_drill(red_flag=True)) == 2 and db.messages_drill(red_flag=True)[0]["red_flag"] is True
    assert [r["text"] for r in db.messages_drill(topic="увольнение")] == ["Хочу уволиться"]
    today = date.today().isoformat()
    assert len(db.messages_drill(day=today)) == 3 and len(db.messages_drill(user_id="ua")) == 2
    assert db.messages_drill(days=7, manager="Иванов")[0]["name"] == "Анна Петрова"
    assert db.user_history("ua")[0]["text"] == "Старый вопрос" or db.user_history("ua")


# ---------- этапы, воронка ----------
def test_phase_stages_and_funnel():
    seed()
    today = date(2026, 10, 24)
    e = {"id": 0, "hire_date": "2026-10-20", "probation_end": None}
    assert insights.phase({**e, "id": 99999}, today) == "1-я неделя"
    assert insights.phase({**e, "hire_date": "2026-11-02", "id": 99999}, today) == "До выхода"
    assert insights.phase({**e, "hire_date": "2026-10-01", "id": 99999}, today) == "2–4 неделя"
    assert insights.phase({**e, "hire_date": "2026-08-30", "id": 99999}, today) == "2-й месяц"
    assert insights.phase({**e, "hire_date": "2026-08-01", "id": 99999}, today) == "3-й месяц"
    assert insights.phase({**e, "hire_date": "2026-01-01", "id": 99999}, today) == "Испытательный срок пройден"
    done = db.create_employee(code="X1", name="Готов", site="GrPP", hire_date="2026-10-20")
    db.mark_step(done, "end_probation", "done")
    assert insights.phase(db.get_employee(done), today) == "Испытательный срок пройден"
    st = {s["stage"]: s["employees"] for s in insights.stages()}
    assert [x["name"] for x in st["2–4 неделя"] + st["2-й месяц"] + st["3-й месяц"]] != []
    assert [x["name"] for s in insights.stages(site="GTP") for x in s["employees"]] == ["Борис Сидоров"]
    assert [x["name"] for s in insights.stages(manager="Иванов") for x in s["employees"]] == ["Анна Петрова"]
    emp = db.employee_by_user("ua")
    db.mark_step(emp["id"], "welcome", "done")
    db.mark_step(emp["id"], "d1_access", "sent")
    f = {x["id"]: x for x in insights.funnel()}
    assert f["welcome"]["sent"] == 1 and f["welcome"]["done"] == 1 and f["welcome"]["applicable"] == 2
    assert f["d1_access"]["sent"] == 1 and "d3_gym" in f
    assert all(x["applicable"] for x in [f["welcome"]]) and insights.funnel(site="GTP")[0]["applicable"] == 1


# ---------- риск ----------
def test_risk_scores_and_factors():
    seed()
    emp_b = db.employee_by_user("ub")
    db.add_answer(emp_b["id"], "d3_survey", "q1", "scale", "3", "3")
    db.add_answer(emp_b["id"], "d3_survey", "q2", "scale", "6", "6")
    db.add_answer(emp_b["id"], "d3_survey", "q3", "scale", "9", "9")
    r = insights.risk()
    by = {x["name"]: x for x in r}
    b, a = by["Борис Сидоров"], by["Анна Петрова"]
    assert r[0] is b and b["level"] == "high" and b["score"] >= 90
    text = " | ".join(b["factors"])
    for part in ("красные флаги", "негативных", "средняя тональность", "увольнение", "низкие оценки", "открытых обращений"):
        assert part in text
    assert a["level"] == "low" and a["factors"] == [] and a["phase"] == "1-я неделя"
    assert [x["name"] for x in insights.risk(site="GrPP")] == ["Анна Петрова"] and insights.risk(manager="Нет") == []
    assert insights.risk(days=1)[0]["name"]
    # молчание больше 5 дней
    with db.conn() as c:
        c.execute("UPDATE messages SET created_at=? WHERE user_id='ua'", (time.time() - 9 * 86400,))
    assert "молчит больше 5 дней" in {x["name"]: x for x in insights.risk()}["Анна Петрова"]["factors"]
    make_emp(user=None, name="Не подключён")
    assert "Не подключён" not in [x["name"] for x in insights.risk()]


def test_survey_comments():
    a, _, _ = seed()
    db.add_answer(a["id"], "d3_survey", "q1", "scale", "5", "5", comment="Не получилось войти в систему")
    db.add_answer(a["id"], "m1_survey", "free", "text", "Хочу больше задач", None)
    db.add_answer(a["id"], "d3_survey", "q4", "text", "   ", None)
    db.add_answer(a["id"], "d3_survey", "q5", "choice", "A", "A")
    texts = {c["text"]: c for c in insights.survey_comments()}
    assert set(texts) == {"Не получилось войти в систему", "Хочу больше задач"} and texts["Не получилось войти в систему"]["scale"] == "5"
    assert insights.survey_comments(emp_id=a["id"], days=1) and insights.survey_comments(days=0)


# ---------- дайджест ----------
def test_parse_clusters_filters_unknown_ids_and_bad_fields():
    raw = json.dumps({"clusters": [{"title": "Доступы", "risk": "high", "summary": "s", "ids": [1, 2, 99]},
                                   {"title": "Пустой", "ids": [77]}, {"ids": [1]}, {"title": "Риск?", "risk": "wow", "ids": [2]}]})
    c = insights.parse_clusters("текст " + raw, {1, 2})
    assert [(x["title"], x["risk"], x["ids"]) for x in c] == [("Доступы", "high", [1, 2]), ("Риск?", "low", [2])]
    assert insights.parse_clusters("не json", {1}) == []



@respx.mock
async def test_cluster_llm_topics_fallback_and_none():
    items = [{"id": 1, "text": "нет доступа", "topics": ["доступы и ИТ"]}, {"id": 2, "text": "конфликт", "topics": ["конфликт или токсичность"]},
             {"id": 3, "text": "ещё доступ", "topics": []}]
    respx.post(GW).mock(return_value=llm_reply(json.dumps({"clusters": [{"title": "Доступы", "risk": "medium", "summary": "", "ids": [1, 3]}]})))
    c, how = await insights.cluster(items)
    assert how == "llm" and c[0]["title"] == "Доступы"
    respx.post(GW).mock(return_value=llm_reply("мусор без json"))                      # нейросеть ответила не JSON → по темам
    c, how = await insights.cluster(items)
    assert how == "topics" and {x["title"]: x["risk"] for x in c}["конфликт или токсичность"] == "high" and {x["title"] for x in c} >= {"другое"}
    respx.post(GW).mock(side_effect=httpx.ConnectError("down"))
    assert (await insights.cluster(items))[1] == "topics"
    assert await insights.cluster([]) == ([], "none")


@respx.mock
async def test_digest_and_text():
    a, b, _ = seed()
    db.add_answer(a["id"], "d3_survey", "q1", "scale", "4", "4", comment="Не работает доступ к порталу")
    respx.post(GW).mock(side_effect=lambda req: llm_reply(json.dumps({"clusters": [
        {"title": "Доступы", "risk": "medium", "summary": "Не могут войти", "ids": [1]},
        {"title": "Конфликты", "risk": "high", "summary": "Наставник", "ids": [2, 3]}]})))
    d = await insights.digest(7)
    assert d["method"] == "llm" and d["texts"] == 3 and d["clusters"][0]["risk"] == "high" and d["clusters"][0]["n"] == 2
    assert d["clusters"][0]["quotes"][0]["source"] in ("бот", "опрос") and d["red_flags"] == 2
    assert d["at_risk"][0]["name"] == "Борис Сидоров" and sum(s["n"] for s in d["stages"]) == 2
    txt = insights.digest_text(d)
    assert "Настроения в коллективе новичков" in txt and "🔴 Конфликты" in txt and "Зона риска" in txt and len(txt) <= 3900
    empty = {**d, "clusters": [], "at_risk": []}
    assert "Темы" not in insights.digest_text(empty)


# ---------- напоминания руководителям ----------
@respx.mock
async def test_manager_nudges_milestones_risk_and_delivery():
    a = make_emp(user="m1", name="Новичок", hire="2026-10-01", manager="Иванов", manager_user="boss1")
    make_emp(user=None, name="Не подключён", hire="2026-10-01", manager="Иванов")     # без привязки напоминаний нет
    sent = []

    async def send(uid, text):
        sent.append((uid, text))
    n = await insights.manager_nudges(send, datetime(2026, 10, 4, 10, 0))         # 3-й день
    assert n == 1 and sent[0][0] == "boss1" and "Новичок" in sent[0][1] and "3-й день" in sent[0][1]
    assert await insights.manager_nudges(send, datetime(2026, 10, 4, 12, 0)) == 0   # повтор того же события не создаётся
    notices = db.list_notices("Иванов")
    assert len(notices) == 1 and notices[0]["delivered"] == 1 and notices[0]["employee"] == "Новичок"
    # недоставленное остаётся в списке; руководитель без чата — только список
    async def broken(uid, text):
        raise RuntimeError("нет сети")
    assert await insights.manager_nudges(broken, datetime(2026, 10, 15, 9, 0)) == 1   # 2 недели
    assert [n["delivered"] for n in db.list_notices()][0] == 0
    db.update_employee(a["id"], manager_user=None)
    assert await insights.manager_nudges(send, datetime(2026, 11, 1, 9, 0)) == 1       # месяц, без push
    # высокий риск → отдельное предупреждение
    add("m1", "Хочу уволиться, всё достало", "negative", -0.9, ["увольнение"], red=True)
    add("m1", "Никто не помогает", "negative", -0.8, ["коллектив и коммуникации"], red=True)
    assert await insights.manager_nudges(send, datetime(2026, 11, 20, 9, 0)) >= 1
    assert any(n["text"].count("Зона риска") for n in db.list_notices())
    assert db.list_notices(limit=1) and db.add_notice(a["id"], "x", "t", None) and not db.add_notice(a["id"], "x", "t", None)


# ---------- API ----------
@respx.mock
def test_insights_api_endpoints():
    llm_router()
    a, b, _ = seed()
    assert client.get("/api/analytics?days=7&site=GTP").json()["user_messages"] == 2
    assert client.get("/api/analytics?days=&site=&manager=").json()["user_messages"] == 4
    assert len(client.get("/api/analytics/drill?sentiment=negative&manager=Смирнов").json()) == 2
    assert client.get("/api/analytics/drill?topic=увольнение").json()[0]["text"] == "Хочу уволиться"
    assert client.get("/api/analytics/drill?red_flag=true&day=&user_id=ub").json()
    assert len(client.get("/api/insights/stages?site=GrPP").json()) == 6
    assert client.get("/api/insights/funnel").json()[0]["id"] == "welcome"
    assert client.get("/api/insights/risk?manager=Смирнов").json()[0]["level"] == "high"
    db.add_answer(a["id"], "d3_survey", "q1", "scale", "5", "5", comment="Сложно")
    c = client.get("/api/insights/comments").json()
    assert c[0]["name"] == "Анна Петрова" and client.get("/api/insights/comments?site=GTP").json() == []
    assert client.get("/api/insights/comments?manager=Смирнов").json() == [] and client.get("/api/insights/comments?manager=Иванов").json()
    assert client.get("/api/insights/managers").json() == ["Иванов", "Смирнов"]
    assert client.get("/api/users/ua/history").json()[0]["role"] == "user"
    d = client.get("/api/insights/digest?days=7").json()
    assert d["days"] == 7 and "clusters" in d and client.get("/api/insights/digest").json()["days"] == 7
    assert client.put("/api/insights/digest/settings", json={"recipients": " 11, 22 ,,"}).json() == {"ok": True}
    assert client.get("/api/insights/digest/settings").json() == {"recipients": "11,22"}
    assert client.get("/api/insights/notices").json() == [] and client.get("/api/insights/notices?manager=Иванов").json() == []


@respx.mock
def test_employee_manager_fields_and_scoped_access():
    llm_router()
    r = client.post("/api/employees", json={"name": "Иван Новый", "site": "GrPP", "hire_date": "2026-11-02", "manager": " Сергей  Иванов ", "manager_user": "555"}).json()
    assert r["manager"] == "Сергей Иванов" and r["manager_user"] == "555"
    other = client.post("/api/employees", json={"name": "Чужой Сотрудник", "site": "GTP", "hire_date": "2026-11-02"}).json()
    assert other["manager"] in ("", None)
    assert [e["name"] for e in client.get("/api/employees?manager=Сергей Иванов").json()] == ["Иван Новый"]
    assert len(client.get("/api/employees").json()) == 2
    assert client.get(f"/api/employees/{r['id']}?manager=Сергей Иванов").status_code == 200
    assert client.get(f"/api/employees/{other['id']}?manager=Сергей Иванов").status_code == 404
    assert client.patch(f"/api/employees/{r['id']}", json={"manager": "Пётр Петров", "manager_user": ""}).json()["manager"] == "Пётр Петров"


@respx.mock
async def test_digest_send_push_and_weekly_jobs(monkeypatch):
    llm_router()
    seed()
    db.set_setting("digest_recipients", "web1,max1,bad")
    db.upsert_user("max1", "Рук", "max")
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    sent = respx.post("http://max/messages").mock(side_effect=lambda req: httpx.Response(200 if b"bad" not in req.url.query else 500, json={}))
    r = await m.send_digest(7)
    assert r["sent"] == ["web1", "max1"] and [f["user_id"] for f in r["failed"]] == ["bad"]      # у «bad» MAX отвечает ошибкой
    assert sent.called and "Настроения" in r["text"] and db.history("web1")[0]["kind"] == "proactive"
    monkeypatch.delenv("MAX_BOT_TOKEN")                                              # токена нет → ничего не отправлено, ошибка по каждому
    r = await m.send_digest(7)
    assert r["sent"] == [] and len(r["failed"]) == 3
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    assert client.post("/api/insights/digest/send?days=7").json()["text"]
    # понедельник 9:00 — дайджест раз в неделю
    calls = []

    async def fake_send(days=7):
        calls.append(days)
    monkeypatch.setattr(m, "send_digest", fake_send)
    await m.weekly_jobs(datetime(2026, 10, 26, 9, 5))
    await m.weekly_jobs(datetime(2026, 10, 26, 18, 0))
    await m.weekly_jobs(datetime(2026, 10, 27, 10, 0))
    await m.weekly_jobs(datetime(2026, 10, 26, 8, 0))
    assert calls == [7]
    await m.weekly_jobs()


# ---------- таблица обращений: сообщение, тональность, площадка ----------
def test_escalation_rows_have_message_tone_and_site():
    a, b, _ = seed()
    db.upsert_user("ux", "Без привязки", "web")
    add("ux", "Спасибо, всё понятно!", "positive", 0.7)
    add("ux", "Когда выдадут пропуск?", "neutral")
    add("ux", "Я ухожу, достало", "negative", -0.9, red=True)
    pos = db.add_escalation("ux", "web", "Вопрос без ответа в базе знаний", "unanswered")
    rows = {e["user_id"] + e["kind"]: e for e in db.list_escalations()}
    sent = rows["ubsentiment"]
    assert sent["tone"] == "danger" and sent["site"] == "GTP" and sent["message"] == "Хочу уволиться"
    ux = rows["uxunanswered"]
    assert ux["tone"] == "danger" and ux["site"] is None and ux["message"] == "Я ухожу, достало"            # берём последнее сообщение пользователя
    db.upsert_user("uy", "Ю", "web")
    add("uy", "Как выглядит пропуск?", "neutral")
    db.add_message("uy", "web", "user", "[кнопка] Передать HR")                                                # нажатия кнопок — не сообщения
    e_neu = db.add_escalation("uy", "web", "Передача", "agent")
    assert db.get_escalation(e_neu)["message"] == "Как выглядит пропуск?" and db.get_escalation(e_neu)["tone"] == "neutral"
    add("uz", "Спасибо, всё отлично!", "positive", 0.8)
    assert db.get_escalation(db.add_escalation("uz", "web", "Передача", "agent"))["tone"] == "positive"
    e_none = db.add_escalation("ghost", "web", "Причина без сообщений", "contact")
    g = db.get_escalation(e_none)
    assert g["message"] == "Причина без сообщений" and g["tone"] == "neutral" and pos


def test_escalation_user_name_comes_from_employee_list_not_max_account():
    emp = make_emp(user="9001", name="Мария Соколова")
    db.upsert_user("9001", "Masha_official", "max")            # имя в аккаунте MAX — другое
    db.upsert_user("9002", "Чужой аккаунт", "max")             # не сотрудник
    db.add_escalation("9001", "max", "Вопрос", "agent")
    db.add_escalation("9002", "max", "Вопрос", "agent")
    names = {e["user_id"]: e["name"] for e in db.list_escalations()}
    assert names == {"9001": "Мария Соколова", "9002": None}   # у не-сотрудника имени нет — панель покажет MAX ID
    assert emp["name"] == "Мария Соколова"


def test_topic_and_sentiment_filters_and_positive_counts_as_neutral():
    seed()
    allp = db.analytics()
    assert allp["sentiments"] == {"neutral": 2, "negative": 2}            # позитив («Как попасть на портал?») учтён как нейтральное
    assert all("pos" not in d for d in allp["daily"]) and sum(d["neu"] for d in allp["daily"]) == 2
    neg = db.analytics(sentiment="negative")
    assert neg["user_messages"] == 2 and neg["sentiments"] == {"negative": 2} and neg["users"] == 1 and len(neg["red_flags"]) == 2
    neu = db.analytics(sentiment="neutral")
    assert neu["user_messages"] == 2 and neu["sentiments"] == {"neutral": 2} and neu["red_flags"] == []
    assert db.analytics(sentiment="positive")["user_messages"] == 2        # старое значение равно «нейтрально»
    topic = db.analytics(topic="увольнение")
    assert topic["user_messages"] == 1 and topic["top_topics"] == [("увольнение", 1)]
    both = db.analytics(topic="конфликт или токсичность", sentiment="negative", site="GTP", days=7)
    assert both["user_messages"] == 1 and db.analytics(topic="конфликт или токсичность", sentiment="neutral")["user_messages"] == 0
    assert db.analytics(topic="доступы и ИТ")["bot_messages"] == allp["bot_messages"]       # ответы бота темой и тональностью не фильтруются
    assert [r["text"] for r in db.messages_drill(sentiment="neutral")] == ["Старый вопрос", "Как попасть на портал?"]
    assert client.get("/api/analytics?sentiment=negative&topic=увольнение").json()["user_messages"] == 1
    assert len(client.get("/api/analytics/drill?topic=увольнение&sentiment=negative").json()) == 1
    assert "формат" in err(client.get("/api/analytics?sentiment=happy"))


def err(r):
    assert r.status_code == 400
    return r.json()["detail"]
