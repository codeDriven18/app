"""
Bozorlik AI — отдельный Telegram-бот (aiogram, long polling).

Пользователь пишет или диктует список покупок прямо в чате с ботом
(текст — на русском или узбекском, голос — на русском). Бот прогоняет
сообщение через AI-парсер бэкенда (/api/chat, /api/voice), список
сохраняется как активный, и в ответ приходит подтверждение с кнопкой,
открывающей мини-приложение: start_param "botlist" переносит пользователя
на экран чата и проигрывает анимацию импорта (см. index.html).

Запуск отдельным процессом рядом с бэкендом:
    python bot.py

Переменные окружения (.env):
    TELEGRAM_BOT_TOKEN — токен бота (обязателен)
    BACKEND_URL        — адрес FastAPI-бэкенда (по умолчанию http://127.0.0.1:8000)
    BOT_USERNAME       — username бота для deep-link (по умолчанию BozorlikAI_bot)
"""

import asyncio
import html
import io
import logging
import os
import re
from typing import Any, Dict, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, PreCheckoutQuery
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
BOT_USERNAME = os.getenv("BOT_USERNAME", "BozorlikAI_bot")
# Тот же ключ, что BOZORLIK_INTERNAL_KEY бэкенда: только бот может активировать
# оплаченную подписку. Пустой = проверка на бэкенде выключена (локальная разработка).
BOZORLIK_INTERNAL_KEY = os.getenv("BOZORLIK_INTERNAL_KEY", "")

# GPT-парсинг и Whisper на бэкенде могут занять несколько секунд
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=90)
MAX_VOICE_BYTES = 8 * 1024 * 1024  # согласовано с MAX_VOICE_FILE_SIZE_MB бэкенда

logger = logging.getLogger("bozorlik.bot")

MESSAGES = {
    "ru": {
        "welcome": ("👋 Привет! Я Bozorlik AI.\n\n"
                    "Напишите или продиктуйте список покупок прямо сюда — например: "
                    "«2 кг картошки, молоко, хлеб» — и я сохраню его. "
                    "В приложении вы сможете отметить покупки и сравнить цены в магазинах."),
        "saved_title": "✅ Сохранил ваш список базара!",
        "items_line": "🧾 Товаров: {count}",
        "approx": "≈ {price} сум",
        "more_items": "…и ещё {n}",
        "open_hint": "Нажмите кнопку ниже — список уже ждёт вас в приложении 👇",
        "open_app": "🛒 Открыть мой список",
        "open_app_plain": "🛒 Открыть Bozorlik AI",
        "voice_failed": "Не удалось распознать голосовое сообщение. Попробуйте ещё раз или напишите список текстом.",
        "unsupported": "Пришлите список текстом или голосовым сообщением — например: «2 кг картошки, молоко, хлеб».",
        "error": "Что-то пошло не так. Попробуйте ещё раз чуть позже.",
        "pro_paid": "🧡 Оплата получена — подписка <b>Bozorlik AI Pro</b> активна{until}!\n\nСемейный список, «Я на базаре», сканирование чеков и доставка без сервисного сбора уже работают. Спасибо, что с нами!",
        "pro_paid_until": " до {date}",
        "pro_activation_failed": "Оплата получена, но активация подписки затянулась. Откройте приложение через минуту — если Pro не активен, напишите в поддержку: @bozorlikai_support_bot",
        "pro_bad_invoice": "Не удалось проверить счёт. Откройте приложение и оформите подписку заново.",
    },
    "uz": {
        "welcome": ("👋 Salom! Men Bozorlik AI.\n\n"
                    "Xaridlar ro'yxatini shu yerga yozing yoki ovozli xabar yuboring — masalan: "
                    "«2 kg kartoshka, sut, non» — men uni saqlayman. "
                    "Ilovada xaridlarni belgilab, do'kon narxlarini solishtirasiz."),
        "saved_title": "✅ Bozorlik ro'yxatingizni saqladim!",
        "items_line": "🧾 Mahsulotlar: {count}",
        "approx": "≈ {price} so'm",
        "more_items": "…va yana {n} ta",
        "open_hint": "Quyidagi tugmani bosing — ro'yxat ilovada sizni kutmoqda 👇",
        "open_app": "🛒 Ro'yxatimni ochish",
        "open_app_plain": "🛒 Bozorlik AI'ni ochish",
        "voice_failed": "Ovozli xabarni tanib bo'lmadi. Qayta urinib ko'ring yoki ro'yxatni matn bilan yozing.",
        "unsupported": "Ro'yxatni matn yoki ovozli xabar bilan yuboring — masalan: «2 kg kartoshka, sut, non».",
        "error": "Nimadir xato ketdi. Birozdan keyin qayta urinib ko'ring.",
        "pro_paid": "🧡 To'lov qabul qilindi — <b>Bozorlik AI Pro</b> obunasi faol{until}!\n\nOilaviy ro'yxat, «Men bozordaman», chek skaneri va servis yig'imisiz yetkazib berish allaqachon ishlayapti. Biz bilan ekaningiz uchun rahmat!",
        "pro_paid_until": " {date} gacha",
        "pro_activation_failed": "To'lov qabul qilindi, lekin obunani faollashtirish cho'zildi. Bir daqiqadan so'ng ilovani oching — Pro faol bo'lmasa, supportga yozing: @bozorlikai_support_bot",
        "pro_bad_invoice": "Hisobni tekshirib bo'lmadi. Ilovani ochib, obunani qaytadan rasmiylashtiring.",
    },
}


def detect_message_language(text: str, fallback: str = "ru") -> str:
    """ru/uz по алфавиту сообщения: узбекский в приложении — латиница,
    русский — кириллица; при ничьей — язык из fallback."""
    cyr = len(re.findall(r"[а-яё]", text or "", re.IGNORECASE))
    lat = len(re.findall(r"[a-z]", text or "", re.IGNORECASE))
    if lat > cyr:
        return "uz"
    if cyr > lat:
        return "ru"
    return fallback if fallback in ("ru", "uz") else "ru"


def user_fallback_language(message: Message) -> str:
    code = (getattr(message.from_user, "language_code", "") or "").lower()
    return "uz" if code.startswith("uz") else "ru"


def open_app_markup(lang: str, saved_list: bool = True) -> InlineKeyboardMarkup:
    """Кнопка-ссылка в мини-приложение; start_param "botlist" ведёт на экран
    чата с только что сохранённым списком."""
    if saved_list:
        url = f"https://t.me/{BOT_USERNAME}?startapp=botlist"
        text = MESSAGES[lang]["open_app"]
    else:
        url = f"https://t.me/{BOT_USERNAME}?startapp"
        text = MESSAGES[lang]["open_app_plain"]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


def build_saved_list_text(list_data: Dict[str, Any], lang: str, preview_limit: int = 12) -> str:
    """Подтверждение в чате: заголовок, превью товаров, итоги."""
    msgs = MESSAGES[lang]
    lines = []
    for items in (list_data.get("categories") or {}).values():
        for item in items:
            name = html.escape(str(item.get("name") or "").strip())
            qty = html.escape(str(item.get("quantity") or "").strip())
            if name:
                lines.append(f"• {name}" + (f" — {qty}" if qty else ""))
    hidden = len(lines) - preview_limit
    if hidden > 0:
        lines = lines[:preview_limit] + [msgs["more_items"].format(n=hidden)]

    totals = msgs["items_line"].format(count=list_data.get("total_items") or len(lines))
    try:
        price = int(round(float(list_data.get("total_estimated_price") or 0)))
    except (TypeError, ValueError):
        price = 0
    if price > 0:
        totals += " · " + msgs["approx"].format(price=f"{price:,}".replace(",", " "))

    return "\n".join([f"<b>{msgs['saved_title']}</b>", "", *lines, "", totals, "", msgs["open_hint"]])


# ── вызовы бэкенда ────────────────────────────────────────────────────────────

async def api_chat(user_id: int, text: str, lang: str) -> Optional[Dict[str, Any]]:
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.post(f"{BACKEND_URL}/api/chat", json={
                "user_id": user_id, "text": text, "language": lang, "is_voice": False,
            }) as resp:
                return await resp.json()
    except Exception as e:
        logger.error(f"/api/chat failed: {e}")
        return None


async def api_activate_pro(user_id: int) -> Optional[Dict[str, Any]]:
    """Активация оплаченной подписки после successful_payment от Telegram.
    Бот — единственный доверенный источник факта оплаты."""
    headers = {"X-Internal-Key": BOZORLIK_INTERNAL_KEY} if BOZORLIK_INTERNAL_KEY else {}
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.post(f"{BACKEND_URL}/api/pro/{user_id}/subscribe", headers=headers) as resp:
                return await resp.json()
    except Exception as e:
        logger.error(f"/api/pro/subscribe failed: {e}")
        return None


async def api_voice(user_id: int, audio: bytes, filename: str = "voice.ogg") -> Optional[Dict[str, Any]]:
    # Голосовой ввод — только русский (как и в мини-приложении: узбекского STT нет)
    form = aiohttp.FormData()
    form.add_field("user_id", str(user_id))
    form.add_field("language", "ru")
    form.add_field("voice_file", audio, filename=filename, content_type="audio/ogg")
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.post(f"{BACKEND_URL}/api/voice", data=form) as resp:
                return await resp.json()
    except Exception as e:
        logger.error(f"/api/voice failed: {e}")
        return None


# ── обработчики ───────────────────────────────────────────────────────────────

router = Router()
router.message.filter(F.chat.type == "private")


async def reply_with_result(message: Message, data: Optional[Dict[str, Any]], lang: str) -> None:
    if data and data.get("success") and data.get("type") == "shopping_list" and data.get("data"):
        await message.answer(build_saved_list_text(data["data"], lang),
                             reply_markup=open_app_markup(lang))
    elif data and data.get("success") and data.get("type") == "message" and data.get("message"):
        await message.answer(html.escape(str(data["message"])))
    else:
        await message.answer(MESSAGES[lang]["error"])


@router.message(CommandStart())
@router.message(Command("help"))
async def on_start(message: Message) -> None:
    lang = user_fallback_language(message)
    await message.answer(MESSAGES[lang]["welcome"],
                         reply_markup=open_app_markup(lang, saved_list=False))


@router.message(F.voice | F.audio)
async def on_voice(message: Message) -> None:
    lang = "ru"  # голос распознаём только по-русски
    voice = message.voice or message.audio
    if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
        await message.answer(MESSAGES[lang]["voice_failed"])
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    buffer = io.BytesIO()
    try:
        await message.bot.download(voice, destination=buffer)
    except Exception as e:
        logger.error(f"Voice download failed: {e}")
        await message.answer(MESSAGES[lang]["voice_failed"])
        return
    data = await api_voice(message.from_user.id, buffer.getvalue())
    if data and not data.get("success"):
        await message.answer(MESSAGES[lang]["voice_failed"])
        return
    await reply_with_result(message, data, lang)


@router.message(F.text)
async def on_text(message: Message) -> None:
    text = (message.text or "").strip()
    lang = detect_message_language(text, user_fallback_language(message))
    if not text:
        await message.answer(MESSAGES[lang]["unsupported"])
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    data = await api_chat(message.from_user.id, text, lang)
    await reply_with_result(message, data, lang)


# ── Telegram Payments: подписка Bozorlik AI Pro ───────────────────────────────
# payload инвойса: "pro:{user_id}:{provider}" (создаётся бэкендом в /api/pro/{id}/invoice)
PRO_PAYLOAD_PREFIX = "pro:"


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    """Telegram даёт 10 секунд на подтверждение счёта перед списанием."""
    lang = "uz" if (query.from_user.language_code or "").lower().startswith("uz") else "ru"
    payload = query.invoice_payload or ""
    ok = payload.startswith(PRO_PAYLOAD_PREFIX) and query.currency == "UZS"
    await query.answer(ok=ok, error_message=None if ok else MESSAGES[lang]["pro_bad_invoice"])
    if not ok:
        logger.warning(f"Rejected pre_checkout: payload={payload!r}, currency={query.currency}")


@router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    sp = message.successful_payment
    lang = user_fallback_language(message)
    payload = sp.invoice_payload or ""
    try:
        user_id = int(payload.split(":")[1])
    except (IndexError, ValueError):
        user_id = message.from_user.id  # payload повреждён — активируем плательщику
    logger.info(f"Payment received: user={user_id}, amount={sp.total_amount} {sp.currency}, "
                f"provider_charge_id={sp.provider_payment_charge_id}")

    data = await api_activate_pro(user_id)
    if data and data.get("success") and data.get("plan") == "paid":
        until = ""
        paid_until = (data.get("paid_until") or "")[:10]
        if paid_until:
            y, m, d = paid_until.split("-")
            until = MESSAGES[lang]["pro_paid_until"].format(date=f"{d}.{m}.{y}")
        await message.answer(MESSAGES[lang]["pro_paid"].format(until=until))
    else:
        # Оплата прошла, а бэкенд не ответил — не молчим, объясняем что делать
        await message.answer(MESSAGES[lang]["pro_activation_failed"])


@router.message()
async def on_other(message: Message) -> None:
    lang = user_fallback_language(message)
    await message.answer(MESSAGES[lang]["unsupported"])


# ── запуск ────────────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (.env)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    # long polling: снимаем webhook, если был настроен раньше
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info(f"Bozorlik bot started (backend: {BACKEND_URL})")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
