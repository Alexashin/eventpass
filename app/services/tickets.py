import io
import logging
import secrets

import qrcode
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A6
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AuditLog, Ticket, TicketSource, TicketStatus, User
from ..timeutils import utcnow_naive

settings = get_settings()
audit_logger = logging.getLogger("raneparty.audit")


def audit(
    db: Session,
    action: str,
    actor: User | None = None,
    actor_label: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: str | None = None,
):
    db.add(
        AuditLog(
            action=action,
            actor_user_id=actor.id if actor else None,
            actor_label=actor.username if actor else actor_label,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
        )
    )
    audit_logger.info(
        "audit action=%s actor=%s entity_type=%s entity_id=%s",
        action, actor.username if actor else (actor_label or "system"), entity_type or "-", entity_id or "-",
    )


def create_ticket(
    db: Session,
    guest_name: str,
    source: TicketSource,
    actor: User | None = None,
    actor_label: str | None = None,
    purchase_id: int | None = None,
    commit: bool = True,
) -> Ticket:
    guest_name = " ".join(guest_name.strip().split())
    if not 3 <= len(guest_name) <= 200:
        raise ValueError("Некорректное имя гостя")

    ticket = Ticket(
        guest_name=guest_name,
        token=secrets.token_urlsafe(32),
        source=source,
        purchase_id=purchase_id,
    )
    db.add(ticket)
    db.flush()
    ticket.number = f"{settings.ticket_prefix}-{ticket.id:04d}"
    audit(db, "ticket.created", actor, actor_label, "ticket", str(ticket.id), f"{ticket.number}: {guest_name}")
    if commit:
        db.commit()
        db.refresh(ticket)
    return ticket


def ticket_url(ticket: Ticket) -> str:
    return f"{settings.public_base_url.rstrip('/')}/ticket/{ticket.token}"


def scan_url(ticket: Ticket) -> str:
    return f"{settings.public_base_url.rstrip('/')}/q/{ticket.token}"


def qr_png_bytes(ticket: Ticket, box_size: int = 10) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        box_size=box_size,
        border=3,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(scan_url(ticket))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _font(size: int, bold: bool = False):
    names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, min_size: int = 24, bold: bool = True):
    for size in range(start_size, min_size - 1, -2):
        font = _font(size, bold)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font
    return _font(min_size, bold)


def ticket_png_bytes(ticket: Ticket) -> bytes:
    width, height = 1080, 1600
    img = Image.new("RGB", (width, height), "#070A12")
    draw = ImageDraw.Draw(img)

    cyan = "#73E7F4"
    blue = "#77A7FF"
    violet = "#A990FF"
    white = "#F7F9FF"
    muted = "#9DAAC0"

    draw.rounded_rectangle((55, 55, width - 55, height - 55), radius=48, outline="#293550", width=5, fill="#0D1423")
    draw.ellipse((760, 10, 1130, 380), fill="#102437")
    draw.ellipse((-160, 1190, 310, 1660), fill="#211A38")

    title = settings.event_name.upper()
    draw.text((width / 2, 140), "INVITATION", font=_font(24, True), fill=cyan, anchor="ma")
    draw.text((width / 2, 215), title, font=_fit_font(draw, title, 900, 68, 40), fill=white, anchor="ma")
    draw.text((width / 2, 302), settings.event_age, font=_font(34, True), fill=violet, anchor="ma")

    guest_font = _fit_font(draw, ticket.guest_name, 900, 52, 30)
    draw.text((width / 2, 398), ticket.guest_name, font=guest_font, fill=white, anchor="ma")
    draw.text((width / 2, 468), f"Приглашение № {ticket.number}", font=_font(27), fill=muted, anchor="ma")

    qr = Image.open(io.BytesIO(qr_png_bytes(ticket, box_size=12))).resize((600, 600))
    img.paste(qr, ((width - 600) // 2, 555))

    draw.text(
        (width / 2, 1208),
        f"{settings.event_date_display}  •  {settings.event_start}",
        font=_font(32, True),
        fill=white,
        anchor="ma",
    )
    draw.text((width / 2, 1277), "Именное приглашение · 1 билет = 1 проход", font=_font(26), fill=blue, anchor="ma")

    draw.rounded_rectangle((135, 1332, width - 135, 1432), radius=24, fill="#111E31", outline="#29425C", width=2)
    draw.text((width / 2, 1363), "СДЕЛАЙ СКРИНШОТ БИЛЕТА ЗАРАНЕЕ", font=_font(25, True), fill=cyan, anchor="ma")
    draw.text((width / 2, 1404), "На входе мобильный интернет может работать нестабильно", font=_font(20), fill=muted, anchor="ma")
    draw.text((width / 2, 1490), ticket.number, font=_font(24, True), fill=cyan, anchor="ma")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def ticket_pdf_bytes(ticket: Ticket) -> bytes:
    png = ticket_png_bytes(ticket)
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A6)
    page_w, page_h = A6
    image = ImageReader(io.BytesIO(png))
    image_ratio = 1080 / 1600
    page_ratio = page_w / page_h
    if image_ratio > page_ratio:
        draw_w = page_w
        draw_h = draw_w / image_ratio
    else:
        draw_h = page_h
        draw_w = draw_h * image_ratio
    pdf.drawImage(image, (page_w - draw_w) / 2, (page_h - draw_h) / 2, width=draw_w, height=draw_h, preserveAspectRatio=True)
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def use_ticket(db: Session, ticket: Ticket, actor: User) -> bool:
    now = utcnow_naive()
    result = db.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.status == TicketStatus.ACTIVE)
        .values(
            status=TicketStatus.USED,
            used_at=now,
            used_by_user_id=actor.id,
            used_by_label=actor.username,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return False

    audit(db, "ticket.used", actor, entity_type="ticket", entity_id=str(ticket.id), details=ticket.number)
    db.commit()
    return True
