"""Сброс рабочих данных pipeline: сотрудники, переписки, обращения, ответы опросов, аналитика, напоминания.
Остаётся заложенное изначально: сценарий (исходные тексты и выбранные вложения), библиотека картинок и файлов, площадки по умолчанию,
команды бота по умолчанию, служебные настройки MAX. Запуск: python -m app.reset"""
from . import assets, commands, db
from . import scenario as sc

RUNTIME_TABLES = ("messages", "escalations", "users", "employees", "sent_steps", "survey_answers", "manager_notices")
KEEP_PREFIXES = ("imgname:", "maximg:")      # названия загруженных файлов и их токены в MAX
KEEP_KEYS = ("max_marker",)                  # позиция чтения событий MAX (иначе старые события придут заново)


def _image_assignments() -> dict:
    """Вложения сообщений: из сценария и из старых настроек image:<этап>:<idx> (если сценарий сохранён без них)."""
    out = {(st["id"], idx, it["t"]): it["image"] for st in sc.STEPS for idx, it in enumerate(st["items"])
           if it.get("image") and assets.path(it["image"])}
    for st in sc.STEPS:
        for idx, it in enumerate(st["items"]):
            legacy = db.get_setting(f"image:{st['id']}:{idx}")
            if it["t"] == "msg" and legacy and legacy != "none" and assets.path(legacy):
                out.setdefault((st["id"], idx, it["t"]), legacy)
    return out


def reset() -> dict:
    sc.reset()
    images = _image_assignments()
    counts = {}
    with db.conn() as c:
        for t in RUNTIME_TABLES:
            counts[t] = c.execute(f"DELETE FROM {t}").rowcount
        counts["bot_commands"] = c.execute("DELETE FROM bot_commands").rowcount
        keys = [r["key"] for r in c.execute("SELECT key FROM settings")]
        drop = [k for k in keys if k not in KEEP_KEYS and not k.startswith(KEEP_PREFIXES)]
        c.executemany("DELETE FROM settings WHERE key=?", [(k,) for k in drop])
        counts["settings"] = len(drop)
    commands.ensure()                        # встроенные команды — как заложено
    steps = sc.default_steps()               # сценарий и площадки — исходные, вложения сохраняются
    for st in steps:
        for idx, it in enumerate(st["items"]):
            if (st["id"], idx, it["t"]) in images:
                it["image"] = images[(st["id"], idx, it["t"])]
    sc.save(steps)
    with db.conn() as c:
        c.execute("VACUUM")
    return {**counts, "images_kept": len(images), "steps": len(sc.STEPS), "sites": sc.ALL_SITES[:]}


if __name__ == "__main__":  # pragma: no cover
    print(reset())
