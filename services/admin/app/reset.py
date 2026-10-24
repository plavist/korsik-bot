"""Сброс данных панели: созданные пользователи и журнал действий. Главный администратор (из переменных окружения) остаётся.
Запуск: python -m app.reset"""
from . import users


def reset() -> dict:
    with users.conn() as c:
        n_users = c.execute("DELETE FROM users").rowcount
        n_audit = c.execute("DELETE FROM audit").rowcount
    users._cache.clear()
    return {"users": n_users, "audit": n_audit}


if __name__ == "__main__":  # pragma: no cover
    print(reset())
