"""Бесшовная передача диалога живому HR прямо в МАКС.

1. Появилось обращение → HR-получатели (MAX ID в панели управления или HR_MAX_IDS) получают сообщение с кнопками
   «Ответить» · «Взять в работу» · «Закрыть».
2. HR нажимает «Ответить» и пишет текст — он уходит сотруднику в тот же чат как «Ответ HR-специалиста»; панель управления
   не нужна (но работает и она: оба пути используют deliver_reply).
3. Пока HR ведёт диалог (обращение «в работе» и HR писал недавно), бот на свободные вопросы сотрудника не отвечает, а пересылает
   их HR. Закрыли обращение или HR молчит больше HANDOFF_HOURS — бот снова отвечает сам.
"""
import asyncio
import json
import logging
import os

from . import channels, db

log = logging.getLogger("handoff")
HANDOFF_HOURS = 12
SHOW = 700   # сколько символов вопроса показываем в уведомлении

KIND_LABELS = {
    "unanswered": "нет ответа в базе знаний", "agent": "личный или тревожный вопрос", "intent": "вопрос, похожий на эскалацию",
    "forward": "сотрудник написал через Корсика", "command": "команда /HR", "contact": "просит связаться с рекрутером",
    "survey": "сигнал из опроса", "sentiment": "красный флаг в сообщении",
}
_tasks: set[asyncio.Task] = set()


def recipients() -> list[str]:
    """MAX ID сотрудников HR: из панели управления и переменной HR_MAX_IDS (без повторов, порядок сохраняется)."""
    ids = [x.strip() for x in (db.get_setting("hr_recipients", "") or "").split(",") if x.strip()]
    ids += [x.strip() for x in os.getenv("HR_MAX_IDS", "").split(",") if x.strip()]
    return list(dict.fromkeys(ids))


def is_hr(user_id: str) -> bool:
    return user_id in recipients()


def btn(text: str, payload: str) -> dict:
    return {"text": text, "payload": payload}


def action_buttons(esc_id: int, taken: bool = False) -> list:
    first = [btn("Ответить", f"hq:reply:{esc_id}")] + ([] if taken else [btn("Взять в работу", f"hq:take:{esc_id}")])
    return [first, [btn("Закрыть обращение", f"hq:close:{esc_id}")]]


def _who(esc: dict) -> str:
    where = f" · {esc['site']}" if esc.get("site") else ""
    return f"{esc.get('name') or 'MAX ID ' + esc['user_id']}{where}"


def _clip(text: str) -> str:
    return text if len(text) <= SHOW else text[:SHOW] + "…"


def _spawn(coro) -> None:
    """Запускает рассылку в фоне, не задерживая ответ боту; ошибки доставки HR не должны ломать диалог сотрудника."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()                                   # нет работающего цикла событий (синхронный вызов) — уведомлять некому
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def flush() -> None:
    """Дождаться отправленных в фоне уведомлений (тесты, остановка сервиса)."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _send(user_id: str, text: str, buttons: list | None = None) -> bool:
    try:
        await channels.send_max(user_id, text, buttons)
        return True
    except Exception as e:  # noqa: BLE001 — например, HR ещё ни разу не нажимал «Начать» у бота
        log.warning("уведомление HR %s не доставлено: %s", user_id, e)
        return False


def create_escalation(user_id: str, channel: str, reason: str, kind: str) -> int:
    """Создаёт (или дополняет) обращение и уведомляет HR в МАКС. Единственная точка создания обращений в коде бота."""
    reason = reason.strip()[:1500]
    esc_id, created = db.add_escalation_ex(user_id, channel, reason, kind)
    if recipients():
        _spawn(notify_new(esc_id, reason) if created else notify_update(esc_id, reason))
    return esc_id


async def notify_new(esc_id: int, reason: str) -> int:
    esc = db.get_escalation(esc_id)
    if not esc:
        return 0
    text = (f"🔔 Новое обращение №{esc_id}\nСотрудник: {_who(esc)}\nПричина: {KIND_LABELS.get(esc['kind'], esc['kind'])}\n\n«{_clip(reason)}»")
    return sum([await _send(uid, text, action_buttons(esc_id)) for uid in recipients()])


async def notify_update(esc_id: int, reason: str) -> int:
    """В обращении, которое уже ведёт HR, появилось новое — сообщаем ведущему (или всем, если ведущего нет)."""
    esc = db.get_escalation(esc_id)
    if not esc or not (esc.get("assignee") or esc["status"] == "in_progress"):
        return 0                                       # ещё никто не взял — HR уже получил уведомление, дубли не нужны
    text = f"➕ Дополнение к обращению №{esc_id}\nСотрудник: {_who(esc)}\n\n«{_clip(reason)}»"
    to = [esc["assignee"]] if esc.get("assignee") else recipients()
    return sum([await _send(uid, text, action_buttons(esc_id, taken=True)) for uid in to])


def human_engaged(user_id: str) -> bool:
    return db.human_engaged(user_id, HANDOFF_HOURS)


async def forward_followup(user_id: str, text: str) -> int:
    """Сообщение сотрудника, пока HR ведёт диалог, — ведущему HR (или всем получателям, если обращение ведут из панели)."""
    esc = db.open_escalation(user_id)
    if not esc:
        return 0
    msg = f"💬 Сообщение по обращению №{esc['id']}\nСотрудник: {_who(esc)}\n\n«{_clip(text)}»"
    to = [esc["assignee"]] if esc.get("assignee") else recipients()
    return sum([await _send(uid, msg, action_buttons(esc["id"], taken=True)) for uid in to])


async def deliver_reply(esc: dict, text: str, hr_user_id: str | None = None) -> dict:
    """Ответ HR сотруднику (из МАКС или панели): в историю диалога, обращение «в работе», сообщение уходит в чат сотрудника."""
    db.add_message(esc["user_id"], esc["channel"], "hr", text)
    if esc["status"] == "open":
        db.set_escalation_status(esc["id"], "in_progress")
    if hr_user_id and not esc.get("assignee"):
        db.set_escalation_assignee(esc["id"], hr_user_id)
    delivered, error = True, None
    if esc["channel"] == "max":
        try:
            await channels.send_max(esc["user_id"], f"Ответ HR-специалиста:\n{text}")
        except Exception as e:  # noqa: BLE001
            delivered, error = False, str(e)[:200]
    return {"ok": True, "delivered": delivered, "error": error}


# ---------- сторона HR: кнопки и ответ текстом ----------
def _state(hr_id: str) -> dict:
    raw = db.get_setting(f"hq:{hr_id}")
    return json.loads(raw) if raw else {}


def _set_state(hr_id: str, st: dict) -> None:
    db.set_setting(f"hq:{hr_id}", json.dumps(st))


def awaiting(hr_id: str) -> int | None:
    return _state(hr_id).get("reply_to")


def stop_waiting(hr_id: str) -> None:
    if awaiting(hr_id):
        _set_state(hr_id, {})


def _msg(text: str, buttons=None) -> dict:
    return {"text": text, "buttons": buttons, "md": False}


STALE = "Это обращение уже закрыто или не найдено."


async def handle_hr(hr_id: str, text: str | None, payload: str | None) -> list[dict] | None:
    """Обрабатывает действие HR. → сообщения HR либо None, если это не действие по обращениям (обычный разговор с ботом)."""
    if payload and payload.startswith("hq:"):
        return await _payload(hr_id, payload)
    if text and text.startswith("/"):
        stop_waiting(hr_id)                            # команда отменяет режим «жду текст ответа»
        return None
    esc_id = awaiting(hr_id)
    if text and esc_id:
        return await _reply_text(hr_id, esc_id, text)
    return None


async def _payload(hr_id: str, payload: str) -> list[dict]:
    parts = payload.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "cancel":
        stop_waiting(hr_id)
        return [_msg("Хорошо, ничего не отправляю.")]
    esc = db.get_escalation(int(parts[2])) if len(parts) > 2 and parts[2].isdigit() else None
    if not esc or esc["status"] == "resolved":
        return [_msg(STALE)]
    if action == "take":
        db.set_escalation_assignee(esc["id"], hr_id)
        db.set_escalation_status(esc["id"], "in_progress")
        return [_msg(f"Вы ведёте обращение №{esc['id']} ({_who(esc)}). Бот не будет отвечать сотруднику сам, пока вы с ним на связи: его "
                     "сообщения придут сюда.", action_buttons(esc["id"], taken=True))]
    if action == "reply":
        if not esc.get("assignee"):
            db.set_escalation_assignee(esc["id"], hr_id)
        _set_state(hr_id, {"reply_to": esc["id"]})
        return [_msg(f"Напишите ответ сотруднику ({_who(esc)}) одним сообщением — я передам его в тот же чат.",
                     [[btn("Отмена", "hq:cancel")]])]
    if action == "close":
        db.set_escalation_status(esc["id"], "resolved")
        stop_waiting(hr_id)
        return [_msg(f"Обращение №{esc['id']} закрыто. Бот снова отвечает сотруднику сам.")]
    return [_msg(STALE)]


async def _reply_text(hr_id: str, esc_id: int, text: str) -> list[dict]:
    esc = db.get_escalation(esc_id)
    _set_state(hr_id, {})
    if not esc or esc["status"] == "resolved":
        return [_msg(STALE)]
    res = await deliver_reply(esc, text.strip()[:4000], hr_id)
    if not res["delivered"]:
        return [_msg(f"Не удалось доставить ответ: {res['error']}. Он сохранён в диалоге, попробуйте позже.", action_buttons(esc_id, taken=True))]
    return [_msg(f"✅ Ответ отправлен сотруднику ({_who(esc)}).", action_buttons(esc_id, taken=True))]


async def send_test(user_id: str) -> bool:
    """Проверка настройки: тестовое сообщение получателю (получатель должен был нажать «Начать» у бота)."""
    return await _send(user_id, "✅ Вы подключены как получатель обращений HR. Сюда будут приходить новые обращения с кнопками «Ответить», "
                                "«Взять в работу» и «Закрыть обращение».")

