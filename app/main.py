from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from app.api import a2a as a2a_router
from app.api import cases as cases_router
from app.api import doctor as doctor_router
from app.api import fhir_pas as fhir_pas_router
from app.api import metrics as metrics_router
from app.api import policies as policies_router
from app.db.engine import init_db
from app.db.orphan_sweep import sweep_orphans
from app.logging_config import configure_logging, log
from app.mcp_server.server import server as mcp_server
from app.policy.registry import get_registry
from app.settings import settings


_mcp_sessions = StreamableHTTPSessionManager(app=mcp_server, stateless=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "pdfs").mkdir(parents=True, exist_ok=True)
    await init_db()
    # Eager-load policy registry so citation-verification failures surface at startup
    reg = get_registry()
    # Mark any in-flight cases/submissions left over from a previous worker as
    # failed. The background asyncio.create_task that drives the pipeline does
    # not survive a uvicorn --reload restart, so without this sweep the UI
    # would poll a never-completing row forever.
    sweep_result = await sweep_orphans()
    async with _mcp_sessions.run():
        log.info(
            "startup",
            model=settings.anthropic_model,
            policies_dir=str(settings.policies_dir),
            data_dir=str(settings.data_dir),
            database_url=settings.database_url,
            policies_loaded=[p.policy_id for p in reg.all_policies()],
            mcp_mount="/mcp",
            orphan_sweep=sweep_result,
        )
        yield
    log.info("shutdown")


app = FastAPI(
    title="PA Prototype PA Prototype",
    description=(
        "Payer-side Prior Authorization prototype: FHIR extraction, "
        "policy adjudication, Da Vinci PAS responses, and an MCP server "
        "at /mcp (Streamable HTTP). Also runnable as stdio via "
        "`python -m app.mcp_server.server`."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# CORS for the Vite dev server and the production preview origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",  # vite preview
        "http://127.0.0.1:4173",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(cases_router.router)
app.include_router(policies_router.router)
app.include_router(fhir_pas_router.router)
app.include_router(doctor_router.router)
app.include_router(metrics_router.router)
app.include_router(a2a_router.router)


async def _mcp_asgi(scope, receive, send) -> None:
    await _mcp_sessions.handle_request(scope, receive, send)


app.mount("/mcp", _mcp_asgi)


_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend-react" / "dist"
_API_PREFIXES = ("/v1", "/fhir", "/mcp", "/health", "/.well-known")

if _FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=_FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        if any(("/" + full_path).startswith(p) for p in _API_PREFIXES):
            raise HTTPException(status_code=404)
        candidate = _FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_FRONTEND_DIST / "index.html")
