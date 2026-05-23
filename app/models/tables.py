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
                                                     ↘ failed

    A `Submission` becomes `sent` once the doctor's bundle has been handed
    off to the payer via POST /fhir/Claim/$submit. At that point
    `payer_case_id` is populated and the doctor UI switches to polling the
    Case row for the rest of the pipeline.
    """

    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    pdf_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    extracted_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extraction_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_cost_usd: Mapped[float | None] = mapped_column(nullable=True)
    inbound_bundle: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bundle_entry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bundle_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    patient_display: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cpt_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    payer_case_id: Mapped[str | None] = mapped_column(
        ForeignKey("cases.id"), nullable=True, index=True
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
