import asyncio

import pytest
from bs4 import BeautifulSoup
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
        job_id = first.json()["job_id"]
        job = client.get("/api/v1/jobs/" + job_id).json()
        assert job["status"] == "queued" and job["title"] == "example.com"
        recent = client.get("/api/v1/jobs?limit=1").json()[0]
        assert (recent["id"], recent["title"], recent["created_at"]) == (
            job_id,
            job["title"],
            job["created_at"],
        )
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


def test_library_browses_searches_filters_and_detail(rt, source):
    from sqlalchemy import select

    from paoding.storage.tables import AtomElement, KnowledgeNode, NodeCategory, TaxonomyCategory

    for instruction in ("first", "second"):
        rt.ingest_file(source, instruction=instruction)
    worker = Worker(rt)
    while worker.run_once():
        pass
    with rt.db.transaction() as session:
        nodes = session.scalars(select(KnowledgeNode).order_by(KnowledgeNode.created_at)).all()
        nodes[0].platform = "youtube"
        nodes[0].title = "独特标题"
        atom = session.scalar(select(AtomElement).where(AtomElement.node_id == nodes[0].id))
        atom.content = "只有元件包含的搜索词"
        node_id = nodes[0].id
        category = TaxonomyCategory(name="测试分类", root="sop", canonical_key="test-category")
        session.add(category)
        session.flush()
        category_id = category.id
        session.add(NodeCategory(node_id=node_id, category_id=category_id))
    with TestClient(create_app(rt), base_url="http://127.0.0.1:8010") as client:
        assert client.get("/library").status_code == 200
        guide = client.get("/guide")
        assert guide.status_code == 200
        assert "paoding_fetch_atoms" in guide.text
        assert "127.0.0.1:8010/api/v1" in guide.text
        assert 'href="/guide"' in client.get("/library").text
        assert 'href="/guide"' in client.get("/settings").text
        page = client.get("/api/v1/library", params={"limit": 1}).json()
        assert page["total"] == 2 and len(page["items"]) == 1
        assert "youtube" in page["platforms"]
        assert client.get("/api/v1/library", params={"q": "独特标题"}).json()["total"] == 1
        assert (
            client.get("/api/v1/library", params={"q": "只有元件包含的搜索词"}).json()["total"] == 1
        )
        assert client.get("/api/v1/library", params={"q": "%"}).json()["total"] == 0
        assert client.get("/api/v1/library", params={"platform": "youtube"}).json()["total"] == 1
        assert client.get("/api/v1/library", params={"modality": "video"}).json()["total"] == 0
        assert client.get("/api/v1/library", params={"atom_type": "sop"}).json()["total"] == 2
        assert client.get("/api/v1/library", params={"category": category_id}).json()["total"] == 1
        assert client.get("/api/v1/library", params={"atom_type": "invalid"}).status_code == 422
        detail = client.get("/api/v1/nodes/" + node_id).json()
        assert detail["cross_domain_dna"]["universal_principle"]
        assert detail["atoms"] and detail["platform"] == "youtube"
        tracked = client.get("/api/v1/jobs/" + nodes[0].job_id).json()
        assert tracked["title"] == source.name
        assert tracked["status"] == "succeeded" and tracked["result"]["node_id"] == node_id


@pytest.mark.parametrize("page", ["library", "capture", "guide", "settings"])
def test_workspace_pages_share_navigation_and_load_styles(rt, page):
    rt.settings.port = 8128
    with TestClient(create_app(rt), base_url="http://127.0.0.1:8010") as client:
        response = client.get(f"/{page}")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "__PAODING_" not in response.text
        document = BeautifulSoup(response.text, "html.parser")
        if page == "guide":
            assert "127.0.0.1:8128/api/v1/library" in document.select_one("#curl-code").get_text()
        navigation = document.select_one(".workspace-nav")
        assert [a["href"] for a in navigation.select("a")] == [
            "/library",
            "/capture",
            "/guide",
            "/settings",
        ]
        assert [a["href"] for a in navigation.select('[aria-current="page"]')] == [f"/{page}"]
        assert document.select_one("#main-content") is not None
        stylesheet = client.get(document.select_one('link[rel="stylesheet"]')["href"])
        assert stylesheet.status_code == 200
        assert stylesheet.headers["Content-Type"].startswith("text/css")
