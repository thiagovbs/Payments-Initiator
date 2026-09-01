"""Persistência simples (SQLite) do estado dos consentimentos da Iniciadora."""

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

DB_URL = "sqlite:///./initiator.db"

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})


class ConsentRecord(SQLModel, table=True):
    """Representa um consentimento/pagamento em andamento."""

    id: Optional[int] = Field(default=None, primary_key=True)
    consent_id: str = Field(index=True, unique=True)
    request_id: str = ""
    code: Optional[str] = None
    status: str = "CREATED"
    payment_id: Optional[str] = None
    payload: str = ""  # JSON com os dados do consentimento
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def upsert_consent(record: ConsentRecord) -> ConsentRecord:
    with Session(engine) as session:
        existing = session.exec(
            select(ConsentRecord).where(ConsentRecord.request_id == record.request_id)
        ).first()
        if not existing and record.consent_id != record.request_id:
            existing = session.exec(
                select(ConsentRecord).where(
                    ConsentRecord.consent_id == record.consent_id
                )
            ).first()
        if existing:
            existing.consent_id = record.consent_id
            existing.request_id = record.request_id
            existing.code = record.code
            existing.status = record.status
            existing.payment_id = record.payment_id
            existing.payload = record.payload
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def get_by_consent_id(consent_id: str) -> Optional[ConsentRecord]:
    with Session(engine) as session:
        return session.exec(
            select(ConsentRecord).where(ConsentRecord.consent_id == consent_id)
        ).first()


def get_by_request_id(request_id: str) -> Optional[ConsentRecord]:
    with Session(engine) as session:
        return session.exec(
            select(ConsentRecord).where(ConsentRecord.request_id == request_id)
        ).first()


def get_by_payment_id(payment_id: str) -> Optional[ConsentRecord]:
    with Session(engine) as session:
        return session.exec(
            select(ConsentRecord).where(ConsentRecord.payment_id == payment_id)
        ).first()
