"""Аналитика для HR и руководителей: этапы онбординга, воронка, индекс риска ухода, недельный дайджест настроений,
напоминания руководителям подразделений о личных встречах."""
import copy
import functools
import inspect
import json
import os
import re
import time
from datetime import date, datetime, timedelta

from . import db, engine, llm
from . import scenario as sc

PHASES = ["До выхода", "1-я неделя", "2–4 неделя", "2-й месяц", "3-й месяц", "Испытательный срок пройден"]
LEVELS = (("high", 50), ("medium", 25), ("low", 0))


_cache: dict = {}


def cached(fn):
    """Короткий кэш тяжёлых расчётов дашборда (INSIGHTS_CACHE_TTL секунд, по умолчанию 15; 0 — выключен).
    Страница аналитики запрашивает несколько блоков сразу, и каждый пересчитывал бы одно и то же."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ttl = float(os.getenv("INSIGHTS_CACHE_TTL", "15"))
        if ttl <= 0:
            return fn(*args, **kwargs)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()                                   # risk() и risk(None, None) — один и тот же запрос
        key = (fn.__name__, tuple(bound.arguments.items()))
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return copy.deepcopy(hit[1])
        value = fn(*args, **kwargs)
        _cache[key] = (time.time(), value)
        return copy.deepcopy(value)
    wrapper.__wrapped__ = fn
    return wrapper


def cache_clear() -> None:
    _cache.clear()


def _employees(site=None, manager=None) -> list[dict]:
    return [e for e in db.list_employees() if e["active"] and (not site or e["site"] == site)
            and (not manager or e.get("manager") == manager)]


def phase(emp: dict, today: date | None = None, status: dict | None = None) -> str:
    """status — статусы этапов сотрудника, если они уже загружены (иначе читаются из базы)."""
    today = today or engine.now_local().date()
    d = sc.dates(emp)
    status = db.step_status(emp["id"]) if status is None else status
    if today > d["end"] or status.get("end_probation", {}).get("status") == "done":
        return PHASES[5]
    days = (today - d["hire"]).days
    if days < 0:
        return PHASES[0]
    return PHASES[1] if days <= 7 else PHASES[2] if days <= 30 else PHASES[3] if days <= 60 else PHASES[4]


@cached
def stages(site=None, manager=None) -> list[dict]:
    """Сколько новичков на каком этапе онбординга (+ кто именно — для детализации по клику)."""
    emps = _employees(site, manager)
    statuses = db.step_statuses([e["id"] for e in emps])
    phases = {e["id"]: phase(e, status=statuses.get(e["id"], {})) for e in emps}
    return [{"stage": p, "employees": [{"id": e["id"], "name": e["name"], "site": e["site"], "manager": e.get("manager")}
                                       for e in emps if phases[e["id"]] == p]} for p in PHASES]


@cached
def funnel(site=None, manager=None) -> list[dict]:
    """Прохождение этапов сценария: сколько привязанных новичков получили/прошли каждый этап."""
    emps = [e for e in _employees(site, manager) if e["user_id"]]
    statuses = db.step_statuses([e["id"] for e in emps])
    out = []
    for step in sc.STEPS:
        sent = done = 0
        for e in emps:
            if not sc.applies(step, e):
                continue
            st = statuses.get(e["id"], {}).get(step["id"], {}).get("status")
            sent += st in ("sent", "done")
            done += st == "done"
        applicable = sum(1 for e in emps if sc.applies(step, e))
        out.append({"id": step["id"], "title": step["title"], "applicable": applicable, "sent": sent, "done": done,
                    "survey": bool(step["survey"])})
    return out


def survey_comments(emp_id: int | None = None, days: int | None = None) -> list[dict]:
    """Развёрнутые ответы в опросах: открытые вопросы и пояснения к низким оценкам."""
    since = time.time() - days * 86400 if days else 0
    return [{"employee_id": a["employee_id"], "survey": a["survey"], "qkey": a["qkey"], "text": (a["comment"] or a["value"]).strip(),
             "created_at": a["created_at"], "scale": a["value"] if a["qtype"] == "scale" else None}
            for a in db.list_answers(emp_id)
            if a["created_at"] >= since and ((a["qtype"] == "text" and a["value"].strip()) or (a["comment"] or "").strip())]


def _risk_score(e: dict, rows: list[dict], scale: list[str], open_esc: int, silent_days: float | None, ph: str) -> tuple[int, list[str]]:
    """Баллы и причины индекса риска одного сотрудника (веса — в README и документации, раздел «Индекс риска ухода»)."""
    factors, score = [], 0

    def add(points: int, text: str) -> None:
        nonlocal score
        score += points
        factors.append(text)

    flags = sum(r["red_flag"] for r in rows)
    if flags:
        add(min(50, 25 * flags), f"красные флаги в сообщениях: {flags}")
    neg = sum(r["sentiment"] == "negative" for r in rows)
    if len(rows) >= 2 and neg / len(rows) >= 0.4:
        add(15, f"негативных сообщений: {neg} из {len(rows)}")
    avg = sum(r["sentiment_score"] or 0 for r in rows) / len(rows) if rows else 0
    if rows and avg < -0.3:
        add(10, f"средняя тональность {avg:.2f}")
    if any("увольнение" in r["topics"] for r in rows):
        add(30, "упоминает увольнение")
    low = [v for v in scale if v.isdigit() and int(v) < 8]
    if low:
        add(min(24, 8 * len(low)) + (10 if any(int(v) <= 4 for v in low) else 0), "низкие оценки в опросах: " + ", ".join(low[:5]))
    if open_esc:
        add(min(20, 10 * open_esc), f"открытых обращений: {open_esc}")
    if silent_days is not None and silent_days > 5 and ph not in (PHASES[0], PHASES[5]):
        add(10, "молчит больше 5 дней")
    return min(100, score), factors


@cached
def risk(site=None, manager=None, days: int | None = None) -> list[dict]:
    """Прогноз риска ухода по ранним сигналам (0–100): красные флаги, негатив, низкие оценки, открытые обращения, тишина.
    Все данные загружаются пакетно (см. db.risk_inputs), поэтому время растёт с числом сотрудников почти незаметно."""
    emps = [e for e in _employees(site, manager) if e["user_id"]]
    data = db.risk_inputs([e["user_id"] for e in emps], [e["id"] for e in emps], days)
    statuses = db.step_statuses([e["id"] for e in emps])
    now, out = time.time(), []
    for e in emps:
        ph = phase(e, status=statuses.get(e["id"], {}))
        last = data["last"].get(e["user_id"])
        score, factors = _risk_score(e, data["msgs"].get(e["user_id"], []), data["scale"].get(e["id"], []),
                                     data["open_esc"].get(e["user_id"], 0), (now - last) / 86400 if last else None, ph)
        level = next(name for name, lo in LEVELS if score >= lo)
        out.append({"id": e["id"], "name": e["name"], "site": e["site"], "manager": e.get("manager"), "user_id": e["user_id"],
                    "phase": ph, "score": score, "level": level, "factors": factors})
    return sorted(out, key=lambda r: -r["score"])


# ---------- светофор «Статус адаптации» по каждому новичку ----------
LIGHTS = ("red", "yellow", "green", "grey")
STALE_SURVEY = 24 * 3600  # опрос без ответа дольше суток — повод посмотреть


def _light(e: dict, tl: list[dict], r: dict | None, now: datetime) -> tuple[str, list[str]]:
    """Цвет светофора и причины для одного новичка (правила — BR-16 в документации)."""
    by = {s: [t for t in tl if t["status"] == s] for s in ("error", "skipped", "due", "waiting_recruiter")}
    if not e["user_id"]:                      # ещё не подключён: серый, а в день выхода и накануне — жёлтый
        if sc.dates(e)["hire"] <= now.date() + timedelta(days=1):
            return "yellow", [f"не привязан к боту, хотя выход {e['hire_date']}"]
        return "grey", ["ещё не подключён к боту"]
    red, yellow = [], []
    if r and r["level"] == "high":
        red.append(f"высокий риск ухода ({r['score']}/100): " + "; ".join(r["factors"][:2]))
    elif r and r["level"] == "medium":
        yellow.append(f"средний риск ухода ({r['score']}/100): " + "; ".join(r["factors"][:2]))
    if by["error"]:
        red.append("не доставлено: " + ", ".join(t["title"] for t in by["error"][:2]))
    if by["skipped"]:
        yellow.append(f"пропущено этапов: {len(by['skipped'])}")
    late = by["due"] + [t for t in by["waiting_recruiter"] if t["due"] and t["due"] <= now.isoformat()]
    if late:
        yellow.append("этап ждёт отправки: " + late[0]["title"] + ("" if len(late) == 1 else f" (+{len(late) - 1})"))
    p = e["state"].get("pending")
    if p and time.time() - p["asked_at"] > STALE_SURVEY:
        yellow.append("вопрос без ответа больше суток")
    return ("red" if red else "yellow" if yellow else "green"), (red + yellow) or ["всё идёт по плану"]


@cached
def status(site=None, manager=None, now: datetime | None = None) -> list[dict]:
    """Светофор по каждому активному новичку: красный — нужна реакция, жёлтый — внимание, зелёный — всё идёт по плану,
    серый — ещё не подключён к боту. Учитывает индекс риска, сбои и пропуски этапов, неотвеченные опросы."""
    now = now or engine.now_local()
    levels = {r["id"]: r for r in risk(site, manager)}
    emps = _employees(site, manager)
    statuses = db.step_statuses([e["id"] for e in emps])
    out = []
    for e in emps:
        st = statuses.get(e["id"], {})
        tl = engine.timeline(e, now, st)
        r = levels.get(e["id"])
        light, reasons = _light(e, tl, r, now)
        passed = [t for t in tl if t["status"] in ("done", "sent")]
        upcoming = [t for t in tl if t["status"] in ("scheduled", "due", "waiting_recruiter")]
        out.append({"id": e["id"], "name": e["name"], "site": e["site"], "manager": e.get("manager"), "user_id": e["user_id"],
                    "phase": phase(e, now.date(), st), "light": light, "reasons": reasons, "score": r["score"] if r else None,
                    "progress": {"done": sum(t["status"] in ("done", "n/a") for t in tl), "total": len(tl)},
                    "last_step": passed[-1]["title"] if passed else None,
                    "next_step": {"title": upcoming[0]["title"], "due": upcoming[0]["due"]} if upcoming else None})
    return sorted(out, key=lambda x: (LIGHTS.index(x["light"]), -(x["score"] or 0), x["name"]))


# ---------- недельный дайджест «Настроения в коллективе новичков» ----------
CLUSTER_PROMPT = """Ты помогаешь HR-службе. Ниже развёрнутые ответы новых сотрудников в пульс-опросах и сообщения боту (JSON: id, text).
Сгруппируй их по смыслу в 2–6 тем-кластеров (например «проблемы с доступами», «конфликт с наставником», «непонятные задачи»)
и оцени риск каждой темы для HR. Верни ТОЛЬКО JSON:
{"clusters":[{"title":"краткое название","risk":"low|medium|high","summary":"1 предложение","ids":[id,...]}]}
Каждый id — ровно в одном кластере. high — токсичность, конфликт, намерение уйти, нарушение прав, выгорание."""


def parse_clusters(raw: str, known: set[int]) -> list[dict]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    out = []
    for c in (json.loads(m.group(0)).get("clusters", []) if m else []):
        ids = [i for i in c.get("ids", []) if i in known]
        if ids and c.get("title"):
            out.append({"title": str(c["title"])[:80], "risk": c.get("risk") if c.get("risk") in ("low", "medium", "high") else "low",
                        "summary": str(c.get("summary", ""))[:200], "ids": ids})
    return out


async def cluster(items: list[dict]) -> tuple[list[dict], str]:
    """→ (кластеры, способ): нейросеть, а без неё — группировка по темам сообщений."""
    if not items:
        return [], "none"
    try:
        msg = await llm.chat([{"role": "system", "content": CLUSTER_PROMPT},
                              {"role": "user", "content": json.dumps([{"id": i["id"], "text": i["text"][:400]} for i in items], ensure_ascii=False)}],
                             temperature=0, json_mode=True, max_tokens=3000)
        clusters = parse_clusters(msg.get("content") or "", {i["id"] for i in items})
        if clusters:
            return clusters, "llm"
    except Exception:  # noqa: BLE001 — дайджест не должен падать из-за нейросети
        pass
    by_topic: dict[str, list[int]] = {}
    for i in items:
        by_topic.setdefault((i.get("topics") or ["другое"])[0], []).append(i["id"])
    return [{"title": t, "risk": "high" if t in ("конфликт или токсичность", "увольнение") else "low", "summary": "", "ids": ids}
            for t, ids in sorted(by_topic.items(), key=lambda kv: -len(kv[1]))], "topics"


async def digest(days: int = 7, site=None, manager=None) -> dict:
    a = db.analytics(days, site, manager)
    names = {e["id"]: e["name"] for e in db.list_employees()}
    items, n = [], 0
    for c in survey_comments(days=days):
        n += 1
        items.append({"id": n, "text": c["text"], "who": names.get(c["employee_id"], "?"), "source": "опрос", "topics": []})
    for r in db.messages_drill(days, site, manager, sentiment="negative", limit=50):
        n += 1
        items.append({"id": n, "text": r["text"], "who": r["name"] or r["user_id"], "source": "бот", "topics": r["topics"]})
    clusters, how = await cluster(items)
    by_id = {i["id"]: i for i in items}
    for c in clusters:
        c["quotes"] = [{"who": by_id[i]["who"], "text": by_id[i]["text"][:240], "source": by_id[i]["source"]} for i in c["ids"][:4]]
        c["n"] = len(c["ids"])
        del c["ids"]
    clusters.sort(key=lambda c: ({"high": 0, "medium": 1, "low": 2}[c["risk"]], -c["n"]))
    rk = [r for r in risk(site, manager, days) if r["level"] != "low"][:5]
    return {"days": days, "generated_at": time.time(), "method": how, "texts": len(items),
            "messages": a["user_messages"], "sentiments": a["sentiments"], "red_flags": len(a["red_flags"]),
            "top_topics": a["top_topics"][:5], "clusters": clusters, "at_risk": rk,
            "stages": [{"stage": s["stage"], "n": len(s["employees"])} for s in stages(site, manager)]}


def digest_text(d: dict) -> str:
    sent = d["sentiments"]
    lines = [f"📊 Настроения в коллективе новичков — за {d['days']} дн.",
             f"Сообщений: {d['messages']} · нейтрально: {sent.get('neutral', 0)} · негативно: {sent.get('negative', 0)} · красных флагов: {d['red_flags']}"]
    if d["clusters"]:
        lines.append("\nТемы в ответах:")
        icon = {"high": "🔴", "medium": "🟠", "low": "🟢"}
        for c in d["clusters"][:6]:
            lines.append(f"{icon[c['risk']]} {c['title']} — {c['n']}" + (f": {c['summary']}" if c["summary"] else ""))
    if d["at_risk"]:
        lines.append("\nЗона риска:")
        lines += [f"• {r['name']} ({r['score']}/100): {'; '.join(r['factors'][:2])}" for r in d["at_risk"]]
    return "\n".join(lines)[:3900]


# ---------- напоминания руководителям ----------
def _milestones(emp: dict) -> list[tuple[str, date, str]]:
    d = sc.dates(emp)
    return [("mgr_day3", d["hire"] + timedelta(days=3), "3-й день работы: проведите короткую встречу — как первые дни, всё ли понятно по задачам"),
            ("mgr_week2", d["hire"] + timedelta(days=14), "2 недели в компании: обсудите задачи, обратную связь и вопросы адаптации"),
            ("mgr_month1", d["hire"] + timedelta(days=30), "Месяц в компании: проведите личную встречу, подведите промежуточные итоги"),
            ("mgr_month2", d["hire"] + timedelta(days=60), "2 месяца в компании: обсудите результаты и планы на оставшийся испытательный срок"),
            ("mgr_end", d["end"] - timedelta(days=7), "Неделя до конца испытательного срока: подготовьтесь к итоговой встрече и решению")]


async def manager_nudges(send, now: datetime | None = None) -> int:
    """Создаёт напоминания руководителям (раз на событие) и отправляет их push-ом, если известен чат руководителя.
    send(user_id, text) — доставка. → число созданных напоминаний."""
    today = (now or engine.now_local()).date()
    created = 0
    levels = {r["id"]: r for r in risk()}
    for e in _employees():
        if not e["user_id"]:
            continue
        due = [(k, t) for k, when, t in _milestones(e) if when <= today <= when + timedelta(days=3)]
        r = levels.get(e["id"])
        if r and r["level"] == "high":
            due.append(("mgr_risk", f"Зона риска: у сотрудника высокий риск ({r['score']}/100) — " + "; ".join(r["factors"][:3]) +
                        ". Проведите личную встречу как можно скорее"))
        for kind, text in due:
            full = f"👋 {e['name']} ({sc.SITE_TITLES.get(e['site'], e['site'])}): {text}"
            if db.add_notice(e["id"], kind, full, e.get("manager")):
                created += 1
                if e.get("manager_user"):
                    try:
                        await send(e["manager_user"], full)
                        db.mark_notice_delivered(e["id"], kind)
                    except Exception:  # noqa: BLE001 — не доставили: останется в списке уведомлений
                        pass
    return created
