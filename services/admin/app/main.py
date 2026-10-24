"""HR-панель управления: Basic-auth с ролями (RBAC), UI и прокси к ingest/pipeline."""
import hashlib
import os
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator

from . import rbac, users
from . import validation as V

STATIC = Path(__file__).parent / "static"
security = HTTPBasic()


def auth(request: Request, cred: HTTPBasicCredentials = Depends(security)) -> dict:
    user = users.authenticate(cred.username, cred.password)
    if not user:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})
    request.state.user = user
    return user


app = FastAPI(title="admin", dependencies=[Depends(auth)])
app.add_exception_handler(RequestValidationError, V.validation_handler)

# Заголовки безопасности. Интерфейс — один HTML с инлайн-скриптом, поэтому script-src разрешает 'unsafe-inline';
# остальное закрыто: чужие домены, встраивание во фрейм, <base>, отправка форм наружу.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


def need(user: dict, perm: str | None) -> None:
    if not rbac.can(user["role"], perm):
        raise HTTPException(403, f"Недостаточно прав: роль «{rbac.ROLES[user['role']]['title']}» не может менять раздел "
                                 f"«{rbac.PERM_TITLES.get(perm, perm)}»")


def upstream(name: str) -> str:
    return {"ingest": os.getenv("INGEST_URL", "http://ingest:8000"),
            "pipeline": os.getenv("PIPELINE_URL", "http://pipeline:8000")}[name]


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


# ---------- RBAC: кто я, пользователи, журнал ----------
class UserIn(BaseModel):
    login: str = Field(max_length=64)
    name: str = Field("", max_length=100)
    role: str = Field(max_length=16)
    password: str = Field(max_length=256)
    manager: str = Field("", max_length=100)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return V.person_name(v, "имя")

    @field_validator("manager")
    @classmethod
    def _mgr(cls, v):
        return V.person_name(v, "ФИО руководителя", full=True)

    @model_validator(mode="after")
    def _pw(self):
        V.password(self.password, self.login)
        return self


class UserPatch(BaseModel):
    name: str | None = Field(None, max_length=100)
    role: str | None = Field(None, max_length=16)
    active: bool | None = None
    password: str | None = Field(None, max_length=256)
    manager: str | None = Field(None, max_length=100)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return V.person_name(v, "имя") if v is not None else v

    @field_validator("manager")
    @classmethod
    def _mgr(cls, v):
        return V.person_name(v, "ФИО руководителя", full=True) if v is not None else v

    @field_validator("password")
    @classmethod
    def _pw(cls, v):
        return V.password(v) if v is not None else v


class PasswordIn(BaseModel):
    old: str = Field(max_length=256)
    new: str = Field(max_length=256)

    @field_validator("new")
    @classmethod
    def _new(cls, v):
        return V.password(v)


@app.get("/api/rbac/me")
async def me(user: dict = Depends(auth)):
    r = rbac.ROLES[user["role"]]
    return {"login": user["login"], "name": user["name"], "role": user["role"], "role_title": r["title"],
            "perms": sorted(r["perms"]), "root": user.get("root", False), "manager": user.get("manager", "")}


@app.get("/api/rbac/roles")
async def roles():
    return [{"role": k, "title": v["title"], "about": v["about"], "perms": sorted(v["perms"])} for k, v in rbac.ROLES.items()]


@app.get("/api/rbac/users")
async def list_users(user: dict = Depends(auth)):
    need(user, "users")
    return users.list_users()


@app.post("/api/rbac/users")
async def create_user(body: UserIn, user: dict = Depends(auth)):
    need(user, "users")
    try:
        users.create(body.login, body.name, body.role, body.password, body.manager)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    users.log(user["login"], f"создал пользователя {body.login} ({body.role})")
    return {"ok": True}


@app.put("/api/rbac/users/{login}")
async def update_user(login: str, body: UserPatch, user: dict = Depends(auth)):
    need(user, "users")
    if login == user["login"] and (body.active is False or (body.role and body.role != user["role"])):
        raise HTTPException(400, "нельзя отключить или понизить самого себя")
    try:
        found = users.update(login, body.name, body.role, body.active, body.password, body.manager)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if not found:
        raise HTTPException(404, "пользователь не найден")
    users.log(user["login"], f"изменил пользователя {login}" + (" (сменил пароль)" if body.password else ""))
    return {"ok": True}


@app.delete("/api/rbac/users/{login}")
async def delete_user(login: str, user: dict = Depends(auth)):
    need(user, "users")
    if login == user["login"]:
        raise HTTPException(400, "нельзя удалить самого себя")
    if not users.delete(login):
        raise HTTPException(404, "пользователь не найден")
    users.log(user["login"], f"удалил пользователя {login}")
    return {"ok": True}


@app.post("/api/rbac/password")
async def change_password(body: PasswordIn, user: dict = Depends(auth)):
    if user.get("root"):
        raise HTTPException(400, "пароль главного администратора меняется в настройках установки")
    if not users.authenticate(user["login"], body.old):
        raise HTTPException(400, "неверный текущий пароль")
    users.update(user["login"], None, None, None, body.new)
    users.log(user["login"], "сменил свой пароль")
    return {"ok": True}


@app.get("/api/rbac/audit")
async def audit_log(page: int = Query(1, ge=1, le=100000), size: int = Query(20, ge=5, le=100), user: dict = Depends(auth)):
    need(user, "users")
    total = users.audit_count()
    pages = max(1, -(-total // size))
    page = min(page, pages)
    return {"items": users.audit(size, (page - 1) * size), "total": total, "page": page, "pages": pages, "size": size}


# ---------- прокси ----------
@app.api_route("/api/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(service: str, path: str, request: Request, user: dict = Depends(auth)):
    if service not in ("ingest", "pipeline"):
        raise HTTPException(404, "unknown service")
    params = dict(request.query_params)
    if user["role"] == "manager":  # руководитель: только чтение и только свои подчинённые
        if request.method != "GET" or not rbac.manager_can_read(service, path) or not user.get("manager"):
            raise HTTPException(403, "Недостаточно прав: роль «Руководитель подразделения» видит только статус своих сотрудников")
        params["manager"] = user["manager"]
    need(user, rbac.required(request.method, service, path))
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("content-type",)}
    if service == "pipeline":  # токен внутренних маршрутов pipeline (производный от ADMIN_PASSWORD)
        headers["x-internal-token"] = hashlib.sha256(("internal:" + os.getenv("ADMIN_PASSWORD", "admin")).encode()).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.request(request.method, f"{upstream(service)}/{path}", params=params,
                                content=await request.body(), headers=headers)
    except httpx.TransportError as e:
        raise HTTPException(502, f"{service} недоступен: {e}") from None
    if request.method != "GET" and r.status_code < 400 and rbac.required(request.method, service, path):
        users.log(user["login"], f"{request.method} {service}/{path}")
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
