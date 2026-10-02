import asyncio
import html
import logging
import secrets
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import get_settings
from .db import SessionLocal
from .models import Purchase, PurchaseStatus
from .logging_setup import configure_logging
from .services.purchases import PurchaseAlreadyProcessed, approve_purchase, reject_purchase
from .services.receipt import ReceiptError, dhash, extract_receipt, run_checks, sha256_file, validate_receipt_file
from .services.tickets import audit, ticket_url

settings = get_settings()
configure_logging("bot")
logger = logging.getLogger("raneparty.bot")


class BuyFlow(StatesGroup):
    waiting_name = State()
    waiting_receipt = State()


def build_dispatcher() -> Dispatcher:
    if settings.redis_url:
        storage = RedisStorage.from_url(settings.redis_url)
    else:
        storage = MemoryStorage()
    return Dispatcher(storage=storage)


dp = build_dispatcher()


def payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"Оплатить {settings.expected_amount} ₽", url=settings.payment_url)]]
    )


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BuyFlow.waiting_name)
    await message.answer(
        f"<b>{html.escape(settings.event_name)} · {html.escape(settings.event_age)}</b>\n"
        f"{html.escape(settings.event_date_display)} · начало в {html.escape(settings.event_start)}\n\n"
        f"Стоимость билета: <b>{settings.expected_amount} ₽</b>\n\n"
        "Как получить билет:\n"
        "1. Нажми кнопку оплаты ниже и переведи указанную сумму.\n"
        "2. Вернись в бот и напиши <b>имя и фамилию</b> для билета.\n"
        "3. Отправь чек <b>фотографией или PDF-файлом</b>.\n"
        "4. После проверки администратором билет придёт сюда.\n\n"
        f"{html.escape(settings.payment_note)}\n\n"
        "Сейчас напиши имя и фамилию 👇",
        parse_mode="HTML",
        reply_markup=payment_keyboard(),
        disable_web_page_preview=True,
    )


@dp.message(BuyFlow.waiting_name, F.text)
async def got_name(message: Message, state: FSMContext):
    name = " ".join((message.text or "").strip().split())
    if not 3 <= len(name) <= 200:
        await message.answer("Напиши имя и фамилию текстом, как они должны быть указаны на билете.")
        return
    await state.update_data(guest_name=name)
    await state.set_state(BuyFlow.waiting_receipt)
    await message.answer(
        f"Билет оформляем на: <b>{html.escape(name)}</b>\n\n"
        "Теперь пришли чек после оплаты. Подойдут:\n"
        "• фото / скриншот;\n"
        "• PDF-чек из банковского приложения.\n\n"
        f"Ожидаемая сумма: <b>{settings.expected_amount} ₽</b>.",
        parse_mode="HTML",
    )


def _document_suffix(message: Message) -> str | None:
    if message.photo:
        return ".jpg"
    if not message.document:
        return None

    mime = (message.document.mime_type or "").lower()
    mapping = {
        "application/pdf": ".pdf",
        "application/x-pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    if mime in mapping:
        return mapping[mime]

    # Some Telegram clients/banks send a generic MIME type. The downloaded
    # bytes are validated again by magic bytes, so extension is only a hint.
    suffix = Path(message.document.file_name or "").suffix.lower()
    return {".pdf": ".pdf", ".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".webp": ".webp"}.get(suffix)


async def save_receipt(message: Message, bot: Bot) -> tuple[Path, str]:
    suffix = _document_suffix(message)
    if suffix is None:
        raise ReceiptError("Пришли чек фотографией или PDF-файлом")

    if message.photo:
        file_id = message.photo[-1].file_id
        file_size = message.photo[-1].file_size or 0
    else:
        file_id = message.document.file_id
        file_size = message.document.file_size or 0

    if file_size and file_size > settings.receipt_max_bytes:
        raise ReceiptError(f"Файл слишком большой. Максимум {settings.receipt_max_bytes // (1024 * 1024)} МБ")

    tg_file = await bot.get_file(file_id)
    filename = f"{message.from_user.id}-{int(time.time())}-{secrets.token_hex(5)}{suffix}"
    path = settings.receipt_path / filename
    try:
        await bot.download_file(tg_file.file_path, destination=path)
        kind = validate_receipt_file(path)
        return path, kind
    except Exception:
        path.unlink(missing_ok=True)
        raise


@dp.message(BuyFlow.waiting_receipt)
async def got_receipt(message: Message, state: FSMContext, bot: Bot):
    # Do not let one account fill receipt storage with repeated pending requests.
    precheck_db = SessionLocal()
    try:
        pending = (
            precheck_db.query(Purchase.id)
            .filter(
                Purchase.telegram_user_id == str(message.from_user.id),
                Purchase.status == PurchaseStatus.PENDING,
            )
            .first()
        )
    finally:
        precheck_db.close()
    if pending:
        await message.answer("У тебя уже есть заявка на проверке. Дождись решения администратора перед новой отправкой.")
        return

    try:
        path, kind = await save_receipt(message, bot)
    except ReceiptError as exc:
        await message.answer(f"Не получилось принять чек: {exc}\n\nПришли фото/скриншот или PDF.")
        return
    except Exception:
        logger.exception("Failed to download Telegram receipt")
        await message.answer("Не получилось загрузить файл. Попробуй отправить чек ещё раз.")
        return

    data = await state.get_data()
    guest_name = data.get("guest_name", "Гость")
    db = SessionLocal()
    try:
        extracted = await asyncio.to_thread(extract_receipt, path)
        receipt_dhash = await asyncio.to_thread(dhash, path)
        checks = run_checks(db, path, extracted, current_dhash=receipt_dhash)
        purchase = Purchase(
            guest_name=guest_name,
            telegram_user_id=str(message.from_user.id),
            telegram_username=message.from_user.username,
            telegram_display_name=message.from_user.full_name,
            receipt_path=str(path),
            receipt_sha256=sha256_file(path),
            receipt_dhash=receipt_dhash,
            ocr_text=extracted.get("text"),
            parsed_amount=extracted.get("amount"),
            parsed_datetime=extracted.get("datetime"),
            parsed_operation_id=extracted.get("operation_id"),
            checks_text="\n".join(checks),
        )
        db.add(purchase)
        db.commit()
        db.refresh(purchase)
        logger.info("purchase.received purchase_id=%s telegram_user_id=%s kind=%s", purchase.id, message.from_user.id, kind)
        audit(
            db,
            "purchase.created",
            actor_label=f"tg:{message.from_user.id}",
            entity_type="purchase",
            entity_id=str(purchase.id),
            details=guest_name,
        )
        db.commit()

        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="✅ Выдать билет", callback_data=f"approve:{purchase.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{purchase.id}"),
            ]]
        )
        admin_text = (
            f"Новая заявка #{purchase.id}\n"
            f"Гость: {guest_name}\n"
            f"Telegram: @{message.from_user.username or '-'} / {message.from_user.id}\n"
            f"Формат: {'PDF' if kind == 'pdf' else 'изображение'}\n"
            f"OCR сумма: {extracted.get('amount') or 'не распознана'} ₽\n"
            f"OCR дата: {extracted.get('datetime') or 'не распознана'}\n"
            f"Операция: {extracted.get('operation_id') or 'не распознана'}\n\n"
            + "\n".join(checks)
        )
        admin_text = admin_text[:1000]

        for admin_id in settings.admin_telegram_ids:
            try:
                upload = FSInputFile(path)
                if kind == "pdf":
                    await bot.send_document(admin_id, document=upload, caption=admin_text, reply_markup=kb)
                else:
                    await bot.send_photo(admin_id, photo=upload, caption=admin_text, reply_markup=kb)
            except Exception:
                logger.exception("Failed to notify Telegram admin %s", admin_id)
                try:
                    await bot.send_message(admin_id, admin_text, reply_markup=kb)
                except Exception:
                    logger.exception("Fallback admin notification failed for %s", admin_id)

        await state.clear()
        await message.answer(
            "✅ Чек принят и отправлен администратору. После подтверждения персональный билет придёт сюда автоматически."
        )
    except Exception:
        logger.exception("Failed to create purchase")
        path.unlink(missing_ok=True)
        await message.answer("Произошла ошибка при сохранении заявки. Отправь /start и попробуй ещё раз.")
    finally:
        db.close()


@dp.callback_query(F.data.startswith("approve:"))
async def approve(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in settings.admin_telegram_ids:
        await call.answer("Нет прав", show_alert=True)
        return
    try:
        purchase_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная заявка", show_alert=True)
        return

    db = SessionLocal()
    try:
        try:
            purchase, ticket = approve_purchase(db, purchase_id, actor_label=f"tg-admin:{call.from_user.id}")
        except (LookupError, PurchaseAlreadyProcessed):
            await call.answer("Заявка уже обработана или не найдена", show_alert=True)
            return

        logger.info("purchase.approved purchase_id=%s ticket_id=%s admin_tg_id=%s", purchase.id, ticket.id, call.from_user.id)
        await bot.send_message(
            int(purchase.telegram_user_id),
            f"✅ Билет готов!\n\n{purchase.guest_name}\nПриглашение № {ticket.number}\n\n"
            f"{ticket_url(ticket)}\n\n"
            "Сразу сделай скриншот билета: на входе интернет может работать нестабильно.",
        )
        await bot.send_message(
            call.from_user.id,
            f"✅ Выдан {ticket.number}\nГость: {purchase.guest_name}\n"
            f"Telegram: @{purchase.telegram_username or '-'} / {purchase.telegram_user_id}\n"
            f"Ссылка: {ticket_url(ticket)}",
        )
        await call.answer("Билет выдан")
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    except Exception:
        logger.exception("Telegram approval failed")
        await call.answer("Ошибка при выдаче билета", show_alert=True)
    finally:
        db.close()


@dp.callback_query(F.data.startswith("reject:"))
async def reject(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in settings.admin_telegram_ids:
        await call.answer("Нет прав", show_alert=True)
        return
    try:
        purchase_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректная заявка", show_alert=True)
        return

    db = SessionLocal()
    try:
        try:
            purchase = reject_purchase(db, purchase_id, actor_label=f"tg-admin:{call.from_user.id}")
        except (LookupError, PurchaseAlreadyProcessed):
            await call.answer("Заявка уже обработана или не найдена", show_alert=True)
            return
        logger.info("purchase.rejected purchase_id=%s admin_tg_id=%s", purchase.id, call.from_user.id)
        await bot.send_message(
            int(purchase.telegram_user_id),
            "❌ Заявка отклонена администратором. Если это ошибка, свяжись с организатором.",
        )
        await call.answer("Отклонено")
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    except Exception:
        logger.exception("Telegram rejection failed")
        await call.answer("Ошибка при обработке заявки", show_alert=True)
    finally:
        db.close()


async def main():
    settings.validate_runtime()
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN is empty; bot service disabled")
        return
    bot = Bot(settings.telegram_bot_token)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await dp.storage.close()


if __name__ == "__main__":
    asyncio.run(main())
