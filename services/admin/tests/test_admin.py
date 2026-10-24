import httpx
import respx
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
AUTH = ("hr", "admin")


def test_auth_required_and_wrong_creds():
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("hr", "nope")).status_code == 401
    assert client.get("/", auth=("evil", "admin")).status_code == 401


def test_auth_custom_creds(monkeypatch):
    monkeypatch.setenv("ADMIN_USER", "boss")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    assert client.get("/", auth=("boss", "pw")).status_code == 200
    assert client.get("/", auth=AUTH).status_code == 401


def test_index():
    assert "Панель управления HR" in client.get("/", auth=AUTH).text


@respx.mock
def test_proxy_get_post_delete_and_upload_passthrough():
    respx.get("http://ingest:8000/documents").mock(return_value=httpx.Response(200, json=[{"id": "1"}]))
    assert client.get("/api/ingest/documents", auth=AUTH).json() == [{"id": "1"}]
    up = respx.post("http://ingest:8000/documents").mock(return_value=httpx.Response(202, json={"id": "2"}))
    r = client.post("/api/ingest/documents", auth=AUTH, files={"file": ("a.txt", b"hello")})
    assert r.status_code == 202 and b"hello" in up.calls[0].request.content
    assert up.calls[0].request.headers["content-type"].startswith("multipart/form-data")
    respx.delete("http://ingest:8000/documents/2").mock(return_value=httpx.Response(200, json={"deleted": "2"}))
    assert client.delete("/api/ingest/documents/2", auth=AUTH).json() == {"deleted": "2"}


@respx.mock
def test_proxy_pipeline_query_and_error_status():
    r_ = respx.get("http://pipeline:8000/api/escalations").mock(return_value=httpx.Response(200, json=[]))
    client.get("/api/pipeline/api/escalations?status=open", auth=AUTH)
    assert "status=open" in str(r_.calls[0].request.url)
    respx.get("http://ingest:8000/documents/x").mock(return_value=httpx.Response(404, json={"detail": "not found"}))
    assert client.get("/api/ingest/documents/x", auth=AUTH).status_code == 404


@respx.mock
def test_proxy_unknown_service_and_upstream_down():
    assert client.get("/api/evil/x", auth=AUTH).status_code == 404
    respx.get("http://ingest:8000/documents").mock(side_effect=httpx.ConnectError("down"))
    r = client.get("/api/ingest/documents", auth=AUTH)
    assert r.status_code == 502 and "недоступен" in r.json()["detail"]


@respx.mock
def test_proxy_adds_internal_token_only_for_pipeline(monkeypatch):
    import hashlib
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    r1 = respx.get("http://pipeline:8000/api/analytics").mock(return_value=httpx.Response(200, json={}))
    r2 = respx.get("http://ingest:8000/documents").mock(return_value=httpx.Response(200, json=[]))
    # клиент не может подсунуть свой токен — пробрасывается только content-type
    client.get("/api/pipeline/api/analytics", auth=("hr", "pw"), headers={"x-internal-token": "forged"})
    client.get("/api/ingest/documents", auth=("hr", "pw"))
    expected = hashlib.sha256(b"internal:pw").hexdigest()
    assert r1.calls[0].request.headers["x-internal-token"] == expected
    assert "x-internal-token" not in r2.calls[0].request.headers


@respx.mock
def test_proxy_put_and_patch_methods():
    put = respx.put("http://pipeline:8000/api/scenario/text").mock(return_value=httpx.Response(200, json={"ok": True}))
    patch = respx.patch("http://pipeline:8000/api/employees/1").mock(return_value=httpx.Response(200, json={"id": 1}))
    assert client.put("/api/pipeline/api/scenario/text", auth=AUTH, json={"step_id": "a", "idx": 0, "text": "t"}).json() == {"ok": True}
    assert client.patch("/api/pipeline/api/employees/1", auth=AUTH, json={"name": "x"}).json() == {"id": 1}
    assert put.called and patch.called


# ---------- безопасность интерфейса: XSS и заголовки ----------
def test_inline_handlers_embed_only_server_generated_identifiers():
    """Значение из данных внутри обработчика onclick="…" попадает в JS: HTML-экранирование не защищает (браузер раскодирует апостроф
    обратно). Поэтому туда разрешены только идентификаторы, которые формирует сервер (uuid, числа, ограниченные шаблоном имена);
    всё остальное — названия, ФИО, тексты — берётся из загруженных данных по id."""
    import re
    from pathlib import Path
    html = (Path(__file__).parent.parent / "app" / "static" / "index.html").read_text(encoding="utf-8")
    SAFE = {"d.id", "e.id", "t.id", "x.file", "sid", "i.idx", "c.name", "u.login", "!u.active"}   # uuid · число · up_<hex>.jpg · [a-z0-9_] · [A-Za-z0-9_.-]
    used = {m for h in re.findall(r"""on\w+="([^"]*)\"""", html) for m in re.findall(r"\$\{([^}]*)\}", h)}
    assert used and used <= SAFE, f"в обработчики попали несогласованные данные: {sorted(used - SAFE)}"
    assert "&#39;" in html     # апостроф экранируется для HTML-атрибутов (на JS-строки обработчика это не распространяется)


def test_security_headers_on_every_response():
    r = client.get("/", auth=("hr", "admin"))
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp and "base-uri 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/api/rbac/me").headers["x-frame-options"] == "DENY"                 # и на ответах с ошибкой 401
