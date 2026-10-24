"""Проверка вводимых данных: ограничения полей запросов и понятные сообщения об ошибках на русском."""
import re
from datetime import date, timedelta
from typing import Annotated

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import StringConstraints

CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # управляющие символы (кроме перевода строки и табуляции)
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")             # идентификаторы пользователей (MAX — числа, веб — web-xxxx)


def Str(min_length: int = 0, max_length: int = 255):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


def no_control(v: str | None, field: str) -> str | None:
    if v and CTRL.search(v):
        raise ValueError(f"в поле «{field}» недопустимые управляющие символы")
    return v


def person_name(v: str, field: str = "имя", full: bool = False) -> str:
    """full=True — нужны имя и фамилия (минимум два слова с буквами)."""
    v = re.sub(r"\s+", " ", (v or "").strip())
    if len(v) < 2 or len(v) > 100:
        raise ValueError(f"{field}: от 2 до 100 символов")
    if not re.search(r"[^\W\d_]", v):
        raise ValueError(f"{field}: должно содержать буквы")
    if re.search(r"[<>{}\[\]\\|;=@#$%^*+~`]|[\x00-\x1f]", v):
        raise ValueError(f"{field}: недопустимые символы")
    if full and len([w for w in v.split(" ") if re.search(r"[^\W\d_]", w)]) < 2:
        raise ValueError(f"{field}: укажите имя и фамилию (например, Иван Петров)")
    return v


def user_ref(v: str | None, field: str) -> str | None:
    v = (v or "").strip()
    if v and not ID_RE.match(v):
        raise ValueError(f"{field}: только латинские буквы, цифры, _ и - (до 64 символов)")
    return v or None


def iso_date(v: str | None, field: str) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{field}: дата в формате ГГГГ-ММ-ДД")


def employee_dates(hire: date | None, end: date | None) -> None:
    """Дата выхода — в разумных пределах; конец испытательного срока — после выхода и не позже, чем через год."""
    if hire is None:
        raise ValueError("укажите дату выхода")
    today = date.today()
    if not date(2000, 1, 1) <= hire <= today + timedelta(days=730):
        raise ValueError("дата выхода должна быть между 2000 годом и двумя годами вперёд")
    if end is not None and not hire < end <= hire + timedelta(days=366):
        raise ValueError("конец испытательного срока должен быть после даты выхода и не позже, чем через год")


_MSG = {"string_too_short": "слишком короткое значение (минимум {min_length} симв.)", "string_too_long": "слишком длинное значение (максимум {max_length} симв.)",
        "missing": "обязательное поле", "string_type": "нужна строка", "bool_parsing": "нужно «да» или «нет»", "int_parsing": "нужно целое число",
        "float_parsing": "нужно число", "greater_than": "должно быть больше {gt}", "less_than_equal": "должно быть не больше {le}",
        "greater_than_equal": "должно быть не меньше {ge}", "string_pattern_mismatch": "неверный формат", "too_long": "слишком много элементов (максимум {max_length})",
        "list_type": "нужен список", "dict_type": "нужен объект", "literal_error": "недопустимое значение", "json_invalid": "некорректный JSON"}


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
        text = (e["msg"].removeprefix("Value error, ") if e["type"] == "value_error" else _MSG.get(e["type"], e["msg"]).format(**{k: ctx.get(k, "") for k in ("min_length", "max_length", "gt", "le", "ge")}))
        out.append(f"{FIELDS.get(field, field)}: {text}" if field and e["type"] != "value_error" else text)
    return "; ".join(out) or "неверные данные"


async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse({"detail": format_errors(exc)}, status_code=400)
