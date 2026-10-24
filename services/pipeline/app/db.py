import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, channel TEXT, role TEXT, text TEXT,
  sources TEXT, grounded INTEGER, sentiment TEXT, sentiment_score REAL, topics TEXT,
  red_flag INTEGER DEFAULT 0, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_msg_user ON messages(user_id, id);
CREATE TABLE IF NOT EXISTS escalations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, channel TEXT, reason TEXT, kind TEXT,
  status TEXT DEFAULT 'open', created_at REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, name TEXT, site TEXT, is_office INTEGER DEFAULT 1,
  hire_date TEXT, probation_end TEXT, recruiter_ok INTEGER DEFAULT 0, form TEXT, user_id TEXT, channel TEXT,
  state TEXT DEFAULT '{}', active INTEGER DEFAULT 1, created_at REAL);
CREATE TABLE IF NOT EXISTS manager_notices (
  id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER, kind TEXT, text TEXT, manager TEXT, delivered INTEGER DEFAULT 0,
  created_at REAL, UNIQUE(employee_id, kind));
CREATE TABLE IF NOT EXISTS sent_steps (
  employee_id INTEGER, step_id TEXT, status TEXT, at REAL, PRIMARY KEY (employee_id, step_id));
CREATE TABLE IF NOT EXISTS survey_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER, survey TEXT, qkey TEXT, qtype TEXT,
  value TEXT, label TEXT, comment TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS bot_commands (name TEXT PRIMARY KEY, description TEXT, kind TEXT, reply TEXT, query TEXT,
  fallback TEXT, builtin INTEGER DEFAULT 0, sort INTEGER DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS rewrites (key TEXT PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, name TEXT, channel TEXT, created_at REAL);
"""


@contextmanager
def conn():
    Path(config.data_dir()).mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(Path(config.data_dir()) / "pipeline.db"))
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
    for col, ddl in (("kind", "TEXT"), ("buttons", "TEXT"), ("image", "TEXT")):  # миграция: proactive-сообщения и кнопки
        if col not in cols:
            c.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
    ecols = {r["name"] for r in c.execute("PRAGMA table_info(employees)")}
    for col in ("manager", "manager_user"):  # руководитель подразделения и его чат для push-уведомлений
        if col not in ecols:
            c.execute(f"ALTER TABLE employees ADD COLUMN {col} TEXT")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def add_message(user_id, channel, role, text, sources=None, grounded=None, kind=None, buttons=None, image=None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO messages(user_id,channel,role,text,sources,grounded,created_at,kind,buttons,image) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, channel, role, text, json.dumps(sources or [], ensure_ascii=False),
             None if grounded is None else int(grounded), time.time(), kind,
             json.dumps(buttons, ensure_ascii=False) if buttons else None, image),
        )
        return cur.lastrowid


def set_sentiment(msg_id: int, sentiment: str, score: float, topics: list[str], red_flag: bool) -> None:
    with conn() as c:
        c.execute(
            "UPDATE messages SET sentiment=?, sentiment_score=?, topics=?, red_flag=? WHERE id=?",
            (sentiment, score, json.dumps(topics, ensure_ascii=False), int(red_flag), msg_id),
        )


def history(user_id: str, limit: int = 10) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT id, role, text, channel, kind, buttons, image, created_at FROM messages WHERE user_id=? "
                         "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    out = [dict(r) for r in reversed(rows)]
    for m in out:
        m["buttons"] = json.loads(m["buttons"]) if m["buttons"] else None
    return out


def get_escalation(esc_id: int) -> dict | None:
    with conn() as c:
        r = c.execute(ESC_SELECT + " WHERE e.id=?", (esc_id,)).fetchone()
        return _esc(r) if r else None


def add_escalation(user_id, channel, reason, kind) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO escalations(user_id,channel,reason,kind,created_at) VALUES(?,?,?,?,?)",
            (user_id, channel, reason, kind, time.time()),
        ).lastrowid


def upsert_user(user_id: str, name: str | None, channel: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO users(user_id,name,channel,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), users.name)",
            (user_id, (name or "").strip()[:80], channel, time.time()),
        )


def get_user(user_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(r) if r else None


def list_users(channel: str | None = None) -> list[dict]:
    with conn() as c:
        q, a = "SELECT * FROM users", ()
        if channel:
            q, a = q + " WHERE channel=?", (channel,)
        return [dict(r) for r in c.execute(q + " ORDER BY created_at", a)]


ESC_SELECT = (
    "SELECT e.*, (SELECT emp.name FROM employees emp WHERE emp.user_id=e.user_id ORDER BY emp.id DESC LIMIT 1) AS name, "
    "(SELECT json_object('t', m.text, 's', m.sentiment, 'r', m.red_flag) FROM messages m WHERE m.user_id=e.user_id AND m.role='user' "
    "AND m.text NOT LIKE '[кнопка]%' AND m.created_at <= e.created_at + 1 ORDER BY m.id DESC LIMIT 1) AS last_msg, "
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
            q, a = q + " WHERE e.status=?", (status,)
        return [_esc(r) for r in c.execute(q + " ORDER BY e.id DESC LIMIT 200", a)]


def set_escalation_status(esc_id: int, status: str) -> bool:
    with conn() as c:
        return c.execute("UPDATE escalations SET status=? WHERE id=?", (status, esc_id)).rowcount > 0


def scope(days: int | None = None, site: str | None = None, manager: str | None = None, alias: str = "") -> tuple[str, list]:
    """SQL-условие для messages по периоду, площадке и руководителю (пусто = без фильтра)."""
    p = f"{alias}." if alias else ""
    where, args = [], []
    if days:
        where.append(f"{p}created_at >= ?")
        args.append(time.time() - days * 86400)
    if site or manager:
        sub, sa = "SELECT user_id FROM employees WHERE user_id IS NOT NULL", []
        if site:
            sub, sa = sub + " AND site=?", sa + [site]
        if manager:
            sub, sa = sub + " AND manager=?", sa + [manager]
        where.append(f"{p}user_id IN ({sub})")
        args += sa
    return (" AND " + " AND ".join(where)) if where else "", args


def msg_filter(topic: str | None = None, sentiment: str | None = None) -> tuple[str, list]:
    """Условие для сообщений сотрудников по теме и тональности. «Нейтрально» включает позитивные: отдельного «позитива» в аналитике нет."""
    sql, args = "", []
    if topic:
        sql, args = sql + " AND topics LIKE ?", args + [f'%"{topic}"%']
    if sentiment == "negative":
        sql += " AND sentiment='negative'"
    elif sentiment in ("neutral", "positive"):
        sql += " AND sentiment IN ('neutral','positive')"
    return sql, args


def analytics(days: int | None = None, site: str | None = None, manager: str | None = None,
              topic: str | None = None, sentiment: str | None = None) -> dict:
    flt, fa = scope(days, site, manager)
    mf, ma = msg_filter(topic, sentiment)
    uflt, ufa = flt + mf, fa + ma                      # условие для сообщений сотрудников (с темой и тональностью)
    with conn() as c:
        one = lambda q, a=(): c.execute(q, a).fetchone()[0]  # noqa: E731
        user_msgs = one("SELECT COUNT(*) FROM messages WHERE role='user' AND text NOT LIKE '[кнопка]%'" + uflt, ufa)  # нажатия кнопок — не вопросы
        bot_msgs = one("SELECT COUNT(*) FROM messages WHERE role='assistant'" + flt, fa)
        grounded = one("SELECT COUNT(*) FROM messages WHERE role='assistant' AND grounded=1" + flt, fa)
        sentiments: dict[str, int] = {}
        for r in c.execute("SELECT sentiment, COUNT(*) n FROM messages WHERE role='user' AND sentiment IS NOT NULL" + uflt + " GROUP BY sentiment", ufa):
            key = "neutral" if r["sentiment"] == "positive" else r["sentiment"]      # позитив учитывается как нейтральное
            sentiments[key] = sentiments.get(key, 0) + r["n"]
        topic_counts: dict[str, dict] = {}
        for r in c.execute("SELECT topics, sentiment FROM messages WHERE role='user' AND topics IS NOT NULL" + uflt, ufa):
            for t in json.loads(r["topics"]):
                d = topic_counts.setdefault(t, {"n": 0, "neg": 0})
                d["n"] += 1
                d["neg"] += r["sentiment"] == "negative"
        daily = [dict(r) for r in c.execute(
            "SELECT date(created_at,'unixepoch') day, COUNT(*) n, "
            "SUM(sentiment='negative') neg, SUM(sentiment IN ('neutral','positive')) neu, "
            "ROUND(AVG(sentiment_score),2) avg FROM messages WHERE role='user' AND text NOT LIKE '[кнопка]%'" + uflt +
            " GROUP BY day ORDER BY day DESC LIMIT 60", ufa)]
        flags = [dict(r) for r in c.execute(
            "SELECT id,user_id,(SELECT emp.name FROM employees emp WHERE emp.user_id=messages.user_id ORDER BY emp.id DESC LIMIT 1) AS name,text,sentiment,topics,created_at "
            "FROM messages WHERE red_flag=1" + uflt + " ORDER BY id DESC LIMIT 20", ufa)]
        for f in flags:
            f["topics"] = json.loads(f["topics"] or "[]")
        eflt, ea = scope(days, site, manager, "e")
        esc = {r["status"]: r["n"] for r in c.execute(
            "SELECT e.status, COUNT(*) n FROM escalations e WHERE 1=1" + eflt + " GROUP BY e.status", ea)}
        esc_kinds = {r["kind"]: r["n"] for r in c.execute(
            "SELECT e.kind, COUNT(*) n FROM escalations e WHERE 1=1" + eflt + " GROUP BY e.kind", ea)}
        users = one("SELECT COUNT(DISTINCT user_id) FROM messages WHERE role='user'" + uflt, ufa)
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
        "filters": {"days": days, "site": site, "manager": manager, "topic": topic, "sentiment": sentiment},
    }


def messages_drill(days=None, site=None, manager=None, topic=None, sentiment=None, day=None, red_flag=False,
                   user_id=None, limit: int = 100) -> list[dict]:
    """Сообщения сотрудников для детализации диаграмм (клик по теме, тональности, дню, красным флагам)."""
    flt, fa = scope(days, site, manager)
    q = ("SELECT id,user_id,(SELECT emp.name FROM employees emp WHERE emp.user_id=messages.user_id ORDER BY emp.id DESC LIMIT 1) AS name,text,sentiment,sentiment_score,topics,"
         "red_flag,created_at FROM messages WHERE role='user' AND text NOT LIKE '[кнопка]%'" + flt)
    if sentiment:
        sf, sa = msg_filter(None, sentiment)
        q, fa = q + sf, fa + sa
    if red_flag:
        q += " AND red_flag=1"
    if day:
        q, fa = q + " AND date(created_at,'unixepoch')=?", fa + [day]
    if user_id:
        q, fa = q + " AND user_id=?", fa + [user_id]
    if topic:
        q, fa = q + " AND topics LIKE ?", fa + [f'%"{topic}"%']
    with conn() as c:
        rows = [dict(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", fa + [limit])]
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
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f["code"], f["name"], f["site"], int(f.get("is_office", True)), f["hire_date"], f.get("probation_end") or None,
             int(f.get("recruiter_ok", False)), time.time(), f.get("manager") or None, f.get("manager_user") or None),
        ).lastrowid


def _emp(r) -> dict:
    d = dict(r)
    d["state"] = json.loads(d["state"] or "{}")
    for k in ("is_office", "recruiter_ok", "active"):
        d[k] = bool(d[k])
    return d


def get_employee(emp_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
        return _emp(r) if r else None


def employee_by_code(code: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM employees WHERE code=? COLLATE NOCASE", (code.strip(),)).fetchone()
        return _emp(r) if r else None


def employee_by_user(user_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM employees WHERE user_id=? AND active=1 ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        return _emp(r) if r else None


def list_employees(only_bound_active: bool = False) -> list[dict]:
    with conn() as c:
        q = "SELECT * FROM employees" + (" WHERE active=1 AND user_id IS NOT NULL" if only_bound_active else "") + " ORDER BY id DESC"
        return [_emp(r) for r in c.execute(q)]


def update_employee(emp_id: int, **fields) -> None:
    if "state" in fields:
        fields["state"] = json.dumps(fields["state"], ensure_ascii=False)
    for k in ("is_office", "recruiter_ok", "active"):
        if k in fields:
            fields[k] = int(fields[k])
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE employees SET {cols} WHERE id=?", (*fields.values(), emp_id))


def delete_employee(emp_id: int) -> None:
    with conn() as c:
        for t, col in (("sent_steps", "employee_id"), ("survey_answers", "employee_id"), ("employees", "id")):
            c.execute(f"DELETE FROM {t} WHERE {col}=?", (emp_id,))


def step_status(emp_id: int) -> dict[str, dict]:
    with conn() as c:
        return {r["step_id"]: {"status": r["status"], "at": r["at"]} for r in
                c.execute("SELECT * FROM sent_steps WHERE employee_id=?", (emp_id,))}


def mark_step(emp_id: int, step_id: str, status: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO sent_steps(employee_id,step_id,status,at) VALUES(?,?,?,?) "
                  "ON CONFLICT(employee_id,step_id) DO UPDATE SET status=excluded.status, at=excluded.at",
                  (emp_id, step_id, status, time.time()))


def reset_employee_progress(emp_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM sent_steps WHERE employee_id=?", (emp_id,))
        c.execute("DELETE FROM survey_answers WHERE employee_id=?", (emp_id,))
        c.execute("UPDATE employees SET state='{}', form=NULL WHERE id=?", (emp_id,))


def add_answer(emp_id: int, survey: str, qkey: str, qtype: str, value, label=None, comment=None) -> None:
    with conn() as c:
        c.execute("INSERT INTO survey_answers(employee_id,survey,qkey,qtype,value,label,comment,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (emp_id, survey, qkey, qtype, str(value), label, comment, time.time()))


def attach_comment(emp_id: int, survey: str, qkey: str, comment: str) -> None:
    with conn() as c:
        c.execute("UPDATE survey_answers SET comment=? WHERE id=(SELECT MAX(id) FROM survey_answers "
                  "WHERE employee_id=? AND survey=? AND qkey=?)", (comment, emp_id, survey, qkey))


def list_answers(emp_id: int | None = None) -> list[dict]:
    with conn() as c:
        q, a = "SELECT * FROM survey_answers", ()
        if emp_id is not None:
            q, a = q + " WHERE employee_id=?", (emp_id,)
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
        r = c.execute("SELECT text FROM rewrites WHERE key=?", (key,)).fetchone()
        return r["text"] if r else None


def set_rewrite(key: str, text: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO rewrites(key,text) VALUES(?,?)", (key, text))


# ---------- команды бота (редактируются в панели управления) ----------
def list_commands() -> list[dict]:
    with conn() as c:
        return [dict(r) | {"builtin": bool(r["builtin"])} for r in c.execute("SELECT * FROM bot_commands ORDER BY builtin DESC, sort, name")]


def get_command(name: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM bot_commands WHERE name=?", (name,)).fetchone()
        return (dict(r) | {"builtin": bool(r["builtin"])}) if r else None


def save_command(name: str, **f) -> None:
    cur = get_command(name) or {"description": "", "kind": "static", "reply": "", "query": "", "fallback": "", "builtin": False, "sort": 0}
    row = {**cur, **f}
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO bot_commands(name,description,kind,reply,query,fallback,builtin,sort,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (name, row["description"], row["kind"], row["reply"], row["query"], row["fallback"], int(row["builtin"]), row["sort"], time.time()))


def delete_command(name: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM bot_commands WHERE name=?", (name,))


# ---------- уведомления руководителям подразделений ----------
def add_notice(emp_id: int, kind: str, text: str, manager: str | None) -> bool:
    """True — уведомление новое (повтор того же вида для сотрудника не создаётся)."""
    with conn() as c:
        return c.execute("INSERT OR IGNORE INTO manager_notices(employee_id,kind,text,manager,created_at) VALUES(?,?,?,?,?)",
                         (emp_id, kind, text, manager, time.time())).rowcount > 0


def mark_notice_delivered(emp_id: int, kind: str) -> None:
    with conn() as c:
        c.execute("UPDATE manager_notices SET delivered=1 WHERE employee_id=? AND kind=?", (emp_id, kind))


def list_notices(manager: str | None = None, limit: int = 100) -> list[dict]:
    with conn() as c:
        q = ("SELECT n.*, e.name AS employee FROM manager_notices n JOIN employees e ON e.id=n.employee_id"
             + (" WHERE n.manager=?" if manager else "") + " ORDER BY n.id DESC LIMIT ?")
        return [dict(r) for r in c.execute(q, ([manager] if manager else []) + [limit])]
