from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Ticket, TicketStatus
from tests.conftest import extract_csrf, login


def create_manual_ticket(client: TestClient, name: str = "Иван Иванов") -> Ticket:
    page = client.get("/admin/tickets/new")
    csrf = extract_csrf(page.text)
    response = client.post(
        "/admin/tickets/new",
        data={"guest_name": name, "csrf": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        ticket = db.query(Ticket).order_by(Ticket.id.desc()).first()
        db.expunge(ticket)
        return ticket


def test_manual_ticket_public_and_staff_flow(client):
    login(client)
    ticket = create_manual_ticket(client)

    with TestClient(client.app) as guest:
        public = guest.get(f"/q/{ticket.token}")
        assert public.status_code == 200
        assert "Сделай скриншот билета заранее" in public.text
        assert "17+" in public.text
        assert "ПРОПУСТИТЬ" not in public.text

        png = guest.get(f"/ticket/{ticket.token}/download.png")
        assert png.status_code == 200
        assert png.headers["content-type"].startswith("image/png")
        assert len(png.content) > 10_000

        pdf = guest.get(f"/ticket/{ticket.token}/download.pdf")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"].startswith("application/pdf")
        assert pdf.content.startswith(b"%PDF")

    staff = client.get(f"/q/{ticket.token}")
    assert staff.status_code == 200
    assert "БИЛЕТ ДЕЙСТВИТЕЛЕН" in staff.text
    csrf = extract_csrf(staff.text)

    used = client.post(f"/q/{ticket.token}/use", data={"csrf": csrf}, follow_redirects=True)
    assert used.status_code == 200
    assert "БИЛЕТ УЖЕ ИСПОЛЬЗОВАН" in used.text

    with SessionLocal() as db:
        row = db.get(Ticket, ticket.id)
        assert row.status == TicketStatus.USED
        assert row.used_by_label == "owner"


def test_wrong_csrf_is_rejected(client):
    login(client)
    response = client.post("/admin/tickets/new", data={"guest_name": "Иван Иванов", "csrf": "wrong"})
    assert response.status_code == 403


def test_security_headers_present(client):
    response = client.get("/login")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_logout_clears_session(client):
    login(client)
    admin = client.get("/admin")
    csrf = extract_csrf(admin.text)
    response = client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/admin").status_code == 401


def test_removed_web_scanner_is_not_exposed(client):
    assert client.get("/scanner").status_code == 404


def test_request_id_and_private_ticket_headers(client):
    login(client)
    ticket = create_manual_ticket(client, "Лог Тест")
    with TestClient(client.app) as guest:
        response = guest.get(f"/ticket/{ticket.token}")
        assert response.status_code == 200
        assert response.headers.get("x-request-id")
        assert response.headers.get("cache-control") == "no-store"
