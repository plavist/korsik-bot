import asyncio
import hmac
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from fastapi.exceptions import RequestValidationError

from . import sentiment as sentiment_mod
from . import validation as V
from . import assets, bot, channels, commands, config, db, engine, insights, max_poller, scenario_edit
from . import scenario as sc



@asynccontextmanager
async def lifespan(app: FastAPI):
    commands.ensure()
    sc.reset()
    poller = asyncio.create_task(max_poller.run(process_max))
    registrar = asyncio.create_task(register_commands_on_start())
    scheduler = asyncio.create_task(scheduler_loop())
    yield
    for t in (poller, registrar, scheduler):
        t.cancel()


async def register_commands_on_start(attempts: int = 3, delay: float = 5.0) -> None:
    """Подсказки команд в MAX регистрируем при старте; сбой не критичен — повторить можно из панели управления."""
    if not config.max_token():
        return
    for i in range(attempts):
        try:
            await channels.register_commands()
            max_poller.state["commands_registered"] = True
            return
        except Exception as e:  # noqa: BLE001
            max_poller.state["last_error"] = f"регистрация команд: {e}"[:300]
            if i < attempts - 1:
                await asyncio.sleep(delay)


app = FastAPI(title="pipeline", lifespan=lifespan)
app.add_exception_handler(RequestValidationError, V.validation_handler)


def internal_auth(x_internal_token: str | None = Header(default=None)) -> None:
    """Админские маршруты доступны только сервису admin (порт pipeline открыт наружу ради веб-чата)."""
    if not hmac.compare_digest(config.internal_token(), x_internal_token or ""):
        raise HTTPException(401, "internal only")


ADMIN = [Depends(internal_auth)]


class StatusIn(BaseModel):
    status: str = Field(pattern="^(open|in_progress|resolved)$")


class ReplyIn(BaseModel):
    text: V.Str(1, 4000)

    @field_validator("text")
    @classmethod
    def _text(cls, v):
        return V.no_control(v, "ответ")


class SubscribeIn(BaseModel):
    url: V.Str(8, 500)

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?(/[^\s]*)?", v):
            raise ValueError("адрес вебхука: https://домен/путь")
        return v


class EmployeeIn(BaseModel):
    name: V.Str(2, 100)
    site: V.Str(1, 24)
    hire_date: V.Str(1, 10)
    is_office: bool | None = None
    probation_end: V.Str(0, 10) | None = None
    manager: V.Str(0, 100) | None = None
    manager_user: V.Str(0, 64) | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return V.person_name(v, "имя и фамилия", full=True)

    @field_validator("manager")
    @classmethod
    def _mgr(cls, v):
        return V.person_name(v, "руководитель", full=True) if v else v

    @field_validator("manager_user")
    @classmethod
    def _mgr_user(cls, v):
        return V.user_ref(v, "ID чата руководителя")

    @model_validator(mode="after")
    def _dates(self):
        V.employee_dates(V.iso_date(self.hire_date, "дата выхода"), V.iso_date(self.probation_end, "конец испытательного срока"))
        return self


class EmployeePatch(BaseModel):
    name: V.Str(2, 100) | None = None
    site: V.Str(1, 24) | None = None
    hire_date: V.Str(0, 10) | None = None
    is_office: bool | None = None
    probation_end: V.Str(0, 10) | None = None
    recruiter_ok: bool | None = None
    active: bool | None = None
    manager: V.Str(0, 100) | None = None
    manager_user: V.Str(0, 64) | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return V.person_name(v, "имя и фамилия", full=True) if v is not None else v

    @field_validator("manager")
    @classmethod
    def _mgr(cls, v):
        return V.person_name(v, "руководитель", full=True) if v else v

    @field_validator("manager_user")
    @classmethod
    def _mgr_user(cls, v):
        return (V.user_ref(v, "ID чата руководителя") or "") if v is not None else v


class BindIn(BaseModel):
    user_id: V.Str(1, 64)

    @field_validator("user_id")
    @classmethod
    def _uid(cls, v):
        return V.user_ref(v, "user_id")


class SendStepIn(BaseModel):
    step_id: V.Str(1, 40)


class TickIn(BaseModel):
    now: V.Str(0, 32) | None = None      # ISO-время по Москве: «притвориться», что сейчас именно оно


class AgeIn(BaseModel):
    hours: float = Field(25, gt=0, le=720)


class CommandIn(BaseModel):
    name: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=300)
    kind: str | None = Field(None, max_length=16)
    reply: str | None = Field(None, max_length=8000)
    query: str | None = Field(None, max_length=500)
    fallback: str | None = Field(None, max_length=8000)


class TextIn(BaseModel):
    step_id: V.Str(1, 40)
    idx: int = Field(ge=0, le=500)
    text: str = Field(max_length=3500)


async def deliver(emp: dict, msgs: list[dict]) -> None:
    """Проактивная доставка сообщений сотруднику (планировщик, панель управления): в историю и в канал сотрудника."""
    channel = emp["channel"] or "max"
    for m in msgs:
        db.add_message(emp["user_id"], channel, "assistant", m["text"], kind="proactive", buttons=m.get("buttons"), image=m.get("image"))
    if channel == "max":
        for m in msgs:
            await channels.send_max(emp["user_id"], m["text"], m.get("buttons"), md=m.get("md", False), image=m.get("image"))


async def push(user_id: str, text: str) -> None:
    """Push-сообщение произвольному пользователю (руководителю, получателю дайджеста): в историю и в MAX."""
    db.add_message(user_id, "max", "assistant", text, kind="proactive")
    await channels.send_max(user_id, text)


async def send_digest(days: int = 7) -> dict:
    """Недельный дайджест настроений → получателям (руководитель управления по подбору и др.)."""
    d = await insights.digest(days)
    text = insights.digest_text(d)
    ok, failed = [], []
    for uid in [x for x in (db.get_setting("digest_recipients", "") or "").split(",") if x]:
        try:
            await push(uid, text)
            ok.append(uid)
        except Exception as e:  # noqa: BLE001
            failed.append({"user_id": uid, "error": str(e)[:120]})
    return {"sent": ok, "failed": failed, "text": text}


async def weekly_jobs(now=None) -> None:
    """Раз в неделю (пн, с 9:00) — дайджест; ежедневно — напоминания руководителям."""
    now = now or engine.now_local()
    await insights.manager_nudges(push, now)
    week = now.strftime("%G-W%V")
    if now.weekday() == 0 and now.hour >= 9 and db.get_setting("digest:last") != week:
        db.set_setting("digest:last", week)
        await send_digest(7)


async def scheduler_loop(interval: float = 30.0) -> None:
    while True:
        try:
            await engine.tick(deliver)
            await weekly_jobs()
        except Exception as e:  # noqa: BLE001 — планировщик не должен падать из-за одного сотрудника
            max_poller.state["last_error"] = f"планировщик: {e}"[:300]
        await asyncio.sleep(interval)


async def process_max(parsed: dict) -> None:
    """Событие MAX (из polling или webhook) → бот → ответ пользователю в MAX."""
    try:
        res = await bot.handle(parsed["user_id"], "max", text=parsed.get("text"), payload=parsed.get("payload"),
                               name=parsed.get("name"))
        for m in res["messages"]:
            await channels.send_max(parsed["user_id"], m["text"], m.get("buttons"), md=m.get("md", False), image=m.get("image"))
        if parsed.get("callback_id"):
            await channels.answer_callback(parsed["callback_id"])
        max_poller.state["processed"] += 1
    except Exception as e:  # noqa: BLE001 — сбой одного события не должен останавливать канал
        max_poller.state["last_error"] = f"обработка/отправка: {e}"[:300]


# ---------- публичные маршруты ----------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/assets/{fname}")
async def asset_file(fname: str):
    p = assets.path(fname)
    if not p:
        raise HTTPException(404, "no such asset")
    return FileResponse(p, media_type=assets.media_type(fname))


# ---------- MAX ----------
@app.post("/max/webhook")
async def max_webhook(request: Request, background: BackgroundTasks,
                      x_max_bot_api_secret: str | None = Header(default=None)):
    secret = config.max_webhook_secret()
    if secret and not hmac.compare_digest(secret, x_max_bot_api_secret or ""):
        raise HTTPException(401, "bad secret")
    if not channels.max_enabled():
        return {"ok": True, "ignored": "max channel disabled"}
    update = await request.json()
    max_poller.recent.appendleft({"at": 0, "update": update})
    parsed = channels.parse_max_update(update)
    if not parsed:
        return {"ok": True, "ignored": "not a user message"}
    background.add_task(process_max, parsed)
    return {"ok": True}


@app.get("/api/max/status", dependencies=ADMIN)
async def max_status():
    out = {"configured": bool(config.max_token()), "transport": config.max_transport(),
           "api": config.max_api(), "poller": max_poller.state, "bot": None, "subscriptions": None, "error": None}
    if not out["configured"]:
        return out
    try:
        me = await channels.max_request("GET", "/me", timeout=15)
        out["bot"] = me.json() if me.status_code == 200 else None
        if me.status_code != 200:
            out["error"] = f"/me → {me.status_code}"
        subs = await channels.max_request("GET", "/subscriptions", timeout=15)
        if subs.status_code == 200:
            out["subscriptions"] = subs.json().get("subscriptions", [])
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:300]
    return out


@app.post("/api/max/subscribe", dependencies=ADMIN)
async def max_subscribe(body: SubscribeIn):
    payload = {"url": body.url, "update_types": ["message_created", "bot_started"]}
    if config.max_webhook_secret():
        payload["secret"] = config.max_webhook_secret()
    r = await channels.max_request("POST", "/subscriptions", json=payload)
    return {"status": r.status_code, "body": r.json() if r.content else None}


@app.post("/api/max/unsubscribe", dependencies=ADMIN)
async def max_unsubscribe():
    r = await channels.max_request("GET", "/subscriptions")
    urls = [s["url"] for s in r.json().get("subscriptions", [])] if r.status_code == 200 else []
    for u in urls:
        await channels.max_request("DELETE", "/subscriptions", params={"url": u})
    return {"removed": urls}


@app.post("/api/max/commands", dependencies=ADMIN)
async def max_commands():
    try:
        return {"ok": True, "response": await channels.register_commands()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)[:300])


async def _publish_commands() -> dict:
    """Обновляет подсказки «/» в MAX после правок (если MAX настроен). Ошибка не мешает сохранению команды."""
    if not config.max_token():
        return {"published": False, "error": None, "reason": "токен MAX-бота не задан"}
    try:
        await channels.register_commands()
        max_poller.state["commands_registered"] = True
        return {"published": True, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"published": False, "error": str(e)[:200]}


@app.get("/api/commands", dependencies=ADMIN)
async def list_bot_commands():
    return commands.all_commands()


@app.post("/api/commands", dependencies=ADMIN)
async def create_bot_command(body: CommandIn):
    name = (body.name or "").strip().lower().lstrip("/")
    try:
        fields = commands.validate(body.model_dump(exclude={"name"}), name, creating=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.save_command(name, **fields, builtin=False, sort=100 + len(db.list_commands()))
    return {"command": db.get_command(name), **await _publish_commands()}


@app.put("/api/commands/{name}", dependencies=ADMIN)
async def update_bot_command(name: str, body: CommandIn):
    if not commands.get(name):
        raise HTTPException(404, "not found")
    try:
        fields = commands.validate(body.model_dump(exclude={"name", "kind"}, exclude_none=True), name.lower(), creating=False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.save_command(name.lower(), **fields)
    return {"command": db.get_command(name.lower()), **await _publish_commands()}


@app.delete("/api/commands/{name}", dependencies=ADMIN)
async def delete_bot_command(name: str):
    row = commands.get(name)
    if not row:
        raise HTTPException(404, "not found")
    if row["builtin"]:
        raise HTTPException(409, "встроенную команду удалить нельзя — её можно только отредактировать")
    db.delete_command(row["name"])
    return {"deleted": row["name"], **await _publish_commands()}


@app.post("/api/commands/{name}/preview", dependencies=ADMIN)
async def preview_bot_command(name: str):
    """«Вывод» команды: что увидит сотрудник (без привязки к сценарию, без записи в историю)."""
    if not commands.get(name):
        raise HTTPException(404, "not found")
    msgs = await bot._command("preview", "max", f"/{name}", None)
    return {"messages": msgs}


@app.get("/api/max/debug", dependencies=ADMIN)
async def max_debug():
    return list(max_poller.recent)


# ---------- админские маршруты (только через сервис admin) ----------
def _days(v: str | None) -> int | None:
    if v and not (v.isdigit() and int(v) <= 3650):
        raise HTTPException(400, "период: число дней от 1 до 3650")
    return int(v) if v and int(v) > 0 else None


class Flt:  # общие фильтры страницы аналитики (пустое значение = без фильтра)
    def __init__(self, days: str | None = None, site: str | None = None, manager: str | None = None, topic: str | None = None,
                 sentiment: str | None = Query(None, pattern="^(neutral|negative|positive)?$")):
        if topic and topic not in sentiment_mod.TOPICS:
            raise HTTPException(400, "неизвестная тема")
        self.topic, self.sentiment = topic or None, sentiment or None
        if site and site not in sc.ALL_SITES:
            raise HTTPException(400, "неизвестная площадка")
        if manager and len(manager) > 100:
            raise HTTPException(400, "руководитель: не длиннее 100 символов")
        self.days, self.site, self.manager = _days(days), site or None, manager or None


@app.get("/api/analytics", dependencies=ADMIN)
async def analytics(f: Flt = Depends()):
    return db.analytics(f.days, f.site, f.manager, f.topic, f.sentiment)


@app.get("/api/analytics/drill", dependencies=ADMIN)
async def analytics_drill(f: Flt = Depends(), day: str | None = Query(None, pattern=r"^(\d{4}-\d{2}-\d{2})?$"), red_flag: bool = False, user_id: str | None = Query(None, max_length=64)):
    return db.messages_drill(f.days, f.site, f.manager, f.topic, f.sentiment, day or None, red_flag, user_id or None)


@app.get("/api/insights/stages", dependencies=ADMIN)
async def insights_stages(f: Flt = Depends()):
    return insights.stages(f.site, f.manager)


@app.get("/api/insights/funnel", dependencies=ADMIN)
async def insights_funnel(f: Flt = Depends()):
    return insights.funnel(f.site, f.manager)


@app.get("/api/insights/risk", dependencies=ADMIN)
async def insights_risk(f: Flt = Depends()):
    return insights.risk(f.site, f.manager, f.days)


@app.get("/api/insights/comments", dependencies=ADMIN)
async def insights_comments(f: Flt = Depends()):
    names = {e["id"]: e for e in db.list_employees()}
    return [{**c, "name": names[c["employee_id"]]["name"]} for c in insights.survey_comments(days=f.days)
            if c["employee_id"] in names and (not f.site or names[c["employee_id"]]["site"] == f.site)
            and (not f.manager or names[c["employee_id"]].get("manager") == f.manager)]


@app.get("/api/insights/managers", dependencies=ADMIN)
async def insights_managers():
    return sorted({e["manager"] for e in db.list_employees() if e.get("manager")})


@app.get("/api/insights/digest", dependencies=ADMIN)
async def insights_digest(f: Flt = Depends()):
    return await insights.digest(f.days or 7, f.site, f.manager)


class DigestSettings(BaseModel):
    recipients: V.Str(0, 500) = ""

    @field_validator("recipients")
    @classmethod
    def _rcp(cls, v):
        ids = [x.strip() for x in v.split(",") if x.strip()]
        if len(ids) > 20:
            raise ValueError("получателей не больше 20")
        bad = [x for x in ids if not V.ID_RE.match(x)]
        if bad:
            raise ValueError(f"получатели: ID пользователей MAX (цифры) через запятую, неверно: {bad[0]}")
        return ",".join(ids)


@app.get("/api/insights/digest/settings", dependencies=ADMIN)
async def digest_settings():
    return {"recipients": db.get_setting("digest_recipients", "")}


@app.put("/api/insights/digest/settings", dependencies=ADMIN)
async def set_digest_settings(body: DigestSettings):
    db.set_setting("digest_recipients", body.recipients)
    return {"ok": True}


@app.post("/api/insights/digest/send", dependencies=ADMIN)
async def insights_digest_send(f: Flt = Depends()):
    return await send_digest(f.days or 7)


@app.get("/api/insights/notices", dependencies=ADMIN)
async def insights_notices(manager: str | None = None):
    return db.list_notices(manager or None)


@app.get("/api/users/{user_id}/history", dependencies=ADMIN)
async def user_history_admin(user_id: str):
    return db.user_history(user_id)


@app.get("/api/escalations", dependencies=ADMIN)
async def escalations(status: str | None = None):
    return db.list_escalations(status)


@app.post("/api/escalations/{esc_id}/status", dependencies=ADMIN)
async def escalation_status(esc_id: int, body: StatusIn):
    if not db.set_escalation_status(esc_id, body.status):
        raise HTTPException(404, "not found")
    return {"id": esc_id, "status": body.status}


@app.get("/api/escalations/{esc_id}", dependencies=ADMIN)
async def escalation_dialog(esc_id: int):
    esc = db.get_escalation(esc_id)
    if not esc:
        raise HTTPException(404, "not found")
    return {"escalation": esc, "messages": db.history(esc["user_id"], 200)}


@app.post("/api/escalations/{esc_id}/reply", dependencies=ADMIN)
async def escalation_reply(esc_id: int, body: ReplyIn):
    """Ответ HR: сохраняется в диалоге и доставляется пользователю (в MAX — сразу)."""
    esc = db.get_escalation(esc_id)
    if not esc:
        raise HTTPException(404, "not found")
    text = body.text
    db.add_message(esc["user_id"], esc["channel"], "hr", text)
    if esc["status"] == "open":
        db.set_escalation_status(esc_id, "in_progress")
    delivered, error = True, None
    if esc["channel"] == "max":
        try:
            await channels.send_max(esc["user_id"], f"Ответ HR-специалиста:\n{text}")
        except Exception as e:  # noqa: BLE001
            delivered, error = False, str(e)[:200]
    return {"ok": True, "delivered": delivered, "error": error}


# ---------- онбординг: сотрудники и сценарий (только через панель управления) ----------
_bot_username: dict = {}


async def bot_link(code: str) -> str | None:
    """Deep-link на бота: https://max.ru/<username>?start=<код>."""
    if "u" not in _bot_username and config.max_token():
        try:
            r = await channels.max_request("GET", "/me", timeout=10)
            _bot_username["u"] = r.json().get("username") if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            return None
    return f"https://max.ru/{_bot_username['u']}?start={code}" if _bot_username.get("u") else None


def _valid_date(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise HTTPException(400, f"неверная дата: {v} (ГГГГ-ММ-ДД)")


async def _emp_view(emp: dict, detail: bool = False) -> dict:
    user = db.get_user(emp["user_id"]) if emp["user_id"] else None
    tl = engine.timeline(emp)
    out = {**{k: emp[k] for k in ("id", "code", "name", "site", "is_office", "hire_date", "probation_end", "recruiter_ok",
                                  "form", "user_id", "channel", "active", "manager", "manager_user")},
           "user_name": user["name"] if user else None, "link": await bot_link(emp["code"]),
           # у всех один максимум — число этапов сценария; этапы «не для этой площадки» засчитываются как пройденные
           "progress": {"done": sum(t["status"] in ("done", "n/a") for t in tl), "total": len(tl)},
           "pending": (emp["state"].get("pending") or {}).get("step")}
    if detail:
        out.update(timeline=tl, answers=db.list_answers(emp["id"]), next_step=engine.next_step_id(emp))
    return out


def _get_emp(emp_id: int) -> dict:
    emp = db.get_employee(emp_id)
    if not emp:
        raise HTTPException(404, "not found")
    return emp


async def _deliver_or_502(emp: dict, msgs: list[dict]) -> dict:
    try:
        await deliver(emp, msgs)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"сообщения сохранены, но не доставлены: {e}"[:300])
    return {"ok": True, "sent": len(msgs)}


@app.get("/api/employees", dependencies=ADMIN)
async def employees(manager: str | None = None):
    return [await _emp_view(e) for e in db.list_employees() if not manager or e.get("manager") == manager]


@app.post("/api/employees", dependencies=ADMIN)
async def create_employee(body: EmployeeIn):
    if body.site not in sc.ALL_SITES:
        raise HTTPException(400, f"неизвестная площадка; доступны: {', '.join(sc.ALL_SITES)}")
    emp_id = db.create_employee(code=engine.new_code(), name=body.name.strip(), site=body.site,
                                hire_date=_valid_date(body.hire_date) or "", probation_end=_valid_date(body.probation_end), manager=(body.manager or "").strip(),
                                manager_user=(body.manager_user or "").strip(),
                                is_office=sc.SITE_OFFICE.get(body.site, True) if body.is_office is None else body.is_office)
    return await _emp_view(db.get_employee(emp_id), detail=True)


@app.get("/api/employees/{emp_id}", dependencies=ADMIN)
async def employee_detail(emp_id: int, manager: str | None = None):
    emp = _get_emp(emp_id)
    if manager and emp.get("manager") != manager:  # руководитель видит только своих подчинённых
        raise HTTPException(404, "not found")
    return await _emp_view(emp, detail=True)


@app.patch("/api/employees/{emp_id}", dependencies=ADMIN)
async def patch_employee(emp_id: int, body: EmployeePatch):
    emp = _get_emp(emp_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "site" in fields and fields["site"] not in sc.ALL_SITES:
        raise HTTPException(400, f"неизвестная площадка; доступны: {', '.join(sc.ALL_SITES)}")
    if fields.get("hire_date") == "":
        raise HTTPException(400, "дата выхода не может быть пустой")
    for k in ("hire_date", "probation_end"):
        if k in fields:
            fields[k] = _valid_date(fields[k])
    try:  # даты проверяем вместе с сохранёнными: конец испытательного срока должен идти после выхода
        V.employee_dates(V.iso_date(fields.get("hire_date", emp["hire_date"]), "дата выхода"),
                         V.iso_date(fields["probation_end"] if "probation_end" in fields else emp["probation_end"], "конец испытательного срока"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if fields:
        db.update_employee(emp["id"], **fields)
    return await _emp_view(_get_emp(emp_id), detail=True)


@app.delete("/api/employees/{emp_id}", dependencies=ADMIN)
async def remove_employee(emp_id: int):
    _get_emp(emp_id)
    db.delete_employee(emp_id)
    return {"deleted": emp_id}


@app.post("/api/employees/{emp_id}/bind", dependencies=ADMIN)
async def bind_employee(emp_id: int, body: BindIn):
    emp, user = _get_emp(emp_id), db.get_user(body.user_id)
    if not user:
        raise HTTPException(404, "user not found")
    msgs = await engine.bind(emp, user["user_id"], user["channel"])
    return await _deliver_or_502(emp, msgs)


@app.post("/api/employees/{emp_id}/unbind", dependencies=ADMIN)
async def unbind_employee(emp_id: int):
    _get_emp(emp_id)
    db.update_employee(emp_id, user_id=None, channel=None)
    return {"ok": True}


@app.post("/api/employees/{emp_id}/send", dependencies=ADMIN)
async def send_step_now(emp_id: int, body: SendStepIn):
    emp = _get_emp(emp_id)
    if not emp["user_id"]:
        raise HTTPException(409, "сотрудник ещё не привязан к пользователю мессенджера")
    if body.step_id not in sc.STEP_BY_ID:
        raise HTTPException(404, "unknown step")
    return await _deliver_or_502(emp, await engine.send_now(emp, body.step_id))


@app.post("/api/employees/{emp_id}/send-next", dependencies=ADMIN)
async def send_next_step(emp_id: int):
    emp = _get_emp(emp_id)
    step_id = engine.next_step_id(emp)
    if not emp["user_id"]:
        raise HTTPException(409, "сотрудник ещё не привязан к пользователю мессенджера")
    if not step_id:
        raise HTTPException(409, "все этапы уже отправлены")
    return {**await _deliver_or_502(emp, await engine.send_now(emp, step_id)), "step": step_id}


@app.post("/api/employees/{emp_id}/recruiter-ok", dependencies=ADMIN)
async def recruiter_ok(emp_id: int):
    """«Отмашка рекрутера»: разрешает этапы с пометкой «по отмашке» (2-й день, адаптационный план)."""
    _get_emp(emp_id)
    db.update_employee(emp_id, recruiter_ok=True)
    return {"ok": True}


@app.post("/api/employees/{emp_id}/reset", dependencies=ADMIN)
async def reset_employee(emp_id: int):
    _get_emp(emp_id)
    db.reset_employee_progress(emp_id)
    return {"ok": True}


@app.post("/api/scheduler/tick", dependencies=ADMIN)
async def scheduler_tick(body: TickIn):
    """Ручной прогон планировщика (для тестов): now — симулируемое «текущее» время, без него — настоящее."""
    try:
        now = datetime.fromisoformat(body.now) if body.now else None
    except ValueError:
        raise HTTPException(400, f"неверные дата и время: {body.now} (ГГГГ-ММ-ДДTЧЧ:ММ)")
    return {"sent": await engine.tick(deliver, now)}


@app.post("/api/employees/{emp_id}/age-pending", dependencies=ADMIN)
async def age_pending(emp_id: int, body: AgeIn):
    """Тест: «состарить» неотвеченный опрос на N часов — чтобы проверить напоминания, не ожидая суток."""
    emp = _get_emp(emp_id)
    pending = emp["state"].get("pending")
    if not pending:
        raise HTTPException(409, "нет неотвеченного вопроса")
    pending["asked_at"] -= body.hours * 3600
    db.update_employee(emp_id, state=emp["state"])
    return {"ok": True}


@app.get("/api/known-users", dependencies=ADMIN)
async def known_users():
    return [{"user_id": u["user_id"], "name": u["name"] or u["user_id"], "channel": u["channel"]} for u in db.list_users()]


def _edit(fn, *a, **kw):
    """Операции редактора сценария: нет такого этапа/сообщения → 404, неверные данные → 400."""
    try:
        return fn(*a, **kw)
    except KeyError:
        raise HTTPException(404, "этап или сообщение не найдены")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/scenario", dependencies=ADMIN)
async def scenario_view():
    defaults = {x["id"]: x for x in sc.default_steps()}

    def view(step):
        orig = defaults.get(step["id"], {"items": []})["items"]
        items = []
        for idx, it in enumerate(step["items"]):
            o = orig[idx] if idx < len(orig) and orig[idx].get("t") == it["t"] else None
            items.append({"idx": idx, "type": it["t"], "text": it.get("text") or it.get("query", ""),
                          "edited": bool(o and "text" in it and o.get("text") != it.get("text")), "sites": it.get("sites"),
                          "office": it.get("office"), "image": assets.for_item(step["id"], idx, it), "image_choice": it.get("image") or "",
                          "can_image": it["t"] == "msg", "editable": "text" in it,
                          "options": [o_["label"] for o_ in it.get("options", [])]})
        return {"id": step["id"], "title": step["title"], "when": step["when"], "due": step.get("due"), "survey": step["survey"],
                "gate": step.get("gate"), "sites": step.get("sites"), "office": step.get("office"), "items": items,
                "system": step["id"] in scenario_edit.SYSTEM_STEPS, "custom": step["id"] not in defaults}
    return {"steps": [view(s_) for s_ in sc.STEPS], "sites": sc.site_list()}


class StepIn(BaseModel):
    title: V.Str(1, 120)
    due: dict | None = None
    sites: list[V.Str(1, 24)] | None = Field(None, max_length=20)
    office: bool | None = None
    gate: bool = False
    survey: bool = False
    items: list[dict] = Field([], max_length=60)
    after: V.Str(1, 40) | None = None


class StepPatch(BaseModel):
    title: V.Str(1, 120) | None = None
    due: dict | None = None
    sites: list[str] | None = None
    office: bool | None = None
    gate: bool | None = None
    survey: bool | None = None


class ItemIn(BaseModel):
    type: str = Field("msg", pattern="^(msg|text|scale|yesno)$")
    text: V.Str(1, 3500)
    sites: list[V.Str(1, 24)] | None = Field(None, max_length=20)
    office: bool | None = None
    image: V.Str(0, 64) | None = None
    index: int | None = Field(None, ge=0, le=500)


class ItemPatch(BaseModel):
    text: str | None = Field(None, max_length=3500)
    sites: list[V.Str(1, 24)] | None = Field(None, max_length=20)
    office: bool | None = None
    image: V.Str(0, 64) | None = None


class MoveIn(BaseModel):
    direction: str = Field(pattern="^(up|down)$")


class PlaceIn(BaseModel):
    before: V.Str(1, 40) | None = None
    after: V.Str(1, 40) | None = None


@app.post("/api/scenario/steps", dependencies=ADMIN)
async def scenario_step_create(body: StepIn):
    sid = _edit(scenario_edit.create_step, body.title, body.due, body.sites, body.office, body.gate, body.survey, body.items, body.after)
    return {"id": sid}


@app.put("/api/scenario/steps/{sid}", dependencies=ADMIN)
async def scenario_step_update(sid: str, body: StepPatch):
    _edit(scenario_edit.update_step, sid, **{k: getattr(body, k) for k in body.model_fields_set})
    return {"ok": True}


@app.delete("/api/scenario/steps/{sid}", dependencies=ADMIN)
async def scenario_step_delete(sid: str):
    _edit(scenario_edit.delete_step, sid)
    return {"deleted": sid}


@app.post("/api/scenario/steps/{sid}/move", dependencies=ADMIN)
async def scenario_step_move(sid: str, body: MoveIn):
    _edit(scenario_edit.move_step, sid, body.direction)
    return {"ok": True}


@app.post("/api/scenario/steps/{sid}/place", dependencies=ADMIN)
async def scenario_step_place(sid: str, body: PlaceIn):
    _edit(scenario_edit.place_step, sid, body.before, body.after)
    return {"ok": True}


@app.post("/api/scenario/steps/{sid}/items", dependencies=ADMIN)
async def scenario_item_add(sid: str, body: ItemIn):
    if body.image and not assets.path(body.image):
        raise HTTPException(400, "unknown image")
    return {"idx": _edit(scenario_edit.add_item, sid, body.type, body.text, body.sites, body.office, body.image, body.index)}


@app.put("/api/scenario/steps/{sid}/items/{idx}", dependencies=ADMIN)
async def scenario_item_update(sid: str, idx: int, body: ItemPatch):
    f = {k: getattr(body, k) for k in body.model_fields_set}
    if f.get("image") and not assets.path(f["image"]):
        raise HTTPException(400, "unknown image")
    _edit(scenario_edit.update_item, sid, idx, **f)
    return {"ok": True}


@app.delete("/api/scenario/steps/{sid}/items/{idx}", dependencies=ADMIN)
async def scenario_item_delete(sid: str, idx: int):
    _edit(scenario_edit.delete_item, sid, idx)
    return {"deleted": idx}


@app.post("/api/scenario/steps/{sid}/items/{idx}/move", dependencies=ADMIN)
async def scenario_item_move(sid: str, idx: int, body: MoveIn):
    _edit(scenario_edit.move_item, sid, idx, body.direction)
    return {"ok": True}


@app.post("/api/scenario/restore", dependencies=ADMIN)
async def scenario_restore():
    scenario_edit.restore_defaults()
    return {"ok": True}


class SiteIn(BaseModel):
    code: V.Str(2, 24)
    title: V.Str(1, 60)
    office: bool = True
    copy_from: V.Str(0, 24) | None = None


class SitePatch(BaseModel):
    title: V.Str(1, 60) | None = None
    office: bool | None = None


@app.get("/api/sites", dependencies=ADMIN)
async def sites_list():
    return sc.site_list()


@app.post("/api/sites", dependencies=ADMIN)
async def site_create(body: SiteIn):
    _edit(scenario_edit.add_site, body.code.strip(), body.title, body.office, body.copy_from)
    return sc.site_list()


@app.put("/api/sites/{code}", dependencies=ADMIN)
async def site_update(code: str, body: SitePatch):
    _edit(scenario_edit.update_site, code, body.title, body.office)
    return sc.site_list()


@app.delete("/api/sites/{code}", dependencies=ADMIN)
async def site_delete(code: str):
    _edit(scenario_edit.delete_site, code, sum(e["site"] == code for e in db.list_employees()))
    return sc.site_list()


@app.put("/api/scenario/text", dependencies=ADMIN)
async def scenario_text(body: TextIn):
    step = sc.STEP_BY_ID.get(body.step_id)
    if not step or not 0 <= body.idx < len(step["items"]) or "text" not in step["items"][body.idx]:
        raise HTTPException(404, "item not found")
    _edit(scenario_edit.update_item, body.step_id, body.idx, text=body.text)  # пусто → вернётся исходный текст
    return {"ok": True}


class ImageIn(BaseModel):
    step_id: V.Str(1, 40)
    idx: int = Field(ge=0, le=500)
    image: V.Str(0, 64)  # имя файла из библиотеки, "none" — без картинки, "" — сбросить


@app.get("/api/scenario/images", dependencies=ADMIN)
async def scenario_images():
    return assets.library()


@app.post("/api/scenario/images", dependencies=ADMIN)
async def upload_scenario_image(request: Request, name: str = Query("", max_length=80)):
    try:
        return {"file": assets.save_upload(await request.body(), name)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/scenario/images/{fname}", dependencies=ADMIN)
async def delete_scenario_image(fname: str):
    if not assets.remove(fname):
        raise HTTPException(404, "no such image")
    return {"deleted": fname}


@app.put("/api/scenario/image", dependencies=ADMIN)
async def scenario_image(body: ImageIn):
    step = sc.STEP_BY_ID.get(body.step_id)
    if not step or not 0 <= body.idx < len(step["items"]) or step["items"][body.idx]["t"] != "msg":
        raise HTTPException(404, "message not found")
    img = "" if body.image == "none" else body.image  # "none" и "" — убрать вложение
    if img and not assets.path(img):
        raise HTTPException(400, "unknown image")
    _edit(scenario_edit.update_item, body.step_id, body.idx, image=img)
    return {"ok": True, "image": assets.for_item(body.step_id, body.idx, sc.STEP_BY_ID[body.step_id]["items"][body.idx])}


@app.get("/api/surveys", dependencies=ADMIN)
async def surveys():
    return db.survey_stats()


