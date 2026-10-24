"""Очередь по пользователю: события одного человека (сообщение, кнопка, плановая рассылка, действие HR) обрабатываются строго
по очереди. Состояние сотрудника хранится одним JSON и читается/пишется целиком — без этого два быстрых нажатия кнопок
затирали бы друг друга. Блокировки живут, только пока ими кто-то пользуется (слабые ссылки), память не растёт."""
import asyncio
import weakref

_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def user_lock(user_id: str) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[user_id] = lock
    return lock
