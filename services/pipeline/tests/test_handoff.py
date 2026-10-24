"""Бесшовная передача диалога HR прямо в МАКС: уведомления, кнопки, ответ из МАКС, пауза бота, пересылка сообщений сотрудника."""
import json
import time

import httpx
import respx

from app import bot, db, handoff
from app import main as m
from helpers import llm_reply
from test_api import anon, bare, client
from test_onboarding import llm_router

EMP, HR1, HR2 = "55", "900", "901"


class Max:
    """Журнал сообщений, отправленных ботом в МАКС: [(кому, текст, кнопки)]."""
    def __init__(self):
        self.sent = []
        self.fail_for: set[str] = set()
        respx.post("http://max/messages").mock(side_effect=self._cb)

    def _cb(self, request):
        uid = request.url.params["user_id"]
        if uid in self.fail_for:
            return httpx.Response(403, text="chat not found")
        body = json.loads(request.content)
        buttons = [b["payload"] for a in body.get("attachments", []) for row in a["payload"]["buttons"] for b in row]
        self.sent.append((uid, body["text"], buttons))
        return httpx.Response(200, json={})

    def to(self, uid):
        return [s for s in self.sent if s[0] == uid]


def setup(monkeypatch, recipients=f"{HR1},{HR2}", llm=True):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    if recipients:
        db.set_setting("hr_recipients", recipients)
    if llm:
        llm_router(search=True)           # вопрос уходит в поиск и ничего не находит — «нет ответа в базе»
    return Max()


async def ask(text="Какой пароль от wifi?", uid=EMP):
    res = await bot.handle(uid, "max", text=text)
    await handoff.flush()
    return res


@respx.mock
async def test_new_escalation_notifies_every_hr_in_max_with_buttons_and_employee_hears_nothing(monkeypatch):
    mx = setup(monkeypatch)
    res = await ask()
    assert res["messages"] == [] and mx.to(EMP) == []                                  # сотруднику — тишина
    for hr in (HR1, HR2):
        (_, text, buttons), = mx.to(hr)
        assert "Новое обращение №1" in text and "Какой пароль от wifi?" in text and "нет ответа в базе знаний" in text
        assert buttons == ["hq:reply:1", "hq:take:1", "hq:close:1"]


@respx.mock
async def test_without_recipients_nothing_is_sent_and_sync_creation_is_safe(monkeypatch):
    mx = setup(monkeypatch, recipients="")
    await ask()
    assert mx.sent == [] and len(db.list_escalations()) == 1
    db.set_setting("hr_recipients", HR1)
    assert handoff.create_escalation(EMP, "max", "вызов без цикла событий", "contact")      # синхронный вызов: уведомлять некому, ошибки нет


@respx.mock
async def test_recipients_come_from_panel_and_env_without_duplicates(monkeypatch):
    setup(monkeypatch, recipients="1, 2,2", llm=False)
    monkeypatch.setenv("HR_MAX_IDS", "2,3")
    assert handoff.recipients() == ["1", "2", "3"] and handoff.is_hr("3") and not handoff.is_hr("9")


@respx.mock
async def test_hr_takes_replies_from_max_and_employee_gets_the_answer(monkeypatch):
    mx = setup(monkeypatch)
    await ask()
    res = await bot.handle(HR1, "max", payload="hq:take:1")
    assert res["keep"] is True and "Вы ведёте обращение №1" in res["reply"]               # уведомление не удаляется после нажатия
    esc = db.get_escalation(1)
    assert esc["status"] == "in_progress" and esc["assignee"] == HR1
    res = await bot.handle(HR1, "max", payload="hq:reply:1")
    assert "Напишите ответ" in res["reply"] and handoff.awaiting(HR1) == 1
    res = await bot.handle(HR1, "max", text="Пароль от wifi — в кабинете 12, спросите у администратора")
    assert "Ответ отправлен" in res["reply"] and handoff.awaiting(HR1) is None
    (_, text, _), = mx.to(EMP)
    assert text == "Ответ HR-специалиста:\nПароль от wifi — в кабинете 12, спросите у администратора"
    assert db.history(EMP)[-1]["role"] == "hr"


@respx.mock
async def test_bot_stays_silent_while_hr_is_in_dialog_and_forwards_employee_messages(monkeypatch):
    mx = setup(monkeypatch)
    await ask()
    await bot.handle(HR1, "max", payload="hq:reply:1")
    await bot.handle(HR1, "max", text="Сейчас уточню")
    assert handoff.human_engaged(EMP)
    sent_before = len(mx.sent)
    res = await ask("Спасибо, жду")
    assert res["messages"] == [] and mx.to(EMP)[-1][1].startswith("Ответ HR-специалиста")      # бот сам не отвечал
    fwd = mx.sent[sent_before:]
    assert len(fwd) == 1 and fwd[0][0] == HR1 and "Сообщение по обращению №1" in fwd[0][1] and "Спасибо, жду" in fwd[0][1]
    assert fwd[0][2] == ["hq:reply:1", "hq:close:1"]                                              # «взять в работу» уже не нужно
    await bot.handle(HR1, "max", payload="hq:close:1")
    assert not handoff.human_engaged(EMP) and db.get_escalation(1)["status"] == "resolved"
    llm_router(kb_text="Ответ от бота")
    res = await bot.handle(EMP, "max", text="А где столовая?")
    assert res["reply"] == "Ответ от бота"                                                         # после закрытия бот снова отвечает


@respx.mock
async def test_bot_resumes_when_hr_goes_quiet(monkeypatch):
    setup(monkeypatch)
    await ask()
    await bot.handle(HR1, "max", payload="hq:reply:1")
    await bot.handle(HR1, "max", text="Сейчас уточню")
    assert handoff.human_engaged(EMP)
    with db.conn() as c:                                                                           # HR молчит дольше окна ведения диалога
        c.execute("UPDATE messages SET created_at=%s WHERE role='hr'", (time.time() - (handoff.HANDOFF_HOURS + 1) * 3600,))
    assert not handoff.human_engaged(EMP)
    llm_router(kb_text="Ответ от бота")
    assert (await bot.handle(EMP, "max", text="Алло?"))["reply"] == "Ответ от бота"


@respx.mock
async def test_new_questions_in_open_escalation_update_only_the_hr_who_took_it(monkeypatch):
    mx = setup(monkeypatch)
    await ask("Первый вопрос без ответа")
    n = len(mx.sent)
    await ask("Второй вопрос без ответа")                                                          # обращение ещё никто не взял — уведомление уже было
    assert len(mx.sent) == n and db.get_escalation(1)["repeats"] == 2
    await bot.handle(HR2, "max", payload="hq:take:1")
    n = len(mx.sent)
    await ask("Третий вопрос без ответа")
    # обращение «в работе», но бот молчит (HR писал?) — HR ещё не писал, поэтому бот отработал вопрос и дополнил обращение
    upd = [s for s in mx.sent[n:] if "Дополнение к обращению №1" in s[1]]
    assert len(upd) == 1 and upd[0][0] == HR2 and "Третий вопрос" in upd[0][1]


@respx.mock
async def test_only_hr_can_use_buttons_and_hr_chat_without_context_is_ordinary(monkeypatch):
    mx = setup(monkeypatch)
    await ask()
    res = await bot.handle("777", "max", payload="hq:close:1")                                     # чужой пользователь: кнопка HR не действует
    assert res["reply"] == "Этот вопрос уже пройден 🙂" and db.get_escalation(1)["status"] == "open" and "keep" not in res
    llm_router(kb_text="Ответ")
    assert (await bot.handle(HR1, "max", text="Во сколько обед?"))["reply"] == "Ответ"           # HR без контекста — обычный разговор с ботом
    await bot.handle(HR1, "max", payload="hq:reply:1")
    res = await bot.handle(HR1, "max", payload="hq:cancel")
    assert res["reply"] == "Хорошо, ничего не отправляю." and handoff.awaiting(HR1) is None
    await bot.handle(HR1, "max", payload="hq:reply:1")
    assert (await bot.handle(HR1, "max", text="/help"))["reply"]                                    # команда сбрасывает режим ответа и работает как обычно
    assert handoff.awaiting(HR1) is None
    handoff.stop_waiting(HR1)                                                                      # безопасно, если ждать нечего
    assert mx.to(EMP) == []


@respx.mock
async def test_stale_and_broken_hr_payloads(monkeypatch):
    setup(monkeypatch)
    await ask()
    await bot.handle(HR1, "max", payload="hq:close:1")
    for p in ("hq:reply:1", "hq:take:1", "hq:close:1", "hq:reply:999", "hq:take:abc", "hq:", "hq:wat:1"):
        assert (await bot.handle(HR1, "max", payload=p))["reply"] == handoff.STALE, p
    await bot.handle(HR1, "max", payload="hq:reply:1")                                             # закрытое обращение ответ принять не может
    db.set_setting(f"hq:{HR1}", json.dumps({"reply_to": 1}))
    assert (await bot.handle(HR1, "max", text="поздно"))["reply"] == handoff.STALE and handoff.awaiting(HR1) is None
    db.set_setting(f"hq:{HR1}", json.dumps({"reply_to": 424242}))                                  # обращения уже нет вообще
    assert (await bot.handle(HR1, "max", text="?"))["reply"] == handoff.STALE


@respx.mock
async def test_failed_delivery_to_employee_is_reported_to_hr_and_saved(monkeypatch):
    mx = setup(monkeypatch)
    await ask()
    mx.fail_for.add(EMP)
    await bot.handle(HR1, "max", payload="hq:reply:1")
    res = await bot.handle(HR1, "max", text="Ответ, который не дойдёт")
    assert "Не удалось доставить" in res["reply"] and db.history(EMP)[-1]["text"] == "Ответ, который не дойдёт"


@respx.mock
async def test_notification_failures_are_swallowed_and_test_message_reports_status(monkeypatch):
    mx = setup(monkeypatch)
    mx.fail_for.add(HR1)                                                                           # HR1 ещё не нажимал «Начать» у бота
    await ask()
    assert len(mx.to(HR2)) == 1 and mx.to(HR1) == [] and len(db.list_escalations()) == 1
    r = client.post("/api/escalations/recipients/test").json()
    assert r["results"] == [{"user_id": HR1, "delivered": False}, {"user_id": HR2, "delivered": True}]
    assert "Вы подключены как получатель" in mx.to(HR2)[-1][1]


@respx.mock
async def test_notify_functions_ignore_missing_escalations_and_unassigned_updates(monkeypatch):
    mx = setup(monkeypatch, llm=False)
    assert await handoff.notify_new(999, "x") == 0 and await handoff.notify_update(999, "x") == 0
    assert await handoff.forward_followup("nobody", "привет") == 0
    eid = db.add_escalation("u9", "max", "обращение ведут из панели", "contact")
    db.set_escalation_status(eid, "in_progress")
    assert await handoff.notify_update(eid, "ещё одно") == 2                                       # ведущего нет — всем получателям
    assert await handoff.forward_followup("u9", "сообщение сотрудника") == 2
    assert len(mx.sent) == 4 and await handoff.flush() is None


@respx.mock
async def test_red_flag_and_survey_escalations_also_notify_hr(monkeypatch):
    from app import sentiment
    mx = setup(monkeypatch, llm=False)
    respx.post("http://gw/v1/chat/completions").mock(return_value=llm_reply(json.dumps(
        {"sentiment": "negative", "score": -0.9, "topics": ["конфликт или токсичность"], "red_flag": True, "reason": "конфликт с наставником"})))
    mid = db.add_message(EMP, "max", "user", "Меня обижает наставник")
    await sentiment.analyze_and_store(mid, EMP, "max", "Меня обижает наставник")
    await handoff.flush()
    assert any("Красный флаг: конфликт с наставником" in s[1] for s in mx.to(HR1))


@respx.mock
async def test_myid_command_and_hr_notification_is_not_deleted_after_press(monkeypatch):
    mx = setup(monkeypatch)
    assert (await bot.handle("4242", "max", text="/myid"))["reply"] == "Ваш MAX ID: 4242"
    await ask()
    deleted = respx.delete("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    edited = respx.put("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    respx.post("http://max/answers").mock(return_value=httpx.Response(200, json={}))
    await m.process_max({"kind": "callback", "user_id": HR1, "name": "HR", "text": None, "payload": "hq:take:1", "callback_id": "c1", "message_id": "mid-1"})
    assert not deleted.called and not edited.called and db.get_escalation(1)["assignee"] == HR1


def test_panel_recipients_settings_validation_and_reply_endpoint(monkeypatch):
    assert client.get("/api/escalations/recipients").json() == {"recipients": "", "from_env": "", "active": []}
    assert client.post("/api/escalations/recipients/test").status_code == 409
    monkeypatch.setenv("HR_MAX_IDS", "77")
    r = client.put("/api/escalations/recipients", json={"recipients": " 11, 22 ,,"}).json()
    assert r["active"] == ["11", "22", "77"] and client.get("/api/escalations/recipients").json()["recipients"] == "11,22"
    bad = client.put("/api/escalations/recipients", json={"recipients": "11, bad id!"})
    assert bad.status_code == 400 and "ID пользователей MAX" in bad.json()["detail"]
    assert client.put("/api/escalations/recipients", json={"recipients": ",".join(str(i) for i in range(21))}).status_code == 400
    assert bare.get("/api/escalations/recipients").status_code == 401
    eid = db.add_escalation("web-1", "web", "вопрос", "contact")
    assert client.post(f"/api/escalations/{eid}/reply", json={"text": "Ответ из панели"}).json()["delivered"] is True
    assert db.get_escalation(eid)["status"] == "in_progress" and db.get_escalation(eid)["assignee"] is None   # из панели ведущий не назначается


def test_creating_escalation_outside_event_loop_does_not_fail(monkeypatch):
    db.set_setting("hr_recipients", HR1)                                                           # получатели есть, а цикла событий нет (синхронный код)
    eid = handoff.create_escalation(EMP, "max", "синхронный вызов", "contact")
    assert db.get_escalation(eid)["reason"] == "синхронный вызов"


@respx.mock
async def test_unknown_action_on_open_escalation_and_reply_assigns_the_first_hr(monkeypatch):
    mx = setup(monkeypatch, llm=False)
    eid = db.add_escalation(EMP, "max", "вопрос", "contact")
    assert (await bot.handle(HR1, "max", payload=f"hq:wat:{eid}"))["reply"] == handoff.STALE
    res = await handoff.deliver_reply(db.get_escalation(eid), "Ответ напрямую", HR2)             # ответ без предварительного «взять в работу»
    assert res["delivered"] and db.get_escalation(eid)["assignee"] == HR2 and mx.to(EMP)
    await handoff.deliver_reply(db.get_escalation(eid), "Ещё один ответ", HR1)                    # ведущий уже есть — не меняется
    assert db.get_escalation(eid)["assignee"] == HR2


@respx.mock
async def test_non_standard_question_connects_hr_even_if_model_answers_from_memory(monkeypatch):
    """Сквозная проверка: нестандартный вопрос, на который модель «из головы» отвечает уверенно, — сотрудник ничего не видит, HR в МАКС получает вопрос."""
    mx = setup(monkeypatch, llm=False)
    respx.post("http://ing/search").mock(return_value=httpx.Response(200, json=[]))
    respx.post("http://gw/v1/chat/completions").mock(return_value=llm_reply("Конечно, премию обычно выплачивают раз в квартал."))
    res = await bot.handle(EMP, "max", text="Когда мне выплатят премию за проект Х?")
    await handoff.flush()
    assert res["messages"] == [] and mx.to(EMP) == []                                  # выдумка модели в чат не попала
    for hr in (HR1, HR2):
        (_, text, buttons), = mx.to(hr)
        assert "Когда мне выплатят премию за проект Х?" in text and buttons[0] == "hq:reply:1"


@respx.mock
async def test_thanks_and_greetings_do_not_disturb_hr(monkeypatch):
    mx = setup(monkeypatch, llm=False)
    respx.post("http://gw/v1/chat/completions").mock(return_value=llm_reply("Пожалуйста 🙂"))
    for hello in ("Спасибо!", "привет", "Ок, понятно"):
        res = await bot.handle(EMP, "max", text=hello)
        assert res["reply"] == "Пожалуйста 🙂"
    await handoff.flush()
    assert db.list_escalations() == [] and mx.sent == []
