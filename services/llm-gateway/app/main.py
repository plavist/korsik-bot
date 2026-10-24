"""LLM gateway: единая точка для chat/embeddings с переключаемым провайдером.

Провайдеры задаются переменными окружения, сервисы-клиенты знают только
алиасы моделей ("chat", "embed") и URL этого сервиса.
"""
import asyncio
import logging
import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import pg

log = logging.getLogger("gateway")

app = FastAPI(title="llm-gateway")

RETRY_STATUSES = {429, 500, 502, 503, 504}


def providers() -> dict[str, dict]:
    return {
        "openrouter": {
            "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            "api_key": os.getenv("OPENROUTER_API_KEY", ""),
            "headers": {"X-Title": "Polyplastic Korsik"},
        },
        # любой OpenAI-совместимый endpoint (vLLM, Ollama, LM Studio, YandexGPT-proxy...)
        "custom": {
            "base_url": os.getenv("CUSTOM_BASE_URL", ""),
            "api_key": os.getenv("CUSTOM_API_KEY", ""),
            "headers": {},
        },
    }


def route(kind: str, requested: str | None) -> tuple[dict, str]:
    """kind: 'chat' | 'embed'. Возвращает (provider_cfg, model_id)."""
    if kind == "chat":
        provider = os.getenv("LLM_PROVIDER", "openrouter")
        default_model = os.getenv("LLM_MODEL", "qwen/qwen3.8-27b")
    else:
        provider = os.getenv("EMBED_PROVIDER", "openrouter")
        default_model = os.getenv("EMBED_MODEL", "qwen/qwen3-embedding-8b")
    cfg = providers().get(provider)
    if cfg is None:
        raise HTTPException(500, f"unknown provider '{provider}'")
    if not cfg["base_url"]:
        raise HTTPException(500, f"provider '{provider}' has no base_url")
    model = default_model if requested in (None, "", "chat", "embed", "default") else requested
    return cfg, model


async def call_upstream(cfg: dict, path: str, payload: dict) -> httpx.Response:
    headers = {"Content-Type": "application/json", **cfg["headers"]}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    attempts = int(os.getenv("UPSTREAM_RETRIES", "3"))
    timeout = float(os.getenv("UPSTREAM_TIMEOUT", "120"))
    resp: httpx.Response | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for i in range(attempts):
            try:
                resp = await client.post(cfg["base_url"].rstrip("/") + path, json=payload, headers=headers)
            except httpx.TransportError as e:
                if i == attempts - 1:
                    raise HTTPException(502, f"upstream unreachable: {e}") from None
            else:
                if resp.status_code not in RETRY_STATUSES:
                    return resp
            await asyncio.sleep(float(os.getenv("RETRY_BACKOFF", "1")) * (i + 1))
    assert resp is not None
    return resp


def record_usage(kind: str, model: str, resp: httpx.Response) -> None:
    """Учёт расхода токенов по дням (для оценки стоимости). Сбой учёта не должен ломать запрос."""
    if resp.status_code != 200:
        return
    try:
        usage = resp.json().get("usage") or {}
        p, c = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        with pg.conn() as db:
            db.execute(
                "INSERT INTO llm_usage(day, kind, model, requests, prompt_tokens, completion_tokens) VALUES (%s,%s,%s,1,%s,%s) "
                "ON CONFLICT (day, kind, model) DO UPDATE SET requests = llm_usage.requests + 1, "
                "prompt_tokens = llm_usage.prompt_tokens + excluded.prompt_tokens, "
                "completion_tokens = llm_usage.completion_tokens + excluded.completion_tokens",
                (time.strftime("%Y-%m-%d", time.gmtime()), kind, model, p, c))
    except Exception:  # noqa: BLE001
        log.exception("usage accounting failed")


def price(kind: str, prompt: int, completion: int) -> float | None:
    """Стоимость по ценам из окружения (за 1 млн токенов): LLM_PRICE_IN/LLM_PRICE_OUT, EMBED_PRICE; нет цен — None."""
    if kind == "chat":
        pin, pout = os.getenv("LLM_PRICE_IN"), os.getenv("LLM_PRICE_OUT")
        return None if pin is None or pout is None else (prompt * float(pin) + completion * float(pout)) / 1e6
    pe = os.getenv("EMBED_PRICE")
    return None if pe is None else (prompt + completion) * float(pe) / 1e6


def relay(resp: httpx.Response) -> JSONResponse:
    try:
        body = resp.json()
    except ValueError:
        body = {"error": {"message": resp.text[:500]}}
    return JSONResponse(body, status_code=resp.status_code)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "llm_provider": os.getenv("LLM_PROVIDER", "openrouter"),
        "llm_model": os.getenv("LLM_MODEL", "qwen/qwen3.8-27b"),
        "embed_provider": os.getenv("EMBED_PROVIDER", "openrouter"),
        "embed_model": os.getenv("EMBED_MODEL", "qwen/qwen3-embedding-8b"),
    }


@app.post("/v1/chat/completions")
async def chat(request: Request):
    payload = await request.json()
    cfg, model = route("chat", payload.get("model"))
    payload["model"] = model
    payload.pop("stream", None)
    resp = await call_upstream(cfg, "/chat/completions", payload)
    record_usage("chat", model, resp)
    return relay(resp)


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    payload = await request.json()
    if not payload.get("input"):
        raise HTTPException(400, "input is required")
    cfg, model = route("embed", payload.get("model"))
    payload["model"] = model
    resp = await call_upstream(cfg, "/embeddings", payload)
    record_usage("embed", model, resp)
    return relay(resp)


@app.get("/usage")
async def usage(days: int = 30):
    """Расход токенов за период: по дням и видам, итоги и стоимость (если заданы цены LLM_PRICE_IN/OUT, EMBED_PRICE)."""
    days = max(1, min(days, 3650))
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    with pg.conn() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM llm_usage WHERE day >= %s ORDER BY day, kind, model", (since,))]
    totals: dict[str, dict] = {}
    for r in rows:
        t = totals.setdefault(r["kind"], {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
        t["requests"] += r["requests"]
        t["prompt_tokens"] += r["prompt_tokens"]
        t["completion_tokens"] += r["completion_tokens"]
    for kind, t in totals.items():
        t["cost"] = price(kind, t["prompt_tokens"], t["completion_tokens"])
    costs = [t["cost"] for t in totals.values()]
    return {"days": days, "rows": rows, "totals": totals,
            "cost": None if not costs or any(c is None for c in costs) else round(sum(costs), 4),
            "currency": os.getenv("PRICE_CURRENCY", "USD")}
