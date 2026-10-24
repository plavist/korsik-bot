"""Аналитика для HR и руководителей: этапы онбординга, воронка, индекс риска ухода, недельный дайджест настроений,
напоминания руководителям подразделений о личных встречах."""
import json
import re
import time
from datetime import date, datetime, timedelta

from . import db, engine, llm
from . import scenario as sc

PHASES = ["До выхода", "1-я неделя", "2–4 неделя", "2-й месяц", "3-й месяц", "Испытательный срок пройден"]
LEVELS = (("high", 50), ("medium", 25), ("low", 0))


def _employees(site=None, manager=None) -> list[dict]:
    return [e for e in db.list_employees() if e["active"] and (not site or e["site"] == site)
            and (not manager or e.get("manager") == manager)]


def phase(emp: dict, today: date | None = None) -> str:
    today = today or engine.now_local().date()
    d = sc.dates(emp)
    if today > d["end"] or db.step_status(emp["id"]).get("end_probation", {}).get("status") == "done":
        return PHASES[5]
    days = (today - d["hire"]).days
    if days < 0:
        return PHASES[0]
    return PHASES[1] if days <= 7 else PHASES[2] if days <= 30 else PHASES[3] if days <= 60 else PHASES[4]


def stages(site=None, manager=None) -> list[dict]:
    """Сколько новичков на каком этапе онбординга (+ кто именно — для детализации по клику)."""
    emps = _employees(site, manager)
    return [{"stage": p, "employees": [{"id": e["id"], "name": e["name"], "site": e["site"], "manager": e.get("manager")}
                                       for e in emps if phase(e) == p]} for p in PHASES]


def funnel(site=None, manager=None) -> list[dict]:
    """Прохождение этапов сценария: сколько привязанных новичков получили/прошли каждый этап."""
    emps = [e for e in _employees(site, manager) if e["user_id"]]
    out = []
    for step in sc.STEPS:
        sent = done = 0
        for e in emps:
            if not sc.applies(step, e):
                continue
            st = db.step_status(e["id"]).get(step["id"], {}).get("status")
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


def risk(site=None, manager=None, days: int | None = None) -> list[dict]:
    """Прогноз риска ухода по ранним сигналам (0–100): красные флаги, негатив, низкие оценки, открытые обращения, тишина."""
    out = []
    for e in _employees(site, manager):
        if not e["user_id"]:
            continue
        rows = db.messages_drill(days=days, user_id=e["user_id"], limit=500)
        factors, score = [], 0
        flags = sum(r["red_flag"] for r in rows)
        if flags:
            score += min(50, 25 * flags)
            factors.append(f"красные флаги в сообщениях: {flags}")
        scored = [r for r in rows if r["sentiment"]]
        neg = sum(r["sentiment"] == "negative" for r in scored)
        if len(scored) >= 2 and neg / len(scored) >= 0.4:
            score += 15
            factors.append(f"негативных сообщений: {neg} из {len(scored)}")
        avg = sum(r["sentiment_score"] or 0 for r in scored) / len(scored) if scored else 0
        if scored and avg < -0.3:
            score += 10
            factors.append(f"средняя тональность {avg:.2f}")
        if any("увольнение" in r["topics"] for r in rows):
            score += 30
            factors.append("упоминает увольнение")
        low = [a for a in db.list_answers(e["id"]) if a["qtype"] == "scale" and a["value"].isdigit() and int(a["value"]) < 8]
        if low:
            score += min(24, 8 * len(low)) + (10 if any(int(a["value"]) <= 4 for a in low) else 0)
            factors.append("низкие оценки в опросах: " + ", ".join(a["value"] for a in low[:5]))
        open_esc = [x for x in db.list_escalations() if x["user_id"] == e["user_id"] and x["status"] != "resolved"
                    and x["kind"] in ("survey", "sentiment", "contact", "agent", "intent", "forward")]
        if open_esc:
            score += min(20, 10 * len(open_esc))
            factors.append(f"открытых обращений: {len(open_esc)}")
        last = max((h["created_at"] for h in db.history(e["user_id"], 20)), default=None)
        if last and time.time() - last > 5 * 86400 and phase(e) not in (PHASES[0], PHASES[5]):
            score += 10
            factors.append("молчит больше 5 дней")
        score = min(100, score)
        level = next(name for name, lo in LEVELS if score >= lo)
        out.append({"id": e["id"], "name": e["name"], "site": e["site"], "manager": e.get("manager"), "user_id": e["user_id"],
                    "phase": phase(e), "score": score, "level": level, "factors": factors})
    return sorted(out, key=lambda r: -r["score"])


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
