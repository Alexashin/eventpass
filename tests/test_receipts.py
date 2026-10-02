from pathlib import Path

import pytest
from PIL import Image
from reportlab.pdfgen import canvas

from app.db import SessionLocal
from app.services.receipt import ReceiptError, detect_receipt_kind, extract_receipt, run_checks, validate_receipt_file


def test_image_receipt_validation(tmp_path: Path):
    path = tmp_path / "receipt.jpg"
    Image.new("RGB", (400, 500), "white").save(path, "JPEG")
    assert validate_receipt_file(path) == "image"
    assert detect_receipt_kind(path) == "image"


def test_text_pdf_receipt_is_read_without_ocr(tmp_path: Path):
    path = tmp_path / "receipt.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.drawString(80, 760, "PAYMENT RECEIPT")
    pdf.drawString(80, 730, "500 RUB")
    pdf.drawString(80, 700, "31.10.2026 21:30")
    pdf.drawString(80, 670, "Reference ABCDEF123456")
    pdf.save()

    assert validate_receipt_file(path) == "pdf"
    parsed = extract_receipt(path)
    assert parsed["error"] is None
    assert parsed["amount"] == 500
    assert parsed["operation_id"] == "ABCDEF123456"


def test_pdf_page_limit(tmp_path: Path):
    path = tmp_path / "too-many.pdf"
    pdf = canvas.Canvas(str(path))
    for page in range(6):
        pdf.drawString(80, 760, f"Page {page + 1}")
        pdf.showPage()
    pdf.save()
    with pytest.raises(ReceiptError):
        validate_receipt_file(path)


def test_unknown_file_rejected(tmp_path: Path):
    path = tmp_path / "fake.pdf"
    path.write_text("not a pdf")
    with pytest.raises(ReceiptError):
        validate_receipt_file(path)


def test_checks_report_amount(tmp_path: Path):
    path = tmp_path / "receipt.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.drawString(80, 760, "500 RUB")
    pdf.drawString(80, 730, "Reference ZYXWVU987654")
    pdf.save()
    parsed = extract_receipt(path)
    with SessionLocal() as db:
        checks = run_checks(db, path, parsed)
    assert any("Сумма совпадает" in line for line in checks)


def test_image_pixel_limit(tmp_path: Path, monkeypatch):
    path = tmp_path / "large.png"
    Image.new("RGB", (100, 100), "white").save(path, "PNG")
    monkeypatch.setattr("app.services.receipt.settings.receipt_max_image_pixels", 9_000)
    with pytest.raises(ReceiptError):
        validate_receipt_file(path)
