import enum
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base
from .timeutils import utcnow_naive


class UserRole(str, enum.Enum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    CONTROLLER = "CONTROLLER"


class TicketStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    USED = "USED"
    CANCELLED = "CANCELLED"


class TicketSource(str, enum.Enum):
    MANUAL = "MANUAL"
    TELEGRAM = "TELEGRAM"


class PurchaseStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.CONTROLLER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    guest_name: Mapped[str] = mapped_column(String(200), index=True)
    token: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    status: Mapped[TicketStatus] = mapped_column(Enum(TicketStatus), default=TicketStatus.ACTIVE, index=True)
    source: Mapped[TicketSource] = mapped_column(Enum(TicketSource), default=TicketSource.MANUAL)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    used_by_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id"), nullable=True, unique=True)
    used_by_user: Mapped[User | None] = relationship(foreign_keys=[used_by_user_id])


class Purchase(Base):
    __tablename__ = "purchases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guest_name: Mapped[str] = mapped_column(String(200), index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(40), index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    telegram_display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    receipt_path: Mapped[str] = mapped_column(String(500))
    receipt_sha256: Mapped[str] = mapped_column(String(64), index=True)
    receipt_dhash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parsed_datetime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parsed_operation_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    checks_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PurchaseStatus] = mapped_column(Enum(PurchaseStatus), default=PurchaseStatus.PENDING, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by_label: Mapped[str | None] = mapped_column(String(120), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    actor_user: Mapped[User | None] = relationship()
