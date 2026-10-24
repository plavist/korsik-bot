"""Роли и права панели управления (RBAC). Чтение доступно любой роли; изменения — только по праву на раздел."""
ROLES = {
    "admin": {"title": "Администратор", "perms": {"docs", "scenario", "commands", "employees", "escalations", "channel", "users"},
              "about": "всё, включая канал доставки и управление пользователями"},
    "editor": {"title": "Контент-менеджер", "perms": {"docs", "scenario", "commands"},
               "about": "база знаний, сценарий (тексты и картинки), команды бота"},
    "hr": {"title": "HR-специалист", "perms": {"employees", "escalations"},
           "about": "сотрудники и онбординг, обращения: ответы сотрудникам"},
    "manager": {"title": "Руководитель подразделения", "perms": set(),
                "about": "видит статус адаптации только своих новых сотрудников и аналитику по ним; получает напоминания о личных встречах"},
    "viewer": {"title": "Наблюдатель", "perms": set(), "about": "только просмотр"},
}
PERM_TITLES = {"docs": "база знаний", "scenario": "сценарий", "commands": "команды бота", "employees": "сотрудники",
               "escalations": "обращения", "channel": "канал доставки", "users": "пользователи"}

# что доступно роли «Руководитель подразделения» (только чтение, данные ограничены его подчинёнными)
MANAGER_READ = ("api/sites", "api/analytics", "api/insights/stages", "api/insights/status", "api/insights/funnel", "api/insights/risk", "api/insights/comments",
                "api/insights/notices", "api/employees")


def manager_can_read(service: str, path: str) -> bool:
    return service == "pipeline" and any(path == p or path.startswith(p + "/") for p in MANAGER_READ)


_PIPELINE_AREAS = (("api/insights", "employees"), ("api/sites", "scenario"), ("api/scenario", "scenario"), ("api/commands", "commands"), ("api/employees", "employees"),
                   ("api/scheduler", "employees"), ("api/known-users", "employees"), ("api/escalations", "escalations"),
                   ("api/mode", "channel"), ("api/max", "channel"))


def required(method: str, service: str, path: str) -> str | None:
    """Право, нужное для запроса через прокси; None — достаточно быть авторизованным."""
    if method == "GET":
        return "channel" if path.startswith("api/max/debug") else None  # сырые события MAX — только канал/админ
    if service == "ingest":
        return "docs"
    if path.startswith("api/commands/") and path.endswith("/preview"):  # предпросмотр ничего не меняет
        return None
    for prefix, perm in _PIPELINE_AREAS:
        if path == prefix or path.startswith(prefix + "/"):
            return perm
    return "users"  # неизвестный маршрут записи — только администратору


def can(role: str, perm: str | None) -> bool:
    return perm is None or perm in ROLES.get(role, {}).get("perms", set())
