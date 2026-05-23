from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Case, Document, PolicyRecord, VerdictRecord


class CaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, case_id: str) -> Case | None:
        return await self.session.get(Case, case_id)

    async def list(self, limit: int = 100) -> Sequence[Case]:
        stmt = select(Case).order_by(Case.updated_at.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def upsert(self, case: Case) -> Case:
        existing = await self.session.get(Case, case.id)
        if existing is None:
            self.session.add(case)
            return case
        for field in (
            "status", "patient_display", "cpt_code", "payer_id",
            "selected_policy_id", "branch", "outcome",
            "inbound_bundle", "extracted_facts", "policy_selection",
            "criteria_evaluation", "determination", "pas_response_bundle",
        ):
            setattr(existing, field, getattr(case, field))
        return existing


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, document_id: str) -> Document | None:
        return await self.session.get(Document, document_id)

    async def add(self, document: Document) -> Document:
        self.session.add(document)
        return document


class PolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, policy_id: str) -> PolicyRecord | None:
        return await self.session.get(PolicyRecord, policy_id)

    async def list(self) -> Sequence[PolicyRecord]:
        result = await self.session.execute(select(PolicyRecord))
        return result.scalars().all()


class VerdictRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_many(self, verdicts: list[VerdictRecord]) -> None:
        self.session.add_all(verdicts)


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, entry: AuditLog) -> None:
        self.session.add(entry)
