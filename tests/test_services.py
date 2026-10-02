import pytest

from app.auth import hash_password
from app.db import SessionLocal
from app.models import Purchase, PurchaseStatus, TicketSource, User, UserRole
from app.services.purchases import PurchaseAlreadyProcessed, approve_purchase
from app.services.tickets import create_ticket, use_ticket


def test_atomic_ticket_use(client):
    with SessionLocal() as db:
        owner = db.query(User).filter(User.role == UserRole.OWNER).first()
        ticket = create_ticket(db, "Тест Гость", TicketSource.MANUAL, actor=owner)
        assert use_ticket(db, ticket, owner) is True
        assert use_ticket(db, ticket, owner) is False


def test_purchase_can_only_be_approved_once(client, tmp_path):
    receipt = tmp_path / "x.jpg"
    receipt.write_bytes(b"fake")
    with SessionLocal() as db:
        owner = db.query(User).filter(User.role == UserRole.OWNER).first()
        purchase = Purchase(
            guest_name="Покупатель Тест",
            telegram_user_id="123",
            receipt_path=str(receipt),
            receipt_sha256="0" * 64,
            status=PurchaseStatus.PENDING,
        )
        db.add(purchase)
        db.commit()
        db.refresh(purchase)
        purchase_id = purchase.id
        _, ticket = approve_purchase(db, purchase_id, actor=owner)
        assert ticket.purchase_id == purchase_id
        with pytest.raises(PurchaseAlreadyProcessed):
            approve_purchase(db, purchase_id, actor=owner)
