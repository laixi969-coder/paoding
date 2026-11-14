import base64
import hashlib
import hmac
import json
import os
import struct
import threading
import time
from pathlib import Path
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from defusedxml.ElementTree import fromstring

from paoding.core.errors import ConfigurationError, SourceError, TransientError
from paoding.integrations.http import ServiceHTTP


class WebhookAuthError(Exception):
    pass


def check_timestamp(value, max_age):
    try:
        if abs(time.time() - int(value)) > max_age:
            raise WebhookAuthError("Expired webhook timestamp")
    except (ValueError, TypeError) as exc:
        raise WebhookAuthError("Invalid webhook timestamp") from exc


def decrypt_cbc(key, iv, data, block_size=128):
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def verify_feishu(settings, raw_body, headers):
    key = settings.feishu_encrypt_key.get_secret_value()
    token = settings.feishu_verification_token.get_secret_value()
    try:
        body = json.loads(raw_body)
        if key:
            timestamp = headers.get("x-lark-request-timestamp", "")
            nonce = headers.get("x-lark-request-nonce", "")
            check_timestamp(timestamp, settings.webhook_max_age_seconds)
            signature = hashlib.sha256((timestamp + nonce + key).encode() + raw_body).hexdigest()
            if not hmac.compare_digest(signature, headers.get("x-lark-signature", "")):
                raise WebhookAuthError("Invalid Feishu signature")
            if "encrypt" not in body:
                raise WebhookAuthError("Encrypted Feishu event required")
            encrypted = base64.b64decode(body["encrypt"], validate=True)
            body = json.loads(
                decrypt_cbc(hashlib.sha256(key.encode()).digest(), encrypted[:16], encrypted[16:])
            )
        elif "encrypt" in body:
            raise WebhookAuthError("Feishu encrypt key is not configured")
        supplied = body.get("token") or body.get("header", {}).get("token", "")
        if not token or not hmac.compare_digest(token, str(supplied)):
            raise WebhookAuthError("Invalid Feishu verification token")
        return body
    except (ValueError, TypeError, KeyError) as exc:
        raise WebhookAuthError("Invalid encrypted Feishu event") from exc


class WeComCrypto:
    def __init__(self, settings):
        self.settings = settings
        self.token = settings.wecom_token.get_secret_value()
        self.corp_id = settings.wecom_corp_id
        try:
            self.key = base64.b64decode(
                settings.wecom_encoding_aes_key.get_secret_value() + "=", validate=True
            )
        except ValueError as exc:
            raise WebhookAuthError("WeCom EncodingAESKey is invalid") from exc
        if len(self.key) != 32 or not self.token or not self.corp_id:
            raise WebhookAuthError("WeCom callback credentials are not configured")

    def signature(self, timestamp, nonce, encrypted):
        return hashlib.sha1(
            "".join(sorted([self.token, str(timestamp), nonce, encrypted])).encode()
        ).hexdigest()

    def decrypt(self, encrypted, timestamp, nonce, signature):
        check_timestamp(timestamp, self.settings.webhook_max_age_seconds)
        if not hmac.compare_digest(self.signature(timestamp, nonce, encrypted), signature):
            raise WebhookAuthError("Invalid WeCom signature")
        try:
            clear = decrypt_cbc(
                self.key, self.key[:16], base64.b64decode(encrypted, validate=True), 256
            )
            length = struct.unpack("!I", clear[16:20])[0]
            if length > len(clear) - 20:
                raise ValueError("Bad message length")
            message, receiver = clear[20 : 20 + length], clear[20 + length :]
            if not hmac.compare_digest(receiver, self.corp_id.encode()):
                raise WebhookAuthError("Unexpected WeCom receiver")
            return message
        except (ValueError, struct.error) as exc:
            raise WebhookAuthError("Invalid WeCom ciphertext") from exc

    def encrypt(self, message, timestamp=None, nonce=None):
        timestamp, nonce = str(timestamp or int(time.time())), nonce or os.urandom(8).hex()
        data = os.urandom(16) + struct.pack("!I", len(message)) + message + self.corp_id.encode()
        padder = padding.PKCS7(256).padder()
        data = padder.update(data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).encryptor()
        encoded = base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode()
        root = Element("xml")
        for name, value in {
            "Encrypt": encoded,
            "MsgSignature": self.signature(timestamp, nonce, encoded),
            "TimeStamp": timestamp,
            "Nonce": nonce,
        }.items():
            SubElement(root, name).text = value
        return tostring(root, encoding="utf-8")

    def parse(self, raw, timestamp, nonce, signature):
        try:
            envelope = fromstring(raw)
            clear = self.decrypt(envelope.findtext("Encrypt", ""), timestamp, nonce, signature)
            root = fromstring(clear)
            result = {child.tag: child.text or "" for child in root}
            if self.settings.wecom_agent_id and result.get("AgentID") != str(
                self.settings.wecom_agent_id
            ):
                raise WebhookAuthError("Unexpected WeCom application")
            return result
        except WebhookAuthError:
            raise
        except Exception as exc:
            raise WebhookAuthError("Invalid WeCom XML envelope") from exc

    def acknowledgement(self, message, text):
        root = Element("xml")
        for name, value in {
            "ToUserName": message["FromUserName"],
            "FromUserName": self.corp_id,
            "CreateTime": str(int(time.time())),
            "MsgType": "text",
            "Content": text,
        }.items():
            SubElement(root, name).text = value
        return self.encrypt(tostring(root, encoding="utf-8"))


class BotGateway:
    def __init__(self, settings, http=None):
        self.settings, self.http = settings, http or ServiceHTTP(timeout=60)
        self.cache, self.lock = {}, threading.Lock()

    def token(self, provider):
        with self.lock:
            cached = self.cache.get(provider)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            if provider == "feishu":
                if (
                    not self.settings.feishu_app_id
                    or not self.settings.feishu_app_secret.get_secret_value()
                ):
                    raise ConfigurationError("Configure Feishu App ID and App Secret")
                result = self.http.json(
                    "POST",
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.settings.feishu_app_id,
                        "app_secret": self.settings.feishu_app_secret.get_secret_value(),
                    },
                )
                if result.get("code") != 0:
                    raise ConfigurationError("Feishu tenant token request failed")
                token, expires = result["tenant_access_token"], result["expire"]
            else:
                if (
                    not self.settings.wecom_corp_id
                    or not self.settings.wecom_corp_secret.get_secret_value()
                ):
                    raise ConfigurationError("Configure WeCom Corp ID and application Secret")
                result = self.http.json(
                    "GET",
                    "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                    params={
                        "corpid": self.settings.wecom_corp_id,
                        "corpsecret": self.settings.wecom_corp_secret.get_secret_value(),
                    },
                )
                if result.get("errcode", 0) != 0:
                    raise ConfigurationError("WeCom token request failed")
                token, expires = result["access_token"], result["expires_in"]
            self.cache[provider] = (token, time.monotonic() + max(1, expires - 120))
            return token

    def api(self, provider, method, path, **kwargs):
        for attempt in range(2):
            token = self.token(provider)
            if provider == "feishu":
                result = self.http.json(
                    method,
                    "https://open.feishu.cn/open-apis" + path,
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
                code = result.get("code", 0)
                expired = code in {99991663, 99991664, 99991665}
            else:
                result = self.http.json(
                    method,
                    "https://qyapi.weixin.qq.com/cgi-bin" + path,
                    params={"access_token": token, **kwargs.pop("params", {})},
                    **kwargs,
                )
                code = result.get("errcode", 0)
                expired = code in {40014, 42001}
            if expired and attempt == 0:
                self.cache.pop(provider, None)
                continue
            if code:
                raise TransientError(f"{provider} API returned error code {code}")
            if provider == "wecom" and result.get("invaliduser"):
                raise SourceError("WeCom recipient is outside the application's visible scope")
            return result

    def send(self, target, text, event_id, card=None):
        provider = target["provider"]
        if provider == "telegram":
            token = self.settings.telegram_bot_token.get_secret_value()
            if not token:
                raise ConfigurationError("Configure PAODING_TELEGRAM_BOT_TOKEN")
            result = self.http.json(
                "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": target["chat_id"], "text": text[:4000]},
            )
            if not result.get("ok"):
                raise TransientError("Telegram rejected notification")
        elif provider == "feishu":
            content = {"text": text[:12000]}
            msg_type = "text"
            if card:
                msg_type = "interactive"
                content = {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": card["title"][:100]},
                        "template": "green",
                    },
                    "elements": [
                        {"tag": "div", "text": {"tag": "plain_text", "content": text[:12000]}}
                    ],
                }
            self.api(
                "feishu",
                "POST",
                "/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json={
                    "receive_id": target["chat_id"],
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                    "uuid": hashlib.sha256(event_id.encode()).hexdigest()[:32],
                },
            )
        elif provider == "wecom":
            self.api(
                "wecom",
                "POST",
                "/message/send",
                json={
                    "touser": target["user_id"],
                    "msgtype": "text",
                    "agentid": self.settings.wecom_agent_id,
                    "text": {"content": text[:1800]},
                    "enable_duplicate_check": 1,
                    "duplicate_check_interval": 600,
                },
            )
        else:
            raise SourceError("Unknown notification provider")

    def download(self, provider, attachment, refresh=True):
        default_name = attachment.get("name") or {
            "image": "image.jpg",
            "audio": "audio.ogg",
            "video": "video.mp4",
        }.get(attachment.get("kind"), "document.pdf")
        if provider == "telegram":
            token = self.settings.telegram_bot_token.get_secret_value()
            if not token:
                raise ConfigurationError("Configure Telegram Bot token for file ingestion")
            result = self.http.json(
                "GET",
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": attachment["file_id"]},
            )
            if not result.get("ok"):
                raise SourceError("Telegram file lookup failed")
            path = result["result"]["file_path"]
            if ".." in Path(path).parts or path.startswith(("/", "http")):
                raise SourceError("Invalid Telegram file path")
            data, mime = self.http.download(
                f"https://api.telegram.org/file/bot{token}/{quote(path, safe='/')}",
                self.settings.max_download_bytes,
            )
            default_name = attachment.get("name") or Path(path).name
        elif provider == "feishu":
            path = f"/im/v1/messages/{quote(attachment['message_id'], safe='')}/resources/{quote(attachment['file_key'], safe='')}"
            data, mime = self.http.download(
                "https://open.feishu.cn/open-apis" + path,
                self.settings.max_download_bytes,
                headers={"Authorization": f"Bearer {self.token('feishu')}"},
                params={"type": "image" if attachment.get("kind") == "image" else "file"},
            )
        elif provider == "wecom":
            data, mime = self.http.download(
                "https://qyapi.weixin.qq.com/cgi-bin/media/get",
                self.settings.max_download_bytes,
                params={"access_token": self.token("wecom"), "media_id": attachment["media_id"]},
            )
        else:
            raise SourceError("Unknown attachment provider")
        if "json" in mime:
            status = json.loads(data)
            if provider == "wecom" and status.get("video_url"):
                from paoding.ingestion.http import SafeFetcher

                data, mime, _ = SafeFetcher(self.settings.max_download_bytes).fetch(
                    status["video_url"]
                )
            elif (
                refresh
                and provider in {"wecom", "feishu"}
                and (
                    status.get("errcode") in {40014, 42001}
                    or status.get("code") in {99991663, 99991664, 99991665}
                )
            ):
                self.cache.pop(provider, None)
                return self.download(provider, attachment, refresh=False)
            else:
                raise SourceError("Bot attachment endpoint returned an error instead of media")
        return data, mime, Path(default_name).name


def telegram_message(body):
    message = body.get("message") or body.get("channel_post") or {}
    chat_id, event_id = message.get("chat", {}).get("id"), body.get("update_id")
    if not isinstance(chat_id, int) or not isinstance(event_id, int):
        return None
    attachments = []
    if message.get("photo"):
        attachments.append({"file_id": message["photo"][-1]["file_id"], "kind": "image"})
    for field, kind in [
        ("document", "file"),
        ("video", "video"),
        ("audio", "audio"),
        ("voice", "audio"),
    ]:
        if message.get(field):
            item = message[field]
            attachments.append(
                {"file_id": item["file_id"], "kind": kind, "name": item.get("file_name")}
            )
    return {
        "event_id": str(event_id),
        "provider": "telegram",
        "text": message.get("text", message.get("caption", "")),
        "target": {"provider": "telegram", "chat_id": chat_id},
        "attachments": attachments,
    }


def feishu_message(body):
    message = body.get("event", {}).get("message", {})
    if not body.get("header", {}).get("event_id") or not message.get("chat_id"):
        return None
    content = json.loads(message.get("content", "{}"))
    kind = message.get("message_type")
    text, attachments = content.get("text", ""), []
    if kind == "post":
        post = content.get("zh_cn", content.get("en_us", content))
        text = post.get("title", "") + "\n"
        for line in post.get("content", []):
            for part in line:
                text += part.get("text", part.get("href", "")) + " "
                if part.get("tag") == "a" and part.get("href"):
                    text += part["href"] + " "
                if part.get("tag") == "img":
                    attachments.append(
                        {
                            "file_key": part["image_key"],
                            "kind": "image",
                            "message_id": message["message_id"],
                        }
                    )
    elif kind in {"image", "file", "audio", "media"}:
        key = content.get("image_key") or content.get("file_key")
        if key:
            attachments.append(
                {
                    "file_key": key,
                    "kind": "video" if kind == "media" else kind,
                    "message_id": message["message_id"],
                    "name": content.get("file_name"),
                }
            )
    return {
        "event_id": body["header"]["event_id"],
        "provider": "feishu",
        "text": text,
        "target": {"provider": "feishu", "chat_id": message["chat_id"]},
        "attachments": attachments,
    }


def wecom_message(message):
    if not message.get("MsgId") or not message.get("FromUserName"):
        return None
    kind = message.get("MsgType")
    attachments = []
    if kind in {"image", "voice", "video", "file"} and message.get("MediaId"):
        attachments.append(
            {
                "media_id": message["MediaId"],
                "kind": "audio" if kind == "voice" else kind,
                "name": message.get("FileName") or ("voice.amr" if kind == "voice" else None),
            }
        )
    text = message.get("Content", "")
    if kind == "link":
        text = message.get("Title", "") + "\n" + message.get("Url", "")
    return {
        "event_id": message["MsgId"],
        "provider": "wecom",
        "text": text,
        "target": {"provider": "wecom", "user_id": message["FromUserName"]},
        "attachments": attachments,
    }
