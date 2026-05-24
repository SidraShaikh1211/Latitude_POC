from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    storage_path: Mapped[str] = mapped_column(String(1024))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    sections: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PolicyRecord(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(512))
    payer_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(JSON)
    source_pdf_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    patient_display: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cpt_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    payer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    selected_policy_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    processing_stage: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    inbound_bundle: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extracted_facts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    policy_selection: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    criteria_evaluation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    determination: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pas_response_bundle: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Per-stage performance metrics emitted by the orchestrator. Shape:
    #   {
    #     "duration_seconds": float,
    #     "totals": {tokens_in, tokens_out, cache_read, cache_creation,
    #                cost_usd, llm_calls, cache_hit_rate},
    #     "stages": [
    #        {name, duration_seconds, tokens_in, tokens_out, cache_read,
    #         cache_creation, cost_usd, llm_calls},
    #        ...
    #     ],
    #     "adjudication_leaves": [{criterion_id, iterations, ...usage}],
    #   }
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    verdicts: Mapped[list["VerdictRecord"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    audit_entries: Mapped[list["AuditLog"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )


class VerdictRecord(Base):
    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    criterion_id: Mapped[str] = mapped_column(String(256), index=True)
    verdict: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    case: Mapped["Case"] = relationship(back_populates="verdicts")


class Submission(Base):
    """Doctor-side submission state — separate from `Case` (payer side).

    Lifecycle:
        extracting_metadata → bundle_ready → sending → sent
            → awaiting_payer_response → payer_responded
                                      ↘ payer_failed
                                      ↘ failed (doctor-side)

    The doctor's bundle is POSTed to the payer (`sent`), then the doctor row
    waits in `awaiting_payer_response` until the payer POSTs the
    `ClaimResponse` Bundle back to `/v1/doctor/inbound/claim-response`. That
    second POST writes the outcome / narrative / missing-info / raw
    ClaimResponse onto the Submission row and flips state to
    `payer_responded`. No doctor → payer cross-coupling at the UI layer; the
    doctor UI reads its own Submission row only.
    """

    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    pdf_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    extracted_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extraction_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_cost_usd: Mapped[float | None] = mapped_column(nullable=True)
    # Full Usage breakdown for the metadata-extraction Claude call. Plus
    # wall-clock duration so the Performance page can show the doctor-side
    # leg of the per-run cost/time alongside the payer-side metrics on Case.
    metadata_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_cache_creation_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    inbound_bundle: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bundle_entry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bundle_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    patient_display: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cpt_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    payer_case_id: Mapped[str | None] = mapped_column(
        ForeignKey("cases.id"), nullable=True, index=True
    )

    # Populated by POST /v1/doctor/inbound/claim-response when the payer
    # finalises the case. Mirror of the determination block on the Case row,
    # but persisted on the doctor side so the doctor's UI doesn't have to
    # call into payer-side endpoints.
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    determination_narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_info: Mapped[list | None] = mapped_column(JSON, nullable=True)
    claim_response_bundle: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    payer_responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    case: Mapped["Case"] = relationship(back_populates="audit_entries")
