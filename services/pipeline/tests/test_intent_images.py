"""Интент «обратиться к HR» по смыслу вопроса и картинки Корсика (сценарий, веб, MAX)."""
import json

import httpx
import pytest
import respx

from app import assets, bot, channels, db, engine, intent
from app import scenario as sc
from test_api import anon
from test_onboarding import llm_router, make_emp


# ---------- интент ----------
@pytest.mark.parametrize("text,reason", [
    ("Мой начальник постоянно кричит на меня", "конфликт"),
    ("Мне задержали зарплату", "оплат"),
    ("Хочу уволиться", "увольн"),
    ("Хочу пожаловаться на коллегу", "жалоб"),
    ("Я получил травму на производстве", "здоровье"),
    ("Хочу поговорить с живым человеком", "живом"),
])
def test_intent_detects_escalation_like_text(text, reason):
    assert reason in intent.detect(text)


def test_intent_ignores_regular_questions():
    assert intent.detect("Во сколько начинается рабочий день?") is None and intent.detect(None) is None


@respx.mock
async def test_escalation_like_question_gets_answer_and_is_escalated_at_once():
    llm_router(kb_text="Такие вопросы решает HR.")
    res = await bot.handle("i1", "web", text="Меня обижает коллега, что делать?")
    m = res["messages"][0]
    assert m["text"] == "Такие вопросы решает HR." and m["buttons"] is None   # ответ как обычно, без приписок и кнопок
    esc = db.list_escalations()
    assert len(esc) == 1 and esc[0]["kind"] == "intent" and "конфликт" in esc[0]["reason"] and "обижает" in esc[0]["reason"]


@respx.mock
async def test_intent_escalation_for_formal_form_has_no_extra_text():
    llm_router(kb_text="Такие вопросы решает HR.")
    db.update_employee(make_emp(user="i2")["id"], form="vy")
    res = await bot.handle("i2", "web", text="Хочу уволиться")
    assert res["messages"][0]["text"] == "Такие вопросы решает HR." and db.list_escalations()[0]["kind"] == "intent"


# ---------- картинки: ничего не поставляется заранее, всё через загрузку ----------
from test_api import client  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
JPG = b"\xff\xd8\xff" + b"1" * 20
IMG = "up_" + __import__("hashlib").sha256(JPG).hexdigest()[:12] + ".jpg"  # имя JPG-файла после загрузки


def upload(data=JPG, name=""):
    r = client.post("/api/scenario/images" + (f"?name={name}" if name else ""), content=data)
    assert r.status_code == 200, r.text
    return r.json()["file"]


def first_msg():
    step = sc.STEPS[0]
    return step, next(i for i, it in enumerate(step["items"]) if it["t"] == "msg")


def test_nothing_is_preloaded():
    assert client.get("/api/scenario/images").json() == []
    assert not any(it.get("image") for s in client.get("/api/scenario").json()["steps"] for it in s["items"])
    assert not any(not isinstance(it, dict) for s in sc.STEPS for it in s["items"])


# ---------- отправка в MAX ----------
def _max_mocks(token="tok1"):
    upload()                                                            # файл должен быть в библиотеке
    up = respx.post("http://max/uploads").mock(return_value=httpx.Response(200, json={"url": "http://upload.max/x"}))
    put = respx.post("http://upload.max/x").mock(return_value=httpx.Response(200, json={"photos": {"1": {"token": token}}}))
    return up, put


@respx.mock
async def test_send_max_uploads_image_once_and_attaches_it(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    up, put = _max_mocks()
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    await channels.send_max("1", "Привет", [[{"text": "Ок", "payload": "p"}]], image=IMG)
    await channels.send_max("1", "Ещё", image=IMG)
    assert up.call_count == 1 and put.call_count == 1                  # токен закэширован
    first, second = (json.loads(c.request.content) for c in sent.calls)
    assert [a["type"] for a in first["attachments"]] == ["image", "inline_keyboard"]
    assert first["attachments"][0]["payload"]["token"] == "tok1" and second["attachments"][0]["type"] == "image"


@respx.mock
async def test_send_max_retries_when_attachment_not_ready(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    _max_mocks()

    async def nosleep(_):
        return None
    monkeypatch.setattr(channels.asyncio, "sleep", nosleep)
    sent = respx.post("http://max/messages").mock(side_effect=[
        httpx.Response(400, json={"code": "attachment.not.ready"}), httpx.Response(200, json={})])
    await channels.send_max("1", "Привет", image=IMG)
    assert sent.call_count == 2


@respx.mock
@pytest.mark.parametrize("fail", ["uploads", "put", "format"])
async def test_send_max_falls_back_to_text_when_image_fails(monkeypatch, fail):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    upload()
    respx.post("http://max/uploads").mock(return_value=httpx.Response(500 if fail == "uploads" else 200, json={"url": "http://upload.max/x"}))
    respx.post("http://upload.max/x").mock(return_value=httpx.Response(500 if fail == "put" else 200, json={"photos": {"1": {"token": "t1"}}}))
    sent = respx.post("http://max/messages").mock(side_effect=lambda r: httpx.Response(
        400 if fail == "format" and "attachments" in json.loads(r.content) else 200, json={"code": "bad"}))
    await channels.send_max("1", "Привет", image=IMG)
    assert "attachments" not in json.loads(sent.calls[-1].request.content)
    assert db.get_setting(f"maximg:{IMG}") == ""                     # плохой токен сброшен


@respx.mock
async def test_send_max_image_plus_keyboard_survive_retry_without_image(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    _max_mocks()
    sent = respx.post("http://max/messages").mock(side_effect=lambda r: httpx.Response(
        400 if any(a["type"] == "image" for a in json.loads(r.content).get("attachments", [])) else 200, json={"code": "bad"}))
    await channels.send_max("1", "Привет", [[{"text": "Ок", "payload": "p"}]], image=IMG)
    assert [a["type"] for a in json.loads(sent.calls[-1].request.content)["attachments"]] == ["inline_keyboard"]


@respx.mock
async def test_send_max_error_without_image_is_raised(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    respx.post("http://max/messages").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(RuntimeError):
        await channels.send_max("1", "x")
    _max_mocks()
    with pytest.raises(RuntimeError):
        await channels.send_max("1", "слово " * 1500 + "x", image=IMG)  # ошибка во втором куске не маскируется


# ---------- библиотека и выбор картинки сообщения ----------
def test_upload_validates_serves_names_and_deletes():
    assert client.post("/api/scenario/images", content=b"text").status_code == 400
    assert client.post("/api/scenario/images", content=JPG + b"0" * assets.MAX_UPLOAD).status_code == 400
    f = upload(PNG, "Приветствие")
    assert f.endswith(".png") and client.get("/api/scenario/images").json() == [{"file": f, "label": "Приветствие", "kind": "image"}]
    r = client.get(f"/assets/{f}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and assets.media_type("a.jpg") == "image/jpeg"
    g = upload(JPG)
    assert {x["file"]: x["label"] for x in client.get("/api/scenario/images").json()}[g] == g          # без названия — имя файла
    assert client.get("/assets/nope.jpg").status_code == 404 and client.get("/assets/up_000000000000.png").status_code == 404
    assert assets.path("../app/main.py") is None and assets.path("") is None
    assert client.delete(f"/api/scenario/images/{f}").json() == {"deleted": f}
    assert client.delete(f"/api/scenario/images/{f}").status_code == 404 and client.get(f"/assets/{f}").status_code == 404


def test_choose_remove_reset_image_for_message():
    step, idx = first_msg()
    sid = step["id"]
    item = lambda: sc.STEP_BY_ID[sid]["items"][idx]  # noqa: E731 — сценарий пересоздаётся при каждом сохранении
    put = lambda image: client.put("/api/scenario/image", json={"step_id": sid, "idx": idx, "image": image})
    f = upload()
    assert put(f).json()["image"] == f and assets.for_item(sid, idx, item()) == f
    view = next(i for s in client.get("/api/scenario").json()["steps"] if s["id"] == sid for i in s["items"] if i["idx"] == idx)
    assert view["image"] == f and view["image_choice"] == f and view["can_image"]
    assert put("none").json()["image"] is None and put("").json()["image"] is None
    assert put(f).json()["image"] == f
    client.delete(f"/api/scenario/images/{f}")                                  # удалили из библиотеки — у сообщения вложения нет
    assert assets.for_item(sid, idx, item()) is None
    assert put("nope.jpg").status_code == 400
    assert client.put("/api/scenario/image", json={"step_id": "zz", "idx": 0, "image": ""}).status_code == 404
    q = next((s, i) for s in sc.STEPS for i, x in enumerate(s["items"]) if x["t"] != "msg")
    assert client.put("/api/scenario/image", json={"step_id": q[0]["id"], "idx": q[1], "image": ""}).status_code == 404


@respx.mock
async def test_engine_sends_only_chosen_image_and_history_keeps_it():
    llm_router()
    emp = make_emp(user="i4")
    step, idx = first_msg()
    msgs, _ = await engine.run_items(emp, step)
    assert not any(m.get("image") for m in msgs)                                # по умолчанию картинок нет
    f = upload()
    client.put("/api/scenario/image", json={"step_id": step["id"], "idx": idx, "image": f})
    step = sc.STEP_BY_ID[step["id"]]
    msgs, _ = await engine.run_items(emp, step)
    assert msgs[0].get("image") == f
    from app.main import deliver
    await deliver(emp, msgs)
    assert db.history("i4")[0]["image"] == f
    # сообщение, бессмысленное без вложения (карта), заменяется запасным текстом, а с картинкой — уходит как есть
    s2 = next(s for s in sc.STEPS if any(it["t"] == "msg" and it.get("req") for it in s["items"]))
    i2 = next(i for i, it in enumerate(s2["items"]) if it["t"] == "msg" and it.get("req"))
    assert engine._plain(s2["items"][i2], "t", assets.for_item(s2["id"], i2, s2["items"][i2])) != "t"
    client.put("/api/scenario/image", json={"step_id": s2["id"], "idx": i2, "image": f})
    s2 = sc.STEP_BY_ID[s2["id"]]
    assert engine._plain(s2["items"][i2], "t", assets.for_item(s2["id"], i2, s2["items"][i2])) == "t"


# ---------- PDF-вложения ----------
PDF = b"%PDF-1.4\n" + b"x" * 30
PDF_NAME = "up_" + __import__("hashlib").sha256(PDF).hexdigest()[:12] + ".pdf"


def test_pdf_upload_library_and_serve():
    f = upload(PDF, "Карта территории")
    assert f == PDF_NAME and assets.is_file(f) and not assets.is_file(IMG) and not assets.is_file(None)
    assert {"file": f, "label": "Карта территории", "kind": "file"} in client.get("/api/scenario/images").json()
    r = client.get(f"/assets/{f}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and assets.media_type(f) == "application/pdf"
    step, idx = first_msg()
    assert client.put("/api/scenario/image", json={"step_id": step["id"], "idx": idx, "image": f}).json()["image"] == f


@respx.mock
async def test_send_max_attaches_pdf_as_file(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    upload(PDF)
    up = respx.post("http://max/uploads").mock(return_value=httpx.Response(200, json={"url": "http://upload.max/f"}))
    respx.post("http://upload.max/f").mock(return_value=httpx.Response(200, json={"token": "filetok"}))     # у файлов token в корне ответа
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    await channels.send_max("1", "Держи карту", [[{"text": "Ок", "payload": "p"}]], image=PDF_NAME)
    assert up.calls[0].request.url.params["type"] == "file"
    atts = json.loads(sent.calls[0].request.content)["attachments"]
    assert [a["type"] for a in atts] == ["file", "inline_keyboard"] and atts[0]["payload"]["token"] == "filetok"
    # токен файла устарел → повтор без вложения, но с клавиатурой
    respx.post("http://max/messages").mock(side_effect=lambda r: httpx.Response(
        400 if any(a["type"] == "file" for a in json.loads(r.content).get("attachments", [])) else 200, json={"code": "bad"}))
    db.set_setting(f"maximg:{PDF_NAME}", "stale")
    await channels.send_max("1", "Ещё", [[{"text": "Ок", "payload": "p"}]], image=PDF_NAME)
    assert db.get_setting(f"maximg:{PDF_NAME}") == ""


def test_upload_rejects_unknown_format_message():
    r = client.post("/api/scenario/images", content=b"GIF89a")
    assert r.status_code == 400 and "PDF" in r.json()["detail"]
