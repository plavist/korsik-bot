"""Проверка вводимых данных панели: ограничения полей и понятные сообщения на русском."""
import re

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_MSG = {"string_too_short": "слишком короткое значение (минимум {min_length} симв.)", "string_too_long": "слишком длинное значение (максимум {max_length} симв.)",
        "missing": "обязательное поле", "string_type": "нужна строка", "bool_parsing": "нужно «да» или «нет»", "string_pattern_mismatch": "неверный формат",
        "json_invalid": "некорректный JSON", "literal_error": "недопустимое значение"}


FIELDS = {"name": "Имя", "site": "Площадка", "hire_date": "Дата выхода", "probation_end": "Конец испытательного срока", "manager": "Руководитель",
          "manager_user": "ID чата руководителя", "message": "Сообщение", "payload": "Данные кнопки", "text": "Текст", "title": "Название", "code": "Код",
          "user_id": "ID пользователя", "password": "Пароль", "login": "Логин", "role": "Роль", "description": "Описание", "reply": "Ответ", "query": "Запрос",
          "fallback": "Запасной ответ", "url": "Адрес", "recipients": "Получатели", "hours": "Часы", "idx": "Номер", "step_id": "Этап", "image": "Файл", "old": "Текущий пароль",
          "new": "Новый пароль", "items": "Сообщения", "sites": "Площадки", "type": "Тип", "direction": "Направление", "mode": "Режим", "status": "Статус",
          "days": "Период", "sentiment": "Тональность", "day": "Дата", "topic": "Тема", "now": "Время", "due": "Срок", "after": "Этап-ориентир", "index": "Позиция"}


def format_errors(exc: RequestValidationError) -> str:
    out = []
    for e in exc.errors():
        field = ".".join(str(x) for x in e["loc"] if x not in ("body", "query", "path"))
        ctx = e.get("ctx") or {}
        if e["type"] == "value_error":
            out.append(e["msg"].removeprefix("Value error, "))
        else:
            text = _MSG.get(e["type"], e["msg"]).format(**{k: ctx.get(k, "") for k in ("min_length", "max_length")})
            out.append(f"{FIELDS.get(field, field)}: {text}" if field else text)
    return "; ".join(out) or "неверные данные"


async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse({"detail": format_errors(exc)}, status_code=400)


def password(v: str, login: str = "") -> str:
    """Не короче 8 символов, есть буква и цифра, не совпадает с логином, без пробелов по краям."""
    if v != v.strip():
        raise ValueError("пароль: уберите пробелы в начале и в конце")
    if len(v) < 8 or len(v) > 128:
        raise ValueError("пароль: от 8 до 128 символов")
    if not (re.search(r"[^\W\d_]", v) and re.search(r"\d", v)):
        raise ValueError("пароль: нужны хотя бы одна буква и одна цифра")
    if login and v.lower() == login.lower():
        raise ValueError("пароль не должен совпадать с логином")
    return v


def person_name(v: str, field: str, full: bool = False) -> str:
    v = re.sub(r"\s+", " ", (v or "").strip())
    if v and full and len([w for w in v.split(" ") if re.search(r"[^\W\d_]", w)]) < 2:
        raise ValueError(f"{field}: укажите имя и фамилию (например, Сергей Иванов)")
    if v and (len(v) < 2 or len(v) > 100 or not re.search(r"[^\W\d_]", v) or re.search(r"[<>{}\[\]\\|;=@#$%^*+~`]|[\x00-\x1f]", v)):
        raise ValueError(f"{field}: 2–100 символов, буквы, без спецсимволов")
    return v
