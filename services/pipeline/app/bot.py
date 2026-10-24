"""Единая точка обработки входящих (web и MAX): команды → «Передать HR» → сценарий/опросы → RAG-агент."""
import re

from . import agent, commands, db, engine, handoff, hrflow, intent, locks, sentiment, style

HR_LABELS = {"hr:forward": "Передать HR через Корсика", "hr:cancel": "Не передавать", "hr:contact": "Связаться с рекрутером"}
STALE = "Этот вопрос уже пройден 🙂"


async def handle(user_id: str, channel: str, text: str | None = None, payload: str | None = None,
                 name: str | None = None, background=None) -> dict:
    """Точка входа для web и MAX. События одного пользователя идут по очереди (см. locks.py)."""
    async with locks.user_lock(user_id):
        return await _handle(user_id, channel, text, payload, name, background)


async def _scenario_or_question(user_id, channel, text, payload, emp, res) -> int | None:
    """Ответ на вопрос сценария/опроса, «кнопка без вопроса» или свободный вопрос агенту. → id сообщения для анализа тональности."""
    answered = await engine.handle_answer(emp, payload=payload, text=text) if emp and (payload or text) else None
    msg_id = db.add_message(user_id, channel, "user", text or f"[кнопка] {(answered or {}).get('echo') or payload}")
    if answered:
        res["messages"] = answered["msgs"]
        return msg_id if answered["analyze"] else None
    if payload:  # кнопка без активного вопроса
        res["messages"] = [engine.msg(STALE)]
        return None
    if handoff.human_engaged(user_id):  # диалог ведёт живой HR: бот молчит, а сообщение уходит HR в МАКС
        handoff._spawn(handoff.forward_followup(user_id, text))
        res["messages"] = []
        return msg_id
    answer = await _free_question(user_id, channel, text, emp, res)
    res["messages"] = [answer] if answer else []   # нет ответа в базе — в чат ничего не пишем, вопрос уже у HR
    return msg_id


async def _route(user_id, channel, text, payload, emp, res) -> int | None:
    """Выбирает обработчик входящего: действие HR → кнопка HR → команда «/» → текст обращения → сценарий и вопросы.
    → id сообщения для анализа."""
    if handoff.is_hr(user_id):  # сотрудник HR отвечает на обращение прямо в МАКС
        hr = await handoff.handle_hr(user_id, text, payload)
        if hr is not None:
            res["messages"], res["keep"] = hr, True     # кнопки уведомления не удаляем: ими пользуются несколько раз
            return None
    if payload and payload.startswith("hr:"):
        db.add_message(user_id, channel, "user", f"[кнопка] {HR_LABELS.get(payload, payload)}")
        res["messages"] = await _hr_payload(user_id, channel, payload, emp)
        return None
    if text and text.startswith("/"):
        db.add_message(user_id, channel, "user", text)
        hrflow.stop_waiting(user_id)
        res["messages"] = await _command(user_id, channel, text, emp)
        return None
    if text and hrflow.awaiting(user_id):  # следующее сообщение после «Передать HR через Корсика» — текст обращения, уходит HR сразу
        msg_id = db.add_message(user_id, channel, "user", text)
        res["messages"] = [hrflow.take_text(user_id, channel, text)]
        return msg_id
    return await _scenario_or_question(user_id, channel, text, payload, emp, res)


async def _handle(user_id: str, channel: str, text: str | None, payload: str | None, name: str | None, background) -> dict:
    """→ {"messages": [{text, buttons, md?}], "reply": первый текст, "sources", "grounded"}.
    background — BackgroundTasks FastAPI (анализ тональности не задерживает ответ); без него — синхронно."""
    text = (text or "").strip() or None
    db.upsert_user(user_id, name, channel)
    res = {"messages": [], "sources": [], "grounded": True}
    analyze_id = await _route(user_id, channel, text, payload, db.employee_by_user(user_id), res)
    for m in res["messages"]:
        db.add_message(user_id, channel, "assistant", m["text"], res["sources"], res["grounded"], buttons=m.get("buttons"), image=m.get("image"))
    if analyze_id and text:
        if background is not None:
            background.add_task(sentiment.analyze_and_store, analyze_id, user_id, channel, text)
        else:
            await sentiment.analyze_and_store(analyze_id, user_id, channel, text)
    res["reply"] = res["messages"][0]["text"] if res["messages"] else ""
    return res


async def _free_question(user_id, channel, text, emp, res) -> dict | None:
    """Свободный вопрос → RAG-агент. Бесшовная эскалация: если ответа в базе нет — Корсик молчит и сразу передаёт вопрос HR
    (None — сообщений в чат нет); если вопрос личный или тревожный — отвечает и тоже сразу передаёт HR."""
    out = await agent.answer(user_id, channel, text, emp=emp)
    res.update(sources=out["sources"], grounded=out["grounded"])
    if out["hr_options"]:
        hrflow.escalate(user_id, channel, "unanswered", f"Вопрос без ответа в базе знаний: {text}")
        return None
    if out["draft"]:
        hrflow.escalate(user_id, channel, "agent", f"{out['draft']['reason']}. Вопрос сотрудника: {text}")
    elif reason := intent.detect(text):  # вопрос по смыслу похож на эскалацию — передаём HR сразу
        hrflow.escalate(user_id, channel, "intent", f"{reason}. Вопрос сотрудника: {text}")
    return {"text": out["reply"], "buttons": None, "md": True}


async def _hr_payload(user_id: str, channel: str, payload: str, emp: dict | None) -> list[dict]:
    if payload == "hr:forward":
        return [hrflow.start_forward(user_id)]
    if payload == "hr:cancel":
        return [hrflow.cancel(user_id)]
    if payload == "hr:contact":
        return [hrflow.contact(user_id, channel)] + (await engine.after_contact(emp) if emp else [])
    return [engine.msg(STALE)]


async def _command(user_id: str, channel: str, text: str, emp: dict | None) -> list[dict]:
    cmd, _, arg = text.partition(" ")
    if cmd.lower() == "/myid":  # служебная команда: показывает MAX ID — его вписывают в получатели обращений HR
        return [engine.msg(f"Ваш MAX ID: {user_id}")]
    row = commands.get(cmd)
    if not row:
        return [engine.msg("Не знаю такой команды 🙈 Список команд — /help")]
    kind = row["kind"]
    if kind == "start":
        return await _start(user_id, channel, arg.strip(), emp, row["reply"])
    if kind == "hr":
        return [hrflow.hr_command(user_id, channel, row["reply"])]
    if kind == "help":
        return [engine.msg(commands.help_text())]
    if kind == "kb":
        out = await agent.answer(user_id, channel, row["query"], emp=emp, record=False)
        return [{"text": out["reply"] if out["grounded"] else row["fallback"], "buttons": None, "md": True}]
    return [engine.msg(await style.personalize(row["reply"], emp))]


async def _start(user_id: str, channel: str, code: str, emp: dict | None, generic: str) -> list[dict]:
    if code:
        found = db.employee_by_code(re.sub(r"[^A-Za-z0-9]", "", code))
        if not found:
            return [engine.msg("Не нашёл такой код 🙈 Проверь, пожалуйста, ссылку или код от рекрутера. Если не получается — напиши /HR.")]
        return await engine.bind(found, user_id, channel)
    if emp:
        return [engine.msg(f"Привет, {style.first_name(emp)}! Я Корсик, рад видеть тебя снова 🙂 Спрашивай про правила, документы, доступы — "
                           "отвечу по базе знаний. Если нужен человек — команда /HR.")]
    return [engine.msg(generic)]
