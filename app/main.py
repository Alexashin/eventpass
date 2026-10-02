import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import authenticate, current_user, hash_password, require_user, verify_password
from .config import get_settings
from .db import SessionLocal, get_db
from .models import AuditLog, Purchase, PurchaseStatus, Ticket, TicketSource, TicketStatus, User, UserRole
from .logging_setup import configure_logging, reset_request_id, set_request_id
from .security import LoginLimiter, csrf_token, verify_csrf
from .services.purchases import PurchaseAlreadyProcessed, approve_purchase as approve_purchase_service, reject_purchase as reject_purchase_service
from .services.receipt import detect_receipt_kind
from .services.tickets import audit, create_ticket, qr_png_bytes, ticket_pdf_bytes, ticket_png_bytes, ticket_url, use_ticket
from .timeutils import utcnow_naive

settings = get_settings()
configure_logging("web")
logger = logging.getLogger("raneparty.web")
login_limiter = LoginLimiter(settings.login_max_failures, settings.login_window_seconds)


def bootstrap_owner() -> None:
    db = SessionLocal()
    try:
        desired_username = settings.owner_username.strip()
        owner = db.query(User).filter(User.role == UserRole.OWNER).first()
        if owner is None:
            db.add(
                User(
                    username=desired_username,
                    password_hash=hash_password(settings.owner_password),
                    role=UserRole.OWNER,
                )
            )
            db.commit()
            logger.info("Initial OWNER account created: %s", desired_username)
            return

        changed = False
        if owner.username != desired_username:
            conflict = db.query(User).filter(User.username == desired_username, User.id != owner.id).first()
            if conflict:
                raise RuntimeError("OWNER_USERNAME уже занят другим аккаунтом")
            owner.username = desired_username
            changed = True
        if not verify_password(settings.owner_password, owner.password_hash):
            owner.password_hash = hash_password(settings.owner_password)
            changed = True
        if not owner.is_active:
            owner.is_active = True
            changed = True
        if changed:
            db.commit()
            logger.info("OWNER credentials synchronized from production settings")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_runtime()
    bootstrap_owner()
    yield


app = FastAPI(
    title="RANEPARTY Pass",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret,
    session_cookie="raneparty_session",
    max_age=settings.session_max_age_seconds,
    https_only=settings.session_https_only,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(
        "app/static/favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = secrets.token_hex(8)
    ctx = set_request_id(request_id)
    started = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
        return response
    except Exception:
        logger.exception("http.unhandled method=%s path=%s", request.method, _safe_route_name(request))
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        route_name = _safe_route_name(request)
        status_code = getattr(response, "status_code", 500)
        client_ip = request.client.host if request.client else "unknown"
        logger.info(
            "http.request method=%s path=%s status=%s duration_ms=%s client=%s",
            request.method, route_name, status_code, elapsed_ms, client_ip,
        )
        if response is not None:
            response.headers.setdefault("X-Request-ID", request_id)
        reset_request_id(ctx)


def _safe_route_name(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template:
        return template
    path = request.url.path
    if path.startswith("/q/"):
        return "/q/{token}"
    if path.startswith("/ticket/"):
        return "/ticket/{token}/..."
    return path[:160]


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'",
    )
    if settings.session_https_only:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path.startswith(("/admin", "/q/", "/ticket/", "/login")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def render(request: Request, name: str, db: Session, **ctx):
    user = current_user(request, db)
    base = {
        "request": request,
        "user": user,
        "settings": settings,
        "csrf_token": csrf_token(request),
        "TicketStatus": TicketStatus,
        "UserRole": UserRole,
        "PurchaseStatus": PurchaseStatus,
    }
    base.update(ctx)
    return templates.TemplateResponse(request=request, name=name, context=base)


def safe_receipt_path(raw_path: str) -> Path:
    base = settings.receipt_path.resolve()
    path = Path(raw_path).resolve()
    if base != path and base not in path.parents:
        raise HTTPException(status_code=404)
    if not path.is_file():
        raise HTTPException(status_code=404)
    return path


@app.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/admin" if current_user(request, db) else "/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if current_user(request, db):
        return RedirectResponse("/admin", status_code=302)
    return render(request, "login.html", db)


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf)
    client_key = request.client.host if request.client else "unknown"
    if not login_limiter.allowed(client_key):
        logger.warning("auth.rate_limited client=%s", client_key)
        response = render(request, "login.html", db, error="Слишком много попыток входа. Попробуй через несколько минут")
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        return response

    user = authenticate(db, username.strip(), password)
    if not user:
        login_limiter.fail(client_key)
        logger.warning("auth.login_failed client=%s", client_key)
        return render(request, "login.html", db, error="Неверный логин или пароль")

    login_limiter.clear(client_key)
    logger.info("auth.login_success user_id=%s role=%s client=%s", user.id, user.role.value, client_key)
    request.session.clear()
    request.session["user_id"] = user.id
    csrf_token(request)
    return RedirectResponse("/admin", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    user = current_user(request, db)
    if user:
        logger.info("auth.logout user_id=%s", user.id)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    require_user(request, db)
    counts = {
        "all": db.query(func.count(Ticket.id)).scalar() or 0,
        "active": db.query(func.count(Ticket.id)).filter(Ticket.status == TicketStatus.ACTIVE).scalar() or 0,
        "used": db.query(func.count(Ticket.id)).filter(Ticket.status == TicketStatus.USED).scalar() or 0,
        "cancelled": db.query(func.count(Ticket.id)).filter(Ticket.status == TicketStatus.CANCELLED).scalar() or 0,
        "pending": db.query(func.count(Purchase.id)).filter(Purchase.status == PurchaseStatus.PENDING).scalar() or 0,
    }
    recent = db.query(Ticket).filter(Ticket.status == TicketStatus.USED).order_by(Ticket.used_at.desc()).limit(8).all()
    return render(request, "dashboard.html", db, counts=counts, recent=recent)


@app.get("/admin/tickets", response_class=HTMLResponse)
def tickets_page(request: Request, q: str = "", status_filter: str = "", db: Session = Depends(get_db)):
    require_user(request, db)
    query = db.query(Ticket)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(Ticket.guest_name.ilike(like), Ticket.number.ilike(like)))
    if status_filter in TicketStatus.__members__:
        query = query.filter(Ticket.status == TicketStatus[status_filter])
    tickets = query.order_by(Ticket.created_at.desc()).limit(500).all()
    return render(request, "tickets.html", db, tickets=tickets, q=q, status_filter=status_filter)


@app.get("/admin/tickets/new", response_class=HTMLResponse)
def new_ticket_page(request: Request, db: Session = Depends(get_db)):
    require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    return render(request, "ticket_new.html", db)


@app.post("/admin/tickets/new")
def new_ticket(
    request: Request,
    guest_name: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf)
    user = require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    name = " ".join(guest_name.strip().split())
    if not 3 <= len(name) <= 200:
        return render(request, "ticket_new.html", db, error="Укажи корректные имя и фамилию")
    ticket = create_ticket(db, name, TicketSource.MANUAL, actor=user)
    return RedirectResponse(f"/admin/tickets/{ticket.id}", status_code=303)


@app.get("/admin/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(request: Request, ticket_id: int, db: Session = Depends(get_db)):
    require_user(request, db)
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(404)
    return render(request, "ticket_detail.html", db, ticket=ticket, public_url=ticket_url(ticket))


@app.post("/admin/tickets/{ticket_id}/mark-sent")
def mark_sent(request: Request, ticket_id: int, csrf: str = Form(...), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    user = require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(404)
    ticket.sent_at = utcnow_naive()
    audit(db, "ticket.sent", user, entity_type="ticket", entity_id=str(ticket.id), details=ticket.number)
    db.commit()
    return RedirectResponse(f"/admin/tickets/{ticket.id}", status_code=303)


@app.post("/admin/tickets/{ticket_id}/cancel")
def cancel_ticket(request: Request, ticket_id: int, csrf: str = Form(...), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    user = require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(404)
    ticket.status = TicketStatus.CANCELLED
    ticket.cancelled_at = utcnow_naive()
    audit(db, "ticket.cancelled", user, entity_type="ticket", entity_id=str(ticket.id), details=ticket.number)
    db.commit()
    return RedirectResponse(f"/admin/tickets/{ticket.id}", status_code=303)


@app.post("/admin/tickets/{ticket_id}/restore")
def restore_ticket(request: Request, ticket_id: int, csrf: str = Form(...), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    user = require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(404)
    ticket.status = TicketStatus.ACTIVE
    ticket.used_at = None
    ticket.used_by_user_id = None
    ticket.used_by_label = None
    ticket.cancelled_at = None
    audit(db, "ticket.restored", user, entity_type="ticket", entity_id=str(ticket.id), details=ticket.number)
    db.commit()
    return RedirectResponse(f"/admin/tickets/{ticket.id}", status_code=303)


@app.get("/ticket/{token}", response_class=HTMLResponse)
def public_ticket(request: Request, token: str, db: Session = Depends(get_db)):
    ticket = db.query(Ticket).filter(Ticket.token == token).first()
    if not ticket:
        raise HTTPException(404)
    return templates.TemplateResponse(request=request, name="public_ticket.html", context={"request": request, "ticket": ticket, "settings": settings})


@app.get("/q/{token}", response_class=HTMLResponse)
def qr_landing(request: Request, token: str, db: Session = Depends(get_db)):
    ticket = db.query(Ticket).filter(Ticket.token == token).first()
    if not ticket:
        raise HTTPException(404)
    user = current_user(request, db)
    if user and user.role in {UserRole.OWNER, UserRole.ADMIN, UserRole.CONTROLLER}:
        return render(request, "staff_ticket_check.html", db, ticket=ticket)
    return templates.TemplateResponse(request=request, name="public_ticket.html", context={"request": request, "ticket": ticket, "settings": settings})


@app.post("/q/{token}/use")
def qr_use_ticket(request: Request, token: str, csrf: str = Form(...), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    user = require_user(request, db, {UserRole.OWNER, UserRole.ADMIN, UserRole.CONTROLLER})
    ticket = db.query(Ticket).filter(Ticket.token == token).first()
    if not ticket:
        raise HTTPException(404)
    use_ticket(db, ticket, user)
    return RedirectResponse(f"/q/{token}", status_code=303)


@app.get("/ticket/{token}/qr.png")
def public_qr(token: str, db: Session = Depends(get_db)):
    ticket = db.query(Ticket).filter(Ticket.token == token).first()
    if not ticket:
        raise HTTPException(404)
    return Response(qr_png_bytes(ticket), media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/ticket/{token}/download.png")
def download_png(token: str, db: Session = Depends(get_db)):
    ticket = db.query(Ticket).filter(Ticket.token == token).first()
    if not ticket:
        raise HTTPException(404)
    return Response(
        ticket_png_bytes(ticket),
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{ticket.number}.png"'},
    )


@app.get("/ticket/{token}/download.pdf")
def download_pdf(token: str, db: Session = Depends(get_db)):
    ticket = db.query(Ticket).filter(Ticket.token == token).first()
    if not ticket:
        raise HTTPException(404)
    return Response(
        ticket_pdf_bytes(ticket),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{ticket.number}.pdf"'},
    )


@app.get("/admin/purchases", response_class=HTMLResponse)
def purchases_page(request: Request, db: Session = Depends(get_db)):
    require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    purchases = db.query(Purchase).order_by(Purchase.created_at.desc()).limit(300).all()
    ticket_by_purchase = {t.purchase_id: t for t in db.query(Ticket).filter(Ticket.purchase_id.isnot(None)).all()}
    return render(request, "purchases.html", db, purchases=purchases, ticket_by_purchase=ticket_by_purchase)


async def notify_buyer(purchase: Purchase, ticket: Ticket | None, approved: bool) -> None:
    if not settings.telegram_bot_token:
        return
    from aiogram import Bot

    bot = Bot(settings.telegram_bot_token)
    try:
        if approved and ticket:
            text = (
                f"✅ Билет готов!\n\n{purchase.guest_name}\nПриглашение № {ticket.number}\n\n"
                f"Открыть билет: {ticket_url(ticket)}\n\n"
                "Совет: сразу сделай скриншот билета. На входе интернет может работать нестабильно."
            )
        else:
            text = "❌ Заявка на билет отклонена администратором. Если это ошибка, свяжись с организатором."
        await bot.send_message(int(purchase.telegram_user_id), text)

        if approved and ticket:
            admin_text = (
                f"✅ Выдан {ticket.number}\n"
                f"Гость: {purchase.guest_name}\n"
                f"Telegram: @{purchase.telegram_username or '-'} / {purchase.telegram_user_id}\n"
                f"Ссылка: {ticket_url(ticket)}"
            )
            for admin_id in settings.admin_telegram_ids:
                try:
                    await bot.send_message(admin_id, admin_text)
                except Exception:
                    logger.exception("Failed to notify Telegram admin %s", admin_id)
    except Exception:
        logger.exception("Failed to notify buyer for purchase %s", purchase.id)
    finally:
        await bot.session.close()


@app.post("/admin/purchases/{purchase_id}/approve")
async def approve_purchase(request: Request, purchase_id: int, csrf: str = Form(...), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    user = require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    try:
        purchase, ticket = approve_purchase_service(db, purchase_id, actor=user)
    except LookupError:
        raise HTTPException(404)
    except PurchaseAlreadyProcessed:
        return RedirectResponse("/admin/purchases", status_code=303)
    await notify_buyer(purchase, ticket, True)
    return RedirectResponse("/admin/purchases", status_code=303)


@app.post("/admin/purchases/{purchase_id}/reject")
async def reject_purchase(request: Request, purchase_id: int, csrf: str = Form(...), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    user = require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    try:
        purchase = reject_purchase_service(db, purchase_id, actor=user)
    except LookupError:
        raise HTTPException(404)
    except PurchaseAlreadyProcessed:
        return RedirectResponse("/admin/purchases", status_code=303)
    await notify_buyer(purchase, None, False)
    return RedirectResponse("/admin/purchases", status_code=303)


@app.get("/admin/purchases/{purchase_id}/receipt")
def purchase_receipt(request: Request, purchase_id: int, db: Session = Depends(get_db)):
    require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    purchase = db.get(Purchase, purchase_id)
    if not purchase:
        raise HTTPException(404)
    path = safe_receipt_path(purchase.receipt_path)
    try:
        kind = detect_receipt_kind(path)
    except Exception:
        raise HTTPException(415, "Неизвестный формат чека")
    if kind == "pdf":
        media_type = "application/pdf"
    else:
        media_type = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    headers = {"Content-Disposition": f'inline; filename="receipt-{purchase.id}{path.suffix.lower()}"'}
    return FileResponse(path, media_type=media_type, headers=headers)


@app.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    require_user(request, db, {UserRole.OWNER})
    users = db.query(User).order_by(User.created_at.asc()).all()
    return render(request, "users.html", db, users=users)


@app.post("/admin/users")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf)
    owner = require_user(request, db, {UserRole.OWNER})
    username = username.strip()
    if not 3 <= len(username) <= 80 or len(password) < 10 or role not in UserRole.__members__ or role == "OWNER":
        return RedirectResponse("/admin/users?error=1", status_code=303)
    if db.query(User).filter(User.username == username).first():
        return RedirectResponse("/admin/users?exists=1", status_code=303)
    user = User(username=username, password_hash=hash_password(password), role=UserRole[role])
    db.add(user)
    db.flush()
    audit(db, "user.created", owner, entity_type="user", entity_id=str(user.id), details=f"{username} / {role}")
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, csrf: str = Form(...), db: Session = Depends(get_db)):
    verify_csrf(request, csrf)
    owner = require_user(request, db, {UserRole.OWNER})
    user = db.get(User, user_id)
    if not user or user.role == UserRole.OWNER:
        return RedirectResponse("/admin/users", status_code=303)
    user.is_active = not user.is_active
    audit(db, "user.toggled", owner, entity_type="user", entity_id=str(user.id), details=f"active={user.is_active}")
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/admin/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: Session = Depends(get_db)):
    require_user(request, db, {UserRole.OWNER, UserRole.ADMIN})
    logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(500).all()
    return render(request, "logs.html", db, logs=logs)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
        return {"ok": True}
    except Exception:
        logger.exception("Healthcheck DB failure")
        raise HTTPException(503, "database unavailable")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"
