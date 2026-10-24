"""Редактор сценария из панели: этапы (создание, удаление, срок, порядок, площадки) и сообщения внутри этапов.
Все операции работают с копией сценария и сохраняют её целиком; KeyError — нет такого этапа/сообщения, ValueError — неверные данные."""
import copy
import secrets

from . import scenario as sc

ITEM_TYPES = ("msg", "text", "scale", "yesno")
SYSTEM_STEPS = ("welcome",)  # знакомство запускается привязкой к боту — удалять нельзя


def _work() -> list:
    return copy.deepcopy(sc.STEPS)


def _step(steps: list, sid: str) -> dict:
    for s_ in steps:
        if s_["id"] == sid:
            return s_
    raise KeyError(sid)


def _sites(value) -> list | None:
    if not value:
        return None
    bad = [x for x in value if x not in sc.ALL_SITES]
    if bad:
        raise ValueError(f"неизвестная площадка: {', '.join(bad)}")
    return list(value)


def _title(value: str) -> str:
    if not (value or "").strip():
        raise ValueError("название этапа не может быть пустым")
    return value.strip()[:120]


def make_item(type_: str, text: str, sites=None, office=None, image=None) -> dict:
    if type_ not in ITEM_TYPES:
        raise ValueError(f"тип сообщения: {', '.join(ITEM_TYPES)}")
    if not (text or "").strip() or len(text) > 3500:
        raise ValueError("текст: от 1 до 3500 символов")
    base = {"sites": _sites(sites), "office": office, "cond": None}
    key = "q_" + secrets.token_hex(3)
    if type_ == "msg":
        return {**sc.M(text.strip()), **base, "image": image or None}
    if type_ == "text":
        return {**sc.T(key, text.strip()), **base}
    if type_ == "scale":
        return {**sc.S(key, text.strip()), **base}
    return {**sc.Y(key, text.strip()), **base}


def _save(steps: list) -> None:
    sc.save(steps)


def create_step(title: str, due, sites=None, office=None, gate=None, survey=False, items=None, after: str | None = None) -> str:
    steps = _work()
    sid = "c_" + secrets.token_hex(3)
    spec = sc.validate_due(due)
    step = {"id": sid, "title": _title(title), "when": sc.describe_due(spec), "due": spec, "survey": sid if survey else None,
            "sites": _sites(sites), "office": office, "gate": "recruiter" if gate else None,
            "items": [make_item(i["type"], i["text"], i.get("sites"), i.get("office"), i.get("image")) for i in items or []]}
    pos = steps.index(_step(steps, after)) + 1 if after else len(steps)
    steps.insert(pos, step)
    _save(steps)
    return sid


def update_step(sid: str, **f) -> None:
    steps = _work()
    st = _step(steps, sid)
    if f.get("title") is not None:
        st["title"] = _title(f["title"])
    if "due" in f:
        if st["due"] is None and sid in SYSTEM_STEPS:
            raise ValueError("знакомство запускается привязкой к боту — срок не задаётся")
        st["due"] = sc.validate_due(f["due"])
        st["when"] = sc.describe_due(st["due"])
    if "sites" in f:
        st["sites"] = _sites(f["sites"])
    if "office" in f:
        st["office"] = f["office"]
    if f.get("gate") is not None:
        st["gate"] = "recruiter" if f["gate"] else None
    if f.get("survey") is not None:
        st["survey"] = (st["survey"] or sid) if f["survey"] else None
    _save(steps)


def delete_step(sid: str) -> None:
    if sid in SYSTEM_STEPS:
        raise ValueError("этот этап нельзя удалить: он запускается привязкой к боту")
    steps = _work()
    steps.remove(_step(steps, sid))
    _save(steps)


def move_step(sid: str, direction: str) -> None:
    steps = _work()
    i = steps.index(_step(steps, sid))
    j = i - 1 if direction == "up" else i + 1
    if direction not in ("up", "down"):
        raise ValueError("direction: up | down")
    if 0 <= j < len(steps):
        steps[i], steps[j] = steps[j], steps[i]
        _save(steps)


def place_step(sid: str, before: str | None = None, after: str | None = None) -> None:
    """Перенос этапа (drag-and-drop): поставить перед этапом before или после этапа after."""
    if bool(before) == bool(after):
        raise ValueError("укажите, куда поставить этап: before или after")
    steps = _work()
    st = _step(steps, sid)
    ref = before or after
    if ref == sid:
        return
    steps.remove(st)
    pos = steps.index(_step(steps, ref))
    steps.insert(pos if before else pos + 1, st)
    _save(steps)


def _item(st: dict, idx: int) -> dict:
    if not 0 <= idx < len(st["items"]):
        raise KeyError(idx)
    return st["items"][idx]


def add_item(sid: str, type_: str, text: str, sites=None, office=None, image=None, index: int | None = None) -> int:
    steps = _work()
    st = _step(steps, sid)
    it = make_item(type_, text, sites, office, image)
    pos = len(st["items"]) if index is None else max(0, min(index, len(st["items"])))
    st["items"].insert(pos, it)
    _save(steps)
    return pos


def update_item(sid: str, idx: int, **f) -> None:
    steps = _work()
    it = _item(_step(steps, sid), idx)
    if f.get("text") is not None:
        if "text" not in it:
            raise ValueError("у этого элемента нет редактируемого текста")
        if not f["text"].strip():  # пустой текст = вернуть исходный
            orig = next((x for x in sc.default_steps() if x["id"] == sid), None)
            if orig and idx < len(orig["items"]) and orig["items"][idx].get("t") == it["t"]:
                f["text"] = orig["items"][idx]["text"]
            else:
                raise ValueError("текст не может быть пустым")
        it["text"] = f["text"].strip() if len(f["text"]) <= 3500 else f["text"][:3500]
    if "sites" in f:
        it["sites"] = _sites(f["sites"])
    if "office" in f:
        it["office"] = f["office"]
    if "image" in f:
        if it["t"] != "msg":
            raise ValueError("вложение можно добавить только к сообщению")
        it["image"] = f["image"] or None
    _save(steps)


def delete_item(sid: str, idx: int) -> None:
    steps = _work()
    st = _step(steps, sid)
    _item(st, idx)
    del st["items"][idx]
    _save(steps)


def move_item(sid: str, idx: int, direction: str) -> None:
    if direction not in ("up", "down"):
        raise ValueError("direction: up | down")
    steps = _work()
    st = _step(steps, sid)
    _item(st, idx)
    j = idx - 1 if direction == "up" else idx + 1
    if 0 <= j < len(st["items"]):
        st["items"][idx], st["items"][j] = st["items"][j], st["items"][idx]
        _save(steps)


def restore_defaults() -> None:
    sc.save(sc.default_steps())


# ---------- площадки ----------
def _walk(items: list):
    for it in items:
        yield it
        for o in it.get("options", []):
            yield from _walk(o.get("then", []))
        yield from _walk(it.get("then", []) if isinstance(it.get("then"), list) else [])


def add_site(code: str, title: str, office: bool = True, copy_from: str | None = None) -> None:
    import re
    if not re.fullmatch(r"[A-Za-z0-9_]{2,24}", code or ""):
        raise ValueError("код площадки: 2–24 символа, латиница, цифры, _")
    if code in sc.ALL_SITES:
        raise ValueError("такая площадка уже есть")
    if not (title or "").strip():
        raise ValueError("укажите название площадки")
    if copy_from is not None and copy_from not in sc.ALL_SITES:
        raise ValueError("неизвестная площадка-образец")
    sites = sc.site_list() + [{"code": code, "title": title.strip()[:60], "office": bool(office)}]
    if copy_from:  # новая площадка получает то же содержание, что и образец
        steps = _work()
        for st in steps:
            for holder in [st, *_walk(st["items"])]:
                if holder.get("sites") and copy_from in holder["sites"] and code not in holder["sites"]:
                    holder["sites"].append(code)
        sc.save(steps)
    sc.save_sites(sites)


def update_site(code: str, title: str | None, office: bool | None) -> None:
    if code not in sc.ALL_SITES:
        raise KeyError(code)
    sites = sc.site_list()
    for x in sites:
        if x["code"] == code:
            x["title"] = (title or x["title"]).strip()[:60] or x["title"]
            x["office"] = x["office"] if office is None else bool(office)
    sc.save_sites(sites)


def delete_site(code: str, in_use: int) -> None:
    if code not in sc.ALL_SITES:
        raise KeyError(code)
    if in_use:
        raise ValueError(f"на площадке есть сотрудники ({in_use}) — сначала переведите или удалите их")
    if len(sc.ALL_SITES) == 1:
        raise ValueError("нельзя удалить единственную площадку")
    steps = _prune(_work(), code)
    sc.save(steps)
    sc.save_sites([x for x in sc.site_list() if x["code"] != code])


def _prune(items: list, code: str) -> list:
    """Убирает площадку из ограничений; то, что было только для неё, удаляется (иначе оно бы стало «для всех»)."""
    out = []
    for it in items:
        sites = it.get("sites")
        if sites and code in sites:
            sites = [x for x in sites if x != code]
            if not sites:
                continue
            it["sites"] = sites
        if "items" in it:
            it["items"] = _prune(it["items"], code)
        for o in it.get("options", []):
            o["then"] = _prune(o.get("then", []), code)
        if isinstance(it.get("then"), list):
            it["then"] = _prune(it["then"], code)
        out.append(it)
    return out
