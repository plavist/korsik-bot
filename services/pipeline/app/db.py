import json
import time
from contextlib import contextmanager

from . import pg


@contextmanager
def conn():
    """Соединение с PostgreSQL (схема pipeline); commit при успехе, rollback при ошибке."""
    with pg.conn() as c:
        yield c


def _one(c, sql: str, args=()):
    """Первое значение первой строки (COUNT и т. п.)."""
    return next(iter(c.execute(sql, args).fetchone().values()))


def get_setting(key: str, default: str | None = None) -> str | None:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def add_message(user_id, channel, role, text, sources=None, grounded=None, kind=None, buttons=None, image=None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO messages(user_id,channel,role,text,sources,grounded,created_at,kind,buttons,image) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (user_id, channel, role, text, json.dumps(sources or [], ensure_ascii=False),
             None if grounded is None else int(grounded), time.time(), kind,
             json.dumps(buttons, ensure_ascii=False) if buttons else None, image),
        )
        return cur.fetchone()["id"]


def set_sentiment(msg_id: int, sentiment: str, score: float, topics: list[str], red_flag: bool) -> None:
    with conn() as c:
        c.execute(
            "UPDATE messages SET sentiment=%s, sentiment_score=%s, topics=%s, red_flag=%s WHERE id=%s",
            (sentiment, score, json.dumps(topics, ensure_ascii=False), int(red_flag), msg_id),
        )


def history(user_id: str, limit: int = 10) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT id, role, text, channel, kind, buttons, image, created_at FROM messages WHERE user_id=%s "
                         "ORDER BY id DESC LIMIT %s", (user_id, limit)).fetchall()
    out = [dict(r) for r in reversed(rows)]
    for m in out:
        m["buttons"] = json.loads(m["buttons"]) if m["buttons"] else None
    return out


def get_escalation(esc_id: int) -> dict | None:
    with conn() as c:
        r = c.execute(ESC_SELECT + " WHERE e.id=%s", (esc_id,)).fetchone()
        return _esc(r) if r else None


MERGE_WINDOW = 24 * 3600                                      # повторы за сутки склеиваются в одно обращение
MERGE_KINDS = ("unanswered", "sentiment", "agent", "intent")   # автоматические обращения; явные просьбы и опросы — отдельные


def add_escalation(user_id, channel, reason, kind, merge: bool | None = None) -> int:
    """Создаёт обращение для HR. Автоматические обращения (нет ответа в базе, красный флаг, решение модели, интент): если у
    сотрудника уже есть незакрытое обращение того же вида за последние сутки, новая причина дописывается в него
    (счётчик повторов растёт) — HR не получает поток дублей. Явные просьбы и сигналы из опросов создаются отдельно."""
    now = time.time()
    with conn() as c:
        if kind in MERGE_KINDS if merge is None else merge:
            row = c.execute("SELECT id, reason FROM escalations WHERE user_id=%s AND kind=%s AND status<>'resolved' AND created_at>=%s "
                            "ORDER BY id DESC LIMIT 1 FOR UPDATE", (user_id, kind, now - MERGE_WINDOW)).fetchone()
            if row:
                merged = row["reason"] if reason in (row["reason"] or "") else f"{row['reason']}\n{reason}"[:3000]
                c.execute("UPDATE escalations SET reason=%s, repeats=repeats+1, updated_at=%s WHERE id=%s", (merged, now, row["id"]))
                return row["id"]
        return c.execute("INSERT INTO escalations(user_id,channel,reason,kind,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                         (user_id, channel, reason, kind, now, now)).fetchone()["id"]


def upsert_user(user_id: str, name: str | None, channel: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO users(user_id,name,channel,created_at) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(user_id) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), users.name)",
            (user_id, (name or "").strip()[:80], channel, time.time()),
        )


def get_user(user_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=%s", (user_id,)).fetchone()
        return dict(r) if r else None


def list_users(channel: str | None = None) -> list[dict]:
    with conn() as c:
        q, a = "SELECT * FROM users", ()
        if channel:
            q, a = q + " WHERE channel=%s", (channel,)
        return [dict(r) for r in c.execute(q + " ORDER BY created_at", a)]


ESC_SELECT = (
    "SELECT e.*, (SELECT emp.name FROM employees emp WHERE emp.user_id=e.user_id ORDER BY emp.id DESC LIMIT 1) AS name, "
    "(SELECT json_build_object('t', m.text, 's', m.sentiment, 'r', m.red_flag)::text FROM messages m WHERE m.user_id=e.user_id AND m.role='user' "
    "AND NOT starts_with(m.text, '[кнопка]') AND m.created_at <= e.created_at + 1 ORDER BY m.id DESC LIMIT 1) AS last_msg, "
    "(SELECT emp.site FROM employees emp WHERE emp.user_id=e.user_id ORDER BY emp.id DESC LIMIT 1) AS site "
    "FROM escalations e")


def _esc(r) -> dict:
    """Строка обращения для таблицы: сообщение, к которому оно относится, и тональность (positive | neutral | danger)."""
    d = dict(r)
    m = json.loads(d.pop("last_msg") or "null") or {}
    d["message"] = m.get("t") or d.get("reason", "")
    d["tone"] = ("danger" if d["kind"] == "sentiment" or m.get("r") or m.get("s") == "negative"
                 else "positive" if m.get("s") == "positive" else "neutral")
    return d


def list_escalations(status: str | None = None) -> list[dict]:
    with conn() as c:
        q, a = ESC_SELECT, ()
        if status:
            q, a = q + " WHERE e.status=%s", (status,)
        return [_esc(r) for r in c.execute(q + " ORDER BY e.id DESC LIMIT 200", a)]


def set_escalation_status(esc_id: int, status: str) -> bool:
    with conn() as c:
        return c.execute("UPDATE escalations SET status=%s WHERE id=%s", (status, esc_id)).rowcount > 0


def scope(days: int | None = None, site: str | None = None, manager: str | None = None, alias: str = "") -> tuple[str, list]:
    """SQL-условие для messages по периоду, площадке и руководителю (пусто = без фильтра)."""
    p = f"{alias}." if alias else ""
    where, args = [], []
    if days:
        where.append(f"{p}created_at >= %s")
        args.append(time.time() - days * 86400)
    if site or manager:
        sub, sa = "SELECT user_id FROM employees WHERE user_id IS NOT NULL", []
        if site:
            sub, sa = sub + " AND site=%s", sa + [site]
        if manager:
            sub, sa = sub + " AND manager=%s", sa + [manager]
        where.append(f"{p}user_id IN ({sub})")
        args += sa
    return (" AND " + " AND ".join(where)) if where else "", args


def msg_filter(topic: str | None = None, sentiment: str | None = None) -> tuple[str, list]:
    """Условие для сообщений сотрудников по теме и тональности. «Нейтрально» включает позитивные: отдельного «позитива» в аналитике нет."""
    sql, args = "", []
    if topic:
        sql, args = sql + " AND topics LIKE %s", args + [f'%"{topic}"%']
    if sentiment == "negative":
        sql += " AND sentiment='negative'"
    elif sentiment in ("neutral", "positive"):
        sql += " AND sentiment IN ('neutral','positive')"
    return sql, args


# В общую статистику входят только сообщения сотрудников с оценкой тональности: нажатия кнопок, «/»-команды и короткие
# ответы опросов («9», «да») оценки не получают и вопросами к боту не считаются.
SCORED = "role='user' AND sentiment IS NOT NULL"


HR_QUESTION_KINDS = ("unanswered", "agent", "intent", "forward", "contact", "command")   # обращения, где вопрос ушёл к человеку


def handled_without_hr(answered: int, esc_kinds: dict) -> dict:
    """Сколько вопросов бот закрыл сам (ответ по базе знаний) и сколько передал HR; доля — «закрыто без HR»."""
    handed = sum(esc_kinds.get(k, 0) for k in HR_QUESTION_KINDS)
    total = answered + handed
    return {"answered": answered, "handed_over": handed, "share": round(answered / total, 3) if total else None}


def analytics(days: int | None = None, site: str | None = None, manager: str | None = None,
              topic: str | None = None, sentiment: str | None = None) -> dict:
    flt, fa = scope(days, site, manager)
    mf, ma = msg_filter(topic, sentiment)
    uflt, ufa = flt + mf, fa + ma                      # условие для сообщений сотрудников (с темой и тональностью)
    with conn() as c:
        one = lambda q, a=(): _one(c, q, a)  # noqa: E731
        user_msgs = one(f"SELECT COUNT(*) FROM messages WHERE {SCORED}" + uflt, ufa)
        bot_msgs = one("SELECT COUNT(*) FROM messages WHERE role='assistant'" + flt, fa)
        grounded = one("SELECT COUNT(*) FROM messages WHERE role='assistant' AND grounded=1" + flt, fa)
        sentiments: dict[str, int] = {}
        for r in c.execute("SELECT sentiment, COUNT(*) AS n FROM messages WHERE role='user' AND sentiment IS NOT NULL" + uflt + " GROUP BY sentiment", ufa):
            key = "neutral" if r["sentiment"] == "positive" else r["sentiment"]      # позитив учитывается как нейтральное
            sentiments[key] = sentiments.get(key, 0) + r["n"]
        topic_counts: dict[str, dict] = {}
        for r in c.execute("SELECT topics, sentiment FROM messages WHERE role='user' AND topics IS NOT NULL" + uflt, ufa):
            for t in json.loads(r["topics"]):
                d = topic_counts.setdefault(t, {"n": 0, "neg": 0})
                d["n"] += 1
                d["neg"] += r["sentiment"] == "negative"
        daily = [dict(r) for r in c.execute(
            "SELECT to_char(to_timestamp(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day, COUNT(*) AS n, "
            "SUM((sentiment='negative')::int) AS neg, SUM((sentiment IN ('neutral','positive'))::int) AS neu, "
            f"ROUND(AVG(sentiment_score)::numeric, 2) AS avg FROM messages WHERE {SCORED}" + uflt +
            " GROUP BY day ORDER BY day DESC LIMIT 60", ufa)]
        for d in daily:
            d["avg"] = None if d["avg"] is None else float(d["avg"])
        flags = [dict(r) for r in c.execute(
            "SELECT id,user_id,(SELECT emp.name FROM employees emp WHERE emp.user_id=messages.user_id ORDER BY emp.id DESC LIMIT 1) AS name,text,sentiment,topics,created_at "
            "FROM messages WHERE red_flag=1" + uflt + " ORDER BY id DESC LIMIT 20", ufa)]
        for f in flags:
            f["topics"] = json.loads(f["topics"] or "[]")
        eflt, ea = scope(days, site, manager, "e")
        esc = {r["status"]: r["n"] for r in c.execute(
            "SELECT e.status, COUNT(*) AS n FROM escalations e WHERE 1=1" + eflt + " GROUP BY e.status", ea)}
        esc_kinds = {r["kind"]: r["n"] for r in c.execute(
            "SELECT e.kind, COUNT(*) AS n FROM escalations e WHERE 1=1" + eflt + " GROUP BY e.kind", ea)}
        users = one(f"SELECT COUNT(DISTINCT user_id) FROM messages WHERE {SCORED}" + uflt, ufa)
    return {
        "users": users,
        "user_messages": user_msgs,
        "bot_messages": bot_msgs,
        "grounded_answers": grounded,
        "ungrounded_answers": bot_msgs - grounded,
        "grounded_rate": round(grounded / bot_msgs, 3) if bot_msgs else None,
        "sentiments": sentiments,
        "top_topics": [(t, d["n"]) for t, d in sorted(topic_counts.items(), key=lambda kv: -kv[1]["n"])[:10]],
        "topics_detail": [{"topic": t, **d} for t, d in sorted(topic_counts.items(), key=lambda kv: -kv[1]["n"])[:10]],
        "daily": list(reversed(daily)),
        "red_flags": flags,
        "escalations": esc,
        "escalation_kinds": esc_kinds,
        "handled": handled_without_hr(grounded, esc_kinds),
        "filters": {"days": days, "site": site, "manager": manager, "topic": topic, "sentiment": sentiment},
    }


def messages_drill(days=None, site=None, manager=None, topic=None, sentiment=None, day=None, red_flag=False,
                   user_id=None, limit: int = 100) -> list[dict]:
    """Сообщения сотрудников для детализации диаграмм (клик по теме, тональности, дню, красным флагам)."""
    flt, fa = scope(days, site, manager)
    q = ("SELECT id,user_id,(SELECT emp.name FROM employees emp WHERE emp.user_id=messages.user_id ORDER BY emp.id DESC LIMIT 1) AS name,text,sentiment,sentiment_score,topics,"
         f"red_flag,created_at FROM messages WHERE {SCORED}" + flt)
    if sentiment:
        sf, sa = msg_filter(None, sentiment)
        q, fa = q + sf, fa + sa
    if red_flag:
        q += " AND red_flag=1"
    if day:
        q, fa = q + " AND to_char(to_timestamp(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD')=%s", fa + [day]
    if user_id:
        q, fa = q + " AND user_id=%s", fa + [user_id]
    if topic:
        q, fa = q + " AND topics LIKE %s", fa + [f'%"{topic}"%']
    with conn() as c:
        rows = [dict(r) for r in c.execute(q + " ORDER BY id DESC LIMIT %s", fa + [limit])]
    for r in rows:
        r["topics"] = json.loads(r["topics"] or "[]")
        r["red_flag"] = bool(r["red_flag"])
    return rows


def user_history(user_id: str, limit: int = 200) -> list[dict]:
    return history(user_id, limit)


# ---------- сотрудники и онбординг ----------
def create_employee(**f) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO employees(code,name,site,is_office,hire_date,probation_end,recruiter_ok,created_at,manager,manager_user) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (f["code"], f["name"], f["site"], int(f.get("is_office", True)), f["hire_date"], f.get("probation_end") or None,
             int(f.get("recruiter_ok", False)), time.time(), f.get("manager") or None, f.get("manager_user") or None),
        ).fetchone()["id"]


def _emp(r) -> dict:
    d = dict(r)
    d["state"] = json.loads(d["state"] or "{}")
    for k in ("is_office", "recruiter_ok", "active"):
        d[k] = bool(d[k])
    return d


def get_employee(emp_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM employees WHERE id=%s", (emp_id,)).fetchone()
        return _emp(r) if r else None


def employee_by_code(code: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM employees WHERE lower(code)=lower(%s)", (code.strip(),)).fetchone()
        return _emp(r) if r else None


def employee_by_user(user_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM employees WHERE user_id=%s AND active=1 ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        return _emp(r) if r else None


def list_employees(only_bound_active: bool = False) -> list[dict]:
    with conn() as c:
        q = "SELECT * FROM employees" + (" WHERE active=1 AND user_id IS NOT NULL" if only_bound_active else "") + " ORDER BY id DESC"
        return [_emp(r) for r in c.execute(q)]


EMPLOYEE_COLUMNS = {"code", "name", "site", "is_office", "hire_date", "probation_end", "recruiter_ok", "form", "user_id", "channel",
                    "state", "active", "manager", "manager_user"}


def update_employee(emp_id: int, **fields) -> None:
    unknown = set(fields) - EMPLOYEE_COLUMNS
    if unknown:  # имена колонок попадают в SQL — только из белого списка
        raise ValueError(f"неизвестные поля сотрудника: {sorted(unknown)}")
    if "state" in fields:
        fields["state"] = json.dumps(fields["state"], ensure_ascii=False)
    for k in ("is_office", "recruiter_ok", "active"):
        if k in fields:
            fields[k] = int(fields[k])
    cols = ", ".join(f"{k}=%s" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE employees SET {cols} WHERE id=%s", (*fields.values(), emp_id))


def delete_employee(emp_id: int) -> None:
    with conn() as c:
        for t, col in (("sent_steps", "employee_id"), ("survey_answers", "employee_id"), ("employees", "id")):
            c.execute(f"DELETE FROM {t} WHERE {col}=%s", (emp_id,))


def step_status(emp_id: int) -> dict[str, dict]:
    with conn() as c:
        return {r["step_id"]: {"status": r["status"], "at": r["at"]} for r in
                c.execute("SELECT * FROM sent_steps WHERE employee_id=%s", (emp_id,))}


def step_statuses(emp_ids: list[int] | None = None) -> dict[int, dict[str, dict]]:
    """Статусы этапов сразу по многим сотрудникам одним запросом: {employee_id: {step_id: {status, at}}}."""
    with conn() as c:
        rows = (c.execute("SELECT * FROM sent_steps WHERE employee_id = ANY(%s)", (emp_ids,)) if emp_ids is not None
                else c.execute("SELECT * FROM sent_steps"))
        out: dict[int, dict[str, dict]] = {}
        for r in rows:
            out.setdefault(r["employee_id"], {})[r["step_id"]] = {"status": r["status"], "at": r["at"]}
    return out


RISK_ESCALATION_KINDS = ("survey", "sentiment", "contact", "agent", "intent", "forward")   # обращения, повышающие риск ухода
MSGS_PER_USER = 500


def risk_inputs(user_ids: list[str], emp_ids: list[int], days: int | None = None) -> dict:
    """Всё, что нужно индексу риска, — четырьмя запросами на всех сотрудников сразу (а не по запросу на каждого)."""
    since = time.time() - days * 86400 if days else 0
    with conn() as c:
        msgs: dict[str, list[dict]] = {}
        for r in c.execute(
                "SELECT user_id, sentiment, sentiment_score, topics, red_flag FROM ("
                "  SELECT *, row_number() OVER (PARTITION BY user_id ORDER BY id DESC) AS rn FROM messages "
                "  WHERE role='user' AND sentiment IS NOT NULL AND user_id = ANY(%s) AND created_at >= %s) t "
                "WHERE rn <= %s ORDER BY user_id, rn", (user_ids, since, MSGS_PER_USER)):
            msgs.setdefault(r["user_id"], []).append({**r, "topics": json.loads(r["topics"] or "[]"), "red_flag": bool(r["red_flag"])})
        scale: dict[int, list[str]] = {}
        for r in c.execute("SELECT employee_id, value FROM survey_answers WHERE qtype='scale' AND employee_id = ANY(%s) ORDER BY id", (emp_ids,)):
            scale.setdefault(r["employee_id"], []).append(r["value"])
        open_esc = {r["user_id"]: r["n"] for r in c.execute(
            "SELECT user_id, COUNT(*) AS n FROM escalations WHERE status<>'resolved' AND kind = ANY(%s) AND user_id = ANY(%s) GROUP BY user_id",
            (list(RISK_ESCALATION_KINDS), user_ids))}
        last = {r["user_id"]: r["ts"] for r in c.execute(
            "SELECT user_id, MAX(created_at) AS ts FROM messages WHERE user_id = ANY(%s) GROUP BY user_id", (user_ids,))}
    return {"msgs": msgs, "scale": scale, "open_esc": open_esc, "last": last}


def mark_step(emp_id: int, step_id: str, status: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO sent_steps(employee_id,step_id,status,at) VALUES(%s,%s,%s,%s) "
                  "ON CONFLICT(employee_id,step_id) DO UPDATE SET status=excluded.status, at=excluded.at",
                  (emp_id, step_id, status, time.time()))


def reset_employee_progress(emp_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM sent_steps WHERE employee_id=%s", (emp_id,))
        c.execute("DELETE FROM survey_answers WHERE employee_id=%s", (emp_id,))
        c.execute("UPDATE employees SET state='{}', form=NULL WHERE id=%s", (emp_id,))


def add_answer(emp_id: int, survey: str, qkey: str, qtype: str, value, label=None, comment=None) -> None:
    with conn() as c:
        c.execute("INSERT INTO survey_answers(employee_id,survey,qkey,qtype,value,label,comment,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                  (emp_id, survey, qkey, qtype, str(value), label, comment, time.time()))


def attach_comment(emp_id: int, survey: str, qkey: str, comment: str) -> None:
    with conn() as c:
        c.execute("UPDATE survey_answers SET comment=%s WHERE id=(SELECT MAX(id) FROM survey_answers "
                  "WHERE employee_id=%s AND survey=%s AND qkey=%s)", (comment, emp_id, survey, qkey))


def list_answers(emp_id: int | None = None) -> list[dict]:
    with conn() as c:
        q, a = "SELECT * FROM survey_answers", ()
        if emp_id is not None:
            q, a = q + " WHERE employee_id=%s", (emp_id,)
        return [dict(r) for r in c.execute(q + " ORDER BY id", a)]


def survey_stats() -> list[dict]:
    """Сводка по вопросам: среднее для шкал, распределение для остальных, число низких оценок."""
    out: dict[tuple, dict] = {}
    for a in list_answers():
        d = out.setdefault((a["survey"], a["qkey"]), {"survey": a["survey"], "qkey": a["qkey"], "qtype": a["qtype"],
                                                      "n": 0, "dist": {}, "sum": 0.0, "low": 0, "comments": 0})
        d["n"] += 1
        d["dist"][a["label"] or a["value"]] = d["dist"].get(a["label"] or a["value"], 0) + 1
        if a["comment"]:
            d["comments"] += 1
        if a["qtype"] == "scale" and a["value"].isdigit():
            d["sum"] += int(a["value"])
            d["low"] += int(a["value"]) < 8
    for d in out.values():
        d["avg"] = round(d.pop("sum") / d["n"], 2) if d["qtype"] == "scale" else None
    return list(out.values())


def get_rewrite(key: str) -> str | None:
    with conn() as c:
        r = c.execute("SELECT text FROM rewrites WHERE key=%s", (key,)).fetchone()
        return r["text"] if r else None


def set_rewrite(key: str, text: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO rewrites(key,text) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET text=excluded.text", (key, text))


# ---------- команды бота (редактируются в панели управления) ----------
def list_commands() -> list[dict]:
    with conn() as c:
        return [dict(r) | {"builtin": bool(r["builtin"])} for r in c.execute("SELECT * FROM bot_commands ORDER BY builtin DESC, sort, name")]


def get_command(name: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM bot_commands WHERE name=%s", (name,)).fetchone()
        return (dict(r) | {"builtin": bool(r["builtin"])}) if r else None


def save_command(name: str, **f) -> None:
    cur = get_command(name) or {"description": "", "kind": "static", "reply": "", "query": "", "fallback": "", "builtin": False, "sort": 0}
    row = {**cur, **f}
    with conn() as c:
        c.execute("INSERT INTO bot_commands(name,description,kind,reply,query,fallback,builtin,sort,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                  "ON CONFLICT(name) DO UPDATE SET description=excluded.description, kind=excluded.kind, reply=excluded.reply, query=excluded.query, "
                  "fallback=excluded.fallback, builtin=excluded.builtin, sort=excluded.sort, updated_at=excluded.updated_at",
                  (name, row["description"], row["kind"], row["reply"], row["query"], row["fallback"], int(row["builtin"]), row["sort"], time.time()))


def delete_command(name: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM bot_commands WHERE name=%s", (name,))


# ---------- уведомления руководителям подразделений ----------
def add_notice(emp_id: int, kind: str, text: str, manager: str | None) -> bool:
    """True — уведомление новое (повтор того же вида для сотрудника не создаётся)."""
    with conn() as c:
        return c.execute("INSERT INTO manager_notices(employee_id,kind,text,manager,created_at) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                         (emp_id, kind, text, manager, time.time())).rowcount > 0


def mark_notice_delivered(emp_id: int, kind: str) -> None:
    with conn() as c:
        c.execute("UPDATE manager_notices SET delivered=1 WHERE employee_id=%s AND kind=%s", (emp_id, kind))


def list_notices(manager: str | None = None, limit: int = 100) -> list[dict]:
    with conn() as c:
        q = ("SELECT n.*, e.name AS employee FROM manager_notices n JOIN employees e ON e.id=n.employee_id"
             + (" WHERE n.manager=%s" if manager else "") + " ORDER BY n.id DESC LIMIT %s")
        return [dict(r) for r in c.execute(q, ([manager] if manager else []) + [limit])]


# ---------- входящие события МАКС: «в работе» до успешной обработки, чтобы не терять их при сбое или перезапуске ----------
def inbox_add(user_id: str | None, payload: str) -> int:
    now = time.time()
    with conn() as c:
        return c.execute("INSERT INTO inbox(user_id,payload,created_at,updated_at) VALUES(%s,%s,%s,%s) RETURNING id",
                         (user_id, payload, now, now)).fetchone()["id"]


def inbox_done(event_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM inbox WHERE id=%s", (event_id,))


def inbox_fail(event_id: int, error: str) -> None:
    with conn() as c:
        c.execute("UPDATE inbox SET status='failed', error=%s, updated_at=%s WHERE id=%s", (error[:500], time.time(), event_id))


def inbox_pending() -> list[dict]:
    """События, которые начали обрабатываться, но не закончили (процесс остановился)."""
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM inbox WHERE status='processing' ORDER BY id")]


def inbox_retry(event_id: int) -> int:
    """Отмечает новую попытку, возвращает её номер."""
    with conn() as c:
        return c.execute("UPDATE inbox SET attempts=attempts+1, updated_at=%s WHERE id=%s RETURNING attempts",
                         (time.time(), event_id)).fetchone()["attempts"]


def inbox_counts() -> dict:
    with conn() as c:
        return {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM inbox GROUP BY status")}


def list_failed_events(limit: int = 50) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM inbox WHERE status='failed' ORDER BY id DESC LIMIT %s", (limit,))]
