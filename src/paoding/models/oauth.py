"""Official browser authorization for OpenRouter and Sign in with ChatGPT."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
from filelock import FileLock

from paoding.core.errors import ConfigurationError, TransientError
from paoding.models.custom import ProviderInput, custom_key_env, save_custom
from paoding.storage.vault import atomic_write

OPENAI_ISSUER = "https://auth.openai.com"
OPENAI_TOKEN = OPENAI_ISSUER + "/api/accounts/oauth/token"
OPENAI_RESOURCE = "https://api.openai.com/v1"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GOOGLE_GEMINI_SCOPE = "https://www.googleapis.com/auth/generative-language.retriever"
GOOGLE_SCOPES = {GOOGLE_SCOPE, GOOGLE_GEMINI_SCOPE}


class OAuthManager:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport
        self.pending, self.mutex = {}, threading.Lock()

    @property
    def root(self):
        path = Path(self.settings.data_dir) / "oauth"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def client(self):
        return httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            transport=self.transport or httpx.HTTPTransport(retries=2),
            trust_env=False,
        )

    def host_id(self):
        path = self.root / "host-id"
        with FileLock(str(self.root / "host-id.lock")):
            if not path.exists():
                atomic_write(path, ("urn:uuid:" + str(uuid4())).encode())
            return path.read_text().strip()

    def account(self):
        path = self.root / "chatgpt-account.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def google_account(self):
        path = self.root / "google-account.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def start(self, provider, port):
        if provider not in {"openrouter", "chatgpt", "gemini_google"}:
            raise ValueError("此服务商尚未提供可用的网页登录授权")
        if provider == "openrouter" and os.getenv(custom_key_env("openrouter")):
            raise ConfigurationError("OpenRouter 系统环境变量会覆盖授权凭据，请先移除该环境变量")
        if provider == "gemini_google" and not all(
            [
                self.settings.google_oauth_client_id,
                self.settings.google_oauth_client_secret.get_secret_value(),
                self.settings.google_project_id,
            ]
        ):
            raise ConfigurationError(
                "请先填写 Google OAuth Client ID、Client Secret 和 Google Cloud 项目 ID"
            )
        state, browser_session, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(4))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        callback = f"http://127.0.0.1:{port}/auth/callback"
        account = (
            self.account()
            if provider == "chatgpt"
            else self.google_account()
            if provider == "gemini_google"
            else None
        )
        with self.mutex:
            now = time.monotonic()
            self.pending = {
                key: value for key, value in self.pending.items() if value["expires"] > now
            }
            self.pending[state] = {
                "provider": provider,
                "session": browser_session,
                "verifier": verifier,
                "nonce": nonce,
                "callback": callback,
                "expires": now + 600,
                "account": account,
            }
        if provider == "openrouter":
            params = {
                "callback_url": callback,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "key_label": "PaoDing",
            }
            return "https://openrouter.ai/auth?" + urlencode(params), browser_session
        if provider == "gemini_google":
            params = {
                "client_id": self.settings.google_oauth_client_id,
                "response_type": "code",
                "redirect_uri": callback,
                "scope": "openid email " + " ".join(sorted(GOOGLE_SCOPES)),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
            return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
                params
            ), browser_session
        params = {
            "client_id": account["client_id"] if account else "dynamic_agent_client",
            "ext_agent_host_id": self.host_id(),
            "response_type": "code",
            "redirect_uri": callback,
            "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
            "resource": OPENAI_RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if account:
            if account.get("id_token"):
                params["id_token_hint"] = account["id_token"]
        else:
            params["agent_name_hint"] = "PaoDing"
        return OPENAI_ISSUER + "/api/accounts/authorize?" + urlencode(params), browser_session

    def finish(self, state, session, code, client_id=None, error=None):
        with self.mutex:
            record = self.pending.get(state)
            if (
                not record
                or record["expires"] < time.monotonic()
                or not session
                or not hmac.compare_digest(record["session"], session)
            ):
                raise ValueError("授权已过期或状态不匹配，请重新发起")
            # Single-use even when the exchange fails, preventing replay.
            del self.pending[state]
        if error:
            raise ValueError("用户取消了授权")
        if not code or len(code) > 4096:
            raise ValueError("授权响应缺少有效授权码")
        if record["provider"] == "openrouter":
            with self.client() as client:
                response = client.post(
                    OPENROUTER_BASE + "/auth/keys",
                    json={
                        "code": code,
                        "code_verifier": record["verifier"],
                        "code_challenge_method": "S256",
                    },
                )
                response.raise_for_status()
                key = response.json().get("key")
            if not isinstance(key, str) or not key:
                raise ValueError("OpenRouter 未返回授权凭据")
            save_custom(
                self.settings,
                ProviderInput(
                    id="openrouter",
                    name="OpenRouter",
                    base_url=OPENROUTER_BASE,
                    api_key=key,
                    video_url_support=True,
                ),
            )
            return "openrouter"
        if record["provider"] == "gemini_google":
            return self.finish_google(record, code)
        previous = record["account"]
        if previous and client_id and not hmac.compare_digest(previous["client_id"], client_id):
            raise ValueError("授权返回了不匹配的客户端")
        issued_id = previous["client_id"] if previous else client_id
        if (
            not issued_id
            or issued_id == "dynamic_agent_client"
            or not issued_id.startswith("oaiapp_")
        ):
            raise ValueError("ChatGPT 未返回有效客户端标识")
        with self.client() as client:
            response = client.post(
                OPENAI_TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "client_id": issued_id,
                    "code": code,
                    "code_verifier": record["verifier"],
                    "redirect_uri": record["callback"],
                    "resource": OPENAI_RESOURCE,
                },
            )
            response.raise_for_status()
            tokens = response.json()
            jwks = client.get(OPENAI_ISSUER + "/.well-known/jwks.json")
            jwks.raise_for_status()
            keys = jwks.json()["keys"]
        token = tokens.get("id_token", "")
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in {"RS256", "ES256"}:
            raise ValueError("不支持的身份令牌算法")
        matches = [
            key
            for key in keys
            if key.get("kid") == header.get("kid")
            and key.get("alg", header["alg"]) == header["alg"]
        ]
        if len(matches) != 1:
            raise ValueError("无法验证 ChatGPT 身份签名")
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(matches[0]).key,
            algorithms=[header["alg"]],
            audience=issued_id,
            issuer=OPENAI_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "aud", "nonce"]},
        )
        if not hmac.compare_digest(str(claims.get("nonce", "")), record["nonce"]):
            raise ValueError("ChatGPT 身份验证 nonce 不匹配")
        if previous and previous["subject"] != claims["sub"]:
            raise ValueError("登录账号与原授权账号不一致")
        scopes = tokens.get("scope", "").split()
        if not {"chatgpt.tokens.use.direct", "resource.invoke"}.issubset(scopes):
            raise ValueError("尚未授予使用 ChatGPT 方案调用模型的权限")
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise ValueError("ChatGPT 未返回完整授权令牌")
        account = {
            "client_id": issued_id,
            "subject": claims["sub"],
            "email": claims.get("email", ""),
            "id_token": token,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
            "scopes": scopes,
        }
        with FileLock(str(self.root / "chatgpt-account.lock")):
            atomic_write(self.root / "chatgpt-account.json", json.dumps(account).encode())
        return "chatgpt"

    def finish_google(self, record, code):
        client_id = self.settings.google_oauth_client_id
        with self.client() as client:
            response = client.post(
                GOOGLE_TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": self.settings.google_oauth_client_secret.get_secret_value(),
                    "code": code,
                    "code_verifier": record["verifier"],
                    "redirect_uri": record["callback"],
                },
            )
            response.raise_for_status()
            tokens = response.json()
            jwks = client.get("https://www.googleapis.com/oauth2/v3/certs")
            jwks.raise_for_status()
            keys = jwks.json()["keys"]
        token = tokens.get("id_token", "")
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise ValueError("不支持的 Google 身份令牌算法")
        matches = [key for key in keys if key.get("kid") == header.get("kid")]
        if len(matches) != 1:
            raise ValueError("无法验证 Google 身份签名")
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(matches[0]).key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=["https://accounts.google.com", "accounts.google.com"],
            options={"require": ["exp", "iat", "iss", "sub", "aud", "nonce"]},
        )
        if not hmac.compare_digest(str(claims.get("nonce", "")), record["nonce"]):
            raise ValueError("Google 身份验证 nonce 不匹配")
        if record["account"] and record["account"]["subject"] != claims["sub"]:
            raise ValueError("登录的 Google 账号与原授权账号不一致")
        scopes = tokens.get("scope", "").split()
        if not GOOGLE_SCOPES.issubset(scopes):
            raise ValueError("尚未授予 Google Cloud 和 Gemini 模型调用权限")
        previous = record["account"]
        refresh_token = tokens.get("refresh_token")
        if (
            not refresh_token
            and previous
            and previous["subject"] == claims["sub"]
            and previous["client_id"] == client_id
            and previous["project_id"] == self.settings.google_project_id
            and GOOGLE_SCOPES.issubset(previous.get("scopes", []))
        ):
            refresh_token = previous.get("refresh_token")
        if not tokens.get("access_token") or not refresh_token:
            raise ValueError("Google 未返回完整授权令牌")
        account = {
            "client_id": client_id,
            "project_id": self.settings.google_project_id,
            "subject": claims["sub"],
            "email": claims.get("email", ""),
            "access_token": tokens["access_token"],
            "refresh_token": refresh_token,
            "scopes": scopes,
            "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
        }
        with FileLock(str(self.root / "google-account.lock")):
            atomic_write(self.root / "google-account.json", json.dumps(account).encode())
        return "gemini_google"

    def google_access(self):
        with FileLock(str(self.root / "google-account.lock")):
            account = self.google_account()
            if not account:
                return ""
            if not GOOGLE_SCOPES.issubset(account.get("scopes", [])):
                raise ConfigurationError("Gemini 授权权限不足，请重新授权")
            if (
                account["client_id"] != self.settings.google_oauth_client_id
                or account["project_id"] != self.settings.google_project_id
            ):
                raise ConfigurationError("Google 客户端或项目已变更，请重新授权")
            if time.time() < account["expires_at"] - 60:
                return account["access_token"]
            try:
                with self.client() as client:
                    response = client.post(
                        GOOGLE_TOKEN,
                        data={
                            "grant_type": "refresh_token",
                            "client_id": account["client_id"],
                            "client_secret": self.settings.google_oauth_client_secret.get_secret_value(),
                            "refresh_token": account["refresh_token"],
                        },
                    )
                    response.raise_for_status()
                    tokens = response.json()
                if not tokens.get("access_token"):
                    raise ConfigurationError("Google 授权需要重新登录")
                account.update(
                    access_token=tokens["access_token"],
                    expires_at=time.time() + int(tokens.get("expires_in", 3600)),
                )
                if tokens.get("refresh_token"):
                    account["refresh_token"] = tokens["refresh_token"]
                atomic_write(self.root / "google-account.json", json.dumps(account).encode())
                return account["access_token"]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {400, 401, 403}:
                    raise ConfigurationError("Google 授权已失效，请重新登录") from exc
                raise TransientError("Google 授权刷新暂时不可用") from exc
            except httpx.HTTPError as exc:
                raise TransientError("Google 授权刷新暂时不可用") from exc

    def chatgpt_access(self):
        with FileLock(str(self.root / "chatgpt-account.lock")):
            account = self.account()
            if not account:
                return ""
            if time.time() < account["expires_at"] - 60:
                return account["access_token"]
            try:
                with self.client() as client:
                    response = client.post(
                        OPENAI_TOKEN,
                        data={
                            "grant_type": "refresh_token",
                            "client_id": account["client_id"],
                            "refresh_token": account["refresh_token"],
                            "resource": OPENAI_RESOURCE,
                        },
                    )
                    response.raise_for_status()
                    tokens = response.json()
                if not tokens.get("access_token") or not tokens.get("refresh_token"):
                    raise ConfigurationError("ChatGPT 授权需要重新登录")
                account.update(
                    access_token=tokens["access_token"],
                    refresh_token=tokens["refresh_token"],
                    expires_at=time.time() + int(tokens.get("expires_in", 3600)),
                )
                if tokens.get("id_token"):
                    # Keep the previously verified ID token as identity hint; refresh response is not an identity reauthentication.
                    pass
                atomic_write(self.root / "chatgpt-account.json", json.dumps(account).encode())
                return account["access_token"]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {400, 401, 403}:
                    raise ConfigurationError("ChatGPT 授权已失效，请重新登录") from exc
                raise TransientError("ChatGPT 授权刷新暂时不可用") from exc
            except httpx.HTTPError as exc:
                raise TransientError("ChatGPT 授权刷新暂时不可用") from exc
