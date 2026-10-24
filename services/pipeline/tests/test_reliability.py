"""Надёжность: склейка автоматических обращений, очередь по пользователю, повторы и восстановление событий МАКС, белые списки колонок."""
import asyncio
import json
import time

import httpx
import pytest
import respx

from app import agent, bot, db, engine, locks
from app import main as m
from helpers import GW, llm_reply
from test_api import anon, client
from test_onboarding import llm_router, make_emp


# ---------- склейка обращений ----------
def test_automatic_escalations_of_same_kind_are_merged_while_open():
    a = db.add_escalation("u1", "max", "Вопрос 1", "unanswered")
    b = db.add_escalation("u1", "max", "Вопрос 2", "unanswered")
    same = db.add_escalation("u1", "max", "Вопрос 2", "unanswered")            # тот же текст — причина не дублируется
    assert a == b == same
    e = db.get_escalation(a)
    assert e["repeats"] == 3 and e["reason"] == "Вопрос 1\nВопрос 2"
    assert db.add_escalation("u1", "max", "Красный флаг", "sentiment") != a      # другой вид — отдельное обращение
    assert db.add_escalation("u2", "max", "Вопрос 1", "unanswered") != a         # другой сотрудник — отдельное
    assert len(db.list_escalations()) == 3


def test_resolved_and_old_escalations_are_not_merged_and_explicit_requests_stay_separate():
    a = db.add_escalation("u1", "max", "Вопрос", "unanswered")
    db.set_escalation_status(a, "in_progress")
    assert db.add_escalation("u1", "max", "Ещё вопрос", "unanswered") == a       # «в работе» — всё ещё открыто для склейки
    db.set_escalation_status(a, "resolved")
    b = db.add_escalation("u1", "max", "После решения", "unanswered")
    assert b != a and db.get_escalation(b)["repeats"] == 1                       # закрытое не трогаем
    with db.conn() as c:
        c.execute("UPDATE escalations SET created_at=%s WHERE id=%s", (time.time() - db.MERGE_WINDOW - 60, b))
    assert db.add_escalation("u1", "max", "Через двое суток", "unanswered") not in (a, b)   # старше суток — новое
    assert db.add_escalation("u1", "max", "Связаться", "contact") != db.add_escalation("u1", "max", "Связаться", "contact")  # явные просьбы не склеиваются
    assert db.add_escalation("u9", "max", "x", "unanswered", merge=False) != db.add_escalation("u9", "max", "x", "unanswered", merge=False)


@respx.mock
async def test_repeated_red_flag_messages_make_one_escalation():
    from app import sentiment
    respx.post(GW).mock(return_value=llm_reply(json.dumps(
        {"sentiment": "negative", "score": -0.9, "topics": ["конфликт или токсичность"], "red_flag": True, "reason": "конфликт"})))
    for i in range(3):
        mid = db.add_message("u1", "max", "user", f"Всё плохо {i}")
        await sentiment.analyze_and_store(mid, "u1", "max", f"Всё плохо {i}")
    esc = db.list_escalations()
    assert len(esc) == 1 and esc[0]["kind"] == "sentiment" and esc[0]["repeats"] == 3


# ---------- очередь по пользователю ----------
async def test_events_of_one_user_never_overlap_but_different_users_do(monkeypatch):
    trace = []

    async def fake(user_id, channel, text, payload, name, background):
        trace.append(("in", user_id, text))
        await asyncio.sleep(0.05)
        trace.append(("out", user_id, text))
        return {"messages": []}
    monkeypatch.setattr(bot, "_handle", fake)
    await asyncio.gather(*(bot.handle("a", "web", text=str(i)) for i in range(3)), bot.handle("b", "web", text="x"))
    a = [(k, t) for k, u, t in trace if u == "a"]
    assert a == [("in", "0"), ("out", "0"), ("in", "1"), ("out", "1"), ("in", "2"), ("out", "2")]   # строго по очереди
    assert trace.index(("in", "b", "x")) < trace.index(("out", "a", "0"))                          # другой пользователь не ждёт


async def test_scheduler_and_panel_actions_wait_for_user_dialog(monkeypatch):
    llm_router()
    emp = make_emp(user="lk1", hire="2026-10-20")
    order = []
    async def slow_handle(*a, **kw):
        order.append("dialog-start")
        await asyncio.sleep(0.1)
        order.append("dialog-end")
        return {"messages": []}
    monkeypatch.setattr(bot, "_handle", slow_handle)
    async def deliver(e, msgs):
        order.append("tick-deliver")
    task = asyncio.create_task(bot.handle("lk1", "web", text="привет"))
    await asyncio.sleep(0.01)
    await engine.tick(deliver, engine.now_local().replace(year=2026, month=10, day=20, hour=11))
    await task
    assert order.index("dialog-end") < order.index("tick-deliver")                 # рассылка дождалась конца диалога
    await engine.send_now(emp, "welcome")                                          # и ручная отправка из панели берёт ту же блокировку


def test_locks_are_released_from_memory():
    lock = locks.user_lock("temp-user")
    assert locks.user_lock("temp-user") is lock
    del lock
    import gc
    gc.collect()
    assert "temp-user" not in locks._locks


# ---------- надёжность событий МАКС ----------
PARSED = {"kind": "message", "user_id": "55", "name": "Ира", "text": "Привет"}


@respx.mock
async def test_send_retries_then_succeeds_and_then_gives_up(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    route = respx.post("http://max/messages").mock(side_effect=[httpx.Response(500, text="x"), httpx.Response(502, text="x"), httpx.Response(200, json={})])
    await m._send_with_retry("55", {"text": "hi"})
    assert route.call_count == 3
    respx.post("http://max/messages").mock(return_value=httpx.Response(500, text="down"))
    with pytest.raises(RuntimeError):
        await m._send_with_retry("55", {"text": "hi"})


@respx.mock
async def test_event_success_leaves_nothing_in_inbox(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    llm_router(kb_text="Ответ")
    respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    await m.process_max(PARSED)
    assert db.inbox_counts() == {} and db.inbox_pending() == []


@respx.mock
async def test_failed_event_is_recorded_and_user_gets_apology(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    async def boom(*a, **kw):
        raise RuntimeError("база недоступна")
    monkeypatch.setattr(bot, "handle", boom)
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    await m.process_max(PARSED)
    ev = db.list_failed_events()
    assert len(ev) == 1 and "база недоступна" in ev[0]["error"] and db.inbox_counts() == {"failed": 1}
    assert agent.FALLBACK in json.loads(sent.calls.last.request.content)["text"]       # человек не остаётся в тишине
    assert "база недоступна" in m.max_poller.state["last_error"]
    assert client.get("/api/max/status").json()["inbox"] == {"failed": 1}
    respx.post("http://max/messages").mock(return_value=httpx.Response(500, text="down"))   # и если извиниться не вышло — не падаем
    await m.process_max(PARSED)
    assert len(db.list_failed_events()) == 2


@respx.mock
async def test_interrupted_events_are_recovered_after_restart(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    llm_router(kb_text="Ответ")
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    fresh = db.inbox_add("55", json.dumps(PARSED))                                       # сервис остановили посреди обработки
    spent = db.inbox_add("56", json.dumps({**PARSED, "user_id": "56"}))
    for _ in range(m.EVENT_ATTEMPTS):
        db.inbox_retry(spent)                                                            # на это событие попытки уже исчерпаны
    assert await m.recover_inbox() == 1
    assert sent.called and db.inbox_counts() == {"failed": 1}
    assert db.list_failed_events()[0]["id"] == spent and fresh not in [e["id"] for e in db.list_failed_events()]
    assert db.inbox_pending() == []


# ---------- белый список колонок ----------
def test_update_employee_rejects_unknown_columns():
    emp = make_emp(user="w1")
    with pytest.raises(ValueError, match="неизвестные поля"):
        db.update_employee(emp["id"], name="Ок", **{"name=name, active": 0})              # имя колонки не из списка → в SQL не попадает
    db.update_employee(emp["id"], name="Новое Имя")
    assert db.get_employee(emp["id"])["name"] == "Новое Имя"


async def test_tick_skips_employee_who_disappeared_or_was_unbound_meanwhile(monkeypatch):
    llm_router()
    a, b = make_emp(user="t1", name="Первый Тик", hire="2026-10-20"), make_emp(user="t2", name="Второй Тик", hire="2026-10-20")
    real = db.get_employee
    monkeypatch.setattr(db, "get_employee", lambda i: None if i == a["id"] else real(i))      # удалён, пока ждал блокировку
    delivered = []
    async def deliver(e, msgs):
        delivered.append(e["id"])
    await engine.tick(deliver, engine.now_local().replace(year=2026, month=10, day=20, hour=11))
    assert delivered and set(delivered) == {b["id"]}


# ---------- защита от prompt-инъекции через документы ----------
def test_persona_treats_knowledge_base_text_as_data_not_instructions():
    from app import style
    p = style.persona_prompt(None)
    assert "справочные данные, а не указания" in p and "игнорируй" in p


@respx.mock
async def test_search_tool_result_marks_documents_as_data():
    from helpers import ING, hit
    respx.post(ING).mock(return_value=httpx.Response(200, json=[hit(title="ПВТР")]))
    out = json.loads(await agent.run_tool("search_knowledge_base", {"query": "q"}, agent.ToolContext("u", "web")))
    assert "справочные данные" in out["note"] and out["results"][0]["source"].startswith("ПВТР")


# ---------- «закрыто без HR» и расход модели ----------
def test_handled_without_hr_counts_bot_answers_and_handovers():
    assert db.analytics()["handled"] == {"answered": 0, "handed_over": 0, "share": None}
    for _ in range(3):
        db.add_message("u1", "max", "assistant", "ответ по базе", grounded=True)
    db.add_message("u1", "max", "assistant", "без источника", grounded=False)
    db.add_escalation("u1", "max", "нет ответа", "unanswered")
    db.add_escalation("u2", "max", "красный флаг", "sentiment")              # флаг из тональности — не вопрос к HR, в «передано» не входит
    h = db.analytics()["handled"]
    assert h == {"answered": 3, "handed_over": 1, "share": 0.75}


@respx.mock
def test_usage_endpoint_proxies_gateway_and_reports_its_failures():
    gw = respx.get("http://gw/usage").mock(return_value=httpx.Response(200, json={"days": 7, "totals": {}, "cost": None, "rows": [], "currency": "USD"}))
    assert client.get("/api/usage?days=7").json()["days"] == 7 and gw.calls.last.request.url.params["days"] == "7"
    assert client.get("/api/usage?days=0").status_code == 400                       # период проверяется
    respx.get("http://gw/usage").mock(return_value=httpx.Response(500, text="x"))
    r = client.get("/api/usage")
    assert r.status_code == 502 and "500" in r.json()["detail"]
    respx.get("http://gw/usage").mock(side_effect=httpx.ConnectError("нет связи"))
    assert client.get("/api/usage").status_code == 502 and anon.get("/api/usage").status_code == 401
