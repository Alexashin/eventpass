import hashlib
import re
from pathlib import Path

import pytesseract
from pdf2image import convert_from_path
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Purchase

settings = get_settings()
Image.MAX_IMAGE_PIXELS = 40_000_000


class ReceiptError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_receipt_kind(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(16)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    raise ReceiptError("Поддерживаются только PDF, JPG, PNG и WEBP")


def validate_receipt_file(path: Path) -> str:
    size = path.stat().st_size
    if size <= 0:
        raise ReceiptError("Файл пустой")
    if size > settings.receipt_max_bytes:
        raise ReceiptError(f"Файл слишком большой. Максимум {settings.receipt_max_bytes // (1024 * 1024)} МБ")

    kind = detect_receipt_kind(path)
    if kind == "image":
        try:
            with Image.open(path) as img:
                width, height = img.size
                if width <= 0 or height <= 0 or width * height > settings.receipt_max_image_pixels:
                    raise ReceiptError(
                        f"Изображение слишком большое. Максимум {settings.receipt_max_image_pixels:,} пикселей"
                    )
                img.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ReceiptError("Не удалось открыть изображение чека") from exc
    else:
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted:
                try:
                    if reader.decrypt("") == 0:
                        raise ReceiptError("PDF защищён паролем")
                except Exception as exc:
                    raise ReceiptError("PDF защищён паролем") from exc
            pages = len(reader.pages)
            if pages < 1:
                raise ReceiptError("В PDF нет страниц")
            if pages > settings.receipt_max_pdf_pages:
                raise ReceiptError(f"В чеке слишком много страниц. Максимум {settings.receipt_max_pdf_pages}")
        except ReceiptError:
            raise
        except Exception as exc:
            raise ReceiptError("Не удалось открыть PDF") from exc
    return kind


def _prepare_image(img: Image.Image, max_side: int = 2600) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


def _ocr_image(img: Image.Image) -> str:
    prepared = _prepare_image(img)
    try:
        return pytesseract.image_to_string(prepared, lang="rus+eng", timeout=18)
    except RuntimeError as exc:
        raise ReceiptError("OCR не успел обработать чек") from exc


def _pdf_images(path: Path, last_page: int | None = None) -> list[Image.Image]:
    try:
        return convert_from_path(
            str(path),
            dpi=180,
            first_page=1,
            last_page=last_page or settings.receipt_ocr_pdf_pages,
            fmt="jpeg",
            thread_count=1,
            timeout=20,
        )
    except Exception as exc:
        raise ReceiptError("Не удалось преобразовать PDF для распознавания") from exc


def _receipt_preview_image(path: Path) -> Image.Image:
    kind = detect_receipt_kind(path)
    if kind == "image":
        with Image.open(path) as img:
            return _prepare_image(img.copy(), max_side=1200)
    images = _pdf_images(path, last_page=1)
    if not images:
        raise ReceiptError("Не удалось получить превью PDF")
    return _prepare_image(images[0], max_side=1200)


def dhash(path: Path, hash_size: int = 8) -> str | None:
    try:
        img = _receipt_preview_image(path).convert("L").resize((hash_size + 1, hash_size))
    except Exception:
        return None
    pixels = list(img.get_flattened_data()) if hasattr(img, "get_flattened_data") else list(img.getdata())
    bits = []
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            bits.append(pixels[offset + col] > pixels[offset + col + 1])
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{hash_size * hash_size // 4}x}"


def hamming_hex(a: str, b: str) -> int:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except Exception:
        return 999


def _parse_text(text: str) -> dict:
    compact = " ".join((text or "").split())
    amount = None
    amount_patterns = [
        r"(?:сумма|итого|перевод|плат[её]ж|зачислен)[^\d]{0,30}([\d\s]{1,10})(?:[,.]\d{2})?\s*(?:₽|руб(?:лей|ля|ль)?\.?|RUB)",
        r"([\d\s]{1,10})(?:[,.]\d{2})?\s*(?:₽|руб(?:лей|ля|ль)?\.?|RUB)",
    ]
    for pattern in amount_patterns:
        match = re.search(pattern, compact, flags=re.I)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                amount = int(digits)
                break

    receipt_datetime = None
    match = re.search(
        r"(\d{2}[./-]\d{2}[./-]\d{2,4}(?:\s+(?:в\s*)?\d{2}:\d{2}(?::\d{2})?)?)",
        compact,
        flags=re.I,
    )
    if match:
        receipt_datetime = match.group(1)

    operation_id = None
    op_patterns = [
        r"(?:операц\w*|транзакц\w*|reference|rrn|идентификатор|номер\s+(?:операции|платежа))[^A-Za-zА-Яа-я0-9]{0,12}([A-Za-z0-9-]{6,64})",
        r"(?:чек|квитанц\w*)\s*(?:№|N|номер)?\s*[:#]?\s*([A-Za-z0-9-]{8,64})",
    ]
    for pattern in op_patterns:
        match = re.search(pattern, compact, flags=re.I)
        if match:
            operation_id = match.group(1)
            break

    return {"text": text, "amount": amount, "datetime": receipt_datetime, "operation_id": operation_id, "error": None}


def extract_receipt(path: Path) -> dict:
    kind = validate_receipt_file(path)
    try:
        if kind == "image":
            with Image.open(path) as img:
                text = _ocr_image(img)
            return _parse_text(text)

        reader = PdfReader(str(path), strict=False)
        text_parts: list[str] = []
        for page in reader.pages[: settings.receipt_max_pdf_pages]:
            try:
                text_parts.append(page.extract_text() or "")
            except Exception:
                continue
        embedded_text = "\n".join(text_parts).strip()
        parsed = _parse_text(embedded_text)

        # Банковские PDF бывают текстовыми, сканами или содержат плохо извлекаемый
        # текст. Если текста мало или даже из длинного текста не удалось достать
        # сумму, добавляем OCR первых страниц и парсим объединённый результат.
        compact_len = len(" ".join(embedded_text.split()))
        if compact_len < 60 or parsed.get("amount") is None:
            ocr_parts = [_ocr_image(img) for img in _pdf_images(path)]
            text = "\n".join([embedded_text, *ocr_parts]).strip()
            return _parse_text(text)
        return parsed
    except ReceiptError as exc:
        return {"text": "", "amount": None, "datetime": None, "operation_id": None, "error": str(exc)}
    except Exception as exc:
        return {"text": "", "amount": None, "datetime": None, "operation_id": None, "error": f"Ошибка распознавания: {exc}"}


def run_checks(db: Session, path: Path, extracted: dict, current_dhash: str | None = None) -> list[str]:
    checks: list[str] = []
    try:
        kind = detect_receipt_kind(path)
        checks.append("✅ Формат: PDF" if kind == "pdf" else "✅ Формат: изображение")
    except ReceiptError:
        checks.append("⚠️ Неизвестный формат файла")

    sha = sha256_file(path)
    if db.query(Purchase).filter(Purchase.receipt_sha256 == sha).first():
        checks.append("⚠️ Этот же файл уже загружался")
    else:
        checks.append("✅ Точный дубликат не найден")

    if current_dhash is None:
        current_dhash = dhash(path)
    if current_dhash:
        near = False
        for row in db.query(Purchase.receipt_dhash).filter(Purchase.receipt_dhash.isnot(None)).all():
            if row[0] and hamming_hex(current_dhash, row[0]) <= 5:
                near = True
                break
        checks.append("⚠️ Чек визуально похож на ранее загруженный" if near else "✅ Похожих чеков не найдено")
    else:
        checks.append("ℹ️ Визуальное сравнение для этого файла недоступно")

    if extracted.get("error"):
        checks.append(f"⚠️ {extracted['error']}")

    if extracted.get("amount") is None:
        checks.append("⚠️ Сумма автоматически не распознана")
    elif extracted["amount"] == settings.expected_amount:
        checks.append(f"✅ Сумма совпадает: {extracted['amount']} ₽")
    else:
        checks.append(f"⚠️ Распознано {extracted['amount']} ₽, ожидается {settings.expected_amount} ₽")

    op_id = extracted.get("operation_id")
    if op_id:
        duplicate = db.query(Purchase).filter(Purchase.parsed_operation_id == op_id).first()
        checks.append("⚠️ Номер операции уже встречался" if duplicate else "✅ Номер операции ранее не встречался")
    else:
        checks.append("ℹ️ Номер операции не распознан")

    text_len = len(" ".join((extracted.get("text") or "").split()))
    if text_len < 25:
        checks.append("⚠️ Из чека извлечено мало текста — обязательно проверь файл вручную")

    checks.append("ℹ️ Автопроверка не подтверждает факт перевода. Билет выдаёт администратор после просмотра чека")
    return checks
