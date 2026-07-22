import os
import json
import copy
import base64
import logging
import asyncio
import secrets
import re

from urllib.parse import quote_plus
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query, Body, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from pydantic import BaseModel
import openai

# ===== LOAD ENVIRONMENT =====
load_dotenv()
BASE_DIR = Path(__file__).resolve().parent


# ===== DATABASE BACKEND RESOLUTION =====
# This MUST run before postgres_db/postgres_models are imported, because the
# JSONB column type cannot compile on SQLite and has to be swapped for JSON first.
def _patch_jsonb_for_sqlite() -> None:
    """Map the Postgres-only JSONB type to generic JSON so SQLite can build the schema."""
    import sqlalchemy
    import sqlalchemy.dialects.postgresql as _pg
    _pg.JSONB = sqlalchemy.JSON


def _postgres_reachable(url: str) -> bool:
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


def _resolve_db_url() -> Optional[str]:
    """Pick the DB URL. In development, fall back to SQLite when Postgres is down,
    so the app can be launched locally (e.g. VS Code "Run") without a DB server."""
    url = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
    env = os.getenv("ENV", "development")
    if not url:
        return None
    if url.startswith("sqlite"):
        _patch_jsonb_for_sqlite()
        return url
    if env != "production" and not _postgres_reachable(url):
        logging.warning("Postgres unreachable; falling back to SQLite (local_dev.db) for development.")
        _patch_jsonb_for_sqlite()
        return f"sqlite:///{(BASE_DIR / 'local_dev.db').as_posix()}"
    return url


_EFFECTIVE_DB_URL = _resolve_db_url()

from postgres_db import PostgresDatabaseManager
from postgres_shared_repository import PostgresSharedListRepository

try:
    from mini_app.shared_storage import JsonSharedListRepository, SharedListService
except ImportError:
    from shared_storage import JsonSharedListRepository, SharedListService


# ===== CONFIGURATION =====
class Config:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    PORT: int = int(os.getenv("PORT", "8000"))
    ENV: str = os.getenv("ENV", "development")

    # OpenAI models
    CHAT_MODEL: str = os.getenv("CHAT_MODEL", "gpt-4o-mini")
    STT_MODEL: str = os.getenv("STT_MODEL", "whisper-1")
    OCR_MODEL: str = os.getenv("OCR_MODEL", "gpt-4.1-mini-2025-04-14")
    # GPT-4.1 Vision does all receipt recognition (no regex / manual OCR).
    RECEIPT_MODEL: str = os.getenv("RECEIPT_MODEL", "gpt-4.1-mini-2025-04-14")

    # Files
    PRICES_FILE: str = os.getenv("PRICES_FILE", str((BASE_DIR / "prices.json").resolve()))
    RECIPES_FILE: str = os.getenv("RECIPES_FILE", str((BASE_DIR / "recipes.json").resolve()))
    DB_FILE: str = os.getenv("DB_FILE", str((BASE_DIR / "bozorlik.db").resolve()))
    SHARED_LISTS_FILE: str = os.getenv("SHARED_LISTS_FILE", str((BASE_DIR / "shared_lists.json").resolve()))
    CORS_ALLOWED_ORIGINS: str = os.getenv("CORS_ALLOWED_ORIGINS", "*")
    MAX_VOICE_FILE_SIZE_MB: int = int(os.getenv("MAX_VOICE_FILE_SIZE_MB", "8"))
    MAX_PHOTO_FILE_SIZE_MB: int = int(os.getenv("MAX_PHOTO_FILE_SIZE_MB", "10"))

    # Timeouts (seconds)
    OPENAI_TIMEOUT: int = 60
    HTTP_REQUEST_TIMEOUT: int = 30


# Validate environment variables
if not Config.OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY is not set. GPT chat, Whisper transcription and photo OCR will be unavailable.")

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO if Config.ENV == "production" else logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ===== LOCALIZATION =====
LOCALIZATION = {
    "ru": {
        "kg": "кг", "g": "г", "l": "л", "ml": "мл", "pcs": "шт", "currency": "сум",
        "total_items": "Всего товаров:", "purchased": "Куплено:", "estimated_total": "Примерная сумма:",
        "share": "Поделиться", "clear": "Очистить", "quick_add": "Быстрое добавление",
        "quick_add_placeholder": "Название товара...", "quick_add_quantity": "Количество",
        "quick_add_button": "Добавить", "quick_add_hint": "AI определит категорию и рассчитает цену",
        "category_total": "Итого по категории:", "shared_list_title": "Общий список покупок",
        "shared_list_from": "Список от пользователя", "add_to_my_list": "Добавить в мой список"
    },
    "uz": {
        "kg": "kg", "g": "g", "l": "l", "ml": "ml", "pcs": "dona", "currency": "so'm",
        "total_items": "Jami mahsulotlar:", "purchased": "Xarid qilingan:", "estimated_total": "Taxminiy summa:",
        "share": "Ulashish", "clear": "Tozalash", "quick_add": "Tez qo'shish",
        "quick_add_placeholder": "Mahsulot nomi...", "quick_add_quantity": "Miqdori",
        "quick_add_button": "Qo'shish", "quick_add_hint": "AI kategoriya va narxni aniqlaydi",
        "category_total": "Kategoriya summasi:", "shared_list_title": "Umumiy xaridlar ro'yxati",
        "shared_list_from": "Foydalanuvchi ro'yxati", "add_to_my_list": "Mening ro'yxatimga qo'shish"
    }
}

# Unit mapping for normalization
UNIT_MAPPING = {
    "килограмм": "kg", "килограмма": "kg", "килограммов": "kg", "кило": "kg", "кг": "kg",
    "грамм": "g", "грамма": "g", "граммов": "g", "гр": "g", "г": "g",
    "литр": "l", "литра": "l", "литров": "l", "л": "l",
    "миллилитр": "ml", "миллилитра": "ml", "миллилитров": "ml", "мл": "ml",
    "штук": "pcs", "штука": "pcs", "штуки": "pcs", "шт": "pcs",
    "kilo": "kg", "kilogram": "kg", "kg": "kg",
    "gram": "g", "g": "g",
    "litr": "l", "l": "l",
    "millilitr": "ml", "ml": "ml",
    "dona": "pcs", "ta": "pcs", "дона": "pcs", "донa": "pcs",
    "пачка": "pack", "пакет": "pack", "упаковка": "pack", "уп": "pack", "qadoq": "pack",
    "банка": "jar", "бутылка": "bottle", "banka": "jar",
}

# Longest-first so "миллилитр" is tried before "мл", "мл" before "л", etc.
_UNIT_PATTERNS_BY_LENGTH = sorted(UNIT_MAPPING.items(), key=lambda kv: len(kv[0]), reverse=True)


def _find_unit_in_text(text_lower: str) -> Optional[str]:
    """Find a measurement unit as a whole token in a quantity string.

    Letters must not touch the unit (so "л" never matches inside "мл"), but
    digits may ("500г", "1.5l"). Plain substring search wrongly turned
    "500 мл" into "500 л".
    """
    for pattern, unit in _UNIT_PATTERNS_BY_LENGTH:
        if re.search(r"(?<![^\W\d])" + re.escape(pattern) + r"(?![^\W\d])", text_lower, flags=re.UNICODE):
            return unit
    return None

# Spoken number words (RU + UZ) → digit string. Voice transcription often returns
# "два килограмма" / "ikki kilo" instead of "2 кг", which broke quantity parsing.
NUMBER_WORDS = {
    # Russian
    "ноль": "0", "один": "1", "одна": "1", "одно": "1", "одну": "1",
    "два": "2", "две": "2", "пара": "2", "пару": "2", "три": "3", "четыре": "4",
    "пять": "5", "шесть": "6", "семь": "7", "восемь": "8", "девять": "9",
    "десять": "10", "одиннадцать": "11", "двенадцать": "12", "дюжина": "12",
    "тринадцать": "13", "четырнадцать": "14", "пятнадцать": "15",
    "шестнадцать": "16", "семнадцать": "17", "восемнадцать": "18",
    "девятнадцать": "19", "двадцать": "20", "тридцать": "30", "сорок": "40",
    "пятьдесят": "50", "шестьдесят": "60", "семьдесят": "70",
    "восемьдесят": "80", "девяносто": "90", "сто": "100",
    "пол": "0.5", "половина": "0.5", "полкило": "0.5", "полтора": "1.5",
    # Uzbek
    "bir": "1", "ikki": "2", "ikkita": "2", "uch": "3", "uchta": "3",
    "to'rt": "4", "tort": "4", "besh": "5", "olti": "6", "yetti": "7",
    "sakkiz": "8", "to'qqiz": "9", "to'qiz": "9", "o'n": "10", "on": "10",
    "o'nbir": "11", "o'nikki": "12", "yarim": "0.5",
    "yigirma": "20", "o'ttiz": "30", "ottiz": "30", "qirq": "40",
    "ellik": "50", "oltmish": "60", "yetmish": "70", "sakson": "80",
    "to'qson": "90", "toqson": "90", "yuz": "100",
    # English (transcription sometimes slips into EN number words)
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "dozen": "12", "half": "0.5",
}

# Obscene / illegal / unsafe items that must never be turned into a shopping item.
# Matched as whole tokens (exact) and via the substring roots below.
BLOCKED_EXACT_WORDS = {
    # RU profanity / explicit
    "хуй", "хуи", "хуя", "хую", "пизда", "пизду", "пизды", "пизде", "пиздец",
    "ебать", "ебал", "блядь", "бля", "сука", "мудак", "хер", "залупа",
    "сиськи", "сиська", "сись", "член", "дрочить", "порно", "порнуха",
    # weapons
    "автомат", "калаш", "калашников", "калашникова", "пистолет", "винтовка",
    "патрон", "патроны", "граната", "тротил", "взрывчатка", "ствол",
    # drugs
    "наркотик", "наркотики", "героин", "кокаин", "марихуана", "гашиш",
    "анаша", "травка", "амфетамин", "мефедрон", "лсд", "экстази", "спайс",
    # UZ profanity / illegal
    "qo'toq", "qotoq", "am", "jalab", "ko'tagim", "sik", "sikaman",
    "narkotik", "geroin", "kokain", "qurol", "avtomat", "pistolet", "granata",
}

# Substring roots — block any product whose name contains one of these.
BLOCKED_SUBSTRINGS = [
    "хуй", "пизд", "ебат", "ебан", "блядь", "сиськ", "порно", "наркот",
    "героин", "кокаин", "марихуан", "гашиш", "амфетамин", "мефедрон",
    "калашников", "взрывчат", "тротил", "граната",
    "narkotik", "geroin", "kokain", "marixuan", "qurol", "granata",
]


def _is_blocked_product(name: str) -> bool:
    """Return True if a candidate product name is obscene / illegal / unsafe."""
    if not name:
        return False
    lowered = name.lower()
    tokens = re.findall(r"[\w'’\-]+", lowered, flags=re.UNICODE)
    for token in tokens:
        cleaned = token.strip("'’-")
        if cleaned in BLOCKED_EXACT_WORDS:
            return True
    return any(sub in lowered for sub in BLOCKED_SUBSTRINGS)


def _strip_blocked_words(text: str) -> str:
    """Remove obscene/illegal tokens from text, keeping the safe remainder."""
    if not text:
        return text

    def _keep(match: "re.Match") -> str:
        token = match.group(0)
        if _is_blocked_product(token):
            return " "
        return token

    return re.sub(r"[\w'’\-]+", _keep, text, flags=re.UNICODE)


def _has_product_signal(text: str) -> bool:
    """True if text still has a usable (non-filler, non-number, non-unit) word."""
    if not text or not text.strip():
        return False
    for token in re.findall(r"[\w'’\-]+", text.lower(), flags=re.UNICODE):
        cleaned = token.strip("'’-")
        if not cleaned or re.fullmatch(r"\d+[.,]?\d*", cleaned):
            continue
        if cleaned in UNIT_MAPPING or cleaned in SHOPPING_FILLER_WORDS:
            continue
        return True
    return False


def _convert_number_words(text: str) -> str:
    """Replace standalone spoken number words with digits (RU + UZ + EN)."""
    if not text:
        return text

    def _sub(match: "re.Match") -> str:
        word = match.group(0)
        return NUMBER_WORDS.get(word.lower(), word)

    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in sorted(NUMBER_WORDS, key=len, reverse=True)) + r")\b",
        re.IGNORECASE | re.UNICODE,
    )
    converted = pattern.sub(_sub, text)
    # Collapse compound spoken numbers: "двадцать пять" → "20 5" → "25",
    # "o'n bir" → "10 1" → "11", "сто двадцать" → "100 20" → "120".
    converted = re.sub(r"\b100\s+([1-9]0)\b", lambda m: str(100 + int(m.group(1))), converted)
    converted = re.sub(r"\b(\d*[1-9]0)\s+([1-9])\b", lambda m: str(int(m.group(1)) + int(m.group(2))), converted)
    return converted


# Minimum score for a DB match to be trusted enough to assign a price.
# Below this threshold the item is kept but left unpriced.
MATCH_CONFIDENCE_THRESHOLD = 800

# Scores at or above this mean the names are equivalent (exact / stems / word
# reorder). Anything lower is a partial match: the query is only part of the
# product name ("клубника" → "Мохито клубника"), so the matched product may be
# a different thing entirely and its category must not be trusted blindly.
MATCH_CONFIDENCE_EXACT = 900

# Minimum score to use the DB-matched category (vs. keyword heuristics).
MATCH_CONFIDENCE_CATEGORY_THRESHOLD = 500

# ===== LIGHT STEMMING (RU + UZ) =====
# Strips common inflection suffixes so "красной репы" matches "красная репа"
# and "kartoshkani" matches "kartoshka". Conservative: stems stay >= 3 chars.
# Product-level synonyms (картофель → Картошка) live in prices.json "aliases".
_RU_STEM_SUFFIXES = sorted([
    "иями", "ями", "ами", "ыми", "ими", "ого", "его", "ому", "ему",
    "ый", "ий", "ой", "ей", "ая", "яя", "ое", "ее", "ые", "ие",
    "ую", "юю", "ых", "их", "ым", "им", "ов", "ев", "ам", "ям",
    "ах", "ях", "ом", "ем",
    "а", "я", "ы", "и", "у", "ю", "е", "о", "ь",
], key=len, reverse=True)

_UZ_STEM_SUFFIXES = sorted([
    "laridan", "larining", "larni", "larga", "larda", "lardan",
    "ning", "lari", "lar", "dan", "da", "ga", "ni", "cha",
], key=len, reverse=True)

_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


def _stem_word(word: str) -> str:
    """Return a light stem of a single lowercase token (RU or UZ/Latin)."""
    w = word.lower().replace("ё", "е")
    if _CYRILLIC_RE.search(w):
        for suffix in _RU_STEM_SUFFIXES:
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                return w[:-len(suffix)]
        return w
    # UZ suffixes can stack: "kartoshkalarni" -> "kartoshkalar" -> "kartoshka".
    for _ in range(2):
        for suffix in _UZ_STEM_SUFFIXES:
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[:-len(suffix)]
                break
        else:
            break
    return w


def _stem_tokens(words: List[str]) -> List[str]:
    return [_stem_word(w) for w in words if w]


def _stem_phrase(text: str) -> str:
    return " ".join(_stem_tokens(text.split()))


# Unit-to-unit conversion factors for price scaling (500 г → 0.5 кг).
_UNIT_CONVERSIONS = {
    ("g", "kg"): 0.001, ("kg", "g"): 1000.0,
    ("ml", "l"): 0.001, ("l", "ml"): 1000.0,
}


def _convert_quantity_units(qty: float, from_unit: str, to_unit: str) -> Optional[float]:
    """Convert qty between units; None when units are incompatible (кг vs шт)."""
    if not from_unit or not to_unit or from_unit == to_unit:
        return qty
    factor = _UNIT_CONVERSIONS.get((from_unit, to_unit))
    return qty * factor if factor is not None else None


# Russian labels used to render base package quantities from structured data.
_RU_UNIT_LABELS = {
    "kg": "кг", "g": "г", "l": "л", "ml": "мл", "pcs": "шт",
    "pack": "пачка", "jar": "банка", "bottle": "бутылка",
}

# ===== SYSTEM PROMPTS =====
SYSTEM_PROMPTS = {
    "ru": """
Ты — Bozorlik AI, помощник для создания списков покупок.
Отвечай ТОЛЬКО на русском языке.

ПРАВИЛА:
1. Если сообщение состоит ТОЛЬКО из приветственных слов без товаров, ответь: "Привет! Что нужно купить сегодня?"
2. Если вопрос не про покупки, ответь: "Извините, я могу помочь только со списком покупок."
3. Если есть товары, составь список по категориям:

КАТЕГОРИИ (используй именно эти названия с эмодзи):
🥕 Овощи
🍎 Фрукты
🥛 Молочные продукты
🍖 Мясные продукты
📦 Бакалея
🥤 Напитки
🍵 Чай и кофе
🧂 Приправы
🧴 Гигиена и быт
🍿 Снеки
📝 Другое

ПРАВИЛА ФОРМАТИРОВАНИЯ:
- Категорию пиши с эмодзи и двоеточием (🥕 Овощи:)
- Каждый товар с новой строки, начинай с •
- Если есть количество: • Название — количество
- Если нет количества: • Название
- НИКОГДА не пиши цены в тексте!
- Сохраняй количество ТОЧНО как указал пользователь
- Названия товаров пиши с заглавной буквы
- Названия продуктов из нескольких слов — это ОДИН товар. «Красная репа», «Зеленый лист салата», «Куриное филе», «Болгарский перец» НЕЛЬЗЯ разбивать на отдельные позиции.
- Если пользователь назвал продукт синонимом (картофель = картошка, томаты = помидоры, морковка = морковь), запиши его одним товаром, не дублируй.

ВАЖНО:
- Даже если продукта, который написал пользователь, нет в базе средних цен (prices.json), AI ДОЛЖЕН составить список и включить этот продукт без указания цены (estimated price должен отсутствовать).
- НИКОГДА не заменять или не выводить сообщение "Привет! Что нужно купить сегодня?" в ответ на сообщение, где пользователь явно перечисляет продукты или диктует их голосом. Если пользователь написал продукты — всегда формируй список, даже если цены отсутствуют.

Запрос пользователя:
""",
    "uz": """
Ты — Bozorlik AI, xaridlar ro'yxatini tuzuvchi yordamchi.
FAQAT o'zbek tilida javob ber.

QOIDALAR:
1. Agar xabar FAQAT salomlashish so'zlaridan iborat bo'lsa: "Salom! Bugun nima xarid qilish kerak?"
2. Agar savol xaridlarga aloqador bo'lmasa: "Kechirasiz, men faqat xaridlar ro'yxati bilan yordam bera olaman."
3. Agar mahsulotlar bo'lsa, kategoriyalar bo'yicha ro'yxat tuz:

KATEGORIYALAR:
🥕 Sabzavotlar
🍎 Mevalar
🥛 Sut mahsulotlari
🍖 Go'sht mahsulotlari
📦 Oziq-ovqat
🥤 Ichimliklar
🍵 Choy va kofe
🧂 Ziravorlar
🧴 Gigiyena
🍿 Snacklar
📝 Boshqalar

FORMAT:
- Kategoriya: emoji va : (🥕 Sabzavotlar:)
- Har bir mahsulot • bilan boshlanadi
- Miqdor bo'lsa: • Nomi — miqdor
- Miqdor bo'lmasa: • Nomi
- NARXLARNI YOZMANG!
- Mahsulot nomlarini bosh harf bilan yozing
- Bir nechta so'zdan iborat mahsulot nomlari BITTA mahsulot: «Qizil sholg'om», «Yashil salat bargi», «Tovuq filesi» alohida qatorlarga bo'linmasin.

MUHIM:
- Agar foydalanuvchi yozgan mahsulot prices.json ichida bo'lmasa ham, AI ro'yxatni tuzishi va ushbu mahsulotni narxsiz kiritishi kerak (estimated price bo'lmasin).
- Foydalanuvchi aniq mahsulotlarni yozgan yoki ovoz bilan diktirlagan bo'lsa, "Salom! Bugun nima xarid qilish kerak?" kabi salomlashuv javobini hech qachon chiqarmang. Har doim ro'yxat tuzing, narxlar bo'lmasa ham.

Foydalanuvchi so'rovi:
"""
}

SYSTEM_PROMPT_EDIT = {
    "ru": """
Ты — AI для редактирования списка покупок. Определи изменения из сообщения.
Ответь ТОЛЬКО в формате JSON:
{
  "changes": [{
    "action": "add/remove/replace/update",
    "target": "название продукта",
    "new_item": "новый продукт",
    "quantity": "количество ТОЧНО КАК УКАЗАНО",
    "category": "категория"
  }]
}
Сообщение:
""",
    "uz": """
Siz — xaridlar ro'yxatini tahrirlovchi AI. Xabardan o'zgarishlarni aniqlang.
FAQAT JSON formatida javob bering:
{
  "changes": [{
    "action": "add/remove/replace/update",
    "target": "mahsulot nomi",
    "new_item": "yangi mahsulot",
    "quantity": "miqdor AYNAN foydalanuvchi yozganidek",
    "category": "kategoriya"
  }]
}
Xabar:
"""
}

# Extracts the dish name and headcount from a free-form request like
# "Хочу приготовить плов на 10 человек". servings is null if not mentioned.
SYSTEM_PROMPT_RECIPE = {
    "ru": """
Ты определяешь блюдо и количество человек из сообщения пользователя.
Ответь ТОЛЬКО в формате JSON:
{"dish": "название блюда в нижнем регистре", "servings": число или null}
Если количество человек не указано — верни "servings": null.
Не добавляй ничего кроме JSON.
Сообщение:
""",
    "uz": """
Foydalanuvchi xabaridan taom nomini va odamlar sonini aniqlaysan.
FAQAT JSON formatida javob ber:
{"dish": "taom nomi kichik harflarda", "servings": son yoki null}
Agar odamlar soni ko'rsatilmagan bo'lsa — "servings": null qaytar.
JSONdan boshqa hech narsa qo'shma.
Xabar:
"""
}


# ===== PYDANTIC MODELS =====
class ChatMessage(BaseModel):
    user_id: int
    text: str
    language: str = "ru"
    is_voice: bool = False
    is_quick_add: bool = False


class QuickAddRequest(BaseModel):
    user_id: int
    name: str
    quantity: str = ""
    language: str = "ru"


class EditRequest(BaseModel):
    user_id: int
    text: str
    language: str = "ru"


class ShareRequest(BaseModel):
    user_id: int
    list_id: str


class ExpenseConfirmRequest(BaseModel):
    user_id: int
    list_id: str
    confirmed: bool = True
    save_to_history: bool = True


class ItemEditRequest(BaseModel):
    user_id: int
    category: str
    old_item_name: str
    new_item_name: str
    new_quantity: str = ""


class TogglePurchaseRequest(BaseModel):
    category: str
    item_name: str


class ToggleCategoryRequest(BaseModel):
    category: str


class SetLanguageRequest(BaseModel):
    user_id: int
    language: str


class AddSharedListRequest(BaseModel):
    user_id: int
    shared_list_id: str
    language: str = "ru"


class SharedToggleRequest(BaseModel):
    # item_name is None => toggle the whole category
    category: str
    item_name: Optional[str] = None


class ProStatusRequest(BaseModel):
    is_pro: bool


class ProInvoiceRequest(BaseModel):
    provider: str = "payme"  # payme | click


class RecipeListRequest(BaseModel):
    user_id: int
    text: str
    language: str = "ru"
    add_to_list: bool = True


class BazaarSayRequest(BaseModel):
    user_id: int = 0
    text: str = ""
    language: str = "ru"


class BudgetRequest(BaseModel):
    amount: float = 0


# SQLite database manager removed for deployment; Postgres is required

# Initialize database backend. _EFFECTIVE_DB_URL is the Postgres URL in production,
# or a SQLite fallback in development when Postgres is unreachable (see _resolve_db_url).
if not _EFFECTIVE_DB_URL:
    raise RuntimeError('POSTGRES_URL or DATABASE_URL must be set for deployment')
_backend = 'SQLite (local dev)' if _EFFECTIVE_DB_URL.startswith('sqlite') else 'Postgres'
logger.info(f"Using {_backend} database backend: {_EFFECTIVE_DB_URL}")
db = PostgresDatabaseManager(_EFFECTIVE_DB_URL)
# wire shared list repository to Postgres-backed repo
shared_repo = PostgresSharedListRepository(db)
shared_list_service = SharedListService(shared_repo)


# ===== UTILITY FUNCTIONS =====
def render_shared_page_html(token: str) -> str:
    """Return the main app shell with an optional injected shared-token variable."""
    index_path = BASE_DIR / "index.html"
    html = index_path.read_text(encoding="utf-8")
    if not token:
        return html
    token_script = f"<script>window.BOZORLIK_SHARED_TOKEN = {json.dumps(token)};</script>"
    if "</head>" in html:
        return html.replace("</head>", f"    {token_script}\n</head>", 1)
    return token_script + html


def capitalize_first_letter(text: str) -> str:
    """Capitalize first letter of string"""
    if not text or not text.strip():
        return text
    text = text.strip()
    if text and text[0].isalpha():
        return text[0].upper() + text[1:]
    return text


def normalize_quantity_display(qty_text: str, target_lang: str = "ru") -> str:
    """Normalize quantity display with localization"""
    if not qty_text or not qty_text.strip():
        return ""

    original = qty_text.strip()
    qty_text_lower = original.lower()

    number_match = re.search(r'(\d+[.,]?\d*)', qty_text_lower)
    if not number_match:
        return original

    number_str = number_match.group(1).replace(',', '.')
    try:
        number = float(number_str)
        number_str = str(int(number)) if number.is_integer() else str(number)
    except ValueError:
        return original

    found_unit = _find_unit_in_text(qty_text_lower)

    if found_unit:
        localized_unit = LOCALIZATION[target_lang].get(found_unit, found_unit)
        return f"{number_str} {localized_unit}".strip()
    return number_str


def format_price_with_currency(price: Optional[int], lang: str = "ru") -> str:
    """Format price with currency"""
    if price is None:
        return ""
    formatted_price = f"{price:,}".replace(",", " ")
    currency = LOCALIZATION[lang]["currency"]
    return f"{formatted_price} {currency}"


# ===== PRICE DATABASE (JSON-based, unchanged) =====
class PriceDatabase:
    def __init__(self, prices_file: str):
        self.prices_file = prices_file
        self.data = None
        self.category_defs = {}
        self.display_by_key = {}
        self.items_by_id = {}
        self.search_index_ru = {}
        self.search_index_uz = {}
        self.synonym_index_ru = {}
        self.synonym_index_uz = {}
        self.synonyms_ru = {}
        self.synonyms_uz = {}
        # Data-driven product aliases (prices.json "aliases"): synonym → canonical name.
        self.alias_map = {"ru": {}, "uz": {}}
        self.alias_stem_map = {"ru": {}, "uz": {}}
        # Stemmed product names → canonical normalized name ("красной репы" → "красная репа").
        self.name_stem_map = {"ru": {}, "uz": {}}
        self.spices_keywords = [
            "зира", "приправа", "плов", "шашлык", "самса", "фунчоза",
            "мак", "лимонная кислота", "сахарная пудра", "чёрный перец",
            "красный перец", "паприка", "крахмал", "zira", "palov",
            "shashlik", "somsa", "funchoza", "ko'knor", "limon kislotasi",
            "shakar kukuni", "qora murch", "qizil qalampir", "paprika", "kraxmal"
        ]
        self.display_category_map = {
            "🥕 Овощи": {"db_categories": ["Овощи"],
                        "keywords": ["овощ", "картош", "лук", "чеснок", "помидор", "огурец", "перец", "морков", "редис",
                                     "имбирь", "кабачок", "репа", "зелень", "укроп", "кинза", "петрушка", "салат",
                                     "капуст", "баклажан", "свекл", "свёкл", "тыкв", "шпинат", "редьк", "сельдерей"]},
            "🍎 Фрукты": {"db_categories": ["Фрукты"],
                         "keywords": ["фрукт", "яблок", "груш", "гранат", "апельсин", "мандарин", "киви", "ليمون",
                                      "банан", "ананас", "ягод", "клубник", "малин", "виноград", "вишн", "черешн",
                                      "персик", "абрикос", "арбуз", "дын", "хурм", "слива", "сливы", "инжир",
                                      "черник", "смородин", "айв", "нектарин", "манго"]},
            "🥛 Молочные продукты": {"db_categories": ["Молочные продукты"],
                                    "keywords": ["молок", "сыр", "творог", "сметан", "кефир", "йогурт", "ряженк",
                                                 "сливк", "масло сливочн"]},
            "🍖 Мясные продукты": {"db_categories": ["Мясные продукты"],
                                  "keywords": ["мясо", "мясн", "фарш", "колбас", "сосиск", "куриц", "курин",
                                               "куриное", "куриная", "куриный", "грудк", "филе", "окорочк",
                                               "окорок", "голен", "крылыш", "крыл", "индейк", "индюш", "утк",
                                               "гусь", "гуся", "говяд", "говядин", "свинин", "свин",
                                               "баранин", "баран", "ветчин", "тушенк", "субпродукт",
                                               "печень", "сердце", "язык говяж"]},
            "📦 Бакалея": {"db_categories": ["Бакалея"],
                          "keywords": ["масло подсолнечн", "яйц", "мук", "макарон", "рис", "гречк", "горох", "круп",
                                       "сахар", "соль", "кетчуп", "майонез", "томатн", "консерв"]},
            "🥤 Напитки": {"db_categories": ["Напитки"],
                          "keywords": ["вода", "сок", "кола", "пепси", "фант", "спрайт", "липтон", "чай холодн",
                                       "энергетик", "мохито"]},
            "🍵 Чай и кофе": {"db_categories": ["Чай и кофе"], "keywords": ["чай", "кофе", "какао"]},
            "🧂 Приправы": {"db_categories": ["Приправы"],
                           "keywords": ["приправ", "специ", "зира", "паприк", "перец молот", "крахмал"]},
            "🧴 Гигиена и быт": {"db_categories": ["Бакалея"],
                                "keywords": ["мыло", "шампун", "гель", "порошок", "бумаг", "салфетк", "паста зубн",
                                             "щетк", "освежитель", "стиральн"]},
            "🍿 Снеки": {"db_categories": ["Бакалея"], "keywords": ["чипс", "сухарик", "лаваш", "пицц", "снек"]},
            "📝 Другое": {"db_categories": [], "keywords": []},
            "🥕 Sabzavotlar": {"db_categories": ["Овощи"],
                              "keywords": ["sabzavot", "kartoshk", "piyoz", "sarimsoq", "pomidor", "bodring",
                                           "qalampir", "sabzi", "turp", "zanjabil", "qovoq", "sholg'om", "ko'kat",
                                           "karam", "baqlajon", "lavlagi", "ismaloq"]},
            "🍎 Mevalar": {"db_categories": ["Фрукты"],
                          "keywords": ["meva", "olma", "nok", "anor", "apelsin", "mandarin", "kivi", "limon", "banan",
                                       "ananas", "qulupnay", "malina", "gilos", "olcha", "shaftoli", "o'rik", "uzum",
                                       "tarvuz", "qovun", "xurmo", "anjir", "behi", "smorodina"]},
            "🥛 Sut mahsulotlari": {"db_categories": ["Молочные продукты"],
                                   "keywords": ["sut", "pishloq", "tvorog", "smetan", "kefir", "yogurt", "qaymoq"]},
            "🍖 Go'sht mahsulotlari": {"db_categories": ["Мясные продукты"],
                                      "keywords": ["go'sht", "gosht", "qiyma", "kolbasa", "sosiska", "tovuq",
                                                   "tovuq go'shti", "mol go'shti", "filе", "file", "fileси",
                                                   "ko'krak", "kokrak", "qo'y go'shti", "qoramol", "jigar",
                                                   "yurak", "til", "parranda"]},
            "📦 Oziq-ovqat": {"db_categories": ["Бакалея"],
                             "keywords": ["yog'", "tuxum", "un", "makaron", "guruch", "grechka", "no'xat", "shakar",
                                          "tuz", "ketchup", "mayonez"]},
            "🥤 Ichimliklar": {"db_categories": ["Напитки"],
                              "keywords": ["suv", "sharbat", "kola", "pepsi", "fanta", "sprite", "choy sovuq",
                                           "energetik"]},
            "🍵 Choy va kofe": {"db_categories": ["Чай и кофе"], "keywords": ["choy", "kofe", "kakao"]},
            "🧂 Ziravorlar": {"db_categories": ["Приправы"],
                             "keywords": ["ziravor", "zira", "paprika", "qalampir", "kraxmal"]},
            "🧴 Gigiyena": {"db_categories": ["Бакалея"],
                           "keywords": ["sovun", "shampun", "qog'oz", "salfetka", "tish pastasi", "tish cho'tkasi"]},
            "🍿 Snacklar": {"db_categories": ["Бакалея"], "keywords": ["chips", "lavash", "pizza"]},
            "📝 Boshqalar": {"db_categories": [], "keywords": []},
        }
        self.category_map = {
            "vegetables": ["Овощи", "Sabzavotlar"],
            "fruits": ["Фрукты", "Mevalar"],
            "dairy": ["Молочные продукты", "Sut mahsulotlari"],
            "meat": ["Мясные продукты", "Go'sht mahsulotlari"],
            "groceries": ["Бакалея", "Oziq-ovqat"],
            "drinks": ["Напитки", "Ichimliklar"],
            "tea_coffee": ["Чай и кофе", "Choy va kofe"],
            "spices": ["Приправы", "Ziravorlar"],
        }
        self.load_data()

    def _normalize_for_index(self, name: str) -> str:
        if not name:
            return ""
        normalized = name.lower().replace("ё", "е").replace("ъ", "").replace("ъ", "")
        normalized = re.sub(r'[^\w\s]', '', normalized)
        return re.sub(r'\s+', ' ', normalized).strip()

    def _apply_direct_aliases(self, normalized_query: str, lang: str) -> str:
        """Canonicalize a normalized query using prices.json aliases and name stems.

        Resolution order:
          1. exact alias phrase           ("морковка"      → "морковь красная")
          2. stemmed alias phrase         ("морковку"      → "морковь красная")
          3. stemmed product name         ("красной репы"  → "красная репа")
          4. word-level single-word alias ("томаты розовые"→ "помидоры розовые")
        Idempotent for canonical product names.
        """
        if not normalized_query:
            return normalized_query
        alias_map = self.alias_map.get(lang, {})
        alias_stem_map = self.alias_stem_map.get(lang, {})
        name_stem_map = self.name_stem_map.get(lang, {})

        if normalized_query in alias_map:
            return alias_map[normalized_query]

        stemmed_query = _stem_phrase(normalized_query)
        hit = alias_stem_map.get(stemmed_query) or name_stem_map.get(stemmed_query)
        if hit:
            return hit

        # Word-level pass: only single-word replacements, so canonical multi-word
        # names never get duplicated words injected into them.
        words = normalized_query.split()
        replaced: List[str] = []
        changed = False
        for word in words:
            target = alias_map.get(word) or alias_stem_map.get(_stem_word(word))
            if target and target != word and " " not in target:
                replaced.append(target)
                changed = True
            else:
                replaced.append(word)
        result = " ".join(replaced)
        if changed:
            if result in alias_map:
                return alias_map[result]
            stem_hit = name_stem_map.get(_stem_phrase(result))
            if stem_hit:
                return stem_hit
        return result

    def _default_item_bonus(self, item: Dict) -> int:
        """Prefer practical defaults (1 unit) when user didn't specify quantity."""
        qty_text = (item.get("quantity") or "").lower()
        qty_match = re.search(r'(\d+[.,]?\d*)', qty_text)
        qty_value = None
        if qty_match:
            try:
                qty_value = float(qty_match.group(1).replace(',', '.'))
            except ValueError:
                qty_value = None

        unit = self._extract_unit(qty_text)
        if qty_value is None:
            return 0

        if abs(qty_value - 1.0) < 1e-6 and unit in {"kg", "l", "pcs", "pack", "jar", "bottle"}:
            return 18
        if abs(qty_value - 0.5) < 1e-6 and unit in {"kg", "l"}:
            return 10
        if unit in {"g", "ml"} and qty_value in {250, 450, 500}:
            return 8
        if unit in {"kg", "l", "pcs"} and qty_value > 1:
            return -8
        return 0

    def _word_variants(self, word: str) -> List[str]:
        """Return simple lexical variants to improve matching for inflected words (RU + UZ)."""
        variants = {word, _stem_word(word)}
        if len(word) > 4:
            variants.add(word[:-1])

        # Russian case/number and adjective suffixes
        for suffix in ["ами", "ями", "ов", "ев", "ей", "ам", "ям", "ах", "ях",
                       "ого", "его", "ому", "ему", "ый", "ий", "ой", "ая", "яя",
                       "ое", "ее", "ые", "ие", "ую", "юю", "ых", "их", "ым", "им"]:
            if word.endswith(suffix) and len(word) > len(suffix) + 2:
                variants.add(word[:-len(suffix)])

        for suffix in ["а", "я", "ы", "и", "у", "ю", "е", "о", "ь"]:
            if word.endswith(suffix) and len(word) > 3:
                variants.add(word[:-1])

        # Uzbek plural/case suffixes
        for suffix in ["lar", "ni", "ga", "da", "dan", "ning", "lik"]:
            if word.endswith(suffix) and len(word) > len(suffix) + 2:
                variants.add(word[:-len(suffix)])

        return [v for v in variants if v]

    def _query_word_in_name(self, query_word: str, indexed_name: str) -> bool:
        """Check whether query word or one of its variants matches indexed product name."""
        for variant in self._word_variants(query_word):
            if variant in indexed_name:
                return True
        return False

    def _is_per_piece_item(self, item: Dict) -> bool:
        quantity = (item.get("quantity") or "").lower()
        name_ru = (item.get("name_ru") or "").lower()
        name_uz = (item.get("name_uz") or "").lower()
        return (
            "1 шт" in quantity
            or "1 dona" in quantity
            or "за 1 шт" in name_ru
            or "1 dona" in name_uz
        )

    def _score_candidate(self, item: Dict, normalized_query: str, query_words: List[str], lang: str,
                         query_has_quantity: bool) -> int:
        item_name = self._normalize_for_index(item.get(f"name_{lang}", ""))
        if not item_name:
            item_name = self._normalize_for_index(item.get("name_ru", ""))

        score = 0

        item_words = [word for word in item_name.split() if word]
        query_stems = _stem_tokens(query_words)
        item_stems = _stem_tokens(item_words)

        if item_name == normalized_query:
            score += 10000
        elif item_stems == query_stems:
            score += 9000
        elif _is_contiguous_subsequence(query_stems, item_stems):
            score += 500
        elif _is_contiguous_subsequence(item_stems, query_stems):
            score += 250

        item_tokens = item_name.split()
        for query_word in query_words:
            if query_word in item_tokens:
                score += 20
            elif self._query_word_in_name(query_word, item_name):
                score += 10

        egg_like_query = any(token in normalized_query for token in ["яйц", "яйцо", "tuxum"])
        if egg_like_query:
            # For eggs without explicit quantity, prefer per-piece so users can control amount easily.
            if self._is_per_piece_item(item):
                score += 45 if not query_has_quantity else -10
            else:
                score -= 20 if not query_has_quantity else 15

        if self._is_per_piece_item(item) and not query_has_quantity and not egg_like_query:
            score -= 10

        if not query_has_quantity:
            score += self._default_item_bonus(item)

        return score

    @staticmethod
    def _format_base_quantity(base_qty: float, unit: str) -> str:
        """Render a structured package quantity ("qty" + "unit") as display text."""
        qty_str = str(int(base_qty)) if float(base_qty).is_integer() else str(base_qty)
        label = _RU_UNIT_LABELS.get(unit, "")
        return f"{qty_str} {label}".strip()

    def load_data(self):
        """Load the structured prices database (see prices.json):

        - "categories":     category key → {ru, uz, emoji}
        - "products":       flat list with category key, numeric qty + unit, price
        - "aliases":        synonym → canonical product name (per language)
        - "category_hints": word → category key (fallback categorization)
        """
        try:
            with open(self.prices_file, "r", encoding="utf-8") as f:
                self.data = json.load(f)

            self.category_defs = self.data.get("categories", {})
            self.display_by_key = {}
            for key, cat in self.category_defs.items():
                emoji = cat.get("emoji", "")
                self.display_by_key[key] = {
                    "ru": f"{emoji} {cat.get('ru', '')}".strip(),
                    "uz": f"{emoji} {cat.get('uz', '')}".strip(),
                }

            self.items_by_id = {}
            self.search_index_ru = {}
            self.search_index_uz = {}
            self.synonym_index_ru = {}
            self.synonym_index_uz = {}
            self.alias_map = {"ru": {}, "uz": {}}
            self.alias_stem_map = {"ru": {}, "uz": {}}
            self.name_stem_map = {"ru": {}, "uz": {}}

            item_counter = 0
            for product in self.data.get("products", []):
                category_key = product.get("category", "other")
                category = self.category_defs.get(category_key, {})
                try:
                    base_qty = float(product.get("qty", 1) or 1)
                except (TypeError, ValueError):
                    base_qty = 1.0
                unit = product.get("unit", "")
                display_qty = product.get("display") or self._format_base_quantity(base_qty, unit)

                item_ru = product.get("ru", "")
                item_uz = product.get("uz", "")
                item_id = f"item_{item_counter}"
                item_counter += 1

                item_data = {
                    "id": item_id,
                    "name_ru": item_ru,
                    "name_uz": item_uz,
                    "category_key": category_key,
                    "category_ru": category.get("ru", ""),
                    "category_uz": category.get("uz", ""),
                    "quantity": display_qty,
                    "base_qty": base_qty,
                    "price": product.get("price", 0),
                    "unit": unit,
                }
                self.items_by_id[item_id] = item_data

                norm_ru = self._normalize_for_index(item_ru)
                if norm_ru:
                    self.search_index_ru.setdefault(norm_ru, []).append(item_data)
                    self.name_stem_map["ru"].setdefault(_stem_phrase(norm_ru), norm_ru)

                norm_uz = self._normalize_for_index(item_uz)
                if norm_uz:
                    self.search_index_uz.setdefault(norm_uz, []).append(item_data)
                    self.name_stem_map["uz"].setdefault(_stem_phrase(norm_uz), norm_uz)

            # Product aliases: "морковка" → "Морковь красная" (stored normalized).
            aliases = self.data.get("aliases", {})
            for lang in ("ru", "uz"):
                for alias, target in aliases.get(lang, {}).items():
                    norm_alias = self._normalize_for_index(alias)
                    norm_target = self._normalize_for_index(target)
                    if norm_alias and norm_target and norm_alias != norm_target:
                        self.alias_map[lang][norm_alias] = norm_target
                        self.alias_stem_map[lang].setdefault(_stem_phrase(norm_alias), norm_target)

            # Category hints: single words → category key, for products not in the DB.
            hints = self.data.get("category_hints", {})
            self.synonyms_ru = hints.get("ru", {})
            self.synonyms_uz = hints.get("uz", {})
            for synonym, category_key in self.synonyms_ru.items():
                norm_syn = self._normalize_for_index(synonym)
                if norm_syn:
                    self.synonym_index_ru.setdefault(norm_syn, []).append(category_key)
            for synonym, category_key in self.synonyms_uz.items():
                norm_syn = self._normalize_for_index(synonym)
                if norm_syn:
                    self.synonym_index_uz.setdefault(norm_syn, []).append(category_key)

            logger.info(f"Loaded {item_counter} products from {self.prices_file}")
        except Exception as e:
            logger.error(f"Error loading prices: {e}")
            self.data = None

    def _extract_unit(self, quantity_str: str) -> str:
        return _find_unit_in_text(quantity_str.lower()) or ""

    def is_spice(self, product_name: str) -> bool:
        """Whole-token spice detection ("мак" matches, "макароны" does not)."""
        name_lower = product_name.lower()
        name_stems = set(_stem_tokens(re.findall(r"[\w'’\-]+", name_lower, flags=re.UNICODE)))
        for keyword in self.spices_keywords:
            if " " in keyword:
                if keyword in name_lower:
                    return True
            elif _stem_word(keyword) in name_stems:
                return True
        return False

    def find_products(self, product_name: str, lang: str = "ru") -> List[Dict]:
        normalized_query = self._normalize_for_index(product_name)
        normalized_query = self._apply_direct_aliases(normalized_query, lang)
        if not normalized_query:
            return []

        query_words = normalized_query.split()
        query_has_quantity = self.extract_quantity_from_text(product_name)[0] is not None
        scored_items: Dict[str, Tuple[int, Dict]] = {}

        def add_scored_item(item: Dict):
            item_id = item["id"]
            score = self._score_candidate(item, normalized_query, query_words, lang, query_has_quantity)
            existing = scored_items.get(item_id)
            if existing is None or score > existing[0]:
                scored_items[item_id] = (score, item)

        query_variants = {normalized_query}
        for word in query_words:
            for variant in self._word_variants(word):
                if variant and variant != word:
                    query_variants.add(normalized_query.replace(word, variant))

        search_index = self.search_index_ru if lang == "ru" else self.search_index_uz
        for candidate_query in query_variants:
            candidate_words = [w for w in candidate_query.split() if w]
            if not candidate_words:
                continue
            for idx_name, items in search_index.items():
                if all(self._query_word_in_name(word, idx_name) for word in candidate_words):
                    for item in items:
                        add_scored_item(item)

        if not scored_items:
            other_index = self.search_index_uz if lang == "ru" else self.search_index_ru
            for idx_name, items in other_index.items():
                if all(self._query_word_in_name(word, idx_name) for word in query_words):
                    for item in items:
                        add_scored_item(item)

        if not scored_items:
            synonym_index = self.synonym_index_ru if lang == "ru" else self.synonym_index_uz
            for norm_syn, category_keys in synonym_index.items():
                if self._query_word_in_name(norm_syn, normalized_query):
                    for category_key in category_keys:
                        possible_categories = self.category_map.get(category_key, [])
                        if len(possible_categories) >= 2:
                            cat_ru, cat_uz = possible_categories[0], possible_categories[1]
                            for item in self.items_by_id.values():
                                if item["category_ru"] == cat_ru or item["category_uz"] == cat_uz:
                                    add_scored_item(item)

        sorted_items = sorted(
            scored_items.values(),
            key=lambda x: (-x[0], len(x[1].get(f"name_{lang}", "")))
        )
        return [item for _, item in sorted_items[:10]]

    def choose_best_product_match(
        self,
        possible_products: List[Dict],
        original_name: str,
        lang: str,
        expected_db_category: str = "",
        requested_quantity_text: str = ""
    ) -> Optional[Dict]:
        if not possible_products:
            return None

        normalized_name = self._apply_direct_aliases(self._normalize_for_index(original_name), lang)
        requested_qty, requested_unit, _ = self.extract_quantity_from_text(requested_quantity_text)
        egg_like_query = any(token in normalized_name for token in ["яйц", "яйцо", "tuxum"])

        best_item = None
        best_score = -10**9

        for product in possible_products:
            prod_name = self._normalize_for_index(product.get(f"name_{lang}", "") or product.get("name_ru", ""))
            score = 0

            if prod_name == normalized_name:
                score += 120
            elif normalized_name and normalized_name in prod_name:
                score += 45

            if expected_db_category and (
                product.get("category_ru") == expected_db_category or product.get("category_uz") == expected_db_category
            ):
                score += 20

            base_unit = product.get("unit") or self._extract_unit(product.get("quantity", ""))
            if requested_qty is not None and requested_unit and base_unit == requested_unit:
                score += 30

            if requested_qty is not None:
                # Prefer the package size the user actually asked for
                # ("мука 5 кг" → the 5 kg pack, not 1 kg × 5).
                base_qty = product.get("base_qty") or 1.0
                converted = _convert_quantity_units(requested_qty, requested_unit or base_unit, base_unit)
                if converted is not None and base_qty and abs(converted - base_qty) < 1e-6:
                    score += 60
                elif converted is None:
                    # Units are incompatible (кг vs шт) — this package can't be scaled.
                    score -= 25

            if requested_qty is None:
                score += self._default_item_bonus(product)

            if egg_like_query:
                if self._is_per_piece_item(product):
                    score += 70 if requested_qty is None else 15
                else:
                    score -= 35 if requested_qty is None else 5

            if score > best_score:
                best_score = score
                best_item = product

        return best_item or possible_products[0]

    def extract_quantity_from_text(self, text: str) -> Tuple[Optional[float], Optional[str], str]:
        text_lower = text.lower()
        number_match = re.search(r'(\d+[.,]?\d*)', text_lower)
        if not number_match:
            return None, None, ""

        try:
            quantity_str = number_match.group(1).replace(',', '.')
            quantity = float(quantity_str)
        except ValueError:
            return None, None, ""

        unit = _find_unit_in_text(text_lower)
        if unit:
            return quantity, unit, number_match.group(0)

        return quantity, None, number_match.group(0)

    def calculate_price_for_product(self, product_item: Dict, requested_quantity_text: str, target_lang: str = "ru") -> \
            Tuple[Optional[int], str, bool]:
        """Scale the base package price to the quantity the user asked for.

        "Картошка 1 кг = 8000" + "5 кг"  → 40000
        "Картошка 1 кг = 8000" + "500 г" → 4000 (units are converted)
        Incompatible units (кг vs шт) yield no price instead of a wrong one.
        """
        base_price = product_item.get("price", 0)
        base_quantity_str = product_item.get("quantity", "")
        base_unit = product_item.get("unit") or self._extract_unit(base_quantity_str)

        base_qty = product_item.get("base_qty")
        if not base_qty:
            base_qty = 1.0
            base_qty_match = re.search(r'(\d+[.,]?\d*)', base_quantity_str)
            if base_qty_match:
                try:
                    base_qty = float(base_qty_match.group(1).replace(',', '.'))
                except ValueError:
                    pass

        requested_qty, requested_unit, _ = self.extract_quantity_from_text(requested_quantity_text)
        user_specified_quantity = requested_qty is not None and requested_quantity_text.strip() != ""

        if user_specified_quantity and requested_qty is not None:
            effective_qty = requested_qty
            if requested_unit and base_unit and requested_unit != base_unit:
                effective_qty = _convert_quantity_units(requested_qty, requested_unit, base_unit)

            estimated_price: Optional[int] = None
            if effective_qty is not None and base_qty > 0:
                estimated_price = int(round(base_price / base_qty * effective_qty))
            elif effective_qty is None and base_price and requested_unit in ("g", "ml"):
                # A small gram/ml amount of a product sold per package
                # («соль 45 г» vs «Соль, 1 пачка»): the shopper still buys one
                # package, so the package price is the honest estimate. Larger
                # unconvertible requests («хлеб 2 кг» vs price per piece) stay
                # unpriced — no price beats a wrong one.
                estimated_price = int(round(base_price))

            if requested_unit and requested_unit in LOCALIZATION[target_lang]:
                localized_unit = LOCALIZATION[target_lang][requested_unit]
            else:
                localized_unit = LOCALIZATION[target_lang].get(base_unit, "")

            qty_value = int(requested_qty) if requested_qty.is_integer() else requested_qty
            final_quantity = f"{qty_value} {localized_unit}".strip()
            return estimated_price, final_quantity, True
        else:
            localized_quantity = base_quantity_str
            for pattern, unit_en in _UNIT_PATTERNS_BY_LENGTH:
                if unit_en not in LOCALIZATION[target_lang]:
                    continue
                unit_re = r"(?<![^\W\d])" + re.escape(pattern) + r"(?![^\W\d])"
                if re.search(unit_re, base_quantity_str.lower(), flags=re.UNICODE):
                    localized_quantity = re.sub(unit_re, LOCALIZATION[target_lang][unit_en],
                                                base_quantity_str, flags=re.IGNORECASE | re.UNICODE)
                    break
            return base_price, localized_quantity, False

    def _display_category_for_key(self, category_key: str, lang: str) -> Optional[str]:
        entry = self.display_by_key.get(category_key)
        if not entry:
            return None
        return entry.get(lang if lang in ("ru", "uz") else "ru")

    def _keyword_category_hits(self, name_lower: str, lang: str) -> Dict[str, int]:
        """Display categories whose keyword lists match the name, with hit counts."""
        if lang == "ru":
            markers = ["Овощи", "Фрукты", "Молочные", "Мясные", "Бакалея", "Напитки", "Чай",
                       "Приправы", "Гигиена", "Снеки", "Другое"]
        else:
            markers = ["Sabzavotlar", "Mevalar", "Sut", "Go'sht", "Oziq-ovqat", "Ichimliklar",
                       "Choy", "Ziravorlar", "Gigiyena", "Snacklar", "Boshqalar"]
        hits: Dict[str, int] = {}
        for category, info in self.display_category_map.items():
            if not any(m in category for m in markers):
                continue
            matches = sum(1 for keyword in info["keywords"] if keyword in name_lower)
            if matches > 0:
                hits[category] = matches
        return hits

    def determine_category(self, product_name: str, lang: str = "ru") -> str:
        """Pick a display category for a product.

        Order: eggs special-case → confident DB match (its category key) →
        keyword heuristics (for items not in the DB, e.g. hygiene/snacks) →
        medium DB match → category hints from prices.json → "Другое".
        """
        name_lower = product_name.lower()

        if any(k in name_lower for k in ["яйц", "яйцо", "tuxum"]):
            return "📦 Бакалея" if lang == "ru" else "📦 Oziq-ovqat"

        keyword_hits = self._keyword_category_hits(name_lower, lang)

        # 1) A confident DB match (exact / alias / stemmed) decides the category.
        #    Exception: a *partial* match means the catalog product is a superset
        #    of the query ("клубника" → "Мохито клубника"), i.e. possibly a
        #    completely different product. When the keywords unambiguously point
        #    to a different category, they know better than such a match.
        best_match, _ = _best_product_match_info(product_name, lang)
        if best_match:
            confidence = _score_match_confidence(product_name, best_match, lang)
            if confidence >= MATCH_CONFIDENCE_THRESHOLD:
                display = self._display_category_for_key(best_match.get("category_key", ""), lang)
                if display:
                    if confidence < MATCH_CONFIDENCE_EXACT and len(keyword_hits) == 1:
                        (keyword_category,) = keyword_hits
                        if keyword_category != display:
                            return keyword_category
                    return display

        # 2) Keyword heuristics — covers products missing from the DB.
        if keyword_hits:
            top = max(keyword_hits.values())
            top_categories = [c for c, n in keyword_hits.items() if n == top]
            # On a tie a fruit word is usually just the flavor ("йогурт
            # клубничный", "сок вишнёвый") — the other category is the product.
            non_fruit = [c for c in top_categories if "Фрукты" not in c and "Mevalar" not in c]
            return (non_fruit or top_categories)[0]

        # 3) Weaker DB match still beats "Другое".
        products = self.find_products(product_name, lang)
        if products:
            best_product = products[0]
            confidence = _score_match_confidence(product_name, best_product, lang)
            if confidence >= MATCH_CONFIDENCE_CATEGORY_THRESHOLD:
                display = self._display_category_for_key(best_product.get("category_key", ""), lang)
                if display:
                    return display

        # 4) Category hints from prices.json: known words without a product entry.
        synonym_index = self.synonym_index_ru if lang == "ru" else self.synonym_index_uz
        normalized = self._normalize_for_index(product_name)
        query_stems = set(_stem_tokens(normalized.split()))
        if query_stems:
            for norm_syn, category_keys in synonym_index.items():
                syn_stems = set(_stem_tokens(norm_syn.split()))
                if syn_stems and syn_stems <= query_stems:
                    display = self._display_category_for_key(category_keys[0], lang)
                    if display:
                        return display

        return "📝 Другое" if lang == "ru" else "📝 Boshqalar"


# Initialize price database
price_db = PriceDatabase(Config.PRICES_FILE)

# ===== OPENAI CLIENT =====
if Config.OPENAI_API_KEY:
    openai.api_key = Config.OPENAI_API_KEY
    client = openai.OpenAI(api_key=Config.OPENAI_API_KEY, timeout=Config.OPENAI_TIMEOUT)
else:
    client = None


def _is_openai_available() -> bool:
    return client is not None


def _voice_max_size_bytes() -> int:
    return max(1, Config.MAX_VOICE_FILE_SIZE_MB) * 1024 * 1024

# ===== RUNTIME STATE (only for websockets) =====
websocket_connections: Dict[int, WebSocket] = {}


# ===== LIST PROCESSING FUNCTIONS =====
def merge_categories(current_categories: Dict[str, List[Dict]], new_categories: Dict[str, List[Dict]]) -> Dict[
    str, List[Dict]]:
    result = copy.deepcopy(current_categories)
    for category, items in new_categories.items():
        if category not in result:
            result[category] = []
        for item in items:
            item["name"] = capitalize_first_letter(item["name"])
            item["original_name"] = capitalize_first_letter(item["original_name"])
            exists = False
            for existing_item in result[category]:
                if existing_item["name"].lower() == item["name"].lower():
                    if item.get("quantity"):
                        existing_item["quantity"] = item["quantity"]
                        existing_item["user_specified_quantity"] = True
                    exists = True
                    break
            if not exists:
                result[category].append(item)
    return result


async def format_list_with_gpt(text: str, lang: str = "ru") -> str:
    if not _is_openai_available():
        return (
            "Сервис AI временно недоступен: не настроен OPENAI_API_KEY."
            if lang == "ru"
            else "AI xizmati vaqtincha mavjud emas: OPENAI_API_KEY sozlanmagan."
        )
    try:
        completion = client.chat.completions.create(
            model=Config.CHAT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPTS[lang]},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=1000
        )
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"GPT error: {e}")
        return "Извините, произошла ошибка при обработке запроса." if lang == "ru" else "Kechirasiz, so'rovni qayta ishlashda xatolik yuz berdi."


def parse_shopping_list(text: str, lang: str = "ru") -> Dict[str, List[Dict]]:
    categories = {}
    current_category = None
    emojis = ["🥕", "🍎", "🥛", "🍖", "📦", "🥤", "🧴", "🧂", "📝", "🍵", "🍿", "🥚"]

    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue

        category_found = False
        for emoji in emojis:
            if line.startswith(emoji) and (':' in line or line.endswith(':')):
                current_category = line.split(':')[0].strip()
                categories[current_category] = []
                category_found = True
                break

        # Fallback category detection for unexpected emoji/styles: "Категория:" lines.
        if not category_found and not line.startswith('•') and ':' in line:
            possible_header = line.split(':', 1)[0].strip()
            if 2 <= len(possible_header) <= 60:
                current_category = possible_header
                if current_category not in categories:
                    categories[current_category] = []
                category_found = True

        if category_found:
            continue

        if line.startswith('•') and current_category:
            item_text = line[1:].strip()
            if '—' in item_text:
                parts = item_text.split('—', 1)
                product_name = parts[0].strip()
                quantity = parts[1].strip()
                user_specified = True
            else:
                product_name = item_text
                quantity = ""
                user_specified = False

            product_name = capitalize_first_letter(product_name)

            # Never list obscene / illegal / unsafe items, even if the model emitted them.
            if _is_blocked_product(product_name):
                continue

            target_category = current_category
            generic_other_categories = {"📝 Другое", "📝 Boshqalar", "Другое", "Boshqalar"}
            if current_category in generic_other_categories:
                detected_category = get_display_category_for_product(product_name, lang)
                if detected_category not in generic_other_categories:
                    target_category = detected_category
                    if target_category not in categories:
                        categories[target_category] = []

            if price_db.is_spice(product_name) and target_category not in ["🧂 Приправы", "🧂 Ziravorlar"]:
                spice_category = "🧂 Приправы" if lang == "ru" else "🧂 Ziravorlar"
                if spice_category not in categories:
                    categories[spice_category] = []
                categories[spice_category].append({
                    "name": product_name, "quantity": quantity, "purchased": False,
                    "estimated_price": None, "original_name": product_name, "user_specified_quantity": user_specified
                })
            else:
                categories[target_category].append({
                    "name": product_name, "quantity": quantity, "purchased": False,
                    "estimated_price": None, "original_name": product_name, "user_specified_quantity": user_specified
                })
    return {k: v for k, v in categories.items() if v}


def _build_unit_pattern() -> str:
    """Build a regex alternation from all known unit strings, longest first."""
    sorted_units = sorted(UNIT_MAPPING.keys(), key=len, reverse=True)
    return "(" + "|".join(re.escape(u) for u in sorted_units) + ")"

# Pre-compiled unit pattern used by quantity extraction helpers.
_UNIT_PATTERN = _build_unit_pattern()


def _extract_name_and_quantity(fragment: str, lang: str) -> Tuple[str, str]:
    """Extract (product_name, quantity_display) from a text fragment.

    Handles both orderings:
      - "product qty unit"  →  "картошка 15 кг"
      - "qty unit product"  →  "2 кг картошки"
    """
    cleaned = re.sub(r'^[•\-\*\s]+', '', fragment.strip())
    if not cleaned:
        return "", ""

    # Pattern: "qty unit product"  e.g. "2 кг картошки"
    prefix_re = re.compile(
        r'^(\d+[.,]?\d*)\s*' + _UNIT_PATTERN + r'\s+(.+)$',
        re.IGNORECASE | re.UNICODE
    )
    m = prefix_re.match(cleaned)
    if m:
        num_str = m.group(1)
        unit_str = m.group(2).lower()
        name_str = m.group(3).strip(" ,.-")
        unit_canonical = UNIT_MAPPING.get(unit_str, "")
        localized_unit = LOCALIZATION[lang].get(unit_canonical, unit_str) if unit_canonical else unit_str
        qty_display = f"{num_str} {localized_unit}".strip()
        return capitalize_first_letter(name_str), qty_display

    # Pattern: "product qty unit"  e.g. "картошка 15 кг" or "cola 1.5l"
    suffix_re = re.compile(
        r'(\d+[.,]?\d*)\s*' + _UNIT_PATTERN + r'(?:\s|$)',
        re.IGNORECASE | re.UNICODE
    )
    m = suffix_re.search(cleaned)
    if m:
        num_str = m.group(1)
        unit_str = m.group(2).lower()
        name_str = (cleaned[:m.start()] + cleaned[m.end():]).strip(" ,.-")
        unit_canonical = UNIT_MAPPING.get(unit_str, "")
        localized_unit = LOCALIZATION[lang].get(unit_canonical, unit_str) if unit_canonical else unit_str
        qty_display = f"{num_str} {localized_unit}".strip()
        if name_str:
            return capitalize_first_letter(name_str), qty_display
        # If nothing is left after stripping qty, fragment was purely a quantity token.
        return "", qty_display

    # No quantity with a known unit — return cleaned text as product name only.
    return capitalize_first_letter(cleaned), ""


# Standalone conjunctions that mark boundaries between product names.
_CONJUNCTION_SPLIT_RE = re.compile(
    r'\s+(?:и|или|да|va|yoki|hamda|также|тоже)\s+',
    re.IGNORECASE | re.UNICODE
)


def _smart_split_fragments(text: str) -> List[str]:
    """Split on commas/semicolons/newlines/conjunctions without breaking qty from unit.

    - Splits on commas, semicolons, newlines.
    - Also splits on standalone conjunctions ("и", "va", etc.) between products.
    - Merges orphan pure-numbers and pure-units back into the preceding fragment.

    "сыр, 15, кг"  ->  ["сыр 15 кг"]
    "картошку 3 кг и лук"  ->  ["картошку 3 кг", "лук"]
    """
    # Pass 1: split on commas / semicolons / newlines.
    comma_parts = [p.strip() for p in re.split(r'[;,\n]+', text) if p.strip()]
    if not comma_parts:
        return [text.strip()] if text.strip() else []

    # Pass 2: split each part on standalone conjunctions.
    conj_parts: List[str] = []
    for part in comma_parts:
        subs = [s.strip() for s in _CONJUNCTION_SPLIT_RE.split(part) if s.strip()]
        conj_parts.extend(subs if len(subs) > 1 else [part])

    # Pass 3: merge pure-number / pure-unit orphans into preceding fragment.
    result: List[str] = []
    i = 0
    while i < len(conj_parts):
        part = conj_parts[i]
        part_lower = part.lower()
        is_pure_number = bool(re.fullmatch(r'\d+[.,]?\d*', part))
        is_pure_unit = part_lower in UNIT_MAPPING

        if is_pure_number or is_pure_unit:
            if result:
                result[-1] = result[-1] + " " + part
            else:
                if i + 1 < len(conj_parts):
                    result.append(part + " " + conj_parts[i + 1])
                    i += 2
                    continue
                # Orphan with nothing around — drop silently.
        else:
            result.append(part)
        i += 1

    return result if result else [text.strip()]


SHOPPING_MAX_PRODUCT_WORDS = 5
SHOPPING_MIN_DIRECT_MATCH_SCORE = 40

# Words that should never become product names — polite/command/filler words in RU and UZ.
SHOPPING_FILLER_WORDS = {
    # Russian conjunctions and particles
    "и", "или", "да", "же", "то", "вот", "ну", "ой", "ай", "эй", "вообще",
    # Russian polite/command noise
    "пожалуйста", "пожалста", "пожал", "плз",
    "добавь", "добавьте", "добавить", "прибавь",
    "купи", "купите", "купить",
    "нужно", "нужен", "нужна", "нужны", "надо", "надобно",
    "еще", "ещё", "тоже", "также",
    "мне", "нам", "нас", "мы", "нам", "вам", "вас",
    "хочу", "хочется", "хотим",
    "возьми", "возьмите",
    "принеси", "принесите",
    "положи", "положите",
    # English
    "not", "the", "a", "an", "and", "or", "with", "also", "plus", "then",
    "pls", "please", "buy", "get", "need", "want",
    # Uzbek conjunctions and polite words
    "va", "ham", "hamda", "yoki", "lekin", "ammo",
    "iltimos", "marhamat",
    "ol", "oling", "olib", "ber", "bering",
    "keling", "kerak", "zarur",
    "yana", "yana ham",
    "menga", "bizga", "senga", "sizga",
    "men", "biz", "sen", "siz",
    "meni", "bizni",
    "kupi", "sotib",
}

# Tokens that are units only — never valid standalone product names.
SHOPPING_UNIT_WORDS: set = set(UNIT_MAPPING.keys()) | {
    "kg.", "g.", "l.", "ml.", "шт.", "дона", "уп.",
}


def _best_product_match_info(query: str, lang: str) -> Tuple[Optional[Dict], int]:
    candidates = price_db.find_products(query, lang)
    if not candidates:
        return None, 0

    normalized_query = price_db._apply_direct_aliases(price_db._normalize_for_index(query), lang)
    query_words = [word for word in normalized_query.split() if word]
    if not query_words:
        return None, 0

    query_has_quantity = price_db.extract_quantity_from_text(query)[0] is not None
    query_stems = _stem_tokens(query_words)
    best_candidate = None
    best_score = 0

    for candidate in candidates:
        candidate_name = candidate.get(f"name_{lang}") or candidate.get("name_ru") or ""
        normalized_candidate = price_db._apply_direct_aliases(price_db._normalize_for_index(candidate_name), lang)
        candidate_words = [word for word in normalized_candidate.split() if word]
        if not candidate_words:
            continue

        # Compare stems so inflected forms match ("красной репы" == "красная репа").
        candidate_stems = _stem_tokens(candidate_words)
        if candidate_words == query_words or candidate_stems == query_stems:
            match_bonus = 1000
        elif _is_contiguous_subsequence(query_stems, candidate_stems):
            match_bonus = 800 + len(query_words)
        elif _is_contiguous_subsequence(candidate_stems, query_stems):
            match_bonus = 600 + len(candidate_words)
        else:
            continue

        score = price_db._score_candidate(candidate, normalized_query, query_words, lang, query_has_quantity)
        score += match_bonus

        if score > best_score:
            best_candidate = candidate
            best_score = score

    if best_candidate is None:
        return None, 0

    return best_candidate, best_score


def _normalize_direct_token(token: str) -> str:
    return re.sub(r"^[^\w'']+|[^\w'']+$", "", token.lower().strip())


def _tokenize_shopping_segment(segment: str) -> List[str]:
    return [token for token in re.findall(r"[\w''\-]+", segment.lower(), flags=re.UNICODE) if token]


def _is_contiguous_subsequence(needle: List[str], haystack: List[str]) -> bool:
    if not needle or not haystack or len(needle) > len(haystack):
        return False
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start:start + len(needle)] == needle:
            return True
    return False


def _contiguous_subsequence_index(needle: List[str], haystack: List[str]) -> int:
    if not needle or not haystack or len(needle) > len(haystack):
        return -1
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start:start + len(needle)] == needle:
            return start
    return -1


def _is_match_compatible_with_span(span_words: List[str], candidate_words: List[str]) -> bool:
    """Stem-aware compatibility between a text span and a matched product name."""
    if not span_words or not candidate_words:
        return False
    span_stems = _stem_tokens(span_words)
    candidate_stems = _stem_tokens(candidate_words)
    if span_words == candidate_words or span_stems == candidate_stems:
        return True

    candidate_in_span = _contiguous_subsequence_index(candidate_stems, span_stems)
    if candidate_in_span != -1:
        extra_words = span_words[:candidate_in_span] + span_words[candidate_in_span + len(candidate_stems):]
        if all(_is_noise_token(word) for word in extra_words):
            return True

    span_in_candidate = _contiguous_subsequence_index(span_stems, candidate_stems)
    if span_in_candidate != -1:
        extra_words = candidate_words[:span_in_candidate] + candidate_words[span_in_candidate + len(span_stems):]
        if all(_is_noise_token(word) for word in extra_words):
            return True

    return False


def _is_noise_token(token: str) -> bool:
    normalized = _normalize_direct_token(token)
    if not normalized:
        return True
    if normalized in SHOPPING_FILLER_WORDS:
        return True
    if normalized in SHOPPING_UNIT_WORDS:
        return True
    if re.fullmatch(r"\d+[.,]?\d*", normalized):
        return True
    return False


def _is_valid_product_candidate(text: str) -> bool:
    """Return False if text is a pure number, pure unit, or a filler word.

    Used to reject garbage tokens before they become list items.
    """
    stripped = text.strip().lower()
    if not stripped:
        return False
    if re.fullmatch(r'\d+[.,]?\d*', stripped):
        return False
    if stripped in UNIT_MAPPING:
        return False
    if stripped in SHOPPING_FILLER_WORDS:
        return False
    # A token that is very short AND all-digit-or-punctuation is noise.
    if len(stripped) <= 1 and not stripped.isalpha():
        return False
    return True


def _score_match_confidence(original_name: str, best_match: Optional[Dict], lang: str) -> int:
    """Compute a 0-1000 confidence score for a DB product match against original_name.

    Used to gate price and category assignment.
    """
    if not best_match:
        return 0

    normalized_query = price_db._apply_direct_aliases(
        price_db._normalize_for_index(original_name), lang
    )
    matched_name_raw = best_match.get(f"name_{lang}") or best_match.get("name_ru") or ""
    normalized_matched = price_db._apply_direct_aliases(
        price_db._normalize_for_index(matched_name_raw), lang
    )

    if not normalized_query or not normalized_matched:
        return 0

    # Exact match (raw or stem-level, so inflected forms count as exact)
    if normalized_query == normalized_matched:
        return 1000

    query_stems = _stem_tokens(normalized_query.split())
    matched_stems = _stem_tokens(normalized_matched.split())
    if query_stems == matched_stems:
        return 1000

    # Packaging noise in catalog names ("Яйца (за 1 шт)" → numbers, units) must
    # not dilute the comparison: "яйца" is effectively that exact product.
    query_stems = [s for s in query_stems if not _is_noise_token(s)] or query_stems
    matched_stems = [s for s in matched_stems if not _is_noise_token(s)] or matched_stems
    if query_stems == matched_stems:
        return 1000

    # Same words in a different order ("масло подсолнечное" vs "подсолнечное масло")
    if set(query_stems) == set(matched_stems):
        return 900

    # Query is fully contained in product name (stems, in order)
    if _is_contiguous_subsequence(query_stems, matched_stems):
        ratio = len(query_stems) / max(len(matched_stems), 1)
        return int(700 + 200 * ratio)

    # Product name is fully contained in query (query has extra words)
    if _is_contiguous_subsequence(matched_stems, query_stems):
        ratio = len(matched_stems) / max(len(query_stems), 1)
        return int(500 + 200 * ratio)

    # String containment fallback (partial-word matches)
    if normalized_query in normalized_matched:
        ratio = len(normalized_query) / max(len(normalized_matched), 1)
        return int(700 + 200 * ratio)
    if normalized_matched in normalized_query:
        ratio = len(normalized_matched) / max(len(normalized_query), 1)
        return int(500 + 200 * ratio)

    # Stem overlap score
    overlap = set(query_stems) & set(matched_stems)
    if not overlap:
        return 0

    precision = len(overlap) / max(len(query_stems), 1)
    recall = len(overlap) / max(len(matched_stems), 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-6)
    return int(f1 * 400)


def _build_direct_item(product_name: str, quantity: str, lang: str, original_name: Optional[str] = None,
                       user_specified_quantity: bool = False) -> Dict[str, Any]:
    display_name = capitalize_first_letter(product_name.strip())
    original_display = capitalize_first_letter((original_name or product_name).strip())
    return {
        "name": display_name,
        "quantity": quantity,
        "purchased": False,
        "estimated_price": None,
        "original_name": original_display,
        "user_specified_quantity": user_specified_quantity,
    }


def _is_number_token(token: str) -> bool:
    return bool(re.fullmatch(r"\d+[.,]?\d*", token))


def _tokenize_with_numbers(text: str) -> List[str]:
    """Tokenize keeping decimal numbers intact (e.g. "1.5") and words separate.

    "картошка 1.5кг" → ["картошка", "1.5", "кг"]
    """
    return [t for t in re.findall(r"\d+[.,]?\d*|[\w'’\-]+", text.lower(), flags=re.UNICODE) if t]


def _format_quantity(num: str, unit_token: str, lang: str) -> str:
    """Build a localized quantity display from a raw number + optional unit token."""
    num = num.replace(",", ".")
    if not unit_token:
        return num
    unit_canonical = UNIT_MAPPING.get(unit_token, "")
    localized_unit = LOCALIZATION[lang].get(unit_canonical, unit_token) if unit_canonical else unit_token
    return f"{num} {localized_unit}".strip()


def _parse_direct_segment(segment: str, lang: str) -> List[Dict[str, Any]]:
    """Parse one fragment into zero or more shopping items via greedy left-to-right scan.

    Handles natural voice streams without punctuation, e.g.
    "картошка 2 кг помидор 3 кг лук" → 3 separate products with their own quantities.
    Quantities bind to the product they sit next to, in either order
    ("картошка 2 кг" or "2 кг картошки"). Filler/command/noise tokens are dropped,
    spoken number words are converted to digits, and obscene/illegal items are skipped.
    """
    cleaned_segment = re.sub(r"\s+", " ", segment.strip())
    # Spoken numbers → digits ("два килограмма" → "2 килограмма").
    cleaned_segment = _convert_number_words(cleaned_segment)
    if not cleaned_segment:
        return []

    tokens = _tokenize_with_numbers(cleaned_segment)
    if not tokens:
        return []

    items: List[Dict[str, Any]] = []
    pending_qty: Optional[str] = None       # quantity seen before its product ("2 кг картошки")
    last_item: Optional[Dict[str, Any]] = None  # last product still awaiting a trailing quantity
    n = len(tokens)
    i = 0

    def _attach_quantity(qty: str):
        nonlocal pending_qty, last_item
        if last_item is not None and not last_item.get("quantity"):
            last_item["quantity"] = qty
            last_item["user_specified_quantity"] = True
            last_item = None
        else:
            pending_qty = qty

    def _emit(item: Dict[str, Any]):
        nonlocal pending_qty, last_item
        if pending_qty:
            item["quantity"] = pending_qty
            item["user_specified_quantity"] = True
            pending_qty = None
            last_item = None
        else:
            last_item = item
        items.append(item)

    while i < n:
        token = tokens[i]

        # Quantity: a number optionally followed by a unit token.
        if _is_number_token(token):
            unit_token = ""
            if i + 1 < n and tokens[i + 1] in UNIT_MAPPING:
                unit_token = tokens[i + 1]
                i += 1
            _attach_quantity(_format_quantity(token, unit_token, lang))
            i += 1
            continue

        # Stray unit without a number, or filler/conjunction noise — skip.
        if token in UNIT_MAPPING or _is_noise_token(token):
            i += 1
            continue

        # Greedy product span match (longest first), word tokens only.
        matched = False
        max_span = min(SHOPPING_MAX_PRODUCT_WORDS, n - i)
        for span_len in range(max_span, 0, -1):
            span_tokens = tokens[i:i + span_len]
            if any(_is_number_token(t) or t in UNIT_MAPPING for t in span_tokens):
                continue
            if all(_is_noise_token(t) for t in span_tokens):
                continue

            candidate = " ".join(span_tokens).strip()
            best_match, score = _best_product_match_info(candidate, lang)
            if not best_match:
                continue

            matched_name = best_match.get(f"name_{lang}") or best_match.get("name_ru") or candidate
            normalized_candidate = price_db._apply_direct_aliases(price_db._normalize_for_index(matched_name), lang)
            matched_words = [word for word in normalized_candidate.split() if word]
            # Compare alias-normalized span (so inflected "картошки" matches canonical "картошка").
            normalized_span = price_db._apply_direct_aliases(price_db._normalize_for_index(candidate), lang)
            span_for_compat = [word for word in normalized_span.split() if word] or span_tokens
            if not _is_match_compatible_with_span(span_for_compat, matched_words):
                continue
            if span_len > 1 and score < SHOPPING_MIN_DIRECT_MATCH_SCORE:
                continue
            if span_len == 1 and score < 30:
                continue

            i += span_len
            matched = True
            # Show the canonical DB name only when it has the same "shape" as what
            # the user said ("картошки" → "Картошка"). When an alias jumped to a
            # more specific product ("молоко" → "Молоко Lactel 3,2%"), keep the
            # user's wording — the price still comes from the alias later.
            raw_span_words = price_db._normalize_for_index(candidate).split()
            raw_span_stems = _stem_tokens(raw_span_words)
            matched_stems = _stem_tokens(matched_words)
            same_shape = (raw_span_stems == matched_stems
                          or (len(raw_span_words) == len(matched_words)
                              and span_for_compat == matched_words))
            display_name = matched_name if same_shape else candidate
            if not _is_blocked_product(display_name):
                _emit(_build_direct_item(display_name, "", lang, display_name, False))
            break

        if matched:
            continue

        # No DB match here: gather the run of consecutive unknown word tokens into a
        # single unknown product (so "куриное филе" stays one item, not two).
        run = [token]
        j = i + 1
        while j < n:
            t = tokens[j]
            if _is_number_token(t) or t in UNIT_MAPPING or _is_noise_token(t):
                break
            probe, _ = _best_product_match_info(t, lang)
            if probe is not None:
                break
            run.append(t)
            j += 1

        unknown_name = " ".join(run).strip()
        i = j
        if _is_valid_product_candidate(unknown_name) and not _is_blocked_product(unknown_name):
            _emit(_build_direct_item(unknown_name, "", lang, unknown_name, False))

    return items


def try_parse_direct_shopping_input(text: str, lang: str = "ru") -> Dict[str, List[Dict]]:
    """Parse direct shopping text without GPT using the prices database.

    Uses smart splitting to avoid breaking "product, qty, unit" across fragments,
    and pre-filters filler/command words before passing to the segment parser.
    """
    if not text or "?" in text:
        return {}

    candidate_text = text.strip()
    categories: Dict[str, List[Dict]] = {}

    # Smart split avoids "сыр, 15, кг" → three broken items.
    fragments = _smart_split_fragments(candidate_text)
    if not fragments:
        fragments = [candidate_text]

    seen_names: set = set()  # Duplicate guard within this parse pass.
    for fragment in fragments:
        parsed_items = _parse_direct_segment(fragment, lang)
        for item in parsed_items:
            name_key = item["original_name"].lower().strip()
            if name_key in seen_names:
                continue
            seen_names.add(name_key)
            category = get_display_category_for_product(item["original_name"], lang)
            categories.setdefault(category, []).append(item)

    return categories if categories else {}


def format_shopping_list_for_json(categories: Dict[str, List[Dict]], user_id: int, lang: str = "ru",
                                  original_text: str = "") -> Dict:
    result = {
        "categories": {}, "items": [], "total_items": 0, "purchased_items": 0,
        "total_estimated_price": 0, "list_id": secrets.token_hex(8),
        "created_at": datetime.now().isoformat(), "all_purchased": False,
        "original_text": original_text, "needs_confirmation": False,
        "localization": LOCALIZATION[lang], "owner_id": user_id
    }

    category_mapping = {
        "🥕 Овощи": "Овощи", "🍎 Фрукты": "Фрукты", "🥛 Молочные продукты": "Молочные продукты",
        "🍖 Мясные продукты": "Мясные продукты", "📦 Бакалея": "Бакалея", "🥤 Напитки": "Напитки",
        "🧴 Гигиена и быт": "Бакалея", "🧂 Приправы": "Приправы", "📝 Другое": "Бакалея",
        "🍵 Чай и кофе": "Чай и кофе", "🍿 Снеки": "Бакалея",
        "🥕 Sabzavotlar": "Овощи", "🍎 Mevalar": "Фрукты", "🥛 Sut mahsulotlari": "Молочные продукты",
        "🍖 Go'sht mahsulotlari": "Мясные продукты", "📦 Oziq-ovqat": "Бакалея", "🥤 Ichimliklar": "Напитки",
        "🧴 Gigiyena": "Бакалея", "🧂 Ziravorlar": "Приправы", "📝 Boshqalar": "Бакалея",
        "🍵 Choy va kofe": "Чай и кофе", "🍿 Snacklar": "Бакалея",
    }

    for category, items in categories.items():
        result["categories"][category] = []
        result["total_items"] += len(items)
        expected_db_category = category_mapping.get(category, "")

        for item in items:
            item["name"] = capitalize_first_letter(item["name"])
            item["original_name"] = capitalize_first_letter(item["original_name"])

            possible_products = price_db.find_products(item["original_name"], lang)
            item_data = {
                "name": item["name"], "quantity": item.get("quantity", ""),
                "purchased": item.get("purchased", False), "category": category,
                "estimated_price": None, "user_specified_quantity": item.get("user_specified_quantity", False)
            }

            best_match = None
            if possible_products:
                best_match = price_db.choose_best_product_match(
                    possible_products,
                    item["original_name"],
                    lang,
                    expected_db_category=expected_db_category,
                    requested_quantity_text=item.get("quantity", "")
                )

                if best_match:
                    # Only assign a price when the match is confident enough.
                    confidence = _score_match_confidence(item["original_name"], best_match, lang)
                    price, final_quantity, user_specified = price_db.calculate_price_for_product(
                        best_match, item.get("quantity", ""), lang
                    )
                    if confidence >= MATCH_CONFIDENCE_THRESHOLD:
                        item_data["estimated_price"] = price
                    else:
                        # Keep the match for quantity normalization but skip price.
                        item_data["estimated_price"] = None
                    if user_specified:
                        item_data["quantity"] = final_quantity

            result["categories"][category].append(item_data)
            result["items"].append(item_data)
            if item_data["estimated_price"]:
                result["total_estimated_price"] += item_data["estimated_price"]
            if item.get("purchased"):
                result["purchased_items"] += 1

    if result["total_items"] > 0 and result["purchased_items"] == result["total_items"]:
        result["all_purchased"] = True
        result["needs_confirmation"] = True

    return result


async def detect_edit_changes(text: str, lang: str = "ru") -> List[Dict]:
    if not _is_openai_available():
        return []
    try:
        completion = client.chat.completions.create(
            model=Config.CHAT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_EDIT[lang]},
                {"role": "user", "content": text},
            ],
            temperature=0.1
        )
        response = completion.choices[0].message.content
        data = json.loads(response)
        return data.get("changes", [])
    except Exception as e:
        logger.error(f"Edit detection error: {e}")
        return []


def get_display_category_for_product(product_name: str, lang: str = "ru") -> str:
    return price_db.determine_category(product_name, lang)


# ===== SHOPPING LIST BY DISH (recipes.json) =====
# A user asks, in free form, "Хочу приготовить плов на 10 человек" and gets back a
# ready shopping list. All recipe data lives in recipes.json — nothing is hardcoded
# here. The pipeline is split into small, independently testable functions:
#   extract_dish_and_servings() -> find_recipe() -> scale_ingredients()

# Words that signal the user wants a shopping list for a dish (not a raw product list).
RECIPE_INTENT_KEYWORDS = {
    "ru": ["приготовить", "приготовь", "готовить", "готовлю", "сделай список",
           "список продуктов", "список для", "продукты для", "ингредиенты",
           "рецепт", "блюдо", "хочу приготовить", "сварить", "приготовление"],
    "uz": ["tayyorlamoqchi", "tayyorlash", "tayyorla", "pishirmoqchi", "pishirish",
           "retsept", "taom", "mahsulotlar ro'yxati", "ingredientlar", "ovqat"],
}

# Message shown when the requested dish is not yet in recipes.json.
RECIPE_NOT_FOUND_MESSAGE = {
    "ru": "К сожалению, такого блюда пока нет в базе.",
    "uz": "Afsuski, bunday taom hozircha bazada yo'q.",
}

# Cached recipes so we don't hit disk on every request. Call load_recipes(force_reload=True)
# to pick up edits made to recipes.json without restarting the server.
_RECIPES_CACHE: Optional[Dict[str, Any]] = None


def load_recipes(force_reload: bool = False) -> Dict[str, Any]:
    """Load all recipes from recipes.json. Returns {} if the file is missing/invalid."""
    global _RECIPES_CACHE
    if _RECIPES_CACHE is not None and not force_reload:
        return _RECIPES_CACHE
    try:
        with open(Config.RECIPES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("recipes.json root must be an object")
        _RECIPES_CACHE = data
    except FileNotFoundError:
        logger.warning(f"recipes.json not found at {Config.RECIPES_FILE}")
        _RECIPES_CACHE = {}
    except Exception as e:
        logger.error(f"Failed to load recipes.json: {e}")
        _RECIPES_CACHE = {}
    return _RECIPES_CACHE


def _extract_servings_regex(text: str) -> Optional[int]:
    """Deterministic fallback for the number of people, e.g. 'на 10 человек', '6 kishi'."""
    match = re.search(
        r"(\d+)\s*(?:человек|человека|персон|порц|edok|kishi|ta\s+odam|odam|person|people)",
        text.lower(),
    )
    if match:
        try:
            value = int(match.group(1))
            return value if value > 0 else None
        except ValueError:
            return None
    return None


def _find_dish_in_text(text: str, recipes: Dict[str, Any]) -> str:
    """Fallback dish detection: return the first alias that appears as a whole word.

    An alias matches at a word start (so 'плов' also catches the inflected 'плова'),
    which avoids false hits like alias 'ош' inside the middle of 'картошка'.
    """
    lowered = text.lower()
    for recipe in recipes.values():
        for alias in recipe.get("aliases", []):
            pattern = r"(?<!\w)" + re.escape(str(alias).lower())
            if re.search(pattern, lowered, flags=re.UNICODE):
                return alias
    return ""


def extract_dish_and_servings(text: str, lang: str = "ru") -> Tuple[str, Optional[int]]:
    """Determine the dish name and requested number of people from a free-form request.

    Uses the LLM when available; falls back to matching known aliases + a numeric
    regex so the feature still works without an OpenAI key. `servings` is None when
    the user did not specify a headcount (caller then uses the recipe default).
    """
    recipes = load_recipes()

    if _is_openai_available():
        try:
            completion = client.chat.completions.create(
                model=Config.CHAT_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT_RECIPE[lang]},
                    {"role": "user", "content": text},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            data = json.loads(completion.choices[0].message.content)
            dish = (data.get("dish") or "").strip()
            servings = data.get("servings")
            if isinstance(servings, str):
                servings = int(servings) if servings.strip().isdigit() else None
            if not isinstance(servings, int) or servings <= 0:
                servings = None
            if dish:
                return dish, servings
        except Exception as e:
            logger.error(f"extract_dish_and_servings LLM error: {e}")

    # Deterministic fallback (no key / LLM failure).
    return _find_dish_in_text(text, recipes), _extract_servings_regex(text)


def find_recipe(recipes: Dict[str, Any], dish: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Look a dish up by its aliases. Returns (recipe_key, recipe) or (None, None)."""
    if not dish:
        return None, None
    needle = dish.strip().lower()
    for key, recipe in recipes.items():
        aliases = [str(a).lower() for a in recipe.get("aliases", [])]
        aliases.append(key.lower())
        for alias in aliases:
            # Match either direction so "плов" finds "плов" and "хочу плов" finds "плов".
            if alias == needle or alias in needle or needle in alias:
                return key, recipe
    return None, None


def scale_ingredients(recipe: Dict[str, Any], requested_servings: int) -> List[Dict[str, Any]]:
    """Scale every ingredient amount by requested_servings / recipe.servings."""
    base_servings = recipe.get("servings") or 1
    factor = requested_servings / base_servings
    scaled = []
    for ingredient in recipe.get("ingredients", []):
        amount = ingredient.get("amount", 0)
        new_amount = amount * factor
        # Keep integers clean (2000 not 2000.0), round fractions to 2 decimals.
        new_amount = int(new_amount) if float(new_amount).is_integer() else round(new_amount, 2)
        scaled.append({
            "name": ingredient.get("name", ""),
            "amount": new_amount,
            "unit": ingredient.get("unit", ""),
        })
    return scaled


def looks_like_recipe_request(text: str, lang: str = "ru") -> bool:
    """Cheap gate before spending an LLM call: does the text look like a dish request?

    True when the message contains a cooking-intent keyword, or a known recipe alias.
    """
    lowered = text.lower()
    keywords = RECIPE_INTENT_KEYWORDS.get(lang, RECIPE_INTENT_KEYWORDS["ru"])
    if any(kw in lowered for kw in keywords):
        return True
    return bool(_find_dish_in_text(text, load_recipes()))


def build_recipe_shopping_list(text: str, lang: str = "ru") -> Dict[str, Any]:
    """Full pipeline: free-form request -> ready shopping list of scaled ingredients.

    Returns a dict:
      {"found": True,  "dish": <key>, "servings": <int>, "ingredients": [...]}
      {"found": False, "message": <localized "not in base" text>}
    """
    recipes = load_recipes()
    dish, servings = extract_dish_and_servings(text, lang)
    recipe_key, recipe = find_recipe(recipes, dish)

    if not recipe:
        return {"found": False, "message": RECIPE_NOT_FOUND_MESSAGE.get(lang, RECIPE_NOT_FOUND_MESSAGE["ru"])}

    # No headcount given -> use the recipe's default servings.
    requested_servings = servings or recipe.get("servings") or 1
    return {
        "found": True,
        "dish": recipe_key,
        "servings": requested_servings,
        "ingredients": scale_ingredients(recipe, requested_servings),
    }


def add_recipe_ingredients_to_list(list_data: Dict, ingredients: List[Dict[str, Any]], lang: str) -> Dict:
    """Add scaled recipe ingredients to a shopping list (categorized + priced)."""
    for ingredient in ingredients:
        name = ingredient.get("name", "")
        amount = ingredient.get("amount", "")
        unit = ingredient.get("unit", "")
        quantity = f"{amount} {unit}".strip()
        if name:
            list_data = add_item_to_list(list_data, name, quantity, lang)
    return list_data


# ===== DETERMINISTIC VOICE COMMANDS (mark purchased / remove / replace) =====
# Keywords that signal the user is editing an existing list by voice, in RU and UZ.
VOICE_PURCHASE_KEYWORDS = {
    # "убери/убрать" spoken over an active list means "check it off" (mark purchased),
    # not "delete" — deletion is the explicit "удали/сотри" family below.
    "ru": ["купил", "купила", "купили", "куплено", "приобрел", "приобрёл", "приобрела",
           "приобрели", "взял", "взяла", "взяли", "отметь", "отметить", "отметьте",
           "отметила", "набрал", "набрала", "положил", "положила", "беру", "покупаю",
           "приобретено", "есть уже", "убери", "убрать", "уберите", "вычеркни",
           "вычеркнуть", "вычеркните", "галочку", "галочка"],
    "uz": ["sotib oldim", "sotib oldik", "oldim", "oldik", "xarid qildim", "belgila",
           "belgilab", "belgilang", "olib bo'ldim", "olib keldim", "sotib olindi", "bor"],
}
VOICE_REMOVE_KEYWORDS = {
    "ru": ["удали", "удалить", "удалите", "сотри", "стереть", "сотрите",
           "выкинь", "выброси", "выкини", "исключи", "минус"],
    "uz": ["o'chir", "ochir", "o'chirib tashla", "o'chiring", "olib tashla", "yo'q qil",
           "bekor qil", "olib tashlang"],
}
VOICE_REPLACE_KEYWORDS = {
    "ru": ["замени", "заменить", "замените", "поменяй", "поменять", "вместо",
           "измени", "изменить", "измените"],
    "uz": ["almashtir", "almashtiring", "o'rniga", "orniga"],
}
# Conjunctions/prepositions used to split "X на Y" (replace) and product lists.
_REPLACE_SPLIT_RE = {
    "ru": re.compile(r"\s+на\s+", re.IGNORECASE | re.UNICODE),
    "uz": re.compile(r"\s+(?:ga|o'rniga|orniga)\s+", re.IGNORECASE | re.UNICODE),
}


def _strip_keywords(text: str, keywords: List[str]) -> str:
    """Remove command keywords (whole words) from text."""
    result = text
    for kw in sorted(keywords, key=len, reverse=True):
        result = re.sub(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", " ", result, flags=re.IGNORECASE | re.UNICODE)
    return re.sub(r"\s+", " ", result).strip()


def _contains_keyword(text_lower: str, keywords: List[str]) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", text_lower, flags=re.UNICODE) for kw in keywords)


def _normalized_item_name(item: Dict, lang: str) -> str:
    raw = item.get("original_name") or item.get("name") or ""
    return price_db._apply_direct_aliases(price_db._normalize_for_index(raw), lang)


def _resolve_list_items(list_data: Dict, query: str, lang: str) -> List[Tuple[str, Dict]]:
    """Find existing list items that correspond to a spoken product query."""
    categories = list_data.get("categories", {})
    query_norm = price_db._apply_direct_aliases(price_db._normalize_for_index(query), lang)
    if not query_norm:
        return []

    best, score = _best_product_match_info(query, lang)
    canonical_norm = ""
    if best and score >= SHOPPING_MIN_DIRECT_MATCH_SCORE:
        canonical_raw = best.get(f"name_{lang}") or best.get("name_ru") or ""
        canonical_norm = price_db._apply_direct_aliases(price_db._normalize_for_index(canonical_raw), lang)

    matches: List[Tuple[str, Dict]] = []
    for cat, items in categories.items():
        for item in items:
            name_norm = _normalized_item_name(item, lang)
            if not name_norm:
                continue
            if (query_norm == name_norm or query_norm in name_norm or name_norm in query_norm
                    or (canonical_norm and canonical_norm == name_norm)):
                matches.append((cat, item))
    return matches


def _extract_command_products(text: str, lang: str) -> List[str]:
    """Pull product names out of a command body using the deterministic parser."""
    parsed = try_parse_direct_shopping_input(text, lang)
    names: List[str] = []
    for items in parsed.values():
        for item in items:
            name = item.get("original_name") or item.get("name")
            if name:
                names.append(name)
    return names


def detect_voice_list_command(text: str, lang: str) -> Optional[Dict[str, Any]]:
    """Detect a deterministic edit command (purchase / remove / replace) in spoken text.

    Returns a structured command dict, or None when the text is not such a command.
    """
    if not text:
        return None
    lang = lang if lang in ("ru", "uz") else "ru"
    lowered = text.lower()

    # Replace must be checked first ("замени X на Y" also contains a target product).
    if _contains_keyword(lowered, VOICE_REPLACE_KEYWORDS[lang]):
        body = _strip_keywords(text, VOICE_REPLACE_KEYWORDS[lang])
        # 1) Explicit delimiter: RU "X на Y", UZ "X o'rniga Y".
        parts = _REPLACE_SPLIT_RE[lang].split(body, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return {"type": "replace", "target": parts[0].strip(), "new_item": parts[1].strip()}
        # 2) Suffix form (mostly UZ: "kartoshkani pomidorga almashtir") — fall back to
        #    extracting the two products in order: first = target, second = new item.
        products = _extract_command_products(body, lang)
        if len(products) >= 2:
            return {"type": "replace", "target": products[0], "new_item": products[1]}
        # Fall through: not a well-formed replace.

    if _contains_keyword(lowered, VOICE_REMOVE_KEYWORDS[lang]):
        body = _strip_keywords(text, VOICE_REMOVE_KEYWORDS[lang])
        products = _extract_command_products(body, lang)
        if products:
            return {"type": "remove", "products": products}

    if _contains_keyword(lowered, VOICE_PURCHASE_KEYWORDS[lang]):
        body = _strip_keywords(text, VOICE_PURCHASE_KEYWORDS[lang])
        products = _extract_command_products(body, lang)
        if products:
            return {"type": "purchase", "products": products}

    return None


def apply_voice_list_command(list_data: Dict, command: Dict[str, Any], lang: str) -> Tuple[Dict, bool]:
    """Apply a detected voice command to the list. Returns (list_data, changed)."""
    changed = False
    ctype = command.get("type")

    if ctype == "purchase":
        for product in command.get("products", []):
            for _cat, item in _resolve_list_items(list_data, product, lang):
                if not item.get("purchased", False):
                    item["purchased"] = True
                    changed = True
        if changed:
            list_data = recalculate_list_totals(list_data)
        return list_data, changed

    if ctype == "remove":
        changes = []
        for product in command.get("products", []):
            resolved = _resolve_list_items(list_data, product, lang)
            for _cat, item in resolved:
                changes.append({"action": "remove", "target": item["name"], "new_item": "", "quantity": ""})
        if changes:
            updated = apply_edit_changes(list_data.get("categories", {}), changes, lang)
            list_data["categories"] = updated
            list_data = recalculate_list_totals(list_data)
            changed = True
        return list_data, changed

    if ctype == "replace":
        target = command.get("target", "")
        new_item_raw = command.get("new_item", "")
        # Allow "на лук 2 кг" → product + quantity.
        new_name, new_qty = _extract_name_and_quantity(new_item_raw, lang)
        if not new_name:
            new_name = new_item_raw
        resolved = _resolve_list_items(list_data, target, lang)
        target_name = resolved[0][1]["name"] if resolved else target
        # Canonicalize the new product, tolerating inflection / case suffixes
        # (RU "куриное филе", UZ "bodringga" → "Bodring") via word-variant matching.
        new_canonical = new_name
        candidates = price_db.find_products(new_name, lang)
        if candidates:
            best = price_db.choose_best_product_match(candidates, new_name, lang)
            if best and _score_match_confidence(new_name, best, lang) >= MATCH_CONFIDENCE_CATEGORY_THRESHOLD:
                new_canonical = best.get(f"name_{lang}") or best.get("name_ru") or new_name
        if _is_blocked_product(new_canonical):
            return list_data, False
        change = {"action": "replace", "target": target_name,
                  "new_item": capitalize_first_letter(new_canonical), "quantity": new_qty}
        updated = apply_edit_changes(list_data.get("categories", {}), [change], lang)
        list_data["categories"] = updated
        list_data = recalculate_list_totals(list_data)
        changed = True
        return list_data, changed

    return list_data, changed


# ===== "Я НА БАЗАРЕ" (offline bazaar shopping mode) =====
# The user walks around a real bazaar/market and dictates purchases in free form:
#   "Картошка 38", "Купил мясо за 120 тысяч", "Olma 25 ming".
# Bare numbers without a currency are thousands of soums: "38" -> 38 000.

BAZAAR_FINISH_PHRASES = {
    "ru": ["все куплено", "всё куплено", "все купил", "всё купил", "все купили", "всё купили",
           "все купила", "всё купила",
           "закончил покупки", "закончила покупки", "закончили покупки",
           "закончил закупку", "закончила закупку", "покупки завершены", "закупка завершена",
           "заверши список", "завершить список", "завершил список", "завершила список",
           "заверши покупки", "завершить покупки", "заверши закупку", "завершить закупку",
           "сохрани список", "сохранить список", "закончил покупать", "закончила покупать"],
    "uz": ["hammasi olindi", "hammasi sotib olindi", "hammasini oldim", "hammasini oldik",
           "xarid tugadi", "xaridni tugatdim", "xaridni yakunladim", "xaridni yakunla",
           "xaridlar tugadi", "ro'yxatni yakunla", "royxatni yakunla",
           "ro'yxatni saqla", "royxatni saqla", "ro'yxatni tugat", "royxatni tugat"],
}

_BAZAAR_THOUSAND_RE = re.compile(r"^(тысяч\w*|тыщ\w*|тыс\.?|минг\w*|ming\w*)$", re.IGNORECASE)
_BAZAAR_MILLION_RE = re.compile(r"^(миллион\w*|млн\.?|million\w*|mln)$", re.IGNORECASE)
_BAZAAR_CURRENCY_RE = re.compile(r"^(сум\w*|сўм\w*|so'?m\w*|sum)$", re.IGNORECASE)

# Glue words around a dictated purchase that carry no product meaning:
# purchase verbs, "paid/spent" verbs and prepositions. Both languages are
# always stripped — voice transcripts often mix RU/UZ.
_BAZAAR_STOP_WORDS = {
    "купил", "купила", "купили", "куплено", "взял", "взяла", "взяли",
    "приобрел", "приобрёл", "приобрела", "приобрели", "заплатил", "заплатила",
    "отдал", "отдала", "потратил", "потратила", "стоит", "стоил", "стоила",
    "за", "по", "на", "это", "уже", "а",
    "oldim", "oldik", "sotib", "uchun", "berdim", "turadi", "turdi",
    "to'ladim", "toladim", "sarfladim",
}

_BAZAAR_NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")


def _bazaar_normalize_price(value: float, has_thousand: bool, has_million: bool) -> int:
    """Interpret a spoken price in soums: bare short numbers mean thousands."""
    if has_million:
        return int(round(value * 1_000_000))
    if has_thousand:
        return int(round(value * 1000))
    if value < 1000:
        return int(round(value * 1000))
    return int(round(value))


def detect_bazaar_finish(text: str, lang: str) -> bool:
    """True when the utterance is a "shopping done / save the list" command."""
    lowered = re.sub(r"[^\w\s'’]", " ", (text or "").lower(), flags=re.UNICODE)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    if not lowered:
        return False
    other = "uz" if lang == "ru" else "ru"
    for check_lang in (lang, other):
        for phrase in BAZAAR_FINISH_PHRASES.get(check_lang, []):
            if phrase in lowered:
                return True
    return False


def parse_bazaar_purchases(text: str, lang: str = "ru") -> List[Dict[str, Any]]:
    """Extract (product, actual price) pairs from a free-form bazaar utterance.

    Deterministic parser for inputs like "Картошка 38", "Морковь 15, лук 12",
    "Купил мясо за 120 тысяч", "2 кг картошки за 38 тысяч", "Olma 25 ming".
    Returns a list of {"name": str, "price": int} dicts (price in soums).
    """
    if not text:
        return []
    lang = lang if lang in ("ru", "uz") else "ru"
    converted = _convert_number_words(text)
    purchases: List[Dict[str, Any]] = []

    for segment in re.split(r"[,;.!?\n]+", converted):
        tokens = re.findall(r"\d+(?:[.,]\d+)?|[\w'’-]+", segment, flags=re.UNICODE)
        name_tokens: List[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if _BAZAAR_NUMBER_RE.match(tok):
                # Merge thousand groups spoken with a pause: "38 500" -> "38500".
                value_str = tok
                j = i + 1
                while ("." not in value_str and "," not in value_str
                       and j < len(tokens) and re.fullmatch(r"\d{3}", tokens[j])):
                    value_str += tokens[j]
                    j += 1
                # A number right before a measurement unit is a quantity ("2 кг"),
                # not a price — skip both and keep collecting the product name.
                if j < len(tokens) and tokens[j].lower() in UNIT_MAPPING:
                    i = j + 1
                    continue
                value = float(value_str.replace(",", "."))
                has_thousand = has_million = False
                while j < len(tokens):
                    low = tokens[j].lower()
                    if _BAZAAR_THOUSAND_RE.match(low):
                        has_thousand = True
                        j += 1
                        continue
                    if _BAZAAR_MILLION_RE.match(low):
                        has_million = True
                        j += 1
                        continue
                    if _BAZAAR_CURRENCY_RE.match(low):
                        j += 1
                        continue
                    break
                name = " ".join(name_tokens).strip()
                if name and value > 0:
                    price = _bazaar_normalize_price(value, has_thousand, has_million)
                    if price > 0:
                        purchases.append({"name": name, "price": price})
                name_tokens = []
                i = j
                continue
            low = tok.lower().strip("'’-")
            if low and low not in _BAZAAR_STOP_WORDS and low not in SHOPPING_FILLER_WORDS:
                name_tokens.append(tok)
            i += 1
    return purchases


BAZAAR_GPT_PROMPT = {
    "ru": (
        "Ты учитываешь покупки пользователя на базаре. Из фразы извлеки покупки: "
        "название товара и фактическую цену в узбекских сумах. Правила цен: число без "
        "валюты меньше 1000 — это тысячи сумов (38 → 38000); «тысяч»/«ming» — умножь "
        "на 1000. Если пользователь сообщает, что закончил покупки (всё куплено, "
        "заверши список, сохрани список и т.п.) — верни finish=true. Отвечай ТОЛЬКО "
        'JSON вида: {"finish": false, "purchases": [{"name": "Картошка", "price": 38000}]}'
    ),
    "uz": (
        "Sen foydalanuvchining bozordagi xaridlarini hisobga olasan. Gapdan xaridlarni "
        "ajrat: mahsulot nomi va so'mdagi haqiqiy narx. Narx qoidalari: valyutasiz "
        "1000 dan kichik son — ming so'm (38 → 38000); «ming»/«тысяч» — 1000 ga "
        "ko'paytir. Agar foydalanuvchi xarid tugaganini aytsa (hammasi olindi, "
        "ro'yxatni yakunla va h.k.) — finish=true qaytar. FAQAT JSON qaytar: "
        '{"finish": false, "purchases": [{"name": "Kartoshka", "price": 38000}]}'
    ),
}


async def extract_bazaar_purchases_gpt(text: str, lang: str) -> Optional[Dict[str, Any]]:
    """GPT fallback for bazaar phrases the deterministic parser did not catch."""
    if not _is_openai_available():
        return None
    try:
        completion = client.chat.completions.create(
            model=Config.CHAT_MODEL,
            messages=[
                {"role": "system", "content": BAZAAR_GPT_PROMPT.get(lang, BAZAAR_GPT_PROMPT["ru"])},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
        )
        data = json.loads(completion.choices[0].message.content)
        purchases = []
        for p in data.get("purchases", []):
            name = str(p.get("name") or "").strip()
            try:
                price = int(round(float(p.get("price") or 0)))
            except (TypeError, ValueError):
                continue
            if name and price > 0:
                purchases.append({"name": name, "price": price})
        return {"finish": bool(data.get("finish")), "purchases": purchases}
    except Exception as e:
        logger.error(f"Bazaar GPT extraction error: {e}")
        return None


def enable_bazaar_mode(list_data: Dict) -> Dict:
    """Turn bazaar mode on: snapshot the forecast so plan-vs-actual can be compared."""
    if list_data.get("bazaar_mode"):
        return list_data
    list_data["bazaar_mode"] = True
    list_data["bazaar_started_at"] = datetime.now().isoformat()
    list_data["bazaar_planned_total"] = int(list_data.get("total_estimated_price") or 0)
    for items in list_data.get("categories", {}).values():
        for item in items:
            item.setdefault("planned_price", item.get("estimated_price"))
    return list_data


def apply_bazaar_purchases(list_data: Dict, purchases: List[Dict[str, Any]],
                           lang: str) -> Tuple[Dict, List[Dict[str, Any]]]:
    """Apply dictated purchases: check items off and replace forecast prices with
    real ones. Products missing from the list are added as already-purchased
    extras so offline analytics stays complete.

    Returns (list_data, applied) where each applied entry is
    {"name", "price", "planned_price", "added"}.
    """
    applied: List[Dict[str, Any]] = []
    for purchase in purchases:
        name = (purchase.get("name") or "").strip()
        price = int(purchase.get("price") or 0)
        if not name or price <= 0:
            continue

        matches = _resolve_list_items(list_data, name, lang)
        target = None
        for _cat, item in matches:
            if not item.get("purchased", False):
                target = item
                break
        if target is None and matches:
            target = matches[0][1]

        if target is None:
            # Bought something that was not on the list — add it as purchased.
            display_name = capitalize_first_letter(name)
            list_data = add_item_to_list(list_data, display_name, "", lang)
            for items in list_data.get("categories", {}).values():
                for item in items:
                    if item.get("name", "").lower() == display_name.lower():
                        target = item
                        break
                if target is not None:
                    break
            if target is None:  # blocked/unaddable product
                continue
            target["planned_price"] = None  # extras have no forecast to compare with
            target["purchased"] = True
            target["actual_price"] = price
            target["estimated_price"] = price
            applied.append({"name": target["name"], "price": price,
                            "planned_price": None, "added": True})
            continue

        if "planned_price" not in target:
            target["planned_price"] = target.get("estimated_price")
        target["purchased"] = True
        target["actual_price"] = price
        target["estimated_price"] = price
        applied.append({"name": target["name"], "price": price,
                        "planned_price": target.get("planned_price"), "added": False})

    if applied:
        list_data = recalculate_list_totals(list_data)
    return list_data, applied


def bazaar_summary(list_data: Dict) -> Dict[str, Any]:
    """Running plan-vs-actual stats for bazaar mode.

    planned_total — forecast for the whole list (snapshot at mode start);
    actual_total — real spend so far (actual prices; forecast for items checked
    off without a dictated price); savings — planned minus actual over items
    that have both prices (positive = saved, negative = overpaid).
    """
    planned_total = int(list_data.get("bazaar_planned_total") or 0)
    actual_total = 0
    savings = 0
    total_items = 0
    purchased_items = 0
    for items in list_data.get("categories", {}).values():
        for item in items:
            total_items += 1
            if not item.get("purchased", False):
                continue
            purchased_items += 1
            actual = item.get("actual_price")
            spent = actual if actual else (item.get("estimated_price") or 0)
            actual_total += spent or 0
            planned = item.get("planned_price")
            if actual and planned:
                savings += planned - actual
    return {
        "planned_total": planned_total,
        "actual_total": int(actual_total),
        "savings": int(savings),
        "purchased_items": purchased_items,
        "total_items": total_items,
    }


def _bazaar_hint_message(lang: str) -> str:
    return ("Не понял покупку. Скажите, например: «Картошка 38» или «Купил мясо за 120 тысяч»."
            if lang == "ru"
            else "Xaridni tushunmadim. Masalan: «Kartoshka 38» yoki «Go'sht 120 ming» deng.")


async def process_bazaar_text(user_id: int, text: str, lang: str) -> Tuple[int, Dict[str, Any]]:
    """Shared handler for dictated/typed bazaar phrases. Returns (status, payload)."""
    list_data = db.get_active_list(user_id)
    if not list_data:
        return 404, {"success": False, "error": "List not found"}
    lang = lang if lang in ("ru", "uz") else "ru"

    # Tolerate a lost mode flag (e.g. list re-created mid-shopping).
    if not list_data.get("bazaar_mode"):
        list_data = enable_bazaar_mode(list_data)
        db.save_active_list(user_id, list_data)

    if detect_bazaar_finish(text, lang):
        return 200, {"success": True, "type": "bazaar_finish", "summary": bazaar_summary(list_data)}

    purchases = parse_bazaar_purchases(text, lang)
    if not purchases:
        gpt_result = await extract_bazaar_purchases_gpt(text, lang)
        if gpt_result:
            if gpt_result.get("finish"):
                return 200, {"success": True, "type": "bazaar_finish",
                             "summary": bazaar_summary(list_data)}
            purchases = gpt_result.get("purchases") or []
    if not purchases:
        return 200, {"success": True, "type": "message", "message": _bazaar_hint_message(lang)}

    list_data, applied = apply_bazaar_purchases(list_data, purchases, lang)
    db.save_active_list(user_id, list_data)
    return 200, {"success": True, "type": "bazaar_update", "data": list_data,
                 "applied": applied, "summary": bazaar_summary(list_data)}


def apply_edit_changes(categories: Dict[str, List[Dict]], changes: List[Dict], lang: str = "ru") -> Dict[
    str, List[Dict]]:
    updated_categories = {k: [dict(item) for item in v] for k, v in categories.items()}

    for change in changes:
        action = change.get("action")
        target = (change.get("target") or "").strip()
        new_item = (change.get("new_item") or "").strip()
        quantity = (change.get("quantity") or "").strip()

        if new_item:
            new_item = capitalize_first_letter(new_item)
        if target:
            target = capitalize_first_letter(target)
        if quantity:
            quantity = normalize_quantity_display(quantity, lang)

        # Never introduce obscene / illegal / unsafe items via add/replace.
        if new_item and _is_blocked_product(new_item):
            if action in ("add", "replace", "update"):
                continue

        if action == "remove" and target:
            for cat_name in list(updated_categories.keys()):
                updated_items = [item for item in updated_categories[cat_name] if
                                 target.lower() not in item["name"].lower()]
                if updated_items:
                    updated_categories[cat_name] = updated_items
                else:
                    del updated_categories[cat_name]

        elif action == "add" and new_item:
            existing_found = False
            for items in updated_categories.values():
                for item in items:
                    if item["name"].lower() == new_item.lower():
                        if quantity:
                            item["quantity"] = quantity
                            item["user_specified_quantity"] = True
                        existing_found = True
                        break
                if existing_found:
                    break

            if not existing_found:
                target_category = get_display_category_for_product(new_item, lang)
                if target_category not in updated_categories:
                    updated_categories[target_category] = []
                updated_categories[target_category].append({
                    "name": new_item, "quantity": quantity, "purchased": False,
                    "estimated_price": None, "original_name": new_item, "user_specified_quantity": bool(quantity)
                })

        elif action == "replace" and target and new_item:
            target_found = False
            target_cat = None
            target_idx = None
            purchased_status = False

            for cat_name in list(updated_categories.keys()):
                for i, item in enumerate(updated_categories[cat_name]):
                    if target.lower() in item["name"].lower() or item["name"].lower() in target.lower():
                        target_found = True
                        target_cat = cat_name
                        target_idx = i
                        purchased_status = item.get("purchased", False)
                        break
                if target_found:
                    break

            if target_found:
                del updated_categories[target_cat][target_idx]
                if not updated_categories[target_cat]:
                    del updated_categories[target_cat]

                target_category = get_display_category_for_product(new_item, lang)
                if target_category not in updated_categories:
                    updated_categories[target_category] = []

                existing_found = False
                for item in updated_categories[target_category]:
                    if item["name"].lower() == new_item.lower():
                        if quantity:
                            item["quantity"] = quantity
                            item["user_specified_quantity"] = True
                        item["purchased"] = purchased_status
                        existing_found = True
                        break

                if not existing_found:
                    updated_categories[target_category].append({
                        "name": new_item, "quantity": quantity, "purchased": purchased_status,
                        "estimated_price": None, "original_name": new_item, "user_specified_quantity": bool(quantity)
                    })

        elif action == "update" and target and quantity:
            for items in updated_categories.values():
                for item in items:
                    if target.lower() in item["name"].lower() or item["name"].lower() in target.lower():
                        item["quantity"] = quantity
                        item["estimated_price"] = None
                        item["user_specified_quantity"] = True

    return {k: v for k, v in updated_categories.items() if v}


# ===== VOICE TRANSCRIPTION =====
# Uzbek STT (Aisha AI) has been removed. Voice input currently works for the
# Russian interface only; Uzbek-interface users get a "coming soon" notice and
# keep full text input. Whisper is pinned to the interface language so a
# Russian user never receives a mixed RU/UZ/EN transcript.
WHISPER_PROMPTS = {
    "ru": (
        "Список покупок на русском языке: продукты и количества. Например: "
        "картошка два килограмма, лук один килограмм, морковь, куриное филе, яйца десять штук."
    ),
}

VOICE_COMING_SOON_MESSAGE = {
    "ru": "Эта функция будет доступна в скором времени.",
    "uz": "Bu funksiya tez orada mavjud bo'ladi.",
}


async def transcribe_voice(file_path: str, lang: str = "ru") -> Optional[str]:
    """Transcribe a voice recording with OpenAI Whisper, forcing the target language."""
    if not _is_openai_available():
        logger.info("No transcription backend available (OPENAI_API_KEY missing)")
        return None

    logger.info(f"Whisper transcription start: file={file_path}, lang={lang}")

    def _do_transcribe() -> Optional[str]:
        with open(file_path, "rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=Config.STT_MODEL,
                file=audio_file,
                language=lang,
                prompt=WHISPER_PROMPTS.get(lang, ""),
                temperature=0,
            )
        return getattr(response, "text", None)

    text = await asyncio.to_thread(_do_transcribe)
    logger.info(f"Whisper transcription end: file={file_path}, chars={len(text) if text else 0}")
    return text


# ===== WEBSOCKET MANAGER =====
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, WebSocket] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        if user_id in self.active_connections:
            del self.active_connections[user_id]

    async def send_personal_message(self, user_id: int, message: dict):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(message)
            except:
                self.disconnect(user_id)


ws_manager = ConnectionManager()


# ===== HELPER FUNCTIONS FOR DATABASE-BASED LIST MANAGEMENT =====
def recalculate_list_totals(list_data: Dict) -> Dict:
    """Recalculate totals for a shopping list"""
    categories = list_data.get("categories", {})
    total_items = 0
    purchased_items = 0
    total_estimated_price = 0

    for category, items in categories.items():
        for item in items:
            total_items += 1
            if item.get("purchased", False):
                purchased_items += 1
            if item.get("estimated_price"):
                total_estimated_price += item.get("estimated_price", 0)

    list_data["total_items"] = total_items
    list_data["purchased_items"] = purchased_items
    list_data["total_estimated_price"] = total_estimated_price
    list_data["all_purchased"] = (total_items > 0 and purchased_items == total_items)
    list_data["needs_confirmation"] = list_data["all_purchased"]

    return list_data


def recalculate_list_prices(list_data: Dict, lang: str) -> Dict:
    """Recalculate item prices and normalized quantities for all list items."""
    categories = list_data.get("categories", {})

    for items in categories.values():
        for item in items:
            # A real price the user paid at the bazaar must never be overwritten
            # by a catalog forecast.
            if item.get("actual_price"):
                continue
            original_name = item.get("original_name") or item.get("name") or ""
            if not original_name:
                continue

            matches = price_db.find_products(original_name, lang)
            if not matches:
                continue

            best_match = price_db.choose_best_product_match(
                matches,
                original_name,
                lang,
                requested_quantity_text=item.get("quantity", "")
            ) or matches[0]
            price, normalized_qty, user_specified = price_db.calculate_price_for_product(
                best_match,
                item.get("quantity", ""),
                lang
            )
            # Same confidence gate as list creation: no price beats a wrong price.
            confidence = _score_match_confidence(original_name, best_match, lang)
            item["estimated_price"] = price if confidence >= MATCH_CONFIDENCE_THRESHOLD else None

            if user_specified:
                item["quantity"] = normalized_qty
            elif not item.get("quantity"):
                item["quantity"] = normalized_qty

    list_data["categories"] = categories
    return list_data


def toggle_item_purchased_in_list(list_data: Dict, category: str, item_name: str) -> Dict:
    """Toggle purchased status of a single item in list data"""
    categories = list_data.get("categories", {})

    if category in categories:
        for item in categories[category]:
            if item["name"] == item_name:
                item["purchased"] = not item.get("purchased", False)
                break

    list_data = recalculate_list_totals(list_data)
    return list_data


def toggle_category_purchased_in_list(list_data: Dict, category: str) -> Dict:
    """Toggle purchased status of all items in a category"""
    categories = list_data.get("categories", {})

    if category not in categories:
        return list_data

    # Check if all items in category are purchased
    category_items = categories[category]
    if not category_items:
        return list_data

    all_purchased = all(item.get("purchased", False) for item in category_items)
    new_purchased_status = not all_purchased

    # Toggle all items in category
    for item in category_items:
        item["purchased"] = new_purchased_status

    list_data = recalculate_list_totals(list_data)
    return list_data


def update_item_in_list(list_data: Dict, category: str, old_item_name: str, new_item_name: str, new_quantity: str,
                        lang: str) -> Dict:
    """Update an item in the list data"""
    categories = list_data.get("categories", {})

    if category not in categories:
        return list_data

    # Find and remove the old item
    item_found = False
    purchased_status = False
    item_to_remove_idx = None

    for i, item in enumerate(categories[category]):
        if item["name"] == old_item_name:
            purchased_status = item.get("purchased", False)
            item_to_remove_idx = i
            item_found = True
            break

    if not item_found:
        return list_data

    del categories[category][item_to_remove_idx]
    if not categories[category]:
        del categories[category]

    # Add the new item (possibly in a different category)
    target_category = get_display_category_for_product(new_item_name, lang)
    if target_category not in categories:
        categories[target_category] = []

    categories[target_category].append({
        "name": new_item_name,
        "quantity": new_quantity,
        "purchased": purchased_status,
        "estimated_price": None,
        "original_name": new_item_name,
        "user_specified_quantity": bool(new_quantity.strip())
    })

    list_data["categories"] = categories
    list_data = recalculate_list_prices(list_data, lang)
    list_data = recalculate_list_totals(list_data)
    return list_data


def add_item_to_list(list_data: Dict, item_name: str, quantity: str, lang: str) -> Dict:
    """Add an item to the list data"""
    categories = list_data.get("categories", {})

    # Never add obscene / illegal / unsafe items.
    if _is_blocked_product(item_name):
        list_data["categories"] = categories
        return list_data

    # Determine category for the new item
    target_category = get_display_category_for_product(item_name, lang)
    if target_category not in categories:
        categories[target_category] = []

    # Check if item already exists
    existing_found = False
    for item in categories[target_category]:
        if item["name"].lower() == item_name.lower():
            if quantity:
                item["quantity"] = quantity
                item["user_specified_quantity"] = True
            existing_found = True
            break

    if not existing_found:
        categories[target_category].append({
            "name": item_name,
            "quantity": quantity,
            "purchased": False,
            "estimated_price": None,
            "original_name": item_name,
            "user_specified_quantity": bool(quantity)
        })

    list_data["categories"] = categories
    list_data = recalculate_list_prices(list_data, lang)
    list_data = recalculate_list_totals(list_data)
    return list_data


def remove_item_from_list(list_data: Dict, category: str, item_name: str) -> Dict:
    """Remove an item from the list data"""
    categories = list_data.get("categories", {})

    if category in categories:
        categories[category] = [item for item in categories[category] if item["name"] != item_name]
        if not categories[category]:
            del categories[category]

    list_data["categories"] = categories
    list_data = recalculate_list_totals(list_data)
    return list_data


def merge_categories_into_list(list_data: Dict, new_categories: Dict[str, List[Dict]], lang: str) -> Dict:
    """Merge new categories into existing list data"""
    current_categories = list_data.get("categories", {})
    merged_categories = merge_categories(current_categories, new_categories)

    list_data["categories"] = merged_categories
    list_data = recalculate_list_prices(list_data, lang)
    list_data = recalculate_list_totals(list_data)
    return list_data


# ===== FASTAPI LIFESPAN =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Bozorlik AI Backend...")

    # Cleanup expired shared lists on startup
    try:
        deleted = shared_list_service.cleanup_expired()
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired shared lists")
    except Exception as e:
        logger.error(f"Error cleaning expired shared lists: {e}")

    yield

    logger.info("Shutting down Bozorlik AI Backend...")
    logger.info("Shutdown complete")


# ===== FASTAPI APP =====
app = FastAPI(
    title="Bozorlik AI Web Backend",
    description="Web интерфейс для Telegram бота Bozorlik AI",
    version="5.0.0",
    lifespan=lifespan
)

if Config.CORS_ALLOWED_ORIGINS.strip() == "*":
    _cors_origins = ["*"]
    _allow_credentials = False
    if Config.ENV == "production":
        logger.warning("CORS_ALLOWED_ORIGINS is '*' in production. Restrict it to explicit trusted origins.")
else:
    _cors_origins = [o.strip() for o in Config.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
    if not _cors_origins:
        _cors_origins = ["*"]
        _allow_credentials = False
    else:
        _allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== API ENDPOINTS =====
@app.get("/")
async def root():
    # Telegram WebView агрессивно кеширует HTML: без no-store пользователи после
    # деплоя продолжают видеть старую версию мини-приложения.
    return HTMLResponse(content=render_shared_page_html(""),
                        headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health_check():
    return JSONResponse(content={"status": "healthy", "timestamp": datetime.now().isoformat()})


# Serve static image/asset files (logos, icons) referenced by index.html.
_STATIC_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}


@app.get("/{filename}")
async def static_asset(filename: str):
    # Serve only known image asset types from the project root (single path
    # segment, so /api/... and /shared/... routes are never shadowed).
    suffix = Path(filename).suffix.lower()
    if suffix not in _STATIC_ASSET_EXTS:
        raise HTTPException(status_code=404, detail="Not found")
    file_path = (BASE_DIR / filename).resolve()
    if file_path.parent != BASE_DIR or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(file_path)


@app.get("/api/list/{user_id}")
async def get_active_list(user_id: int):
    """Get active shopping list for user"""
    try:
        list_data = db.get_active_list(user_id)
        if list_data:
            return JSONResponse(content={"success": True, "data": list_data})
        else:
            return JSONResponse(content={"success": True, "data": None})
    except Exception as e:
        logger.error(f"Get active list error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/quick-add")
async def quick_add_item(request: QuickAddRequest):
    try:
        user_id = request.user_id
        name = capitalize_first_letter(request.name.strip())
        quantity = request.quantity.strip()
        lang = request.language

        logger.info(f"Quick add: user={user_id}, name={name}, qty={quantity}")

        if not name:
            return JSONResponse(status_code=400, content={"success": False, "error": "Empty product name"})

        db.set_user_language(user_id, lang)

        if quantity:
            quantity = normalize_quantity_display(quantity, lang)

        # Get existing active list or create new one
        list_data = db.get_active_list(user_id)

        if list_data:
            # Add to existing list
            list_data = add_item_to_list(list_data, name, quantity, lang)
            list_data = recalculate_list_totals(list_data)
            db.save_active_list(user_id, list_data)

            # Add to history if all purchased and confirmed? No, quick add just adds items
            return JSONResponse(content={"success": True, "type": "shopping_list", "data": list_data, "added": True})
        else:
            # Create new list directly from quick-add input (no GPT dependency).
            list_data = {
                "categories": {},
                "items": [],
                "total_items": 0,
                "purchased_items": 0,
                "total_estimated_price": 0,
                "list_id": secrets.token_hex(8),
                "created_at": datetime.now().isoformat(),
                "all_purchased": False,
                "original_text": f"{name} {quantity}".strip(),
                "needs_confirmation": False,
                "localization": LOCALIZATION[lang],
                "owner_id": user_id,
            }

            list_data = add_item_to_list(list_data, name, quantity, lang)
            db.save_active_list(user_id, list_data)

            return JSONResponse(content={"success": True, "type": "shopping_list", "data": list_data, "added": True})
    except Exception as e:
        logger.error(f"Quick add error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/chat")
async def chat_message(chat_request: ChatMessage):
    try:
        user_id = chat_request.user_id
        text = chat_request.text.strip()
        lang = chat_request.language
        is_quick_add = chat_request.is_quick_add

        logger.info(f"Chat: user={user_id}, text={text[:50]}")

        if not text:
            return JSONResponse(status_code=400, content={"success": False, "error": "Empty message"})

        db.set_user_language(user_id, lang)

        if is_quick_add:
            list_data = db.get_active_list(user_id) or {"categories": {}, "total_items": 0, "purchased_items": 0,
                                                        "total_estimated_price": 0, "list_id": secrets.token_hex(8),
                                                        "created_at": datetime.now().isoformat(),
                                                        "all_purchased": False, "original_text": "",
                                                        "needs_confirmation": False, "localization": LOCALIZATION[lang],
                                                        "owner_id": user_id}
            list_data = add_item_to_list(list_data, capitalize_first_letter(text), "", lang)
            db.save_active_list(user_id, list_data)
            return JSONResponse(content={"success": True, "type": "shopping_list", "data": list_data, "added": True})

        greeting_words = {
            "привет", "салам", "здравствуй", "здравствуйте", "хай",
            "hi", "hello", "salom", "assalomu", "alaykum", "assalomu-alaykum"
        }
        greeting_text = re.sub(r"[^\w\s'-]", " ", text.lower(), flags=re.UNICODE)
        words = [w for w in greeting_text.split() if w]
        is_pure_greeting = 1 <= len(words) <= 5 and all(w in greeting_words for w in words)

        list_data = db.get_active_list(user_id)

        # Reject obscene / illegal / unsafe input. Strip blocked words; if nothing
        # meaningful remains, refuse instead of categorizing them as products.
        if _is_blocked_product(text):
            stripped = _strip_blocked_words(text)
            if not _has_product_signal(stripped):
                refusal = ("Извините, я могу помочь только со списком покупок."
                           if lang == "ru"
                           else "Kechirasiz, men faqat xaridlar ro'yxati bilan yordam bera olaman.")
                return JSONResponse(content={"success": True, "type": "message", "message": refusal})
            # Continue processing only the safe remainder.
            text = stripped

        # Deterministic voice commands: mark purchased / remove / replace by voice.
        # Checked before the recipe branch so "удали борщ" edits the list instead of
        # being mistaken for a dish request.
        if list_data:
            voice_command = detect_voice_list_command(text, lang)
            if voice_command:
                list_data, command_changed = apply_voice_list_command(list_data, voice_command, lang)
                if command_changed:
                    list_data = recalculate_list_prices(list_data, lang)
                    list_data = recalculate_list_totals(list_data)
                    db.save_active_list(user_id, list_data)
                    return JSONResponse(
                        content={"success": True, "type": "shopping_list", "data": list_data, "edited": True})
                # The command was understood but none of the named products are in
                # the list. Tell the user instead of falling through to the add
                # parser (which would wrongly ADD the products being removed).
                not_found_msg = ("Не нашёл эти продукты в текущем списке."
                                 if lang == "ru"
                                 else "Bu mahsulotlar joriy ro'yxatda topilmadi.")
                return JSONResponse(content={"success": True, "type": "message", "message": not_found_msg})

        # Shopping list by dish: "Хочу приготовить плов на 10 человек" -> ingredients.
        if looks_like_recipe_request(text, lang):
            recipe_result = build_recipe_shopping_list(text, lang)
            if recipe_result["found"]:
                if not list_data:
                    list_data = format_shopping_list_for_json({}, user_id, lang, original_text=text)
                list_data = add_recipe_ingredients_to_list(list_data, recipe_result["ingredients"], lang)
                db.save_active_list(user_id, list_data)
                return JSONResponse(content={
                    "success": True, "type": "shopping_list", "data": list_data, "added": True,
                    "recipe": {"dish": recipe_result["dish"], "servings": recipe_result["servings"]},
                })
            # A clear cooking-intent request whose dish is unknown -> tell the user.
            intent_keywords = RECIPE_INTENT_KEYWORDS.get(lang, RECIPE_INTENT_KEYWORDS["ru"])
            if any(kw in text.lower() for kw in intent_keywords):
                return JSONResponse(content={
                    "success": True, "type": "message", "message": recipe_result["message"],
                })

        # Deterministic shopping parser for short/direct product inputs.
        direct_categories = try_parse_direct_shopping_input(text, lang)
        if direct_categories:
            if list_data:
                list_data = merge_categories_into_list(list_data, direct_categories, lang)
            else:
                list_data = format_shopping_list_for_json(direct_categories, user_id, lang, original_text=text)
            db.save_active_list(user_id, list_data)
            return JSONResponse(content={"success": True, "type": "shopping_list", "data": list_data, "added": True})

        if is_pure_greeting:
            response_text = "Привет! Что нужно купить сегодня?" if lang == "ru" else "Salom! Bugun nima xarid qilish kerak?"
            return JSONResponse(content={"success": True, "type": "message", "message": response_text})

        add_keywords_ru = ["добавь", "добавить", "прибавь", "ещё", "плюс"]
        add_keywords_uz = ["qo'sh", "qo'shing", "yana", "plus"]
        is_add_command = any(word in text.lower() for word in (add_keywords_ru if lang == "ru" else add_keywords_uz))

        if list_data and is_add_command:
            changes = await detect_edit_changes(text, lang)
            if changes:
                # Apply changes to list
                current_categories = list_data.get("categories", {})
                updated_categories = apply_edit_changes(current_categories, changes, lang)
                list_data["categories"] = updated_categories
                list_data = recalculate_list_prices(list_data, lang)
                list_data = recalculate_list_totals(list_data)
                db.save_active_list(user_id, list_data)
                return JSONResponse(
                    content={"success": True, "type": "shopping_list", "data": list_data, "added": True})
            else:
                add_text = text
                for word in (add_keywords_ru if lang == "ru" else add_keywords_uz):
                    add_text = re.sub(rf'\b{word}\b', '', add_text, flags=re.IGNORECASE).strip()

                if add_text:
                    add_response = await format_list_with_gpt(add_text, lang)
                    add_categories = parse_shopping_list(add_response, lang)
                    list_data = merge_categories_into_list(list_data, add_categories, lang)
                    db.save_active_list(user_id, list_data)
                    return JSONResponse(
                        content={"success": True, "type": "shopping_list", "data": list_data, "added": True})

        if list_data:
            edit_keywords_ru = ["удали", "убрать", "убери", "замени", "измени", "поменяй", "обнови"]
            edit_keywords_uz = ["o'chir", "olib tashla", "almashtir", "o'zgartir", "yangila"]
            is_edit_command = any(
                word in text.lower() for word in (edit_keywords_ru if lang == "ru" else edit_keywords_uz))

            if is_edit_command:
                changes = await detect_edit_changes(text, lang)
                if changes:
                    current_categories = list_data.get("categories", {})
                    updated_categories = apply_edit_changes(current_categories, changes, lang)
                    list_data["categories"] = updated_categories
                    list_data = recalculate_list_prices(list_data, lang)
                    list_data = recalculate_list_totals(list_data)
                    db.save_active_list(user_id, list_data)
                    return JSONResponse(
                        content={"success": True, "type": "shopping_list", "data": list_data, "edited": True,
                                 "message": "Список обновлен"})

        response_text = await format_list_with_gpt(text, lang)

        categories = parse_shopping_list(response_text, lang)
        if categories:

            if list_data:
                list_data = merge_categories_into_list(list_data, categories, lang)
                db.save_active_list(user_id, list_data)
                return JSONResponse(
                    content={"success": True, "type": "shopping_list", "data": list_data, "added": True})
            else:
                list_json = format_shopping_list_for_json(categories, user_id, lang, original_text=text)
                db.save_active_list(user_id, list_json)
                return JSONResponse(content={"success": True, "type": "shopping_list", "data": list_json})
        else:
            if list_data:
                fallback_changes = await detect_edit_changes(text, lang)
                if fallback_changes:
                    current_categories = list_data.get("categories", {})
                    updated_categories = apply_edit_changes(current_categories, fallback_changes, lang)
                    list_data["categories"] = updated_categories
                    list_data = recalculate_list_prices(list_data, lang)
                    list_data = recalculate_list_totals(list_data)
                    db.save_active_list(user_id, list_data)
                    return JSONResponse(
                        content={"success": True, "type": "shopping_list", "data": list_data, "added": True})

            return JSONResponse(content={"success": True, "type": "message", "message": response_text})
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/recipe-list")
async def recipe_shopping_list(request: RecipeListRequest):
    """Build a shopping list from a dish request, e.g. 'плов на 10 человек'.

    When add_to_list is true (default), the scaled ingredients are also merged into
    the user's active list; otherwise only the computed ingredients are returned.
    """
    try:
        text = request.text.strip()
        lang = request.language
        if not text:
            return JSONResponse(status_code=400, content={"success": False, "error": "Empty message"})

        result = build_recipe_shopping_list(text, lang)
        if not result["found"]:
            return JSONResponse(content={"success": True, "found": False, "message": result["message"]})

        response = {
            "success": True, "found": True,
            "dish": result["dish"], "servings": result["servings"],
            "ingredients": result["ingredients"],
        }

        if request.add_to_list:
            db.set_user_language(request.user_id, lang)
            list_data = db.get_active_list(request.user_id)
            if not list_data:
                list_data = format_shopping_list_for_json({}, request.user_id, lang, original_text=text)
            list_data = add_recipe_ingredients_to_list(list_data, result["ingredients"], lang)
            db.save_active_list(request.user_id, list_data)
            response["data"] = list_data

        return JSONResponse(content=response)
    except Exception as e:
        logger.error(f"Recipe list error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


async def _read_and_transcribe_voice(voice_file: UploadFile, language: str) -> Tuple[Optional[str], Optional[JSONResponse]]:
    """Validate an uploaded audio file, transcribe it and clean up the temp file.

    Returns (text, None) on success or (None, error_response) on failure.
    """
    temp_file = None
    try:
        allowed_content_types = {
            "audio/ogg", "audio/oga", "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav",
            "audio/mp4", "audio/webm", "audio/x-m4a", "audio/aac", "audio/x-aac"
        }
        allowed_extensions = {".ogg", ".oga", ".mp3", ".wav", ".m4a", ".mp4", ".webm", ".aac"}

        raw_content_type = (voice_file.content_type or "").lower().strip()
        normalized_content_type = raw_content_type.split(";", 1)[0].strip() if raw_content_type else ""
        file_name = (voice_file.filename or "voice.ogg").lower()
        file_ext = os.path.splitext(file_name)[1]

        content_type_ok = normalized_content_type in allowed_content_types if normalized_content_type else False
        extension_ok = file_ext in allowed_extensions if file_ext else False

        # Some browsers/senders provide codec parameters or generic MIME types.
        # Accept when either MIME or file extension indicates a supported audio format.
        if not content_type_ok and not extension_ok:
            return None, JSONResponse(status_code=400, content={"success": False, "error": "Unsupported audio format"})

        # Save to temp file with extension close to the original format.
        suffix = file_ext if extension_ok else '.ogg'
        if not suffix:
            suffix = '.ogg'

        if normalized_content_type == 'audio/webm':
            suffix = '.webm'
        elif normalized_content_type in {'audio/mp4', 'audio/x-m4a'}:
            suffix = '.m4a'
        elif normalized_content_type in {'audio/wav', 'audio/x-wav'}:
            suffix = '.wav'
        elif normalized_content_type in {'audio/mpeg', 'audio/mp3'}:
            suffix = '.mp3'
        elif normalized_content_type in {'audio/aac', 'audio/x-aac'}:
            suffix = '.aac'
        content = await voice_file.read()
        if not content:
            return None, JSONResponse(status_code=400, content={"success": False, "error": "Empty audio file"})

        max_bytes = _voice_max_size_bytes()
        if len(content) > max_bytes:
            return None, JSONResponse(
                status_code=413,
                content={
                    "success": False,
                    "error": f"Audio file is too large. Max size is {Config.MAX_VOICE_FILE_SIZE_MB} MB"
                }
            )

        logger.info(f"Voice upload: filename={file_name}, content_type={raw_content_type}, normalized={normalized_content_type}, suffix={suffix}, bytes={len(content)}")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(content)
            temp_file = f.name

        try:
            text = await transcribe_voice(temp_file, language)
        except Exception as e:
            logger.error(f"Transcription error for file {temp_file}: {e}", exc_info=True)
            return None, JSONResponse(status_code=500,
                                      content={"success": False, "error": "Transcription failed", "detail": str(e)})

        if not text:
            error_msg = "Не удалось распознать голос" if language == "ru" else "Ovozni tanishib bo'lmadi"
            return None, JSONResponse(status_code=400, content={"success": False, "error": error_msg})

        logger.info(f"Transcribed: {text[:100]}")
        return text, None
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass


@app.post("/api/voice")
async def voice_message(
        user_id: int = Form(...),
        language: str = Form("ru"),
        voice_file: UploadFile = File(...)
):
    try:
        logger.info(f"Voice: user={user_id}, lang={language}")

        if language not in ["ru", "uz"]:
            return JSONResponse(status_code=400, content={"success": False, "error": "Unsupported language"})

        # Uzbek voice input is temporarily unavailable (Aisha STT removed);
        # Uzbek-interface users keep text input and get a "coming soon" notice.
        if language == "uz":
            return JSONResponse(content={
                "success": True,
                "type": "message",
                "message": VOICE_COMING_SOON_MESSAGE["uz"],
                "voice_unavailable": True,
            })

        text, error_response = await _read_and_transcribe_voice(voice_file, language)
        if error_response is not None:
            return error_response

        chat_request = ChatMessage(
            user_id=user_id, text=text, language=language,
            is_voice=True, is_quick_add=False
        )

        response = await chat_message(chat_request)
        response_data = json.loads(response.body)
        response_data["transcribed_text"] = text

        return JSONResponse(content=response_data)
    except Exception as e:
        logger.error(f"Voice error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


# ===== PHOTO LIST RECOGNITION (handwritten shopping lists) =====
PHOTO_OCR_SYSTEM_PROMPT = (
    "Ты распознаёшь рукописные и печатные списки покупок с фотографий. "
    "Текст может быть на русском или узбекском языке (кириллица или латиница), почерк может быть неаккуратным. "
    "Верни ТОЛЬКО товары, по одному на строку, в формате: название количество (если количество указано). "
    "Сохраняй количества ровно как написано. Не добавляй комментарии, пояснения, нумерацию или маркеры списка. "
    "Если на фото нет списка покупок или текст невозможно разобрать, верни ровно: NO_LIST"
)

_ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
_ALLOWED_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def _photo_max_size_bytes() -> int:
    return max(1, Config.MAX_PHOTO_FILE_SIZE_MB) * 1024 * 1024


def _clean_recognized_list_text(recognized: str) -> str:
    """Strip bullet markers / numbering the OCR model may still emit."""
    cleaned_lines = []
    for line in recognized.splitlines():
        line = line.strip()
        if not line or line.upper() == "NO_LIST":
            continue
        line = re.sub(r"^\s*(?:[-•*–—]|\d{1,2}[.)])\s*", "", line).strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


@app.post("/api/photo")
async def photo_message(
        user_id: int = Form(...),
        language: str = Form("ru"),
        photo_file: UploadFile = File(...)
):
    """Recognize a photographed (handwritten) shopping list and build a list from it.

    The OCR model extracts plain product lines; the text then goes through the
    same pipeline as a typed/voice message (deterministic parser + chat model).
    """
    try:
        logger.info(f"Photo: user={user_id}, lang={language}")

        if language not in ["ru", "uz"]:
            return JSONResponse(status_code=400, content={"success": False, "error": "Unsupported language"})

        if not _is_openai_available():
            msg = ("Сервис AI временно недоступен: не настроен OPENAI_API_KEY."
                   if language == "ru"
                   else "AI xizmati vaqtincha mavjud emas: OPENAI_API_KEY sozlanmagan.")
            return JSONResponse(status_code=503, content={"success": False, "error": msg})

        raw_content_type = (photo_file.content_type or "").lower().strip()
        content_type = raw_content_type.split(";", 1)[0].strip()
        file_ext = os.path.splitext((photo_file.filename or "").lower())[1]

        if content_type not in _ALLOWED_PHOTO_TYPES and file_ext not in _ALLOWED_PHOTO_EXTENSIONS:
            return JSONResponse(status_code=400, content={"success": False, "error": "Unsupported image format"})

        content = await photo_file.read()
        if not content:
            return JSONResponse(status_code=400, content={"success": False, "error": "Empty image file"})

        if len(content) > _photo_max_size_bytes():
            return JSONResponse(
                status_code=413,
                content={
                    "success": False,
                    "error": f"Image is too large. Max size is {Config.MAX_PHOTO_FILE_SIZE_MB} MB"
                }
            )

        mime = content_type if content_type in _ALLOWED_PHOTO_TYPES else "image/jpeg"
        data_url = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"

        def _do_ocr() -> str:
            completion = client.chat.completions.create(
                model=Config.OCR_MODEL,
                messages=[
                    {"role": "system", "content": PHOTO_OCR_SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text",
                         "text": "Распознай список покупок на фото и верни его текстом построчно."},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                    ]},
                ],
                temperature=0,
                max_tokens=700,
            )
            return (completion.choices[0].message.content or "").strip()

        recognized = await asyncio.to_thread(_do_ocr)
        recognized_text = _clean_recognized_list_text(recognized or "")

        if not recognized_text:
            error_msg = ("Не удалось распознать список покупок на фото"
                         if language == "ru"
                         else "Fotodagi xaridlar ro'yxatini aniqlab bo'lmadi")
            return JSONResponse(status_code=400, content={"success": False, "error": error_msg})

        logger.info(f"Photo OCR result: {recognized_text[:120]}")

        chat_request = ChatMessage(
            user_id=user_id, text=recognized_text, language=language,
            is_voice=False, is_quick_add=False
        )
        response = await chat_message(chat_request)
        response_data = json.loads(response.body)
        response_data["recognized_text"] = recognized_text

        return JSONResponse(content=response_data)
    except Exception as e:
        logger.error(f"Photo error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


# ===== PREMIUM: RECEIPT SCANNING (Analytics page) =====
# GPT-4.1 Vision does ALL recognition work — no regex parsing, no manual OCR.
# Pipeline: analyze_receipt_image() -> normalize_receipt() -> categorize_items()
#           -> save_receipt() -> save_purchase_history() -> update_analytics()
# Only already-processed structured data is persisted. Purchase history is kept
# per item so future features (repeat purchases, "buy again", "what I usually
# buy", AI recommendations, personal analytics) can build on it directly.

RECEIPT_CATEGORIES = [
    "Овощи", "Фрукты", "Молочные продукты", "Мясо", "Рыба и морепродукты",
    "Бакалея", "Хлебобулочные изделия", "Напитки", "Сладости", "Масла",
    "Замороженные продукты", "Детские товары", "Бытовая химия",
    "Личная гигиена", "Аптека", "Для животных", "Канцелярия", "Электроника",
    "Другое",
]

RECEIPT_VISION_PROMPT = (
    "Ты распознаёшь кассовые чеки магазинов по фотографии. "
    "Извлеки данные и верни ТОЛЬКО JSON строго следующего формата, без пояснений:\n"
    "{\n"
    '  "store": "название магазина",\n'
    '  "date": "дата покупки в формате YYYY-MM-DD (если не видно — пустая строка)",\n'
    '  "currency": "валюта чека (например: сум, руб, USD)",\n'
    '  "total": итоговая сумма числом,\n'
    '  "items": [\n'
    '    {"name_ru": "название товара на русском", '
    '"name_uz": "название товара на узбекском (латиницей)", '
    '"category": "категория", "quantity": число, '
    '"unit": "единица измерения (шт, кг, г, л, мл, уп)", "price": сумма по строке числом}\n'
    "  ]\n"
    "}\n\n"
    "Категорию каждого товара определяй автоматически по смыслу, строго из списка: "
    + ", ".join(RECEIPT_CATEGORIES) + ". "
    "Если категорию определить невозможно — используй \"Другое\".\n\n"
    "СТРУКТУРА ДЛИННЫХ ЧЕКОВ (Korzinka, Makro и похожие магазины Узбекистана):\n"
    "Каждый товар занимает БЛОК из нескольких строк. Блок начинается со строки с названием "
    "товара (название часто продублировано на двух языках — узбекском и русском), и цена "
    "товара — это число СПРАВА от этой первой строки блока. Дальше идут служебные строки "
    "блока: скидка (Chegirma), \"sh.j. QQS 12%\" (сумма НДС), \"Sh.k./MXIK\" (код товара), "
    "\"MK\" (код маркировки), \"Tovarni kelib chiqishi\" и \"Oldi-sotdi\".\n"
    "Строка \"Oldi-sotdi\" ЗАВЕРШАЕТ блок товара. СЛЕДУЮЩИЙ товар начинается СРАЗУ ПОСЛЕ "
    "строки \"Oldi-sotdi\" предыдущего товара: следующая строка = название нового товара, "
    "число справа от неё = настоящая цена этого товара.\n"
    "ОСОБОЕ ВНИМАНИЕ на ВТОРОЙ, ТРЕТИЙ и все последующие товары: рядом с их названием видно "
    "несколько чисел (цена товара, сумма QQS, скидка). НЕ бери первое попавшееся число. "
    "НИКОГДА не используй значение из строки \"QQS 12%\" / \"sh.j. QQS\" как цену товара — "
    "это сумма налога, она всегда намного меньше цены. Если выбираешь между суммой QQS "
    "(например 534,64) и ценой из строки товара (например 4990,00) — всегда выбирай цену "
    "из строки товара.\n"
    "Если у товара есть скидка (Chegirma), ценой по строке считай итоговую цену после "
    "скидки.\n\n"
    "В \"items\" добавляй ТОЛЬКО реально купленные товары. НИКОГДА не добавляй как товар "
    "служебные строки чека, в том числе: QQS, VAT, НДС, TAX, налог, сумма налога, "
    "любые строки с процентом налога (например \"QQS 12%\"), итого, итого к оплате, "
    "всего, к оплате, оплачено, сдача, скидка, акция, cashback, кэшбэк, бонусы, "
    "QR-коды, банковские данные, номера и последние цифры карты, номер терминала, "
    "ID операции, фискальные номера, ИНН, кассир, смена, любую другую служебную "
    "информацию.\n"
    "Строка НДС/QQS/VAT ВСЕГДА относится к предыдущему товару и НЕ является отдельным "
    "товаром. Сумму налога НИКОГДА не используй как цену товара — цена берётся только "
    "из строки самого товара.\n"
    "Каждый товар должен встречаться в \"items\" только один раз. Если сомневаешься, "
    "товар это или служебная строка — НЕ добавляй его.\n\n"
    "ИГНОРИРУЙ и НЕ извлекай: QR-коды, банковские данные, номера карт, ID операций, "
    "данные платёжного терминала, рекламные сообщения, служебную информацию чека "
    "(кассир, смена, ИНН, фискальные номера).\n\n"
    "Название каждого товара возвращай на ДВУХ языках: name_ru — по-русски, name_uz — "
    "по-узбекски латиницей. На чеках Узбекистана название обычно напечатано на обоих "
    "языках — бери обе строки одного блока; если название есть только на одном языке, "
    "переведи его сам. Названия пиши понятно и коротко, с большой буквы, без кассовых "
    "сокращений, если они восстановимы (например \"Мол. 3.2%\" -> \"Молоко 3.2%\").\n"
    "Если на фото нет кассового чека или он нечитаем, верни ровно: {\"error\": \"NOT_A_RECEIPT\"}"
)

RECEIPT_ERROR_MESSAGES = {
    "ru": "Не удалось распознать чек. Сфотографируйте чек целиком при хорошем освещении.",
    "uz": "Chekni aniqlab bo'lmadi. Chekni yaxshi yorug'likda to'liq suratga oling.",
}


def analyze_receipt_image(image_bytes: bytes, mime: str) -> Optional[Dict[str, Any]]:
    """Send the receipt photo to GPT-4.1 Vision and return its raw JSON dict.

    Returns None when the model reports the image is not a readable receipt.
    """
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    completion = client.chat.completions.create(
        model=Config.RECEIPT_MODEL,
        messages=[
            {"role": "system", "content": RECEIPT_VISION_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "Распознай этот кассовый чек и верни JSON."},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
            ]},
        ],
        temperature=0,
        max_tokens=3500,
        response_format={"type": "json_object"},
    )
    raw = (completion.choices[0].message.content or "").strip()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("error") or not data.get("items"):
        return None
    return data


def _to_float(value: Any, default: float = 0.0) -> float:
    """Tolerant number coercion for model output ('12 500,50' -> 12500.5)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


# --- Programmatic receipt sanitation -------------------------------------
# Second line of defence after the vision model. GPT occasionally emits a
# VAT/tax/subtotal/payment line as if it were a purchased product (e.g. a
# "QQS 12%" line becomes a second item priced at the tax amount). These
# helpers strip such service rows and collapse duplicates AFTER the model
# answers, so a non-product line can never survive as a stored item.
# Prefer dropping a doubtful line over keeping a service row as a product.

# High-precision service/tax/payment tokens (ru / uz / en). A line whose name
# contains any of these as a standalone word is service info, not a product.
RECEIPT_SERVICE_TOKENS = frozenset({
    # НДС / VAT / налог
    "qqs", "vat", "ндс", "nds", "tax", "налог", "налога", "налогов",
    "soliq", "солиқ",
    # subtotals / totals / payment
    "итого", "итог", "всего", "jami", "оплачено", "оплате", "сдача",
    "наличными", "картой", "total", "subtotal", "tolov", "tolandi",
    # discounts / loyalty
    "скидка", "скидки", "акция", "cashback", "кэшбэк", "кешбэк",
    "бонус", "бонусы", "chegirma", "aksiya", "bonus",
    # terminal / fiscal / cashier service info
    "терминал", "terminal", "фискальный", "фискальн", "инн", "кассир",
    "смена", "rrn",
})

# Multi-word markers a single token can't catch.
RECEIPT_SERVICE_PHRASES = (
    "к оплате", "сумма ндс", "сумма налога", "общая сумма", "итого к оплате",
    "номер терминала", "номер карты", "последние цифры", "qr-код", "qr код",
    "qr code", "гос налог", "с ндс", "без ндс",
)

# Currency and unit words that carry no product meaning on their own. A line
# made only of these plus digits/percent (e.g. "1060 сум", "12%") is not a
# product.
RECEIPT_NOISE_WORDS = frozenset({
    "сум", "сўм", "som", "so", "m", "руб", "rub", "usd", "uzs", "uz",
    "шт", "кг", "г", "гр", "л", "мл", "уп", "pcs", "kg", "dona", "x", "х",
})


def _is_service_line(name: str) -> bool:
    """True when a receipt line is tax/subtotal/payment/service info rather
    than a purchased product. Such lines must never be stored as items."""
    if not name:
        return True
    lowered = name.lower()
    if any(phrase in lowered for phrase in RECEIPT_SERVICE_PHRASES):
        return True
    letter_tokens = re.findall(r"[^\W\d_]+", lowered, flags=re.UNICODE)
    # Only digits / currency / units left (e.g. "12%", "1060 сум") — a price
    # or percentage row, not a product.
    if not any(tok not in RECEIPT_NOISE_WORDS for tok in letter_tokens):
        return True
    return any(tok in RECEIPT_SERVICE_TOKENS for tok in letter_tokens)


def _dedupe_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep each product only once (first occurrence wins). A tax/service row
    duplicated under a product's name always follows the real line on a
    receipt, so dropping later duplicates removes it while keeping the item."""
    seen: set = set()
    result: List[Dict[str, Any]] = []
    for item in items:
        key = item["name"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def normalize_receipt(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate/coerce the vision output into a stable structure for storage.

    Applies programmatic sanitation on top of the model output: service/tax
    lines are dropped and duplicate products collapsed, so QQS/VAT/total/etc.
    rows can never end up stored as purchased items.
    """
    items = []
    for item in raw.get("items", []):
        if not isinstance(item, dict):
            continue
        name_ru = str(item.get("name_ru") or item.get("name") or "").strip()
        name_uz = str(item.get("name_uz") or "").strip()
        name = name_ru or name_uz
        if not name or _is_blocked_product(name) or _is_service_line(name):
            continue
        if name_uz and (_is_blocked_product(name_uz) or _is_service_line(name_uz)):
            continue
        quantity = _to_float(item.get("quantity"), 1.0) or 1.0
        items.append({
            "name": capitalize_first_letter(name),
            "name_ru": capitalize_first_letter(name_ru or name),
            "name_uz": capitalize_first_letter(name_uz),
            "category": str(item.get("category") or "").strip(),
            "quantity": round(quantity, 3),
            "unit": str(item.get("unit") or "шт").strip() or "шт",
            "price": round(_to_float(item.get("price")), 2),
        })

    items = _dedupe_items(items)

    total = round(_to_float(raw.get("total")), 2)
    if total <= 0:
        total = round(sum(i["price"] for i in items), 2)

    return {
        "store": str(raw.get("store") or "").strip(),
        "date": str(raw.get("date") or "").strip(),
        "currency": str(raw.get("currency") or "сум").strip() or "сум",
        "total": total,
        "items": items,
    }


def categorize_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ensure every item carries a valid category; unknown ones become 'Другое'."""
    valid = {c.lower(): c for c in RECEIPT_CATEGORIES}
    for item in items:
        category = (item.get("category") or "").strip()
        item["category"] = valid.get(category.lower(), "Другое")
    return items


# ===== LANGUAGE LOCALIZATION OF STORED CONTENT =====
# Receipts are stored with canonical Russian categories/units and bilingual
# product names (name_ru / name_uz from the vision model). Everything the UI
# shows is localized to the user's CURRENT language at read time, so switching
# the interface language switches product data too.

RECEIPT_CATEGORY_UZ = {
    "Овощи": "Sabzavotlar", "Фрукты": "Mevalar", "Молочные продукты": "Sut mahsulotlari",
    "Мясо": "Go'sht", "Рыба и морепродукты": "Baliq mahsulotlari",
    "Бакалея": "Oziq-ovqat", "Хлебобулочные изделия": "Non mahsulotlari",
    "Напитки": "Ichimliklar", "Сладости": "Shirinliklar", "Масла": "Yog'lar",
    "Замороженные продукты": "Muzlatilgan mahsulotlar", "Детские товары": "Bolalar mahsulotlari",
    "Бытовая химия": "Maishiy kimyo", "Личная гигиена": "Shaxsiy gigiyena",
    "Аптека": "Dorixona", "Для животных": "Hayvonlar uchun", "Канцелярия": "Kantselyariya",
    "Электроника": "Elektronika", "Другое": "Boshqa",
}
RECEIPT_CATEGORY_RU = {v: k for k, v in RECEIPT_CATEGORY_UZ.items()}

UNIT_RU_UZ = {"шт": "dona", "уп": "qadoq", "пачка": "qadoq", "кг": "kg", "г": "g",
              "гр": "g", "л": "l", "мл": "ml", "банка": "banka", "бутылка": "shisha"}
UNIT_UZ_RU = {"dona": "шт", "qadoq": "уп", "kg": "кг", "g": "г", "l": "л",
              "ml": "мл", "banka": "банка", "shisha": "бутылка"}

_UZ_CURRENCY_WORDS = {"so'm", "so‘m", "som", "so`m", "sum"}
_RU_CURRENCY_WORDS = {"сум", "сўм"}


def localize_receipt_category(category: str, lang: str) -> str:
    category = (category or "").strip() or "Другое"
    if lang == "uz":
        return RECEIPT_CATEGORY_UZ.get(category, category)
    return RECEIPT_CATEGORY_RU.get(category, category)


def localize_unit(unit: str, lang: str) -> str:
    unit = (unit or "").strip()
    if lang == "uz":
        return UNIT_RU_UZ.get(unit.lower(), unit or "dona")
    return UNIT_UZ_RU.get(unit.lower(), unit or "шт")


def localize_currency(currency: str, lang: str) -> str:
    cur = (currency or "").strip()
    if lang == "uz" and cur.lower() in _RU_CURRENCY_WORDS:
        return "so'm"
    if lang == "ru" and cur.lower() in _UZ_CURRENCY_WORDS:
        return "сум"
    return cur or ("so'm" if lang == "uz" else "сум")


def localize_receipt_item(item: Dict[str, Any], lang: str) -> Dict[str, Any]:
    """Return a copy of a receipt/purchase item with name, category and unit
    presented in the requested language (bilingual fields are kept)."""
    out = dict(item)
    name_ru = (item.get("name_ru") or item.get("name") or "").strip()
    name_uz = (item.get("name_uz") or "").strip()
    out["name"] = (name_uz if lang == "uz" and name_uz else name_ru) or (item.get("name") or "")
    out["category"] = localize_receipt_category(item.get("category"), lang)
    out["unit"] = localize_unit(item.get("unit"), lang)
    if "currency" in out:
        out["currency"] = localize_currency(out.get("currency"), lang)
    return out


def localize_receipt(receipt: Dict[str, Any], lang: str) -> Dict[str, Any]:
    out = dict(receipt)
    out["items"] = [localize_receipt_item(i, lang) for i in receipt.get("items", [])]
    out["currency"] = localize_currency(receipt.get("currency"), lang)
    return out


# --- Translation of product names (catalog first, GPT batch as fallback) ---

LIST_CATEGORY_RU_UZ = {
    "🥕 Овощи": "🥕 Sabzavotlar", "🍎 Фрукты": "🍎 Mevalar",
    "🥛 Молочные продукты": "🥛 Sut mahsulotlari", "🍖 Мясные продукты": "🍖 Go'sht mahsulotlari",
    "📦 Бакалея": "📦 Oziq-ovqat", "🥤 Напитки": "🥤 Ichimliklar",
    "🧴 Гигиена и быт": "🧴 Gigiyena", "🧂 Приправы": "🧂 Ziravorlar",
    "📝 Другое": "📝 Boshqalar", "🍵 Чай и кофе": "🍵 Choy va kofe", "🍿 Снеки": "🍿 Snacklar",
}
LIST_CATEGORY_UZ_RU = {v: k for k, v in LIST_CATEGORY_RU_UZ.items()}


def translate_list_category(category: str, dst_lang: str) -> str:
    if dst_lang == "uz":
        return LIST_CATEGORY_RU_UZ.get(category, category)
    return LIST_CATEGORY_UZ_RU.get(category, category)


# name (lowercase), dst_lang -> translated name. Process-wide cache so repeated
# language switches don't re-ask GPT for the same products.
_NAME_TRANSLATION_CACHE: Dict[Tuple[str, str], str] = {}


def translate_name_via_catalog(name: str, src_lang: str, dst_lang: str) -> Optional[str]:
    """Translate a product name through the price catalog (name_ru <-> name_uz).

    Accepted only when the catalog product's source-language name is essentially
    the same phrase as the input (stemmed equality). A merely-confident partial
    match would silently add brand/quantity details ("Молоко" -> "Sut Lactel
    3,2%"), which is wrong for translation; such names go to the GPT fallback."""
    try:
        products = price_db.find_products(name, src_lang)
        if not products:
            return None
        best = price_db.choose_best_product_match(products, name, src_lang)
        if not best or _score_match_confidence(name, best, src_lang) < MATCH_CONFIDENCE_THRESHOLD:
            return None
        source_name = (best.get(f"name_{src_lang}") or "").strip()
        query_norm = _stem_phrase(price_db._normalize_for_index(name))
        match_norm = _stem_phrase(price_db._normalize_for_index(source_name))
        if not query_norm or query_norm != match_norm:
            return None
        translated = (best.get(f"name_{dst_lang}") or "").strip()
        return capitalize_first_letter(translated) if translated else None
    except Exception:
        return None


def _translate_names_gpt(names: List[str], src_lang: str, dst_lang: str) -> Dict[str, str]:
    """One GPT call translating a batch of product names; returns {original: translated}."""
    if not names or not _is_openai_available():
        return {}
    lang_titles = {"ru": "русский", "uz": "узбекский (латиница)"}
    system = (
        "Ты переводишь названия продуктов и товаров из списка покупок с языка "
        f"«{lang_titles[src_lang]}» на язык «{lang_titles[dst_lang]}». "
        "Верни ТОЛЬКО JSON-объект, где ключ — исходное название ровно как в запросе, "
        "значение — перевод. Бренды и торговые марки не переводи (оставляй как есть), "
        "переводи только описательную часть названия. Если название уже на целевом "
        "языке — верни его без изменений."
    )
    result: Dict[str, str] = {}
    chunk_size = 80
    for start in range(0, len(names), chunk_size):
        chunk = names[start:start + chunk_size]
        completion = client.chat.completions.create(
            model=Config.CHAT_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)},
            ],
            temperature=0,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )
        try:
            data = json.loads((completion.choices[0].message.content or "").strip())
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, str) and value.strip():
                        result[key] = value.strip()
        except (ValueError, TypeError):
            continue
    return result


def translate_product_names(names: List[str], src_lang: str, dst_lang: str) -> Dict[str, str]:
    """Translate product names src->dst: cache, then price catalog, then GPT batch.
    Names that cannot be translated stay unchanged."""
    result: Dict[str, str] = {}
    pending: List[str] = []
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        cache_key = (name.lower(), dst_lang)
        if cache_key in _NAME_TRANSLATION_CACHE:
            result[name] = _NAME_TRANSLATION_CACHE[cache_key]
            continue
        via_catalog = translate_name_via_catalog(name, src_lang, dst_lang)
        if via_catalog:
            result[name] = via_catalog
            _NAME_TRANSLATION_CACHE[cache_key] = via_catalog
        else:
            pending.append(name)
    if pending:
        pending = sorted(set(pending))
        try:
            gpt_map = _translate_names_gpt(pending, src_lang, dst_lang)
        except Exception as e:
            logger.error(f"GPT name translation error: {e}")
            gpt_map = {}
        for name in pending:
            translated = capitalize_first_letter((gpt_map.get(name) or "").strip())
            # No translation (GPT unavailable/failed) -> leave the name out so
            # callers keep the original and a later switch can retry.
            if translated:
                result[name] = translated
                _NAME_TRANSLATION_CACHE[(name.lower(), dst_lang)] = translated
    return result


def _collect_list_names(list_data: Dict[str, Any]) -> set:
    names = set()
    for items in (list_data.get("categories") or {}).values():
        for item in items:
            for key in ("name", "original_name"):
                value = (item.get(key) or "").strip()
                if value:
                    names.add(value)
    return names


def translate_list_data(list_data: Dict[str, Any], dst_lang: str, name_map: Dict[str, str]) -> Dict[str, Any]:
    """Rewrite a stored shopping list into dst_lang: category keys, item names,
    quantity units and the embedded localization block."""
    new_categories: Dict[str, List[Dict[str, Any]]] = {}
    for category, items in (list_data.get("categories") or {}).items():
        new_category = translate_list_category(category, dst_lang)
        bucket = new_categories.setdefault(new_category, [])
        for item in items:
            translated = dict(item)
            name = (item.get("name") or "").strip()
            translated["name"] = name_map.get(name, name)
            original = (item.get("original_name") or "").strip() or name
            translated["original_name"] = name_map.get(original, translated["name"])
            if item.get("quantity"):
                translated["quantity"] = normalize_quantity_display(str(item["quantity"]), dst_lang)
            if "category" in translated:
                translated["category"] = new_category
            bucket.append(translated)
    list_data["categories"] = new_categories
    list_data["items"] = [item for items in new_categories.values() for item in items]
    list_data["localization"] = LOCALIZATION[dst_lang]
    return list_data


def _translate_user_content(user_id: int, src_lang: str, dst_lang: str) -> bool:
    """Rewrite all of a user's stored shopping content into dst_lang: the active
    list, saved history lists, and backfill missing bilingual names on scanned
    receipts / purchase history. Runs when the user switches the interface
    language, so products never stay in the previous language."""
    if src_lang == dst_lang or dst_lang not in ("ru", "uz"):
        return False

    active_list = db.get_active_list(user_id)
    history_entries = db.get_user_history_raw(user_id)

    names = set()
    if active_list:
        names |= _collect_list_names(active_list)
    for _, entry_data in history_entries:
        names |= _collect_list_names(entry_data)
    name_map = translate_product_names(sorted(names), src_lang, dst_lang) if names else {}

    if active_list:
        db.save_active_list(user_id, translate_list_data(active_list, dst_lang, name_map))
    for list_id, entry_data in history_entries:
        db.update_history_entry(user_id, list_id, translate_list_data(entry_data, dst_lang, name_map))

    # Receipts/purchases store canonical Russian + bilingual names and are
    # localized at read time; here we only backfill name_uz for items scanned
    # before bilingual storage existed.
    if dst_lang == "uz":
        missing = set()
        receipts = db.get_user_receipts(user_id)
        for receipt in receipts:
            for item in receipt.get("items", []):
                if not (item.get("name_uz") or "").strip():
                    missing.add((item.get("name_ru") or item.get("name") or "").strip())
        purchases = db.get_purchase_history(user_id, limit=1000)
        for row in purchases:
            if not (row.get("name_uz") or "").strip():
                missing.add((row.get("name_ru") or row.get("name") or "").strip())
        missing.discard("")
        if missing:
            uz_map = translate_product_names(sorted(missing), "ru", "uz")
            for receipt in receipts:
                changed = False
                for item in receipt.get("items", []):
                    if not (item.get("name_uz") or "").strip():
                        base = (item.get("name_ru") or item.get("name") or "").strip()
                        if base and uz_map.get(base):
                            item["name_uz"] = uz_map[base]
                            item.setdefault("name_ru", base)
                            changed = True
                if changed:
                    db.update_receipt_items(receipt["id"], receipt.get("items", []))
            for row in purchases:
                if not (row.get("name_uz") or "").strip():
                    base = (row.get("name_ru") or row.get("name") or "").strip()
                    if base and uz_map.get(base):
                        db.update_purchase_item_names(row["id"], name_ru=base, name_uz=uz_map[base])
    return True


def save_receipt(user_id: int, receipt: Dict[str, Any]) -> int:
    """Persist the processed receipt; returns the stored receipt id."""
    return db.save_receipt(user_id, receipt)


def save_purchase_history(user_id: int, receipt_id: int, receipt: Dict[str, Any]) -> int:
    """Append all receipt items to the user's purchase history."""
    return db.add_purchase_history_items(user_id, receipt_id, receipt)


def update_analytics(user_id: int, lang: str = "ru") -> Dict[str, Any]:
    """Recompute receipt analytics for the user from stored receipts.

    Returns totals, receipt count, average receipt, spend by category (with the
    most expensive first and percentage distribution) and recent purchases.
    Category and product names are localized into `lang`.
    """
    receipts = db.get_user_receipts(user_id)
    total_spent = round(sum(r.get("total") or 0 for r in receipts), 2)
    receipt_count = len(receipts)
    average_receipt = round(total_spent / receipt_count, 2) if receipt_count else 0

    by_category: Dict[str, float] = {}
    for receipt in receipts:
        for item in receipt.get("items", []):
            cat = localize_receipt_category(item.get("category"), lang)
            by_category[cat] = round(by_category.get(cat, 0) + (item.get("price") or 0), 2)

    top_categories = [
        {"category": cat, "amount": amount,
         "percent": round(amount / total_spent * 100, 1) if total_spent else 0}
        for cat, amount in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
    ]

    currency = localize_currency(receipts[0].get("currency") if receipts else "", lang)
    recent_purchases = [localize_receipt_item(p, lang)
                        for p in db.get_purchase_history(user_id, limit=10)]

    return {
        "total_spent": total_spent,
        "receipt_count": receipt_count,
        "average_receipt": average_receipt,
        "by_category": by_category,
        "top_categories": top_categories,
        "recent_purchases": recent_purchases,
        "currency": currency,
    }


@app.post("/api/receipt")
async def scan_receipt(
        user_id: int = Form(...),
        language: str = Form("ru"),
        receipt_file: UploadFile = File(...)
):
    """Premium receipt scanning: photo -> GPT-4.1 Vision -> structured receipt.

    On success the receipt and all items are saved automatically, items land in
    the purchase history, and fresh analytics are returned for the UI.
    """
    try:
        logger.info(f"Receipt scan: user={user_id}, lang={language}")
        lang = language if language in ("ru", "uz") else "ru"

        if not _is_openai_available():
            msg = ("Сервис AI временно недоступен: не настроен OPENAI_API_KEY."
                   if lang == "ru"
                   else "AI xizmati vaqtincha mavjud emas: OPENAI_API_KEY sozlanmagan.")
            return JSONResponse(status_code=503, content={"success": False, "error": msg})

        raw_content_type = (receipt_file.content_type or "").lower().strip()
        content_type = raw_content_type.split(";", 1)[0].strip()
        file_ext = os.path.splitext((receipt_file.filename or "").lower())[1]
        if content_type not in _ALLOWED_PHOTO_TYPES and file_ext not in _ALLOWED_PHOTO_EXTENSIONS:
            return JSONResponse(status_code=400, content={"success": False, "error": "Unsupported image format"})

        content = await receipt_file.read()
        if not content:
            return JSONResponse(status_code=400, content={"success": False, "error": "Empty image file"})
        if len(content) > _photo_max_size_bytes():
            return JSONResponse(
                status_code=413,
                content={"success": False,
                         "error": f"Image is too large. Max size is {Config.MAX_PHOTO_FILE_SIZE_MB} MB"})

        mime = content_type if content_type in _ALLOWED_PHOTO_TYPES else "image/jpeg"

        try:
            raw_receipt = await asyncio.to_thread(analyze_receipt_image, content, mime)
        except Exception as e:
            logger.error(f"Receipt vision error: {e}", exc_info=True)
            raw_receipt = None

        if not raw_receipt:
            return JSONResponse(status_code=400,
                                content={"success": False, "error": RECEIPT_ERROR_MESSAGES[lang]})

        receipt = normalize_receipt(raw_receipt)
        receipt["items"] = categorize_items(receipt["items"])
        if not receipt["items"]:
            return JSONResponse(status_code=400,
                                content={"success": False, "error": RECEIPT_ERROR_MESSAGES[lang]})

        receipt_id = save_receipt(user_id, receipt)
        save_purchase_history(user_id, receipt_id, receipt)
        analytics = update_analytics(user_id, lang)

        logger.info(f"Receipt saved: id={receipt_id}, store={receipt['store']}, "
                    f"items={len(receipt['items'])}, total={receipt['total']}")

        return JSONResponse(content={
            "success": True,
            "receipt": {**localize_receipt(receipt, lang), "id": receipt_id},
            "analytics": analytics,
        })
    except Exception as e:
        logger.error(f"Receipt scan error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.get("/api/receipts/{user_id}")
async def get_receipts(user_id: int, lang: str = Query("")):
    """Receipt analytics + saved receipts for the Analytics/History pages,
    localized into the requested (or the user's stored) language."""
    try:
        language = lang if lang in ("ru", "uz") else db.get_user_language(user_id)
        receipts = [localize_receipt(r, language) for r in db.get_user_receipts(user_id)]
        return JSONResponse(content={
            "success": True,
            "analytics": update_analytics(user_id, language),
            "receipts": receipts,
        })
    except Exception as e:
        logger.error(f"Get receipts error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.get("/api/purchases/{user_id}")
async def get_purchases(user_id: int, limit: int = Query(200, ge=1, le=1000), lang: str = Query("")):
    """Per-item purchase history (foundation for repeat purchases / AI recommendations)."""
    try:
        language = lang if lang in ("ru", "uz") else db.get_user_language(user_id)
        return JSONResponse(content={
            "success": True,
            "purchases": [localize_receipt_item(p, language)
                          for p in db.get_purchase_history(user_id, limit=limit)],
        })
    except Exception as e:
        logger.error(f"Get purchases error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


# Receipt category -> display category of the shopping list (Russian keys;
# translated for uz users via translate_list_category).
RECEIPT_TO_LIST_CATEGORY = {
    "Овощи": "🥕 Овощи", "Фрукты": "🍎 Фрукты", "Молочные продукты": "🥛 Молочные продукты",
    "Мясо": "🍖 Мясные продукты", "Рыба и морепродукты": "🍖 Мясные продукты",
    "Бакалея": "📦 Бакалея", "Хлебобулочные изделия": "📦 Бакалея",
    "Напитки": "🥤 Напитки", "Сладости": "🍿 Снеки", "Масла": "📦 Бакалея",
    "Замороженные продукты": "📦 Бакалея", "Детские товары": "📝 Другое",
    "Бытовая химия": "🧴 Гигиена и быт", "Личная гигиена": "🧴 Гигиена и быт",
    "Аптека": "📝 Другое", "Для животных": "📝 Другое", "Канцелярия": "📝 Другое",
    "Электроника": "📝 Другое", "Другое": "📝 Другое",
}


@app.post("/api/receipt/{user_id}/reuse/{receipt_id}")
async def reuse_receipt(user_id: int, receipt_id: int):
    """Turn a scanned receipt back into an active shopping list, so the user
    can repeat the purchase, compare stores and order it again."""
    try:
        receipt = db.get_receipt(user_id, receipt_id)
        if not receipt:
            return JSONResponse(status_code=404, content={"success": False, "error": "Receipt not found"})

        lang = db.get_user_language(user_id)
        categories: Dict[str, List[Dict]] = {}
        for item in receipt.get("items", []):
            localized = localize_receipt_item(item, lang)
            display_category = translate_list_category(
                RECEIPT_TO_LIST_CATEGORY.get(item.get("category") or "Другое", "📝 Другое"), lang)
            quantity = localized.get("quantity") or 1
            quantity_text = f"{quantity:g} {localized.get('unit') or ''}".strip() \
                if isinstance(quantity, (int, float)) else str(quantity)
            categories.setdefault(display_category, []).append({
                "name": capitalize_first_letter(localized.get("name") or ""),
                "quantity": normalize_quantity_display(quantity_text, lang),
                "purchased": False,
                "original_name": capitalize_first_letter(localized.get("name") or ""),
                "user_specified_quantity": True,
            })

        if not categories:
            return JSONResponse(status_code=400, content={"success": False, "error": "Receipt has no items"})

        list_json = format_shopping_list_for_json(categories, user_id, lang,
                                                  original_text=receipt.get("store", ""))
        db.save_active_list(user_id, list_json)
        return JSONResponse(content={"success": True, "data": list_json})
    except Exception as e:
        logger.error(f"Reuse receipt error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/list/{user_id}/toggle")
async def toggle_purchase(user_id: int, request: TogglePurchaseRequest = Body(...)):
    try:
        list_data = db.get_active_list(user_id)
        if not list_data:
            return JSONResponse(status_code=404, content={"success": False, "error": "List not found"})

        list_data = toggle_item_purchased_in_list(list_data, request.category, request.item_name)
        db.save_active_list(user_id, list_data)

        response_data = {"success": True, "data": list_data, "all_purchased": list_data.get("all_purchased", False)}

        if list_data.get("all_purchased", False) and list_data.get("total_items", 0) > 0:
            response_data["show_confirmation"] = True

        return JSONResponse(content=response_data)
    except Exception as e:
        logger.error(f"Toggle error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/list/{user_id}/toggle-category")
async def toggle_category_purchase(user_id: int, request: ToggleCategoryRequest = Body(...)):
    try:
        list_data = db.get_active_list(user_id)
        if not list_data:
            return JSONResponse(status_code=404, content={"success": False, "error": "List not found"})

        list_data = toggle_category_purchased_in_list(list_data, request.category)
        db.save_active_list(user_id, list_data)

        response_data = {"success": True, "data": list_data, "all_purchased": list_data.get("all_purchased", False)}

        if list_data.get("all_purchased", False) and list_data.get("total_items", 0) > 0:
            response_data["show_confirmation"] = True

        return JSONResponse(content=response_data)
    except Exception as e:
        logger.error(f"Toggle category error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/list/{user_id}/item/edit")
async def edit_item(user_id: int, request: ItemEditRequest):
    try:
        list_data = db.get_active_list(user_id)
        if not list_data:
            return JSONResponse(status_code=404, content={"success": False, "error": "List not found"})

        lang = db.get_user_language(user_id)
        new_quantity = request.new_quantity
        if new_quantity:
            new_quantity = normalize_quantity_display(new_quantity, lang)

        list_data = update_item_in_list(list_data, request.category, request.old_item_name,
                                        capitalize_first_letter(request.new_item_name), new_quantity, lang)
        db.save_active_list(user_id, list_data)

        return JSONResponse(content={"success": True, "data": list_data, "message": "Item updated"})
    except Exception as e:
        logger.error(f"Edit item error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/list/{user_id}/edit")
async def edit_shopping_list(user_id: int, edit_request: EditRequest):
    try:
        list_data = db.get_active_list(user_id)
        if not list_data:
            return JSONResponse(status_code=404, content={"success": False, "error": "List not found"})

        changes = await detect_edit_changes(edit_request.text, edit_request.language)

        if not changes:
            return JSONResponse(content={"success": True, "message": "No changes detected", "changes": []})

        current_categories = list_data.get("categories", {})
        updated_categories = apply_edit_changes(current_categories, changes, edit_request.language)
        list_data["categories"] = updated_categories
        list_data = recalculate_list_prices(list_data, edit_request.language)
        list_data = recalculate_list_totals(list_data)
        db.save_active_list(user_id, list_data)

        return JSONResponse(content={"success": True, "changes": changes, "data": list_data, "message": "List updated"})
    except Exception as e:
        logger.error(f"Edit list error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/list/{user_id}/confirm")
async def confirm_purchase_completion(user_id: int, confirm_request: ExpenseConfirmRequest):
    try:
        list_data = db.get_active_list(user_id)
        if not list_data:
            return JSONResponse(status_code=404, content={"success": False, "error": "List not found"})

        confirmed = confirm_request.confirmed
        save_to_history = confirm_request.save_to_history

        if confirmed:
            if save_to_history and list_data and list_data.get("total_items", 0) > 0:
                db.add_history_entry(user_id, list_data)
            db.delete_active_list(user_id)
            return JSONResponse(content={"success": True, "message": "List completed", "completed": True})
        else:
            return JSONResponse(content={"success": True, "message": "Continue shopping", "completed": False})
    except Exception as e:
        logger.error(f"Confirm error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


# ===== "Я НА БАЗАРЕ" endpoints =====
@app.post("/api/bazaar/{user_id}/start")
async def bazaar_start(user_id: int):
    """Enter bazaar mode for the active list: snapshot the forecast total."""
    try:
        list_data = db.get_active_list(user_id)
        if not list_data or not list_data.get("total_items"):
            return JSONResponse(status_code=404, content={"success": False, "error": "List not found"})
        list_data = enable_bazaar_mode(list_data)
        db.save_active_list(user_id, list_data)
        return JSONResponse(content={"success": True, "data": list_data, "summary": bazaar_summary(list_data)})
    except Exception as e:
        logger.error(f"Bazaar start error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/bazaar/{user_id}/stop")
async def bazaar_stop(user_id: int):
    """Leave bazaar mode without finishing: the list and real prices stay."""
    try:
        list_data = db.get_active_list(user_id)
        if not list_data:
            return JSONResponse(status_code=404, content={"success": False, "error": "List not found"})
        list_data["bazaar_mode"] = False
        db.save_active_list(user_id, list_data)
        return JSONResponse(content={"success": True, "data": list_data, "summary": bazaar_summary(list_data)})
    except Exception as e:
        logger.error(f"Bazaar stop error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/bazaar/{user_id}/say")
async def bazaar_say(user_id: int, request: BazaarSayRequest):
    """Typed bazaar phrase: "Картошка 38", "Купил мясо за 120 тысяч", "Всё куплено"."""
    try:
        text = (request.text or "").strip()
        if not text:
            return JSONResponse(status_code=400, content={"success": False, "error": "Empty message"})
        status, payload = await process_bazaar_text(user_id, text, request.language)
        return JSONResponse(status_code=status, content=payload)
    except Exception as e:
        logger.error(f"Bazaar say error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/bazaar/{user_id}/voice")
async def bazaar_voice(
        user_id: int,
        language: str = Form("ru"),
        voice_file: UploadFile = File(...)
):
    """Dictated bazaar phrase: transcribe, then process like /say."""
    try:
        logger.info(f"Bazaar voice: user={user_id}, lang={language}")
        if language not in ["ru", "uz"]:
            return JSONResponse(status_code=400, content={"success": False, "error": "Unsupported language"})
        if language == "uz":
            return JSONResponse(content={
                "success": True,
                "type": "message",
                "message": VOICE_COMING_SOON_MESSAGE["uz"],
                "voice_unavailable": True,
            })
        text, error_response = await _read_and_transcribe_voice(voice_file, language)
        if error_response is not None:
            return error_response
        status, payload = await process_bazaar_text(user_id, text, language)
        payload["transcribed_text"] = text
        return JSONResponse(status_code=status, content=payload)
    except Exception as e:
        logger.error(f"Bazaar voice error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/bazaar/{user_id}/finish")
async def bazaar_finish(user_id: int):
    """Finish the bazaar trip: final plan-vs-actual report, save to history."""
    try:
        list_data = db.get_active_list(user_id)
        if not list_data:
            return JSONResponse(status_code=404, content={"success": False, "error": "List not found"})
        summary = bazaar_summary(list_data)
        list_data["bazaar"] = True
        list_data["bazaar_mode"] = False
        list_data["bazaar_finished_at"] = datetime.now().isoformat()
        list_data["bazaar_actual_total"] = summary["actual_total"]
        list_data["bazaar_savings"] = summary["savings"]
        # History and analytics must count the real spend, not the forecast.
        if summary["actual_total"]:
            list_data["total_estimated_price"] = summary["actual_total"]
        if list_data.get("total_items", 0) > 0:
            db.add_history_entry(user_id, list_data)
        db.delete_active_list(user_id)
        return JSONResponse(content={"success": True, "completed": True, "report": summary})
    except Exception as e:
        logger.error(f"Bazaar finish error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.delete("/api/list/{user_id}")
async def clear_shopping_list(user_id: int):
    try:
        db.delete_active_list(user_id)
        return JSONResponse(content={"success": True, "message": "List cleared"})
    except Exception as e:
        logger.error(f"Clear list error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.get("/api/history/{user_id}")
async def get_history(user_id: int):
    try:
        history = db.get_user_history(user_id)
        return JSONResponse(content={"success": True, "data": history})
    except Exception as e:
        logger.error(f"Get history error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/history/{user_id}/reuse/{list_id}")
async def reuse_history_list(user_id: int, list_id: str):
    try:
        history_item = db.get_history_item(user_id, list_id)
        if not history_item:
            return JSONResponse(status_code=404, content={"success": False, "error": "List not found"})

        categories = {}
        for category_name, items in history_item.get("categories", {}).items():
            categories[category_name] = []
            for item in items:
                categories[category_name].append({
                    "name": capitalize_first_letter(item["name"]),
                    "quantity": item.get("quantity", ""),
                    "purchased": False,
                    "estimated_price": item.get("estimated_price"),
                    "original_name": capitalize_first_letter(item["name"]),
                    "user_specified_quantity": bool(item.get("quantity"))
                })

        lang = db.get_user_language(user_id)
        list_json = format_shopping_list_for_json(categories, user_id, lang,
                                                  original_text=history_item.get("original_text", ""))

        db.save_active_list(user_id, list_json)

        return JSONResponse(content={"success": True, "data": list_json})
    except Exception as e:
        logger.error(f"Reuse history error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.delete("/api/history/{user_id}/clear")
async def clear_history(user_id: int):
    try:
        success = db.clear_user_history(user_id)
        if success:
            return JSONResponse(content={"success": True, "message": "History cleared"})
        return JSONResponse(status_code=404, content={"success": False, "error": "History not found"})
    except Exception as e:
        logger.error(f"Clear history error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


# ===== MONTHLY BUDGET (Analytics) =====

def validate_budget_amount(value) -> Optional[float]:
    """Normalize a monthly budget amount (sums). Returns the rounded amount,
    0 to clear the budget, or None when the input is not a sane number."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount != amount or amount in (float("inf"), float("-inf")):
        return None
    if amount < 0:
        return None
    if amount > 10_000_000_000:  # больше 10 млрд сум в месяц — явно опечатка
        return None
    return float(round(amount))


@app.get("/api/budget/{user_id}")
async def get_budget(user_id: int):
    try:
        return JSONResponse(content={"success": True, "budget": db.get_user_budget(user_id)})
    except Exception as e:
        logger.error(f"Get budget error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/budget/{user_id}")
async def set_budget(user_id: int, request: BudgetRequest):
    try:
        amount = validate_budget_amount(request.amount)
        if amount is None:
            return JSONResponse(status_code=400, content={"success": False, "error": "Invalid budget amount"})
        db.set_user_budget(user_id, amount)
        return JSONResponse(content={"success": True, "budget": amount})
    except Exception as e:
        logger.error(f"Set budget error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


# ===== Bozorlik AI Pro: подписка и триал =====
PRO_TRIAL_DAYS = 7          # бесплатный Pro для новых пользователей
PRO_PRICE_MONTHLY = 19990   # сум/мес — цена подписки (менять здесь)
PRO_PAID_PERIOD_DAYS = 30   # длительность оплаченного периода
PRO_SERVICE_FEE = 2490      # сум — сервисный сбор при доставке; 0 только у ОПЛАЧЕННОЙ подписки (триал платит)

# ===== Telegram Payments: оплата подписки (Global Pay UZ / Payme / Click) =====
# Провайдер-токены из BotFather; :TEST: — тестовый режим, деньги не списываются.
# ВАЖНО: токен работает только у того бота, к которому провайдер подключён в BotFather.
# У Payme/Click тестовый доступ закрыт — токены появятся после договора (env).
PAYMENT_PROVIDER_TOKENS = {
    "globalpay": os.getenv("GLOBALPAY_PROVIDER_TOKEN", "1650291590:TEST:1784015561319_EfBiRs3pJFA7NTPC"),
    "payme": os.getenv("PAYME_PROVIDER_TOKEN", ""),
    "click": os.getenv("CLICK_PROVIDER_TOKEN", ""),
}
PAYMENTS_TEST_MODE = any(":TEST:" in (t or "") for t in PAYMENT_PROVIDER_TOKENS.values())
# Если задан, POST /api/pro/{id}/subscribe принимается только с этим ключом в
# X-Internal-Key (его шлёт бот после successful_payment). Пустой = проверка выключена.
PRO_INTERNAL_KEY = os.getenv("BOZORLIK_INTERNAL_KEY", "")

# ===== Веб-оплата (без Telegram Payments): Payme Merchant API + Click SHOP API =====
# Payme: касса создаётся в merchant.payme.uz (прод) или в песочнице test.paycom.uz;
# в настройках кассы указывается endpoint /api/payments/payme и поле счёта order_id.
PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "")
PAYME_KEY = os.getenv("PAYME_KEY", "")            # боевой ключ кассы
PAYME_TEST_KEY = os.getenv("PAYME_TEST_KEY", "")  # тестовый ключ кассы (вкладка «Для разработчиков»)
PAYME_TEST_MODE = os.getenv("PAYME_TEST_MODE", "1") == "1"  # 1 → checkout.test.paycom.uz
# Click: креды выдаются после регистрации мерчанта (merchant.click.uz);
# колбэки — /api/payments/click/prepare и /api/payments/click/complete.
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "")
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "")
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "")
CLICK_RETURN_URL = os.getenv("CLICK_RETURN_URL", "https://bozorlikai.uz")
PAYME_TXN_TIMEOUT_MS = 12 * 3600 * 1000  # по протоколу Payme транзакция живёт 12 часов
# Какой поток использует кнопка «Оформить подписку»:
#   telegram — Telegram Payments (нужны провайдер-токены BotFather ТОГО ЖЕ бота)
#   web      — платёжная страница Payme/Click (нужны креды кассы/мерчанта)
# По умолчанию telegram: у него есть рабочий тестовый провайдер (Global Pay UZ),
# а веб-креды Payme/Click появятся только после договоров.
PAY_FLOW = os.getenv("PAY_FLOW", "telegram").strip().lower()


def _payme_now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def build_payme_checkout_url(merchant_id: str, order_id: int, amount_sum: int,
                             lang: str = "ru", test_mode: bool = True) -> str:
    """Ссылка на платёжную страницу Payme: base64 от 'm=..;ac.order_id=..;a=..'
    (a — сумма в тийинах)."""
    params = f"m={merchant_id};ac.order_id={order_id};a={amount_sum * 100};l={lang}"
    encoded = base64.b64encode(params.encode()).decode()
    host = "checkout.test.paycom.uz" if test_mode else "checkout.paycom.uz"
    return f"https://{host}/{encoded}"


def build_click_checkout_url(service_id: str, merchant_id: str, amount_sum: int,
                             order_id: int, return_url: str = "") -> str:
    url = (f"https://my.click.uz/services/pay?service_id={service_id}"
           f"&merchant_id={merchant_id}&amount={amount_sum}&transaction_param={order_id}")
    if return_url:
        url += f"&return_url={quote_plus(return_url)}"
    return url


def click_signature(params: Dict[str, Any], secret_key: str) -> str:
    """Подпись запроса Click SHOP API. Для action=1 (complete) в строку входит
    merchant_prepare_id; суммы и id участвуют строками как пришли в запросе."""
    import hashlib
    parts = [str(params.get("click_trans_id", "")), str(params.get("service_id", "")), secret_key,
             str(params.get("merchant_trans_id", ""))]
    if str(params.get("action", "")) == "1":
        parts.append(str(params.get("merchant_prepare_id", "")))
    parts += [str(params.get("amount", "")), str(params.get("action", "")), str(params.get("sign_time", ""))]
    return hashlib.md5("".join(parts).encode()).hexdigest()


def _payme_error(request_id, code: int, message_ru: str, data: Optional[str] = None) -> JSONResponse:
    error: Dict[str, Any] = {"code": code, "message": {"ru": message_ru, "uz": message_ru, "en": message_ru}}
    if data:
        error["data"] = data
    return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "error": error})


def _payme_result(request_id, result: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": result})


def _payme_auth_ok(auth_header: str) -> bool:
    """Basic-авторизация Payme: login всегда 'Paycom', пароль — ключ кассы
    (принимаем боевой и тестовый, чтобы песочница работала параллельно с продом)."""
    valid_keys = [k for k in (PAYME_KEY, PAYME_TEST_KEY) if k]
    if not valid_keys or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode()
    except Exception:
        return False
    return any(decoded == f"Paycom:{key}" for key in valid_keys)


def compute_pro_status(row: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Derive subscription status from a raw user_pro row.

    plan in the result: 'none' | 'trial' | 'trial_expired' | 'paid' | 'expired'.
    The service fee is waived only while a PAID subscription is active —
    trial users still pay it (это осознанное продуктовое решение)."""
    now = now or datetime.now()
    plan, is_pro, days_left = 'none', False, 0
    trial_ends = row.get('trial_ends_at') if row else None
    paid_until = row.get('paid_until') if row else None

    if row:
        raw_plan = row.get('plan') or 'none'
        if raw_plan == 'none' and row.get('is_pro'):
            raw_plan = 'paid'  # legacy manual activation, no expiry
        if raw_plan == 'paid':
            if paid_until is None or paid_until > now:
                plan, is_pro = 'paid', True
            else:
                plan = 'expired'
        elif raw_plan == 'trial':
            if trial_ends and trial_ends > now:
                plan, is_pro = 'trial', True
                seconds = int((trial_ends - now).total_seconds())
                days_left = max(1, (seconds + 86399) // 86400)
            else:
                plan = 'trial_expired'

    return {
        "is_pro": is_pro,
        "plan": plan,
        "days_left": days_left,
        "trial_ends_at": trial_ends.isoformat() if trial_ends else None,
        "paid_until": paid_until.isoformat() if paid_until else None,
        "service_fee": 0 if plan == 'paid' else PRO_SERVICE_FEE,
        "price": PRO_PRICE_MONTHLY,
        "trial_days": PRO_TRIAL_DAYS,
    }


def get_pro_status_for(user_id: int, start_trial_if_new: bool = False) -> Dict[str, Any]:
    row = db.get_pro_row(user_id)
    if row is None and start_trial_if_new:
        db.ensure_trial(user_id, PRO_TRIAL_DAYS)
        row = db.get_pro_row(user_id)
    return compute_pro_status(row)


def pro_status_response(user_id: int, start_trial_if_new: bool = False) -> Dict[str, Any]:
    """Полный ответ для фронта: статус подписки + доступные способы оплаты.
    pay_* нужны везде, где фронт целиком заменяет State.proStatus."""
    status = get_pro_status_for(user_id, start_trial_if_new)
    status["pay_flow"] = PAY_FLOW if PAY_FLOW in ("web", "telegram") else "telegram"
    if status["pay_flow"] == "telegram":
        # показываем только провайдеров с реальными токенами — без мёртвых кнопок
        status["pay_providers"] = [name for name, token in PAYMENT_PROVIDER_TOKENS.items() if token]
        status["pay_test"] = PAYMENTS_TEST_MODE
    else:
        status["pay_providers"] = ["payme", "click"]
        status["pay_test"] = PAYME_TEST_MODE
    return status


@app.get("/api/pro/{user_id}")
async def get_pro_status(user_id: int):
    """Subscription status; the first call from a new user starts the trial."""
    try:
        return JSONResponse(content={"success": True, **pro_status_response(user_id, start_trial_if_new=True)})
    except Exception as e:
        logger.error(f"Get pro status error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/pro/{user_id}/invoice")
async def create_pro_invoice(user_id: int, request: ProInvoiceRequest = Body(...)):
    """Счёт на оплату подписки через Telegram Payments (Payme/Click).
    Возвращает invoice-ссылку для Telegram.WebApp.openInvoice()."""
    try:
        provider = (request.provider or "").lower()
        if provider not in PAYMENT_PROVIDER_TOKENS:
            return JSONResponse(status_code=400, content={"success": False, "error": f"Unknown provider: {provider}"})
        token = PAYMENT_PROVIDER_TOKENS[provider]
        if not token:
            return JSONResponse(status_code=503, content={
                "success": False, "error": f"Провайдер {provider} не настроен: нет токена в env"})
        if not Config.TELEGRAM_BOT_TOKEN:
            return JSONResponse(status_code=503,
                                content={"success": False, "error": "TELEGRAM_BOT_TOKEN is not configured"})

        lang = db.get_user_language(user_id)
        ru = lang == "ru"
        title = "Bozorlik AI Pro — 30 дней" if ru else "Bozorlik AI Pro — 30 kun"
        description = ("Семейный список, «Я на базаре», сканирование чеков, фото списка, "
                       "AI-расчёт блюд и доставка без сервисного сбора" if ru else
                       "Oilaviy ro'yxat, «Men bozordaman», chek skaneri, ro'yxat fotosi, "
                       "AI taom hisobi va servis yig'imisiz yetkazib berish")

        invoice = {
            "title": title,
            "description": description,
            "payload": f"pro:{user_id}:{provider}",
            "provider_token": token,
            "currency": "UZS",
            # Telegram принимает суммы в минимальных единицах валюты (тийины)
            "prices": [{"label": title, "amount": PRO_PRICE_MONTHLY * 100}],
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/createInvoiceLink",
                json=invoice,
            ) as resp:
                data = await resp.json()

        if not data.get("ok"):
            logger.error(f"createInvoiceLink failed for user={user_id}, provider={provider}: {data}")
            return JSONResponse(status_code=502,
                                content={"success": False,
                                         "error": data.get("description") or "Telegram invoice error"})

        logger.info(f"Invoice created: user={user_id}, provider={provider}, test={':TEST:' in token}")
        return JSONResponse(content={
            "success": True,
            "invoice_url": data["result"],
            "provider": provider,
            "test_mode": ":TEST:" in token,
        })
    except Exception as e:
        logger.error(f"Create invoice error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/pro/{user_id}/checkout")
async def create_pro_checkout(user_id: int, request: ProInvoiceRequest = Body(...)):
    """Веб-оплата подписки: создаёт заказ и возвращает ссылку на платёжную
    страницу Payme/Click. Активация происходит в колбэках провайдера."""
    try:
        provider = (request.provider or "").lower()
        lang = db.get_user_language(user_id)
        if provider == "payme":
            if not PAYME_MERCHANT_ID:
                return JSONResponse(status_code=503, content={
                    "success": False, "error": "Payme не настроен: задайте PAYME_MERCHANT_ID и PAYME_TEST_KEY в .env"})
            order = db.create_payment_order(user_id, "payme", PRO_PRICE_MONTHLY)
            url = build_payme_checkout_url(PAYME_MERCHANT_ID, order["id"], PRO_PRICE_MONTHLY,
                                           lang, PAYME_TEST_MODE)
            test_mode = PAYME_TEST_MODE
        elif provider == "click":
            if not (CLICK_SERVICE_ID and CLICK_MERCHANT_ID):
                return JSONResponse(status_code=503, content={
                    "success": False, "error": "Click не настроен: задайте CLICK_SERVICE_ID/CLICK_MERCHANT_ID/CLICK_SECRET_KEY в .env"})
            order = db.create_payment_order(user_id, "click", PRO_PRICE_MONTHLY)
            url = build_click_checkout_url(CLICK_SERVICE_ID, CLICK_MERCHANT_ID, PRO_PRICE_MONTHLY,
                                           order["id"], CLICK_RETURN_URL)
            test_mode = False
        else:
            return JSONResponse(status_code=400, content={"success": False, "error": f"Unknown provider: {provider}"})

        logger.info(f"Checkout created: order={order['id']}, user={user_id}, provider={provider}, "
                    f"amount={PRO_PRICE_MONTHLY}")
        return JSONResponse(content={"success": True, "checkout_url": url, "order_id": order["id"],
                                     "amount": PRO_PRICE_MONTHLY, "provider": provider, "test_mode": test_mode})
    except Exception as e:
        logger.error(f"Create checkout error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


# ===== Payme Merchant API (JSON-RPC 2.0) =====
# Этот endpoint указывается в настройках кассы Payme; Payme сам вызывает его
# при оплате. Протокол: CheckPerform → Create → Perform (+Cancel/Check/Statement).

def _payme_find_order(params: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    account = params.get("account") or {}
    try:
        order_id = int(str(account.get("order_id", "")).strip())
    except (TypeError, ValueError):
        return None, None
    return db.get_payment_order(order_id), order_id


def _payme_check_perform(request_id, params):
    order, _ = _payme_find_order(params)
    if not order:
        return _payme_error(request_id, -31050, "Заказ не найден")
    if order["state"] == "paid":
        return _payme_error(request_id, -31051, "Заказ уже оплачен")
    if order["state"] == "cancelled":
        return _payme_error(request_id, -31051, "Заказ отменён")
    if params.get("amount") != order["amount"] * 100:
        return _payme_error(request_id, -31001, "Неверная сумма")
    return _payme_result(request_id, {"allow": True})


def _payme_create_transaction(request_id, params):
    txn_id = str(params.get("id", ""))
    existing = db.get_order_by_payme_txn(txn_id) if txn_id else None
    if existing:
        # повторный CreateTransaction той же транзакции — идемпотентный ответ
        if existing["payme_state"] != 1:
            return _payme_error(request_id, -31008, "Транзакция уже завершена или отменена")
        if _payme_now_ms() - (existing["payme_create_time"] or 0) > PAYME_TXN_TIMEOUT_MS:
            db.update_payment_order(existing["id"], payme_state=-1, payme_reason=4,
                                    payme_cancel_time=_payme_now_ms(), state="cancelled")
            return _payme_error(request_id, -31008, "Транзакция просрочена")
        return _payme_result(request_id, {"create_time": existing["payme_create_time"],
                                          "transaction": str(existing["id"]), "state": 1})

    order, _ = _payme_find_order(params)
    if not order:
        return _payme_error(request_id, -31050, "Заказ не найден")
    if order["state"] != "pending":
        return _payme_error(request_id, -31051, "Заказ уже оплачен или отменён")
    if params.get("amount") != order["amount"] * 100:
        return _payme_error(request_id, -31001, "Неверная сумма")
    if order["payme_txn_id"] and order["payme_state"] == 1:
        return _payme_error(request_id, -31008, "Заказ уже оплачивается другой транзакцией")

    create_time = _payme_now_ms()
    db.update_payment_order(order["id"], provider="payme", payme_txn_id=txn_id,
                            payme_state=1, payme_create_time=create_time)
    return _payme_result(request_id, {"create_time": create_time,
                                      "transaction": str(order["id"]), "state": 1})


def _payme_perform_transaction(request_id, params):
    order = db.get_order_by_payme_txn(str(params.get("id", "")))
    if not order:
        return _payme_error(request_id, -31003, "Транзакция не найдена")
    if order["payme_state"] == 2:  # идемпотентность
        return _payme_result(request_id, {"transaction": str(order["id"]),
                                          "perform_time": order["payme_perform_time"], "state": 2})
    if order["payme_state"] != 1:
        return _payme_error(request_id, -31008, "Транзакция отменена")
    if _payme_now_ms() - (order["payme_create_time"] or 0) > PAYME_TXN_TIMEOUT_MS:
        db.update_payment_order(order["id"], payme_state=-1, payme_reason=4,
                                payme_cancel_time=_payme_now_ms(), state="cancelled")
        return _payme_error(request_id, -31008, "Транзакция просрочена")

    perform_time = _payme_now_ms()
    db.update_payment_order(order["id"], payme_state=2, payme_perform_time=perform_time, state="paid")
    db.start_paid_subscription(order["user_id"], PRO_PAID_PERIOD_DAYS)
    logger.info(f"Payme payment performed: order={order['id']}, user={order['user_id']}, "
                f"amount={order['amount']} — Pro activated")
    return _payme_result(request_id, {"transaction": str(order["id"]),
                                      "perform_time": perform_time, "state": 2})


def _payme_cancel_transaction(request_id, params):
    order = db.get_order_by_payme_txn(str(params.get("id", "")))
    if not order:
        return _payme_error(request_id, -31003, "Транзакция не найдена")
    if order["payme_state"] in (-1, -2):  # идемпотентность
        return _payme_result(request_id, {"transaction": str(order["id"]),
                                          "cancel_time": order["payme_cancel_time"],
                                          "state": order["payme_state"]})
    new_state = -2 if order["payme_state"] == 2 else -1
    cancel_time = _payme_now_ms()
    db.update_payment_order(order["id"], payme_state=new_state, payme_cancel_time=cancel_time,
                            payme_reason=params.get("reason"), state="cancelled")
    if new_state == -2:
        # возврат после проведения: подписку не откатываем автоматически —
        # решение о даунгрейде принимается вручную (см. логи)
        logger.warning(f"Payme REFUND: order={order['id']}, user={order['user_id']} — "
                       f"subscription left active, handle manually")
    return _payme_result(request_id, {"transaction": str(order["id"]),
                                      "cancel_time": cancel_time, "state": new_state})


def _payme_check_transaction(request_id, params):
    order = db.get_order_by_payme_txn(str(params.get("id", "")))
    if not order:
        return _payme_error(request_id, -31003, "Транзакция не найдена")
    return _payme_result(request_id, {
        "create_time": order["payme_create_time"] or 0,
        "perform_time": order["payme_perform_time"] or 0,
        "cancel_time": order["payme_cancel_time"] or 0,
        "transaction": str(order["id"]),
        "state": order["payme_state"],
        "reason": order["payme_reason"],
    })


def _payme_get_statement(request_id, params):
    rows = db.list_payme_transactions(int(params.get("from", 0)), int(params.get("to", 0)))
    return _payme_result(request_id, {"transactions": [{
        "id": row["payme_txn_id"],
        "time": row["payme_create_time"] or 0,
        "amount": row["amount"] * 100,
        "account": {"order_id": str(row["id"])},
        "create_time": row["payme_create_time"] or 0,
        "perform_time": row["payme_perform_time"] or 0,
        "cancel_time": row["payme_cancel_time"] or 0,
        "transaction": str(row["id"]),
        "state": row["payme_state"],
        "reason": row["payme_reason"],
    } for row in rows]})


_PAYME_METHODS = {
    "CheckPerformTransaction": _payme_check_perform,
    "CreateTransaction": _payme_create_transaction,
    "PerformTransaction": _payme_perform_transaction,
    "CancelTransaction": _payme_cancel_transaction,
    "CheckTransaction": _payme_check_transaction,
    "GetStatement": _payme_get_statement,
}


@app.post("/api/payments/payme")
async def payme_merchant_api(request: Request):
    request_id = None
    try:
        try:
            body = await request.json()
        except Exception:
            return _payme_error(None, -32700, "Ошибка парсинга JSON")
        request_id = body.get("id")
        if not _payme_auth_ok(request.headers.get("Authorization", "")):
            return _payme_error(request_id, -32504, "Недостаточно привилегий")
        method = body.get("method", "")
        handler = _PAYME_METHODS.get(method)
        if not handler:
            return _payme_error(request_id, -32601, f"Метод не найден: {method}")
        return handler(request_id, body.get("params") or {})
    except Exception as e:
        logger.error(f"Payme API error: {e}", exc_info=True)
        return _payme_error(request_id, -32400, "Внутренняя ошибка сервера")


# ===== Click SHOP API (prepare / complete) =====

def _click_response(params: Dict[str, Any], error: int, note: str, **extra) -> JSONResponse:
    return JSONResponse(content={
        "click_trans_id": params.get("click_trans_id"),
        "merchant_trans_id": params.get("merchant_trans_id"),
        "error": error, "error_note": note, **extra,
    })


def _click_get_order(params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        return db.get_payment_order(int(str(params.get("merchant_trans_id", "")).strip()))
    except (TypeError, ValueError):
        return None


@app.post("/api/payments/click/prepare")
async def click_prepare(request: Request):
    try:
        params = dict((await request.form()).items())
        if not CLICK_SECRET_KEY or params.get("sign_string") != click_signature(params, CLICK_SECRET_KEY):
            return _click_response(params, -1, "SIGN CHECK FAILED")
        order = _click_get_order(params)
        if not order:
            return _click_response(params, -5, "Order not found")
        if order["state"] == "paid":
            return _click_response(params, -4, "Already paid")
        if order["state"] == "cancelled":
            return _click_response(params, -9, "Order cancelled")
        try:
            amount_ok = abs(float(params.get("amount", 0)) - order["amount"]) < 0.01
        except (TypeError, ValueError):
            amount_ok = False
        if not amount_ok:
            return _click_response(params, -2, "Incorrect amount")
        db.update_payment_order(order["id"], provider="click",
                                click_trans_id=str(params.get("click_trans_id", "")))
        return _click_response(params, 0, "Success", merchant_prepare_id=order["id"])
    except Exception as e:
        logger.error(f"Click prepare error: {e}", exc_info=True)
        return JSONResponse(content={"error": -8, "error_note": "Internal error"})


@app.post("/api/payments/click/complete")
async def click_complete(request: Request):
    try:
        params = dict((await request.form()).items())
        if not CLICK_SECRET_KEY or params.get("sign_string") != click_signature(params, CLICK_SECRET_KEY):
            return _click_response(params, -1, "SIGN CHECK FAILED")
        order = _click_get_order(params)
        if not order:
            return _click_response(params, -5, "Order not found")
        if str(params.get("merchant_prepare_id", "")) != str(order["id"]):
            return _click_response(params, -6, "Transaction does not exist")
        try:
            click_error = int(params.get("error", 0))
        except (TypeError, ValueError):
            click_error = 0
        if click_error < 0:  # Click сообщает об отмене/ошибке платежа
            db.update_payment_order(order["id"], state="cancelled")
            return _click_response(params, -9, "Transaction cancelled")
        if order["state"] == "paid":
            return _click_response(params, -4, "Already paid")
        if order["state"] == "cancelled":
            return _click_response(params, -9, "Order cancelled")
        try:
            amount_ok = abs(float(params.get("amount", 0)) - order["amount"]) < 0.01
        except (TypeError, ValueError):
            amount_ok = False
        if not amount_ok:
            return _click_response(params, -2, "Incorrect amount")

        db.update_payment_order(order["id"], state="paid")
        db.start_paid_subscription(order["user_id"], PRO_PAID_PERIOD_DAYS)
        logger.info(f"Click payment completed: order={order['id']}, user={order['user_id']}, "
                    f"amount={order['amount']} — Pro activated")
        return _click_response(params, 0, "Success", merchant_confirm_id=order["id"])
    except Exception as e:
        logger.error(f"Click complete error: {e}", exc_info=True)
        return JSONResponse(content={"error": -8, "error_note": "Internal error"})


@app.post("/api/pro/{user_id}/subscribe")
async def subscribe_pro(user_id: int, request: Request):
    """Активация оплаченного периода. В боевом потоке сюда приходит ТОЛЬКО бот
    после successful_payment от Telegram (с X-Internal-Key, если ключ задан)."""
    try:
        if PRO_INTERNAL_KEY and request.headers.get("X-Internal-Key") != PRO_INTERNAL_KEY:
            return JSONResponse(status_code=403, content={"success": False, "error": "Forbidden"})
        db.start_paid_subscription(user_id, PRO_PAID_PERIOD_DAYS)
        logger.info(f"Pro subscription activated: user={user_id}, days={PRO_PAID_PERIOD_DAYS}")
        return JSONResponse(content={"success": True, **pro_status_response(user_id)})
    except Exception as e:
        logger.error(f"Subscribe error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/pro/{user_id}")
async def set_pro_status(user_id: int, request: ProStatusRequest = Body(...)):
    # Ручное управление (dev/admin): True → paid без срока, False → отмена подписки.
    try:
        db.set_user_pro(user_id, request.is_pro)
        return JSONResponse(content={"success": True, **pro_status_response(user_id)})
    except Exception as e:
        logger.error(f"Set pro status error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/share")
async def share_list(share_request: ShareRequest, request: Request):
    try:
        user_id = share_request.user_id
        list_id = share_request.list_id

        logger.info(f"Share request: user={user_id}, list_id={list_id}")

        list_data = db.get_active_list(user_id)
        if not list_data or list_data.get("total_items", 0) == 0:
            return JSONResponse(status_code=400, content={"success": False, "error": "Cannot share empty list"})

        lang = db.get_user_language(user_id)
        is_pro = get_pro_status_for(user_id)["is_pro"]
        shared_record = shared_list_service.create_shared_snapshot(list_data, user_id, lang, live=is_pro)
        share_token = shared_record["token"]

        if is_pro:
            # Stamp the owner's active list so the frontend knows this list is
            # family-synced and starts polling for the family's checkmarks.
            list_data["live_share_token"] = share_token
            db.save_active_list(user_id, list_data)
        # Keep raw share URL for web fallback, but prefer telegram deep link as primary share_url
        encoded_token = quote_plus(share_token)
        # Telegram deep link for mini app using query param startapp
        telegram_share_url = f"https://t.me/BozorlikAI_bot?startapp={encoded_token}"
        # Use telegram deep link as primary share_url (no web share links)
        share_url = telegram_share_url

        share_text_ru = f"🔗 Вот ваша ссылка на список покупок:\n\n{share_url}\n\nОткройте в Telegram: {telegram_share_url}\n\nПоделитесь ей с друзьями!"
        share_text_uz = f"🔗 Mana sizning xaridlar ro'yxati havolasi:\n\n{share_url}\n\nTelegramda ochish: {telegram_share_url}\n\nDo'stlaringizga ulashing!"

        share_text = share_text_ru if lang == "ru" else share_text_uz

        logger.info(f"Created shared list: token={share_token}, owner={user_id}")

        logger.info(f"Created shared list: token={share_token}, owner={user_id}")

        return JSONResponse(content={
            "success": True,
            "share_url": share_url,
            "telegram_share_url": telegram_share_url,
            "list_id": shared_record.get("list_data", {}).get("list_id", list_id),
            "owner_id": user_id,
            "share_token": share_token,
            "share_text": share_text,
            "live": is_pro,
            "message": "List shared successfully"
        })
    except Exception as e:
        logger.error(f"Share error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.get("/shared/{token}")
async def shared_page(token: str, request: Request):
    # For compatibility, redirect any web access to the Telegram deep link (no web share usage)
    encoded = quote_plus(token)
    telegram_link = f"https://t.me/BozorlikAI_bot?startapp={encoded}"
    return HTMLResponse(
        content=f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="0;url={telegram_link}">
    <script>window.location.href = '{telegram_link}';</script>
</head>
<body></body>
</html>'''
    )


@app.get("/api/shared/{token}")
async def get_shared_list(token: str, user_id: Optional[int] = Query(None)):
    try:
        logger.info(f"Get shared list: token={token}, user_id={user_id}")

        shared = shared_list_service.get_shared_snapshot(token)

        if not shared:
            return JSONResponse(status_code=404,
                                content={"success": False, "error": "Shared list not found or expired"})

        lang = "ru"
        if user_id:
            lang = db.get_user_language(user_id)
        elif shared.get("lang"):
            lang = shared["lang"]

        payload = shared["list_data"]
        live = False
        if payload.get("live_sync"):
            # Pro family sync: serve the owner's live list while it is still the
            # same list; once the owner starts a new one, degrade to the snapshot.
            owner_list = db.get_active_list(shared.get("owner_id"))
            if owner_list and owner_list.get("list_id") == payload.get("source_list_id"):
                payload = owner_list
                live = True

        response_data = copy.deepcopy(payload)
        response_data["localization"] = LOCALIZATION[lang]
        response_data["is_shared"] = True
        response_data["shared_list_id"] = token
        response_data["owner_id"] = shared.get("owner_id")

        return JSONResponse(content={
            "success": True,
            "data": response_data,
            "owner_id": shared.get("owner_id"),
            "live": live
        })
    except Exception as e:
        logger.error(f"Get shared list error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/shared/{token}/toggle")
async def toggle_shared_purchase(token: str, request: SharedToggleRequest = Body(...)):
    """Family sync (Pro): a share-link viewer checks items off the owner's live list."""
    try:
        shared = shared_list_service.get_shared_snapshot(token)
        if not shared:
            return JSONResponse(status_code=404,
                                content={"success": False, "error": "Shared list not found or expired"})

        payload = shared["list_data"]
        if not payload.get("live_sync"):
            return JSONResponse(status_code=400,
                                content={"success": False, "error": "Live sync is not enabled for this list"})

        owner_id = shared.get("owner_id")
        owner_list = db.get_active_list(owner_id)
        if not owner_list or owner_list.get("list_id") != payload.get("source_list_id"):
            return JSONResponse(status_code=409,
                                content={"success": False, "code": "list_gone",
                                         "error": "The shared list is no longer active"})

        if request.item_name:
            owner_list = toggle_item_purchased_in_list(owner_list, request.category, request.item_name)
        else:
            owner_list = toggle_category_purchased_in_list(owner_list, request.category)
        db.save_active_list(owner_id, owner_list)

        response_data = copy.deepcopy(owner_list)
        response_data["is_shared"] = True
        response_data["shared_list_id"] = token
        response_data["owner_id"] = owner_id

        return JSONResponse(content={"success": True, "data": response_data, "live": True})
    except Exception as e:
        logger.error(f"Shared toggle error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/shared/add-to-my-list")
async def add_shared_list_to_my_list(request: AddSharedListRequest):
    try:
        user_id = request.user_id
        shared_list_id = request.shared_list_id
        lang = request.language

        logger.info(f"Add shared list to my list: user={user_id}, shared_id={shared_list_id}")

        shared = shared_list_service.get_shared_snapshot(shared_list_id)
        if not shared:
            return JSONResponse(status_code=404,
                                content={"success": False, "error": "Shared list not found or expired"})

        shared_list_data = shared["list_data"]

        # Build categories from shared list data (reset purchased status)
        categories = {}
        for category_name, items in shared_list_data.get("categories", {}).items():
            categories[category_name] = []
            for item in items:
                categories[category_name].append({
                    "name": item["name"],
                    "quantity": item.get("quantity", ""),
                    "purchased": False,  # Reset purchased status for the recipient
                    "estimated_price": item.get("estimated_price"),
                    "original_name": item.get("original_name", item["name"]),
                    "user_specified_quantity": item.get("user_specified_quantity", False)
                })

        # Create or merge with existing active list
        list_data = db.get_active_list(user_id)
        if list_data:
            # Merge with existing active list
            list_data = merge_categories_into_list(list_data, categories, lang)
        else:
            # Create new active list for recipient
            list_data = format_shopping_list_for_json(categories, user_id, lang)
            list_data["from_shared"] = shared_list_id

        db.save_active_list(user_id, list_data)

        logger.info(f"Successfully added shared list {shared_list_id} to user {user_id}")

        return JSONResponse(content={
            "success": True,
            "data": list_data,
            "message": "List added successfully"
        })
    except Exception as e:
        logger.error(f"Add shared list error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.get("/api/prices/search")
async def search_prices(query: str = Query(...), lang: str = Query("ru")):
    try:
        results = []
        found_products = price_db.find_products(query, lang)

        for product in found_products[:10]:
            results.append({
                "id": product.get("id"),
                "name_ru": product.get("name_ru"),
                "name_uz": product.get("name_uz"),
                "category_ru": product.get("category_ru"),
                "category_uz": product.get("category_uz"),
                "price": product.get("price", 0),
                "quantity": product.get("quantity", ""),
                "unit": product.get("unit", "")
            })

        return JSONResponse(content={"success": True, "results": results})
    except Exception as e:
        logger.error(f"Search prices error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.post("/api/set-language")
async def set_language(request: SetLanguageRequest):
    try:
        if request.language not in ["ru", "uz"]:
            return JSONResponse(status_code=400, content={"success": False, "error": "Unsupported language"})

        old_language = db.get_user_language(request.user_id)
        db.set_user_language(request.user_id, request.language)

        # Switching the interface language must also switch stored content:
        # the active bazaar list, saved history lists and scanned receipt
        # items are rewritten/backfilled into the new language.
        translated = False
        if old_language != request.language:
            try:
                translated = await asyncio.to_thread(
                    _translate_user_content, request.user_id, old_language, request.language)
            except Exception as e:
                logger.error(f"Language content translation error: {e}", exc_info=True)

        return JSONResponse(content={"success": True, "translated": translated,
                                     "message": f"Language set to {request.language}"})
    except Exception as e:
        logger.error(f"Set language error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await ws_manager.connect(user_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await ws_manager.send_personal_message(user_id, {"type": "pong"})
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(user_id)


# ===== RUN =====
if __name__ == "__main__":
    import uvicorn

    is_dev = Config.ENV == "development"
    # uvicorn's reload/workers require an import string ("module:app"), not the app
    # object. Pass the string form when reloading; otherwise pass the object directly.
    if is_dev:
        uvicorn.run("app:app", host="0.0.0.0", port=Config.PORT, reload=True)
    else:
        uvicorn.run(app, host="0.0.0.0", port=Config.PORT)