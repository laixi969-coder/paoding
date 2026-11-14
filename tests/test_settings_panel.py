import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from paoding.api.app import create_app
from paoding.services.worker import Worker
from paoding.storage.tables import Job


def test_settings_write_does_not_return_secrets_and_applies_to_worker(rt, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    Path(".env").write_text("# custom comment\nQWEN_API_KEY=old-secret\n")
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
        assert "new-secret" in Path(".env").read_text()
        assert "# custom comment" in Path(".env").read_text()
        assert os.stat(".env").st_mode & 0o077 == 0
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


def test_settings_page_restricted_to_local_host(rt):
    with TestClient(create_app(rt), base_url="http://example.com") as client:
        assert client.get("/settings").status_code == 403
        assert client.get("/api/v1/settings").status_code == 403
