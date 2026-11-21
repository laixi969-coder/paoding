from pathlib import Path

import pytest
from conftest import assert_private_file
from fastapi.testclient import TestClient
from sqlalchemy import select

from paoding.api.app import create_app
from paoding.core.settings_panel import update_values
from paoding.services.worker import Worker
from paoding.storage import credentials
from paoding.storage.tables import Job


def test_settings_write_does_not_return_secrets_and_applies_to_worker(rt, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    Path(".env").write_text("# 本地配置\nQWEN_API_KEY=old-secret\n", encoding="utf-8")
    worker = Worker(rt)
    with TestClient(create_app(rt), base_url="http://localhost") as client:
        response = client.get("/api/v1/settings")
        assert response.status_code == 200
        assert "old-secret" not in response.text
        field = next(x for x in response.json()["fields"] if x["key"] == "QWEN_API_KEY")
        assert field["configured"] and field["value"] == ""
        assert client.put(
            "/api/v1/settings",
            json={"QWEN_API_KEY": "new-secret", "PAODING_TIKHUB_TOKEN": "collector-secret"},
        ).json() == {"saved": True}
        assert client.put("/api/v1/settings", json={"QWEN_API_KEY": ""}).status_code == 200
        saved = Path(".env").read_text(encoding="utf-8")
        assert "new-secret" in saved
        assert "# 本地配置" in saved
        assert_private_file(Path(".env"))
        assert "new-secret" not in client.get("/api/v1/settings").text
        assert client.put("/api/v1/settings", json={"PAODING_HOST": "0.0.0.0"}).status_code == 422
        assert (
            client.put(
                "/api/v1/settings", json={"PAODING_RSSHUB_URL": "file:///etc/passwd"}
            ).status_code
            == 422
        )
        assert (
            client.put("/api/v1/settings", json={"PAODING_TEXT_ROUTE": "wrong"}).status_code == 422
        )
        assert (
            client.put(
                "/api/v1/settings", json={"PAODING_TEXT_ROUTE": "qwen:qwen-plus"}
            ).status_code
            == 200
        )
        assert rt.gateway.config().routes["text"][0].model == "qwen-plus"
        assert (
            client.put(
                "/api/v1/settings", json={"PAODING_QWEN_BASE_URL": "http://example.com/v1"}
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/api/v1/settings",
                json={"PAODING_QWEN_BASE_URL": "https://user:password@example.com/v1"},
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/api/v1/settings", json={"PAODING_QWEN_BASE_URL": "https://models.example/v1"}
            ).status_code
            == 200
        )
        assert rt.gateway.config().providers["qwen"].base_url == "https://models.example/v1"
        worker.run_once()
        assert rt.settings.tikhub_token.get_secret_value() == "collector-secret"
        with rt.db.transaction() as session:
            assert not session.scalars(select(Job)).all()


def test_failed_settings_write_keeps_previous_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path(".env")
    previous = "# 旧配置\nQWEN_API_KEY=previous\n".encode()
    path.write_bytes(previous)
    set_key = credentials.set_key

    def fail_second(path, key, value, **kwargs):
        if key == "OPENAI_API_KEY":
            raise OSError("disk full")
        return set_key(path, key, value, **kwargs)

    monkeypatch.setattr(credentials, "set_key", fail_second)
    with pytest.raises(OSError, match="disk full"):
        update_values({"QWEN_API_KEY": "new-test-value", "OPENAI_API_KEY": "another-test-value"})
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob(".paoding-*"))


def test_settings_page_restricted_to_local_host(rt):
    with TestClient(create_app(rt), base_url="http://example.com") as client:
        response = client.get("/settings")
        assert response.status_code == 403
        assert response.headers["Content-Type"].startswith("text/html")
        assert "配置模型" in response.text
        assert 'href="/guide#mobile"' in response.text
        assert 'aria-current="page"' in response.text
        assert 'id="form"' not in response.text
        assert client.get("/api/v1/settings").status_code == 403
