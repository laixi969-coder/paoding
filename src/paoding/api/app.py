import hmac
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from paoding import __version__
from paoding.core.errors import ConfigurationError, Conflict, PaodingError
from paoding.domain.schemas import FeedbackRequest, IngestRequest
from paoding.services.runtime import Runtime
from paoding.storage.tables import AtomElement, EvolutionLog, Job, KnowledgeNode, WatchedAccount

logger = logging.getLogger(__name__)


def create_app(runtime=None):
    rt = runtime or Runtime()
    from paoding.models.oauth import OAuthManager

    oauth = OAuthManager(rt.settings)

    @asynccontextmanager
    async def lifespan(app):
        rt.settings.prepare_directories()
        yield
        rt.db.engine.dispose()

    app = FastAPI(
        title="庖丁 PaoDing Engine",
        version=__version__,
        lifespan=lifespan,
        description="Local-first 多模态知识引擎。异步任务状态与 Vault 同步状态独立可观测。",
    )
    app.state.runtime = rt

    @app.get("/capture", include_in_schema=False)
    def capture():
        from pathlib import Path

        from fastapi.responses import HTMLResponse

        html = Path(__file__).with_name("capture.html").read_text(encoding="utf-8")
        html = html.replace(
            "__PAODING_AUTH_REQUIRED__",
            "true" if rt.settings.api_token.get_secret_value() else "false",
        )
        return HTMLResponse(
            html,
            headers={"Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"},
        )

    @app.get("/settings", include_in_schema=False)
    def settings_page(request: Request):
        from pathlib import Path

        from fastapi.responses import HTMLResponse

        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(403, "设置页面仅限本机访问")
        return HTMLResponse(
            Path(__file__).with_name("settings.html").read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.exception_handler(PaodingError)
    async def domain_error(request, exc):
        status = (
            409
            if isinstance(exc, Conflict)
            else 503
            if exc.retryable or isinstance(exc, ConfigurationError)
            else 422
        )
        return JSONResponse(
            status_code=status, content={"error": {"code": exc.code, "message": str(exc)}}
        )

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse(
            status_code=404, content={"error": {"code": "not_found", "message": str(exc)}}
        )

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(
            status_code=422, content={"error": {"code": "invalid_input", "message": str(exc)}}
        )

    def authorize(authorization: Annotated[str | None, Header()] = None):
        token = rt.settings.api_token.get_secret_value()
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "Bearer token required")

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(authorize)])

    @router.get("/settings", tags=["settings"])
    def settings_get(request: Request):
        from paoding.core.settings_panel import metadata

        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(403, "设置接口仅限本机访问")
        return {"fields": metadata()}

    @router.put("/settings", tags=["settings"])
    def settings_put(request: Request, body: dict[str, str]):
        from paoding.core.settings_panel import FIELDS, refresh_settings, update_values

        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(403, "设置接口仅限本机访问")
        if len(body) > len(FIELDS):
            raise HTTPException(422, "配置项过多")
        update_values(body, rt.settings)
        refresh_settings(rt.settings)
        rt.bots.cache.clear()
        return {"saved": True}

    @app.get("/health/live", tags=["health"])
    def live():
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    def ready():
        try:
            with rt.db.transaction() as session:
                session.execute(text("SELECT 1 FROM paoding_jobs LIMIT 1"))
        except SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "database_unavailable_or_unmigrated"},
            )
        return {"status": "ready", "demo": rt.settings.demo_mode}

    @router.post("/ingest", status_code=202, tags=["ingest"])
    def ingest(
        body: IngestRequest, idempotency_key: Annotated[str | None, Header(max_length=200)] = None
    ):
        return rt.ingest(body, idempotency_key)

    @router.get("/jobs/{job_id}", tags=["jobs"])
    def job_status(job_id: str):
        with rt.db.transaction() as session:
            job = session.get(Job, job_id)
            if not job:
                raise LookupError("Job not found")
            return {
                "id": job.id,
                "kind": job.kind,
                "status": job.status,
                "attempts": job.attempts,
                "result": job.result,
                "error": {"code": job.error_code, "message": job.error_message}
                if job.error_code
                else None,
                "created_at": job.created_at.isoformat() + "Z",
            }

    @router.get("/jobs", tags=["jobs"])
    def jobs(
        status: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        with rt.db.transaction() as session:
            stmt = select(Job).order_by(Job.created_at.desc())
            if status:
                stmt = stmt.where(Job.status == status)
            return [
                {
                    "id": j.id,
                    "kind": j.kind,
                    "status": j.status,
                    "attempts": j.attempts,
                    "error_code": j.error_code,
                }
                for j in session.scalars(stmt.limit(limit).offset(offset))
            ]

    @router.post("/jobs/{job_id}/retry", status_code=202, tags=["jobs"])
    def retry(job_id: str):
        job = rt.queue.retry(job_id)
        return {"id": job.id, "status": job.status}

    @router.get("/nodes", tags=["knowledge"])
    def nodes(
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        platform: str | None = None,
    ):
        with rt.db.transaction() as session:
            stmt = select(KnowledgeNode).order_by(KnowledgeNode.created_at.desc())
            if platform:
                stmt = stmt.where(KnowledgeNode.platform == platform)
            return [
                {
                    "id": n.id,
                    "title": n.title,
                    "platform": n.platform,
                    "modality": n.modality,
                    "summary": n.summary,
                    "vault_status": n.vault_status,
                    "demo": n.provenance.get("demo", False),
                }
                for n in session.scalars(stmt.offset(offset).limit(limit))
            ]

    @router.get("/nodes/{node_id}", tags=["knowledge"])
    def node(node_id: str):
        with rt.db.transaction() as session:
            row = session.get(KnowledgeNode, node_id)
            if not row:
                raise LookupError("Node not found")
            return {
                "id": row.id,
                "title": row.title,
                "source_url": row.source_url,
                "analysis": row.analysis,
                "provenance": row.provenance,
                "vault_status": row.vault_status,
                "vault_path": row.vault_path,
            }

    @router.get("/atoms", tags=["knowledge"])
    def atoms(
        atom_type: str | None = None,
        tags: list[str] = Query(default=[]),
        target_platform: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        q: str | None = None,
        category: str | None = None,
    ):
        return rt.library.fetch_atoms(atom_type, tags, target_platform, limit, q, category)

    @router.get("/taxonomy", tags=["evolution"])
    def taxonomy():
        return rt.taxonomy.tree()

    @router.post("/taxonomy/run", status_code=202, tags=["evolution"])
    def taxonomy_run():
        from paoding.services.queue import enqueue
        from paoding.storage.tables import new_id

        with rt.db.transaction() as session:
            job = enqueue(session, "taxonomy", {"manual_run": new_id()})
            return {"job_id": job.id}

    @router.get("/evolution/logs", tags=["evolution"])
    def evolution_logs(limit: int = Query(20, ge=1, le=100)):
        with rt.db.transaction() as session:
            return [
                {"id": row.id, "kind": row.kind, "document": row.document}
                for row in session.scalars(
                    select(EvolutionLog).order_by(EvolutionLog.created_at.desc()).limit(limit)
                )
            ]

    @router.post("/atoms/{atom_id}/feedback", tags=["knowledge"])
    def feedback(atom_id: str, body: FeedbackRequest):
        return rt.library.feedback(atom_id, body.is_adopted, body.event_id)

    @router.get("/playbooks", tags=["evolution"])
    def playbooks(domain: str, target_platform: str | None = None):
        return rt.library.query_playbook(domain, target_platform)

    @router.get("/trends", tags=["evolution"])
    def trends(days: int = Query(7, ge=1, le=365), platforms: list[str] = Query(default=[])):
        return rt.library.scan_trends(days, platforms)

    @router.post("/evolution/run", status_code=202, tags=["evolution"])
    def evolve():
        from datetime import UTC, datetime

        from paoding.services.worker import schedule_weekly

        schedule_weekly(rt, datetime.now(UTC).strftime("%G-W%V"))
        return {"status": "scheduled"}

    @router.get("/creators", tags=["creators"])
    def creators():
        with rt.db.transaction() as session:
            return [
                {
                    "id": a.id,
                    "name": a.name,
                    "platform": a.platform,
                    "profile_url": a.profile_url,
                    "status": a.status,
                    "last_polled_at": a.last_polled_at,
                    "persona_node_count": a.persona_node_count,
                }
                for a in session.scalars(
                    select(WatchedAccount).order_by(WatchedAccount.created_at.desc()).limit(100)
                )
            ]

    @router.get("/creators/{name}/persona", tags=["creators"])
    def persona(name: str):
        return rt.library.summon_creator(name)

    @router.post("/creators/{account_id}/pause", tags=["creators"])
    def pause(account_id: str, paused: bool = True):
        with rt.db.transaction() as session:
            account = session.get(WatchedAccount, account_id)
            if not account:
                raise LookupError("Creator not found")
            account.status = "paused" if paused else "active"
            return {"id": account.id, "status": account.status}

    @router.get("/models", tags=["models"])
    def models(provider: str | None = None):
        return [
            row for row in rt.gateway.list_models() if not provider or row["provider"] == provider
        ]

    from paoding.models.custom import ModelInput, ProviderInput

    @router.get("/models/providers", tags=["models"])
    def custom_providers():
        from paoding.models.custom import PRESETS, custom_public
        from paoding.models.oauth import GOOGLE_SCOPES

        config = rt.gateway.config()
        return {
            "providers": custom_public(rt.settings),
            "chatgpt_connected": bool(oauth.account()),
            "gemini_google_connected": GOOGLE_SCOPES.issubset(
                (oauth.google_account() or {}).get("scopes", [])
            ),
            "routes": {
                kind: [r.model_dump() for r in routes] for kind, routes in config.routes.items()
            },
            "presets": [{"id": k, "name": v[0], "base_url": v[1]} for k, v in PRESETS.items()],
        }

    @router.post("/models/providers", tags=["models"])
    def add_custom_provider(body: ProviderInput):
        from paoding.models.custom import load_custom, save_custom

        if body.id in rt.gateway.config().providers and body.id not in load_custom(rt.settings):
            raise ValueError("不能覆盖现有服务商")
        return save_custom(rt.settings, body)

    @router.post("/models/manual", tags=["models"])
    def add_manual_model(body: ModelInput):
        from paoding.models.custom import valid_model
        from paoding.storage.queries import insert_for
        from paoding.storage.tables import ModelRecord

        valid_model(body.model)
        if body.provider not in rt.gateway.config().providers:
            raise ValueError("服务商不存在")
        with rt.db.transaction() as session:
            stmt = insert_for(session, ModelRecord).values(
                id=f"{body.provider}:{body.model}", provider=body.provider, model_id=body.model
            )
            session.execute(stmt.on_conflict_do_nothing(index_elements=["id"]))
        return {"provider": body.provider, "model": body.model}

    @router.post("/models/test-one", tags=["models"])
    def test_one_model(body: ModelInput):
        return rt.gateway.test_one(body.provider, body.model)

    @router.post("/oauth/{provider}/start", tags=["models"])
    def oauth_start(provider: str, request: Request):
        from fastapi.responses import JSONResponse

        if request.url.hostname != "127.0.0.1":
            raise HTTPException(403, "网页登录授权请使用 127.0.0.1 地址")
        origin = request.headers.get("origin")
        if origin and origin != f"http://127.0.0.1:{request.url.port or 80}":
            raise HTTPException(403, "授权请求来源不匹配")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "授权请求需要 JSON")
        url, session = oauth.start(provider, request.url.port or 80)
        response = JSONResponse({"authorization_url": url}, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            "paoding_oauth",
            session,
            max_age=600,
            httponly=True,
            samesite="lax",
            path="/auth/callback",
        )
        return response

    @router.post("/models/sync", tags=["models"])
    def sync_models(provider: str | None = None):
        return rt.gateway.sync(provider)

    @router.post("/models/probe", tags=["models"])
    def probe_model(provider: str):
        return rt.gateway.probe(provider)

    # PRD compatibility alias; POST is preferred because sync updates the registry.
    router.add_api_route(
        "/models/sync", sync_models, methods=["GET"], deprecated=True, tags=["models"]
    )

    @router.post("/models/test", tags=["models"])
    def test_models():
        return rt.gateway.test()

    @router.get("/stats", tags=["system"])
    def stats():
        with rt.db.transaction() as session:
            return {
                "nodes": session.scalar(select(func.count()).select_from(KnowledgeNode)),
                "atoms": session.scalar(select(func.count()).select_from(AtomElement)),
                "creators": session.scalar(select(func.count()).select_from(WatchedAccount)),
                "evolutions": session.scalar(select(func.count()).select_from(EvolutionLog)),
                "jobs": dict(
                    session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
                ),
            }

    app.include_router(router)

    @app.get("/auth/callback", include_in_schema=False)
    def oauth_callback(
        request: Request,
        state: str = "",
        code: str = "",
        client_id: str | None = None,
        error: str | None = None,
    ):
        from fastapi.responses import HTMLResponse

        if request.url.hostname != "127.0.0.1":
            raise HTTPException(403, "授权回调仅限本机")
        try:
            provider = oauth.finish(
                state, request.cookies.get("paoding_oauth"), code, client_id, error
            )
            destination = "/settings?oauth=" + provider
            message = "授权完成，正在返回设置页面。"
        except Exception as exc:
            # Never log the callback URL, authorization code, tokens, or exception text:
            # upstream HTTP errors can include sensitive request details.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("OAuth callback failed: type=%s status=%s", type(exc).__name__, status)
            destination = "/settings?oauth=failed"
            message = "授权未完成，请返回设置页面重试。"
        html = f'<meta charset="utf-8"><p>{message}</p><script>history.replaceState(null,"","/settings");location.replace("{destination}")</script>'
        response = HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
        response.delete_cookie("paoding_oauth", path="/auth/callback")
        return response

    from paoding.api.webhooks import create_webhooks

    app.include_router(create_webhooks(rt))

    return app


def serve():
    import uvicorn

    from paoding.core.config import Settings

    settings = Settings()
    # OAuth callbacks can contain short-lived codes in their URL; do not log access URLs.
    uvicorn.run(
        "paoding.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        access_log=False,
    )
