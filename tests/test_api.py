import asyncio

from fastapi.testclient import TestClient
from fastmcp import Client
from pydantic import SecretStr

from paoding.api.app import create_app
from paoding.mcp import create_mcp
from paoding.services.worker import Worker


def test_auth_validation_and_idempotency(rt):
    rt.settings.api_token = SecretStr("test-secret")
    with TestClient(create_app(rt)) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/stats").status_code == 401
        client.headers["Authorization"] = "Bearer test-secret"
        assert client.post("/api/v1/ingest", json={"url": "file:///etc/passwd"}).status_code == 422
        data = {"url": "https://example.com/article", "instruction": "只拆策略"}
        first = client.post("/api/v1/ingest", json=data, headers={"Idempotency-Key": "abc"})
        assert first.status_code == 202
        assert (
            client.post("/api/v1/ingest", json=data, headers={"Idempotency-Key": "abc"}).json()
            == first.json()
        )
        data["instruction"] = "不同要求"
        assert (
            client.post("/api/v1/ingest", json=data, headers={"Idempotency-Key": "abc"}).status_code
            == 409
        )
        assert client.get("/api/v1/jobs/" + first.json()["job_id"]).json()["status"] == "queued"
        assert client.get("/api/v1/nodes/missing").status_code == 404
        assert client.get("/api/v1/atoms?limit=101").status_code == 422
        assert client.get("/api/v1/atoms?atom_type=bad").status_code == 422
        assert client.get("/openapi.json").status_code == 200


def test_creator_recognition_and_webhook_verification(rt):
    rt.settings.telegram_secret = SecretStr("webhook-secret")
    with TestClient(create_app(rt)) as client:
        creator = client.post(
            "/api/v1/ingest", json={"url": "https://www.youtube.com/@example"}
        ).json()
        assert creator["kind"] == "creator"
        payload = {"update_id": 1, "message": {"chat": {"id": 12}, "text": "https://example.com/a"}}
        assert client.post("/api/v1/webhooks/telegram", json=payload).status_code == 401
        headers = {"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"}
        one = client.post("/api/v1/webhooks/telegram", json=payload, headers=headers)
        two = client.post("/api/v1/webhooks/telegram", json=payload, headers=headers)
        assert one.json() == two.json()
        assert "已接单" in one.json()["text"]
        assert client.post("/api/v1/webhooks/feishu", json={}).status_code == 401


def test_mcp_tools_call_shared_services(rt, source):
    rt.ingest_file(source)
    worker = Worker(rt)
    while worker.run_once():
        pass

    async def run():
        async with Client(create_mcp(rt)) as client:
            tools = await client.list_tools()
            assert len(tools) == 5
            result = await client.call_tool("paoding_fetch_atoms", {"atom_type": "sop", "limit": 2})
            assert not result.is_error
            assert "demo" in str(result)
            empty = await client.call_tool("paoding_query_playbook", {"domain": "unknown"})
            assert not empty.is_error

    asyncio.run(run())
