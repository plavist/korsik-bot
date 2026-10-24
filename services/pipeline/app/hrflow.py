"""Бесшовная передача вопроса живому HR: обращение создаётся сразу, без подтверждения сотрудника.

Если Корсик не знает ответа, он ничего не пишет в чат — вопрос уходит HR автоматически (bot.py). Здесь же — явные способы
позвать человека: «Связаться с рекрутером», /HR и «Передать HR через Корсика» (сотрудник описывает проблему одним сообщением).
Состояние на пользователя (не на сотрудника — работает и без привязки к сценарию) хранится в settings: {"await": bool} —
следующее сообщение станет текстом обращения.
"""
import json
import os

from . import db, handoff
from . import scenario as sc

FORWARD_REPLY = "Передал! Коллеги из подбора персонала свяжутся с тобой 🤝"
CONTACT_REPLY = "Передал рекрутеру, что ты хочешь с ним поговорить — он свяжется с тобой в ближайшее время 🤝"
CANCEL_REPLY = "Хорошо, ничего не передаю 🙂 Если передумаешь — просто напиши мне или набери /HR."
HR_COMMAND_REPLY = ("Передал твой запрос HR-специалисту — коллеги из подбора персонала свяжутся с тобой 🤝 "
                    "Если хочешь что-то добавить, просто напиши — HR увидит весь наш диалог.")


def msg(text: str, buttons=None) -> dict:
    return {"text": text, "buttons": buttons or None}


def btn(text: str, payload: str) -> dict:
    return {"text": text, "payload": payload}


def option_buttons(kind) -> list:
    """Кнопки из сценария («Опции для сотрудника»): both — писать через Корсика или к рекрутеру; contact — только к рекрутеру."""
    if kind == "contact":
        return [[btn(sc.OPT_CONTACT, "hr:contact")]]
    if kind == "ready":
        return [[btn(sc.OPT_READY_TO_TALK, "hr:contact")]]
    return [[btn(sc.OPT_FORWARD, "hr:forward")], [btn(sc.OPT_CONTACT, "hr:contact")]]


def _load(user_id: str) -> dict:
    raw = db.get_setting(f"hr:{user_id}")
    return json.loads(raw) if raw else {}


def _save(user_id: str, st: dict) -> None:
    db.set_setting(f"hr:{user_id}", json.dumps(st, ensure_ascii=False))


def escalate(user_id: str, channel: str, kind: str, reason: str) -> int:
    """Сразу создаёт обращение для HR (панель управления → «Эскалации»)."""
    return handoff.create_escalation(user_id, channel, reason, kind)


def awaiting(user_id: str) -> bool:
    return bool(_load(user_id).get("await"))


def start_forward(user_id: str, prompt: str = sc.HR_FORWARD_PROMPT) -> dict:
    _save(user_id, {"await": True})
    return msg(prompt, [[btn("Не передавать", "hr:cancel")]])


def stop_waiting(user_id: str) -> None:
    """Пришла команда — следующее сообщение уже не считаем текстом обращения."""
    if _load(user_id).get("await"):
        _save(user_id, {"await": False})


def take_text(user_id: str, channel: str, text: str) -> dict:
    """Сообщение после «Передать HR через Корсика» — сразу уходит HR."""
    _save(user_id, {"await": False})
    escalate(user_id, channel, "forward", text)
    return msg(FORWARD_REPLY)


def cancel(user_id: str) -> dict:
    _save(user_id, {"await": False})
    return msg(CANCEL_REPLY)


def contact(user_id: str, channel: str) -> dict:
    """«Связаться с рекрутером» — явная просьба, уходит сразу (обращение для HR в панели управления)."""
    escalate(user_id, channel, "contact", "Сотрудник просит связаться с рекрутером")
    extra = os.getenv("RECRUITER_CONTACT")
    return msg(CONTACT_REPLY + (("\n" + extra) if extra else ""))


def hr_command(user_id: str, channel: str, reply: str = HR_COMMAND_REPLY) -> dict:
    """/HR: обращение создаётся сразу; ответ бота (его можно править в панели управления) подтверждает передачу."""
    escalate(user_id, channel, "command", "Пользователь запросил связь с HR командой /HR")
    return msg(reply)
