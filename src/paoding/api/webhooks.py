import hmac

from fastapi import APIRouter, HTTPException, Request, Response

from paoding.integrations.bots import (
    WebhookAuthError,
    WeComCrypto,
    feishu_message,
    telegram_message,
    verify_feishu,
    wecom_message,
)


async def read_body(request):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 2 * 1024 * 1024:
            raise HTTPException(413, "Webhook envelope exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def create_webhooks(runtime):
    router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

    @router.post("/telegram")
    async def telegram(request: Request):
        import json

        secret = runtime.settings.telegram_secret.get_secret_value()
        if not secret or not hmac.compare_digest(
            secret, request.headers.get("x-telegram-bot-api-secret-token", "")
        ):
            raise HTTPException(401, "Invalid Telegram webhook secret")
        try:
            message = telegram_message(json.loads(await read_body(request)))
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, "Invalid Telegram message") from exc
        if not message or (not message["text"] and not message["attachments"]):
            return {"ok": True}
        job_id = runtime.accept_bot(message)
        return {
            "method": "sendMessage",
            "chat_id": message["target"]["chat_id"],
            "text": f"🔪 庖丁已接单\n任务：{job_id}",
        }

    @router.post("/feishu")
    async def feishu(request: Request):
        try:
            event = verify_feishu(runtime.settings, await read_body(request), request.headers)
        except WebhookAuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        if event.get("type") == "url_verification":
            return {"challenge": event.get("challenge")}
        try:
            message = feishu_message(event)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, "Invalid Feishu message") from exc
        if message and (message["text"] or message["attachments"]):
            runtime.accept_bot(message, queued_ack=True)
        return {"code": 0}

    @router.get("/wecom")
    def wecom_verify(msg_signature: str, timestamp: str, nonce: str, echostr: str):
        try:
            plain = WeComCrypto(runtime.settings).decrypt(echostr, timestamp, nonce, msg_signature)
            return Response(plain, media_type="text/plain")
        except WebhookAuthError as exc:
            raise HTTPException(401, str(exc)) from exc

    @router.post("/wecom")
    async def wecom(request: Request, msg_signature: str, timestamp: str, nonce: str):
        try:
            crypto = WeComCrypto(runtime.settings)
            event = crypto.parse(await read_body(request), timestamp, nonce, msg_signature)
        except WebhookAuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        message = wecom_message(event)
        if not message or (not message["text"] and not message["attachments"]):
            return Response("success", media_type="text/plain")
        job_id = runtime.accept_bot(message)
        return Response(
            crypto.acknowledgement(event, f"🔪 庖丁已接单\n任务：{job_id}"),
            media_type="application/xml",
        )

    return router
