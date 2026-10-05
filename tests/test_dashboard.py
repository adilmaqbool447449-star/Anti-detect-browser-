import pytest
from fastapi.testclient import TestClient

from polybot.dashboard.app import create_app
from tests.test_paper_and_bot import make_bot


@pytest.fixture
def client(tmp_path):
    bot, _ = make_bot(tmp_path)
    return TestClient(create_app(bot, "admin", "s3cret-pass")), bot


def test_requires_auth(client):
    c, _ = client
    assert c.get("/").status_code == 401
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", auth=("admin", "wrong")).status_code == 401
    assert c.post("/api/kill").status_code == 401
    assert c.get("/healthz").status_code == 200


def test_state_and_page(client):
    c, _ = client
    r = c.get("/api/state", auth=("admin", "s3cret-pass"))
    assert r.status_code == 200
    s = r.json()
    assert s["mode"] == "paper" and s["btc"]["price"] == 101_000.0
    assert "Polybot" in c.get("/", auth=("admin", "s3cret-pass")).text
    assert r.headers["cache-control"] == "no-store"


def test_kill_endpoint(client):
    c, bot = client
    assert c.post("/api/kill", auth=("admin", "s3cret-pass")).json() == {"kill_switch": True}
    assert bot.risk.kill_switch_active()


def test_lockout_after_failures(client):
    c, _ = client
    for _ in range(10):
        c.get("/api/state", auth=("admin", "nope"))
    assert c.get("/api/state", auth=("admin", "s3cret-pass")).status_code == 429


def test_empty_password_refused(tmp_path):
    bot, _ = make_bot(tmp_path)
    with pytest.raises(ValueError):
        create_app(bot, "admin", "")
