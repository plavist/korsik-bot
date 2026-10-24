"""Тестовый драйвер диалога: в продакшене веб-чата нет (только MAX), а логику бота удобно гонять через HTTP-подобные вызовы.
Маршруты регистрируются только в тестах; PROD_PATHS — набор путей приложения без них."""
import secrets

from fastapi import BackgroundTasks
from pydantic import BaseModel

from app import bot, db
from app.main import app

PROD_PATHS = {r.path for r in app.routes}


class ChatIn(BaseModel):
    user_id: str
    message: str = ""
    payload: str | None = None


class UserIn(BaseModel):
    name: str


@app.get("/api/users")
async def web_users():
    return [{"user_id": u["user_id"], "name": u["name"] or u["user_id"]} for u in db.list_users("web")]


@app.post("/api/users")
async def create_web_user(body: UserIn):
    user_id = f"web-{secrets.token_hex(4)}"
    db.upsert_user(user_id, body.name, "web")
    return {"user_id": user_id, "name": body.name}


@app.post("/api/chat")
async def chat(body: ChatIn, background: BackgroundTasks):
    return await bot.handle(body.user_id, "web", text=body.message, payload=body.payload, background=background)


@app.get("/api/history/{user_id}")
async def history(user_id: str):
    return db.history(user_id, 50)
