import base64
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from conftest import assert_private_file
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr

from paoding.api.app import create_app
from paoding.models.gateway import ModelGateway
from paoding.models.oauth import OAuthManager


def test_custom_provider_add_manual_model_select_and_test_one(rt, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "google/gemini-pro"}]})
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "OK"}}], "model": body["model"]}
        )

    rt.gateway.transport = httpx.MockTransport(handle)
    with TestClient(create_app(rt), base_url="http://127.0.0.1:8010") as client:
        invalid = client.post(
            "/api/v1/models/providers",
            json={
                "id": "bad",
                "name": "Bad",
                "base_url": "http://example.com/v1",
                "api_key": "secret",
            },
        )
        assert invalid.status_code == 422
        response = client.post(
            "/api/v1/models/providers",
            json={
                "id": "openrouter",
                "name": "OpenRouter",
                "base_url": "https://openrouter.example/api/v1",
                "api_key": "secret-key",
            },
        )
        assert response.status_code == 200 and response.json()["configured"]
        assert "secret-key" not in client.get("/api/v1/models/providers").text
        assert_private_file(Path(".env"))
        assert (
            client.post(
                "/api/v1/models/providers",
                json={"id": "qwen", "name": "bad", "base_url": "https://example.com"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/models/manual",
                json={"provider": "openrouter", "model": "openai/gpt-x:free"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/models/manual", json={"provider": "openrouter", "model": "another-model"}
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/v1/settings", json={"PAODING_TEXT_ROUTE": "openrouter:openai/gpt-x:free"}
            ).status_code
            == 200
        )
        assert rt.gateway.config().routes["text"][0].model == "openai/gpt-x:free"
        response = client.post(
            "/api/v1/models/test-one", json={"provider": "openrouter", "model": "openai/gpt-x:free"}
        )
        assert response.json()["health"] == "healthy"
        assert len(calls) == 1 and json.loads(calls[0].content)["model"] == "openai/gpt-x:free"
        assert calls[0].headers["authorization"] == "Bearer secret-key"
        response = client.post("/api/v1/models/sync?provider=openrouter")
        assert response.json()[0]["models"] == ["google/gemini-pro"]
        assert len(client.get("/api/v1/models?provider=openrouter").json()) == 3
        assert rt.gateway.config().routes["text"][0].model == "openai/gpt-x:free"


def test_openrouter_pkce_one_time_state_and_private_key_storage(rt, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={"key": "sk-or-test-secret"})

    manager = OAuthManager(rt.settings, httpx.MockTransport(handle))
    url, cookie = manager.start("openrouter", 8010)
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).hostname == "openrouter.ai"
    assert query["code_challenge_method"] == ["S256"]
    state = query["state"][0]
    with pytest.raises(ValueError):
        manager.finish(state, "wrong-cookie", "code")
    assert manager.finish(state, cookie, "code") == "openrouter"
    body = json.loads(sent[0].content)
    assert (
        base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
        == query["code_challenge"][0]
    )
    assert "sk-or-test-secret" not in (rt.settings.data_dir / "custom_providers.json").read_text(
        encoding="utf-8"
    )
    assert "sk-or-test-secret" in Path(".env").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        manager.finish(state, cookie, "code")


def test_chatgpt_official_login_validation_and_single_model_stream(rt, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    public.update(kid="test-key", alg="RS256", use="sig")
    state_data = {}

    def auth_request(request):
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [public]})
        token = jwt.encode(
            {
                "iss": "https://auth.openai.com",
                "sub": "user-1",
                "aud": "oaiapp_123",
                "nonce": state_data["nonce"],
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            private,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        return httpx.Response(
            200,
            json={
                "id_token": token,
                "access_token": "access-test",
                "refresh_token": "refresh-test",
                "expires_in": 3600,
                "scope": "openid resource.invoke chatgpt.tokens.use.direct offline_access",
            },
        )

    manager = OAuthManager(rt.settings, httpx.MockTransport(auth_request))
    url, cookie = manager.start("chatgpt", 8010)
    query = parse_qs(urlsplit(url).query)
    assert query["client_id"] == ["dynamic_agent_client"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8010/auth/callback"]
    state_data["nonce"] = query["nonce"][0]
    assert manager.finish(query["state"][0], cookie, "code", "oaiapp_123") == "chatgpt"
    assert manager.chatgpt_access() == "access-test"
    assert_private_file(rt.settings.data_dir / "oauth" / "chatgpt-account.json")
    calls = []

    def inference(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "gpt-test", "visibility": "list"},
                        {"slug": "hidden", "visibility": "hide"},
                    ]
                },
            )
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-test" and payload["stream"] and not payload["store"]
        return httpx.Response(
            200,
            text='data: {"type":"response.output_text.delta","delta":"OK"}\n\ndata: {"type":"response.completed"}\n\n',
        )

    gateway = ModelGateway(rt.settings, rt.db, httpx.MockTransport(inference))
    assert gateway.sync("chatgpt")[0]["models"] == ["gpt-test"]
    assert gateway.test_one("chatgpt", "gpt-test")["health"] == "healthy"
    assert len([r for r in calls if r.url.path.endswith("/responses")]) == 1


def test_chatgpt_declined_scopes_never_save_tokens(rt, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update(kid="kid", alg="RS256")
    nonce = {}

    def respond(request):
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [jwk]})
        id_token = jwt.encode(
            {
                "iss": "https://auth.openai.com",
                "sub": "u",
                "aud": "oaiapp_x",
                "nonce": nonce["value"],
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
            },
            private,
            algorithm="RS256",
            headers={"kid": "kid"},
        )
        return httpx.Response(
            200,
            json={
                "id_token": id_token,
                "access_token": "access",
                "refresh_token": "refresh",
                "scope": "openid",
            },
        )

    manager = OAuthManager(rt.settings, httpx.MockTransport(respond))
    url, cookie = manager.start("chatgpt", 8010)
    query = parse_qs(urlsplit(url).query)
    nonce["value"] = query["nonce"][0]
    with pytest.raises(ValueError, match="尚未授予"):
        manager.finish(query["state"][0], cookie, "code", "oaiapp_x")
    assert manager.account() is None


def test_oauth_start_requires_local_origin_and_preserves_pkce_cookie(rt):
    with TestClient(create_app(rt), base_url="http://127.0.0.1:8010") as client:
        assert client.post("/api/v1/oauth/openrouter/start").status_code == 415
        assert (
            client.post(
                "/api/v1/oauth/openrouter/start",
                json={},
                headers={"Origin": "https://attacker.example"},
            ).status_code
            == 403
        )
        response = client.post("/api/v1/oauth/openrouter/start", json={})
        assert response.status_code == 200 and "HttpOnly" in response.headers["set-cookie"]
        assert urlsplit(response.json()["authorization_url"]).hostname == "openrouter.ai"


def test_openrouter_callback_links_account_without_returning_key(rt, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"key": "sensitive-test-key"})
    )
    monkeypatch.setattr(OAuthManager, "client", lambda self: httpx.Client(transport=transport))
    with TestClient(create_app(rt), base_url="http://127.0.0.1:8010") as client:
        response = client.post("/api/v1/oauth/openrouter/start", json={})
        state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
        callback = client.get("/auth/callback", params={"state": state, "code": "auth-code"})
        assert callback.status_code == 200
        assert "sensitive-test-key" not in callback.text
        assert "oauth=openrouter" in callback.text
        listing = client.get("/api/v1/models/providers").json()
        assert listing["providers"][0]["id"] == "openrouter"
        assert listing["providers"][0]["configured"]
        assert "sensitive-test-key" not in json.dumps(listing)


def test_oauth_callback_failure_logging_does_not_expose_secrets(rt, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise ValueError("sensitive-auth-code")

    monkeypatch.setattr(OAuthManager, "finish", fail)
    with TestClient(create_app(rt), base_url="http://127.0.0.1:8010") as client:
        response = client.get("/auth/callback", params={"state": "secret-state", "code": "code"})
    assert "oauth=failed" in response.text
    assert "type=ValueError" in caplog.text
    assert "sensitive-auth-code" not in caplog.text
    assert "secret-state" not in caplog.text


def test_chatgpt_refresh_rotates_token_without_changing_identity(rt):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        )

    manager = OAuthManager(rt.settings, httpx.MockTransport(respond))
    account = {
        "client_id": "oaiapp_existing",
        "subject": "user-1",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "id_token": "verified-earlier",
        "expires_at": 0,
    }
    (manager.root / "chatgpt-account.json").write_text(json.dumps(account))
    assert manager.chatgpt_access() == "new-access"
    assert manager.chatgpt_access() == "new-access" and len(calls) == 1
    assert manager.account()["refresh_token"] == "new-refresh"
    assert_private_file(rt.settings.data_dir / "oauth" / "chatgpt-account.json")
    assert manager.account()["subject"] == "user-1"


def test_google_oauth_and_gemini_model_use_bearer_project_header(rt, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rt.settings.google_oauth_client_id = "client.apps.googleusercontent.com"
    rt.settings.google_oauth_client_secret = SecretStr("google-client-secret")
    rt.settings.google_project_id = "my-cloud-project"
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update(kid="google-test", alg="RS256")
    nonce = {}
    granted_scopes = ["openid email https://www.googleapis.com/auth/cloud-platform"]

    def auth_request(request):
        if request.url.path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [jwk]})
        assert request.url.host == "oauth2.googleapis.com"
        token = jwt.encode(
            {
                "iss": "https://accounts.google.com",
                "sub": "google-user",
                "aud": rt.settings.google_oauth_client_id,
                "nonce": nonce["value"],
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            private,
            algorithm="RS256",
            headers={"kid": "google-test"},
        )
        return httpx.Response(
            200,
            json={
                "id_token": token,
                "access_token": "google-access",
                "refresh_token": "google-refresh",
                "expires_in": 3600,
                "scope": granted_scopes[0],
            },
        )

    manager = OAuthManager(rt.settings, httpx.MockTransport(auth_request))
    url, cookie = manager.start("gemini_google", 8010)
    params = parse_qs(urlsplit(url).query)
    assert urlsplit(url).hostname == "accounts.google.com"
    assert params["client_id"] == ["client.apps.googleusercontent.com"]
    assert "https://www.googleapis.com/auth/cloud-platform" in params["scope"][0]
    assert "https://www.googleapis.com/auth/generative-language.retriever" in params["scope"][0]
    nonce["value"] = params["nonce"][0]
    with pytest.raises(ValueError, match="Gemini"):
        manager.finish(params["state"][0], cookie, "google-code")
    assert manager.google_account() is None
    granted_scopes[0] += " https://www.googleapis.com/auth/generative-language.retriever"
    url, cookie = manager.start("gemini_google", 8010)
    params = parse_qs(urlsplit(url).query)
    nonce["value"] = params["nonce"][0]
    assert manager.finish(params["state"][0], cookie, "google-code") == "gemini_google"
    assert manager.google_access() == "google-access"
    assert_private_file(rt.settings.data_dir / "oauth" / "google-account.json")
    requests = []

    def gemini(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer google-access"
        assert request.headers["x-goog-user-project"] == "my-cloud-project"
        assert "x-goog-api-key" not in request.headers
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-test"}]})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})

    gateway = ModelGateway(rt.settings, rt.db, httpx.MockTransport(gemini))
    assert gateway.sync("gemini_google")[0]["models"] == ["gemini-test"]
    assert gateway.test_one("gemini_google", "gemini-test")["health"] == "healthy"
    assert all(r.url.host == "generativelanguage.googleapis.com" for r in requests)
    rt.settings.google_project_id = "another-project"
    from paoding.core.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="项目已变更"):
        manager.google_access()


def test_google_oauth_requires_cloud_client_configuration(rt):
    manager = OAuthManager(rt.settings)
    from paoding.core.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="Client ID"):
        manager.start("gemini_google", 8010)
