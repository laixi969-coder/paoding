import json

import httpx
import pytest
import yaml
from pydantic import ValidationError

from paoding.domain.schemas import NormalizedContent, StrategyAnalysis, VideoAnalysis
from paoding.models.demo import demo_analysis
from paoding.models.gateway import ModelGateway


def text_content():
    return NormalizedContent(
        source_url="https://example.com",
        platform="web",
        title="标题",
        modality="text",
        text="事实材料",
    )


def configure(rt, tmp_path, monkeypatch):
    path = tmp_path / "models.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "first": {
                        "protocol": "openai",
                        "base_url": "https://first.example/v1",
                        "api_key_env": "TEST_KEY",
                    },
                    "second": {
                        "protocol": "openai",
                        "base_url": "https://second.example/v1",
                        "api_key_env": "TEST_KEY",
                    },
                },
                "routes": {
                    "text": [
                        {"provider": "first", "model": "a"},
                        {"provider": "second", "model": "b"},
                    ]
                },
            }
        )
    )
    rt.settings.models_file, rt.settings.demo_mode = path, False
    monkeypatch.setenv("TEST_KEY", "not-a-real-key")


@pytest.mark.parametrize("failure", ["http", "schema"])
def test_failover_on_http_and_invalid_structured_output(rt, tmp_path, monkeypatch, failure):
    configure(rt, tmp_path, monkeypatch)
    calls = []

    def handle(request):
        calls.append(request.url.host)
        if request.url.host == "first.example":
            if failure == "http":
                return httpx.Response(503, json={"error": "busy"})
            return httpx.Response(
                200, json={"choices": [{"message": {"content": '{"title":"invalid"}'}}]}
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(demo_analysis(text_content()))}}]},
        )

    gateway = ModelGateway(rt.settings, rt.db, httpx.MockTransport(handle))
    result, provenance = gateway.analyze(text_content())
    assert calls == ["first.example", "second.example"]
    assert result.cross_domain_dna.universal_principle
    assert result.evidence.basis == "source_text"
    assert provenance["provider"] == "second" and len(provenance["fallback_attempts"]) == 1
    assert not provenance["demo"]


def test_schema_rejects_missing_dna_and_impossible_beats():
    data = demo_analysis(text_content())
    del data["cross_domain_dna"]
    with pytest.raises(ValidationError):
        StrategyAnalysis.model_validate(data)
    video = text_content().model_copy(
        update={
            "modality": __import__("paoding.domain.schemas", fromlist=["Modality"]).Modality.VIDEO
        }
    )
    data = demo_analysis(video)
    data["beat_sheet"][0]["start_seconds"] = 2
    with pytest.raises(ValidationError):
        VideoAnalysis.model_validate(data)


def test_gemini_and_anthropic_protocol_payloads(rt, tmp_path, monkeypatch):
    from PIL import Image

    from paoding.models.gateway import ProviderConfig

    path = tmp_path / "image.jpg"
    Image.new("RGB", (8, 8), "red").save(path)
    requests = []

    def handle(request):
        requests.append((request, json.loads(request.content)))
        if "generateContent" in request.url.path:
            return httpx.Response(
                200, json={"candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}]}
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"ok":true}'}]})

    monkeypatch.setenv("TEST_KEY", "test")
    gateway = ModelGateway(rt.settings, rt.db, httpx.MockTransport(handle))
    for protocol in ["gemini", "anthropic"]:
        provider = ProviderConfig(
            protocol=protocol, base_url="https://model.example/v1", api_key_env="TEST_KEY"
        )
        assert json.loads(gateway.complete(provider, "model-id", "analyze", [path], "image"))["ok"]
    assert "inline_data" in requests[0][1]["contents"][0]["parts"][1]
    assert requests[1][1]["messages"][0]["content"][1]["source"]["media_type"] == "image/jpeg"
    assert requests[1][0].headers["x-api-key"] == "test"


def test_model_sync_discovers_without_promoting(rt, tmp_path, monkeypatch):
    configure(rt, tmp_path, monkeypatch)
    before = rt.settings.models_file.read_text()
    gateway = ModelGateway(
        rt.settings,
        rt.db,
        httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "new-model"}]})),
    )
    assert all(row["status"] == "synced" for row in gateway.sync())
    assert len(gateway.list_models()) == 2
    assert rt.settings.models_file.read_text() == before


def test_provider_probe_and_sync_use_selected_configured_url(rt, tmp_path, monkeypatch):
    configure(rt, tmp_path, monkeypatch)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "model-new"}, {"id": "model-old"}]})

    gateway = ModelGateway(rt.settings, rt.db, httpx.MockTransport(handle))
    assert gateway.probe("first")["health"] == "healthy"
    result = gateway.sync("first")
    assert result[0]["models"] == ["model-new", "model-old"]
    assert all(call.url.host == "first.example" for call in calls)
    assert all(call.headers["Authorization"] == "Bearer not-a-real-key" for call in calls)
    assert all(row["provider"] == "first" for row in gateway.list_models())
    assert gateway.config().routes["text"][0].model == "a"


def test_probe_reports_http_errors_without_exposing_credentials(rt, tmp_path, monkeypatch):
    configure(rt, tmp_path, monkeypatch)
    gateway = ModelGateway(
        rt.settings,
        rt.db,
        httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "secret"})),
    )
    result = gateway.probe("first")
    assert result["health"] == "unavailable" and result["reason"] == "HTTP 401"
    assert "secret" not in str(result)
