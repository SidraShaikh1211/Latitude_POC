from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import a2a as a2a_router
from app.api import cases as cases_router
from app.api import doctor as doctor_router
from app.api import fhir_pas as fhir_pas_router
from app.api import policies as policies_router
from app.db.engine import init_db
from app.logging_config import configure_logging, log
from app.policy.registry import get_registry
from app.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "pdfs").mkdir(parents=True, exist_ok=True)
    await init_db()
    # Eager-load policy registry so citation-verification failures surface at startup
    reg = get_registry()
    log.info(
        "startup",
        model=settings.anthropic_model,
        policies_dir=str(settings.policies_dir),
        data_dir=str(settings.data_dir),
        database_url=settings.database_url,
        policies_loaded=[p.policy_id for p in reg.all_policies()],
    )
    yield
    log.info("shutdown")


app = FastAPI(
    title="PA Prototype PA Prototype",
    description=(
        "Payer-side Prior Authorization prototype: FHIR extraction, "
        "policy adjudication, Da Vinci PAS responses, MCP server."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(cases_router.router)
app.include_router(policies_router.router)
app.include_router(fhir_pas_router.router)
app.include_router(doctor_router.router)
app.include_router(a2a_router.router)
