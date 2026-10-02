from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal


def main() -> int:
    settings = get_settings()
    checks: list[tuple[str, bool, str]] = []

    try:
        settings.validate_runtime()
        checks.append(("production config", True, "OK"))
    except Exception as exc:
        checks.append(("production config", False, str(exc)))

    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        checks.append(("database", True, "SELECT 1"))
    except Exception as exc:
        checks.append(("database", False, str(exc)))

    try:
        receipt_dir = settings.receipt_path
        with tempfile.NamedTemporaryFile(dir=receipt_dir, prefix="preflight-", delete=True) as f:
            f.write(b"ok")
            f.flush()
        checks.append(("receipt storage", True, str(receipt_dir)))
    except Exception as exc:
        checks.append(("receipt storage", False, str(exc)))

    for binary in ("tesseract", "pdftoppm"):
        path = shutil.which(binary)
        checks.append((binary, bool(path), path or "not found"))

    failed = False
    for name, ok, detail in checks:
        failed = failed or not ok
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
