
from sqlalchemy.orm import Session

from ..models import Purchase, PurchaseStatus, Ticket, TicketSource, User
from ..timeutils import utcnow_naive
from .tickets import audit, create_ticket


class PurchaseAlreadyProcessed(Exception):
    pass


def approve_purchase(db: Session, purchase_id: int, actor: User | None = None, actor_label: str | None = None) -> tuple[Purchase, Ticket]:
    purchase = db.query(Purchase).filter(Purchase.id == purchase_id).with_for_update().first()
    if not purchase:
        raise LookupError("purchase not found")
    if purchase.status != PurchaseStatus.PENDING:
        raise PurchaseAlreadyProcessed()

    ticket = create_ticket(
        db,
        purchase.guest_name,
        TicketSource.TELEGRAM,
        actor=actor,
        actor_label=actor_label,
        purchase_id=purchase.id,
        commit=False,
    )
    purchase.status = PurchaseStatus.APPROVED
    purchase.decided_at = utcnow_naive()
    purchase.decided_by_label = actor.username if actor else actor_label
    audit(
        db,
        "purchase.approved",
        actor,
        actor_label,
        entity_type="purchase",
        entity_id=str(purchase.id),
        details=ticket.number,
    )
    db.commit()
    db.refresh(purchase)
    db.refresh(ticket)
    return purchase, ticket


def reject_purchase(db: Session, purchase_id: int, actor: User | None = None, actor_label: str | None = None) -> Purchase:
    purchase = db.query(Purchase).filter(Purchase.id == purchase_id).with_for_update().first()
    if not purchase:
        raise LookupError("purchase not found")
    if purchase.status != PurchaseStatus.PENDING:
        raise PurchaseAlreadyProcessed()

    purchase.status = PurchaseStatus.REJECTED
    purchase.decided_at = utcnow_naive()
    purchase.decided_by_label = actor.username if actor else actor_label
    audit(db, "purchase.rejected", actor, actor_label, entity_type="purchase", entity_id=str(purchase.id))
    db.commit()
    db.refresh(purchase)
    return purchase
