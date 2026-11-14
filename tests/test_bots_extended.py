import base64
import hashlib
import io
import json
import time
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import func, select

from paoding.api.app import create_app
from paoding.integrations.bots import BotGateway, WebhookAuthError, WeComCrypto
from paoding.integrations.http import ServiceHTTP
from paoding.services.worker import Worker
from paoding.storage.tables import Job, KnowledgeNode


def feishu_envelope(event, key, timestamp=None):
    iv = bytes(range(16))
    pad = padding.PKCS7(128).padder()
    content = json.dumps(event).encode()
    padded = pad.update(content) + pad.finalize()
    encryptor = Cipher(
        algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)
    ).encryptor()
    encrypted = iv + encryptor.update(padded) + encryptor.finalize()
    body = json.dumps({"encrypt": base64.b64encode(encrypted).decode()}).encode()
    stamp = str(timestamp or int(time.time()))
    nonce = "test-nonce"
    signature = hashlib.sha256((stamp + nonce + key).encode() + body).hexdigest()
    return body, {
        "X-Lark-Request-Timestamp": stamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature,
    }


def test_feishu_encrypted_challenge_and_signed_event_deduplication(rt):
    rt.settings.feishu_encrypt_key = SecretStr("encryption-key")
    rt.settings.feishu_verification_token = SecretStr("verification-token")
    with TestClient(create_app(rt)) as client:
        body, headers = feishu_envelope(
            {"type": "url_verification", "token": "verification-token", "challenge": "abc"},
            "encryption-key",
        )
        assert client.post("/api/v1/webhooks/feishu", content=body, headers=headers).json() == {
            "challenge": "abc"
        }
        event = {
            "header": {"event_id": "event-123", "token": "verification-token"},
            "event": {
                "message": {
                    "chat_id": "chat-1",
                    "message_id": "msg-1",
                    "message_type": "text",
                    "content": json.dumps({"text": "https://example.com"}),
                }
            },
        }
        body, headers = feishu_envelope(event, "encryption-key")
        assert (
            client.post("/api/v1/webhooks/feishu", content=body, headers=headers).status_code == 200
        )
        assert (
            client.post("/api/v1/webhooks/feishu", content=body, headers=headers).status_code == 200
        )
        with rt.db.transaction() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(Job).where(Job.kind == "bot_ingest")
                )
                == 1
            )
            assert (
                session.scalar(select(func.count()).select_from(Job).where(Job.kind == "notify"))
                == 1
            )


@pytest.mark.parametrize("case", ["signature", "expired", "token", "plaintext"])
def test_feishu_rejects_invalid_callback(rt, case):
    rt.settings.feishu_encrypt_key = SecretStr("key")
    rt.settings.feishu_verification_token = SecretStr("token")
    event = {
        "token": "bad" if case == "token" else "token",
        "type": "url_verification",
        "challenge": "x",
    }
    body, headers = feishu_envelope(
        event, "key", int(time.time()) - 900 if case == "expired" else None
    )
    if case == "signature":
        headers["X-Lark-Signature"] = "0" * 64
    if case == "plaintext":
        body = json.dumps(event).encode()
    with TestClient(create_app(rt)) as client:
        assert (
            client.post("/api/v1/webhooks/feishu", content=body, headers=headers).status_code == 401
        )


def configure_wecom(rt):
    rt.settings.wecom_corp_id = "corp-123"
    rt.settings.wecom_agent_id = 100
    rt.settings.wecom_token = SecretStr("callback-token")
    rt.settings.wecom_encoding_aes_key = SecretStr(
        base64.b64encode(bytes(range(32))).decode().rstrip("=")
    )
    return WeComCrypto(rt.settings)


def test_wecom_handshake_and_encrypted_ack(rt):
    crypto = configure_wecom(rt)
    echo = fromstring(crypto.encrypt(b"handshake"))
    params = {
        "timestamp": echo.findtext("TimeStamp"),
        "nonce": echo.findtext("Nonce"),
        "msg_signature": echo.findtext("MsgSignature"),
        "echostr": echo.findtext("Encrypt"),
    }
    with TestClient(create_app(rt)) as client:
        assert client.get("/api/v1/webhooks/wecom", params=params).text == "handshake"
        message = b"<xml><ToUserName>corp-123</ToUserName><FromUserName>user-1</FromUserName><AgentID>100</AgentID><MsgId>m-1</MsgId><MsgType>text</MsgType><Content>https://example.com</Content></xml>"
        body = crypto.encrypt(message)
        envelope = fromstring(body)
        params = {
            "timestamp": envelope.findtext("TimeStamp"),
            "nonce": envelope.findtext("Nonce"),
            "msg_signature": envelope.findtext("MsgSignature"),
        }
        result = client.post("/api/v1/webhooks/wecom", params=params, content=body)
        assert result.status_code == 200
        ack = fromstring(result.content)
        clear = crypto.decrypt(
            ack.findtext("Encrypt"),
            ack.findtext("TimeStamp"),
            ack.findtext("Nonce"),
            ack.findtext("MsgSignature"),
        )
        assert "已接单" in clear.decode()
        assert client.post("/api/v1/webhooks/wecom", params=params, content=body).status_code == 200
    with rt.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_wecom_wrong_receiver_and_replay_are_rejected(rt):
    crypto = configure_wecom(rt)
    envelope = fromstring(crypto.encrypt(b"example"))
    encrypted = envelope.findtext("Encrypt")
    crypto.corp_id = "different-corp"
    with pytest.raises(WebhookAuthError, match="receiver"):
        crypto.decrypt(
            encrypted,
            envelope.findtext("TimeStamp"),
            envelope.findtext("Nonce"),
            envelope.findtext("MsgSignature"),
        )
    with pytest.raises(WebhookAuthError, match="Expired"):
        crypto.decrypt(encrypted, "1", "n", crypto.signature("1", "n", encrypted))


def test_telegram_image_to_vault_and_completion_notification(rt):
    rt.settings.telegram_secret = SecretStr("incoming")
    rt.settings.telegram_bot_token = SecretStr("bot-secret")
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(buffer, "JPEG")
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "photos/image.jpg"}}
            )
        if "/file/" in request.url.path:
            return httpx.Response(
                200, content=buffer.getvalue(), headers={"content-type": "image/jpeg"}
            )
        return httpx.Response(200, json={"ok": True})

    rt.bots = BotGateway(rt.settings, ServiceHTTP(transport=httpx.MockTransport(handle)))
    body = {
        "update_id": 42,
        "message": {
            "chat": {"id": 11},
            "photo": [{"file_id": "small"}, {"file_id": "large"}],
            "caption": "拆配色",
        },
    }
    with TestClient(create_app(rt)) as client:
        response = client.post(
            "/api/v1/webhooks/telegram",
            json=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": "incoming"},
        )
        assert response.status_code == 200
        worker = Worker(rt)
        while worker.run_once():
            pass
        with rt.db.transaction() as session:
            node = session.scalar(select(KnowledgeNode))
            assert node.modality == "image" and node.vault_status == "synced"
            assert all(job.status == "succeeded" for job in session.scalars(select(Job)))
            assert "bot-secret" not in json.dumps(
                [job.payload for job in session.scalars(select(Job))]
            )
    assert len([c for c in calls if c.url.path.endswith("sendMessage")]) == 1
    assert calls[0].url.params["file_id"] == "large"


def test_feishu_notification_card_and_expired_token_refresh(rt):
    rt.settings.feishu_app_id, rt.settings.feishu_app_secret = "app", SecretStr("secret")
    token_calls, sends = [], []

    def handle(request):
        if "tenant_access_token" in request.url.path:
            token_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"token-{len(token_calls)}",
                    "expire": 7200,
                },
            )
        sends.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 99991663 if len(sends) == 1 else 0})

    bots = BotGateway(rt.settings, ServiceHTTP(transport=httpx.MockTransport(handle)))
    bots.send(
        {"provider": "feishu", "chat_id": "chat"}, "已完成", "event-id", {"title": "分析结果"}
    )
    assert len(token_calls) == 2
    assert sends[0]["uuid"] == sends[1]["uuid"]
    assert sends[1]["msg_type"] == "interactive"


def test_bot_plain_text_and_failure_notification(rt, monkeypatch):
    rt.settings.demo_mode = False
    monkeypatch.setattr(rt.gateway, "key", lambda p: "")
    sent = []
    monkeypatch.setattr(rt.bots, "send", lambda *args: sent.append(args))
    rt.accept_bot(
        {
            "provider": "telegram",
            "event_id": "no-key",
            "text": "有价值的文章正文",
            "attachments": [],
            "target": {"provider": "telegram", "chat_id": 1},
        }
    )
    worker = Worker(rt)
    while worker.run_once():
        pass
    assert len(sent) == 1 and "configuration_required" in sent[0][1]
