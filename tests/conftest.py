import os
import re
from pathlib import Path

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "APP_SECRET": "test-secret-that-is-long-enough-for-tests-123456789",
        "PUBLIC_BASE_URL": "http://testserver",
        "ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
        "DATABASE_URL": "sqlite:////tmp/raneparty_pytest.db",
        "OWNER_USERNAME": "owner",
        "OWNER_PASSWORD": "test-owner-password",
        "SESSION_HTTPS_ONLY": "false",
        "PAYMENT_URL": "https://example.com/test",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_ADMIN_IDS": "",
        "REDIS_URL": "",
        "RECEIPT_DIR": "/tmp/raneparty_receipts_test",
        "EVENT_AGE": "17+",
    }
)

Path("/tmp/raneparty_pytest.db").unlink(missing_ok=True)
Path("/tmp/raneparty_receipts_test").mkdir(parents=True, exist_ok=True)

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "CSRF token not found in HTML"
    return match.group(1)


def login(client: TestClient):
    page = client.get("/login")
    csrf = extract_csrf(page.text)
    response = client.post(
        "/login",
        data={"username": "owner", "password": "test-owner-password", "csrf": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response
