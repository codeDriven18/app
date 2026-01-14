import os
import json
import logging
import asyncio
import secrets
import string
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query, Body, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import httpx
import openai
import aiohttp

# ===== ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ =====
load_dotenv()

# ===== КОНФИГУРАЦИЯ =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AISHA_API_KEY = os.getenv("AISHA_API_KEY")

AISHA_POST_URL = os.getenv("AISHA_POST_URL", "https://back.aisha.group/api/v2/stt/post/")
AISHA_GET_URL = os.getenv("AISHA_GET_URL", "https://back.aisha.group/api/v2/stt/get/")

# Файлы данных
LANGUAGES_FILE = "user_languages.json"
EXPENSES_FILE = "shopping_expenses.json"
SHARED_LISTS_FILE = "shared_lists.json"
USER_HISTORY_FILE = "user_history.json"
PRICES_FILE = "prices.json"

# ===== ИНИЦИАЛИЗАЦИЯ OpenAI =====
openai.api_key = OPENAI_API_KEY
client = openai.OpenAI(api_key=OPENAI_API_KEY)

# ===== ЛОГИРОВАНИЕ =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ===== МОДЕЛИ ДАННЫХ =====
class ChatMessage(BaseModel):
    user_id: int
    text: str
    language: str = "ru"
    is_voice: bool = False


class ShoppingListRequest(BaseModel):
    user_id: int
    text: str
    language: str = "ru"


class ShareRequest(BaseModel):
    user_id: int
    list_id: str


class EditRequest(BaseModel):
    user_id: int
    text: str
    language: str = "ru"


class VoiceRequest(BaseModel):
    user_id: int
    language: str = "ru"


class ExpenseRequest(BaseModel):
    user_id: int
    amount: float
    currency: str = "UZS"
    list_id: Optional[str] = None
    date: Optional[str] = None


# ===== УЛУЧШЕННЫЙ КЛАСС ДЛЯ ЦЕН =====
class EnhancedPriceDatabase:
    def __init__(self):
        self.data = None
        self.items = {}
        self.synonyms_ru = {}
        self.synonyms_uz = {}
        self.brand_mapping = {}
        self.load_data()

    def load_data(self):
        try:
            with open(PRICES_FILE, "r", encoding="utf-8") as f:
                self.data = json.load(f)
                self.items = self.data.get("items", {})

                if "synonyms" in self.data:
                    self.synonyms_ru = self.data["synonyms"].get("ru", {})
                    self.synonyms_uz = self.data["synonyms"].get("uz", {})

                logger.info(
                    f"Загружено {len(self.items)} продуктов и {len(self.synonyms_ru) + len(self.synonyms_uz)} синонимов")
        except Exception as e:
            logger.error(f"Ошибка загрузки данных о ценах: {e}")
            self.data = None

    def find_product_by_name(self, product_name: str, lang: str = "ru") -> Tuple[Optional[Dict], Optional[str]]:
        """Найти продукт по названию с учетом синонимов и брендов"""
        product_lower = product_name.lower().strip()

        # 1. Прямое совпадение с ID продукта
        for product_id, product_info in self.items.items():
            if product_id.lower() == product_lower:
                return product_info, product_id

        # 2. Поиск через синонимы
        synonyms_dict = self.synonyms_ru if lang == "ru" else self.synonyms_uz
        for synonym, product_id in synonyms_dict.items():
            if synonym.lower() == product_lower:
                if product_id in self.items:
                    return self.items[product_id], product_id

        # 3. Частичное совпадение с синонимами
        for synonym, product_id in synonyms_dict.items():
            if product_lower in synonym.lower() or synonym.lower() in product_lower:
                if product_id in self.items:
                    return self.items[product_id], product_id

        # 4. Поиск в названиях товаров
        for product_id, product_info in self.items.items():
            display_name = product_info.get("display_name", "").lower()
            if product_lower in display_name or display_name in product_lower:
                return product_info, product_id

        # 5. Поиск в цитатах (бренды)
        for product_id, product_info in self.items.items():
            if "quotes" in product_info:
                for quote in product_info["quotes"]:
                    if "source" in quote:
                        source_lower = quote["source"].lower()
                        if product_lower in source_lower or any(word in product_lower for word in source_lower.split()):
                            return product_info, product_id

        return None, None

    def get_price_for_product(self, product_name: str, quantity: str = "", lang: str = "ru") -> Optional[Dict]:
        """Получить цену для продукта с учетом количества"""
        product_info, product_id = self.find_product_by_name(product_name, lang)

        if not product_info or "quotes" not in product_info:
            return None

        quotes = product_info["quotes"]
        if not quotes:
            return None

        # Извлекаем все цены
        prices = []
        for quote in quotes:
            if "price" in quote:
                prices.append(quote["price"])

        if not prices:
            return None

        # Рассчитываем среднюю цену
        avg_price = int(sum(prices) / len(prices))

        # Извлекаем количество
        quantity_num = self.extract_quantity_number(quantity)

        # Рассчитываем оценочную цену
        estimated_price = int(avg_price * quantity_num)

        return {
            "product_id": product_id,
            "product_name": product_name,
            "price": avg_price,
            "estimated_price": estimated_price,
            "unit": product_info.get("unit", ""),
            "quantity": quantity_num,
            "available": True,
            "source": "Средняя цена"
        }

    @staticmethod
    def extract_quantity_number(quantity_str: str) -> float:
        """Извлекает числовое значение из строки количества"""
        if not quantity_str:
            return 1.0

        try:
            # Ищем числа в строке
            import re
            numbers = re.findall(r'\d+\.?\d*', quantity_str)
            if numbers:
                return float(numbers[0])

            quantity_lower = quantity_str.lower()

            # Русские слова для количества
            if "пол" in quantity_lower or "0.5" in quantity_lower or "половина" in quantity_lower:
                return 0.5
            elif "четверть" in quantity_lower or "0.25" in quantity_lower or "четвертин" in quantity_lower:
                return 0.25
            elif "три четверти" in quantity_lower or "0.75" in quantity_lower:
                return 0.75

            # Узбекские слова для количества
            if "yarim" in quantity_lower or "ярим" in quantity_lower:
                return 0.5
            elif "chorak" in quantity_lower or "чорак" in quantity_lower:
                return 0.25

        except Exception:
            pass

        return 1.0


# ===== УЛУЧШЕННЫЕ СИСТЕМНЫЕ ПРОМПТЫ =====
SYSTEM_PROMPTS = {
    "ru": """
You are Bozorlik AI — an assistant that ONLY creates grocery shopping lists.
You MUST always respond in Russian.

GENERAL RULES:
1) Always respond in Russian.
2) You ONLY help with grocery shopping lists.
3) If the user asks anything unrelated to groceries (math, homework, theory questions), answer:
   "Извините, я могу помочь только со списком покупок."
4) If the user greets you ("привет", "салам", "здравствуйте"), reply:
   "Привет! Что нужно купить сегодня?"

IMPORTANT PRICE RULE:
For EVERY product that has price information, ALWAYS add the estimated price in format:
• Название — количество (≈цена сум)

If price is not available, just show product without price.

CATEGORY FORMAT RULES:
• NEVER write the word "Категория".
• The format MUST be:

     🥕 Овощи:
     • Лук — 1 кг (≈2,800 сум)
     • Морковь — 2 кг (≈9,000 сум)

     🥛 Молочные продукты:
     • Молоко — 1 литр (≈18,500 сум)

• Only category name + emoji + colon.
• Use ONLY bullet points (•) for items.

CATEGORY RULES:
• Create ONLY categories that contain items.
• Never create empty categories.
• Allowed categories (use ONLY these):
     🥕 Овощи
     🍎 Фрукты
     🥛 Молочные продукты
     🍖 Мясо и рыба
     📦 Бакалея
     🥤 Напитки
     🧴 Химия и быт
     📝 Другое

SPECIAL INSTRUCTION:
When you see product names like "картошка", "лук", "молоко нестле", "боржоми", "кола", "сникерс", "порошок" etc.
ALWAYS show them with their estimated prices from our database.

FINAL RULES:
• NO explanations.
• NO English in answers.
• NO commentary.
• ONLY the formatted grocery list OR the short greeting/refusal message.
• ALWAYS add estimated prices for products that have price information.

Process the user input:
""",

    "uz": """
You are Bozorlik AI — an assistant that ONLY creates grocery shopping lists.
You MUST always respond in Uzbek.

GENERAL RULES:
1) Always respond in Uzbek.
2) You ONLY help with grocery shopping lists.
3) If the user asks anything unrelated to groceries (math, homework, theory questions), answer:
   "Kechirasiz, men faqat xaridlar ro'yxati bilan yordam bera olaman."
4) If the user greets you ("salom", "assalomu alaykum", "hello"), reply:
   "Salom! Bugun nima xarid qilish kerak?"

IMPORTANT PRICE RULE:
For EVERY product that has price information, ALWAYS add the estimated price in format:
• Nomi — miqdori (≈narx so'm)

If price is not available, just show product without price.

CATEGORY FORMAT RULES:
• NEVER write the word "Kategoriya".
• The format MUST be:

     🥕 Sabzavotlar:
     • Piyoz — 1 kg (≈2,800 so'm)
     • Sabzi — 2 kg (≈9,000 so'm)

     🥛 Sut mahsulotlari:
     • Sut — 1 litr (≈18,500 so'm)

• Only category name + emoji + colon.
• Use ONLY bullet points (•) for items.

CATEGORY RULES:
• Create ONLY categories that contain items.
• Never create empty categories.
• Allowed categories (use ONLY these):
     🥕 Sabzavotlar
     🍎 Mevalar
     🥛 Sut mahsulotlari
     🍖 Go'sht va baliq
     📦 Boshqa mahsulotlar
     🥤 Ichimliklar
     🧴 Kimyoviy mahsulotlar
     📝 Boshqalar

SPECIAL INSTRUCTION:
When you see product names like "kartoshka", "piyoz", "sut nestle", "borjomi", "kola", "snickers", "kukun" etc.
ALWAYS show them with their estimated prices from our database.

FINAL RULES:
• NO explanations.
• NO English in answers.
• NO commentary.
• ONLY the formatted grocery list OR the short greeting/refusal message.
• ALWAYS add estimated prices for products that have price information.

Process the user input:
"""
}

SYSTEM_PROMPT_EDIT = {
    "ru": """
Ты — AI помощник для редактирования списка покупок. Твоя задача — понять, что пользователь хочет изменить в существующем списке.

ДОСТУПНЫЕ ДЕЙСТВИЯ:
1. Добавить продукт
2. Удалить продукт
3. Заменить продукт
4. Изменить количество

ПРАВИЛА:
1. Отвечай ТОЛЬКО в формате JSON
2. Формат ответа: {
   "changes": [{
     "action": "add/remove/replace/update",
     "target": "название продукта",
     "new_item": "новый продукт (если add/replace)",
     "quantity": "количество (если указано)",
     "category": "категория (если можно определить)"
   }]
}
3. Если не можешь определить изменения, возвращай: {"changes": []}
4. Распознавай команды:
   - "добавь", "добавить", "хочу добавить", "нужно добавить", "еще" → action: "add"
   - "удали", "убрать", "убери", "не нужно", "не надо", "вычеркни", "убери" → action: "remove" 
   - "замени", "измени", "поменяй", "изменить на", "вместо" → action: "replace"
   - "больше", "меньше", "измени количество", "количество" → action: "update"
5. Распознавай продукты и количества

Определи изменения из сообщения:
""",

    "uz": """
Siz — xaridlar ro'yxatini tahrirlash uchun AI yordamchisi. Vazifangiz — foydalanuvchi mavjud ro'yxatda nima o'zgartirishni xohlayotganini tushunish.

MAVJUD HARAKATLAR:
1. Mahsulot qo'shish
2. Mahsulot o'chirish
3. Mahsulot almashtirish
4. Miqdorni o'zgartirish

QOIDALAR:
1. Faqat JSON formatida javob bering
2. Javob formati: {
   "changes": [{
     "action": "add/remove/replace/update",
     "target": "mahsulot nomi",
     "new_item": "yangi mahsulot (agar add/replace bo'lsa)",
     "quantity": "miqdor (agar ko'rsatilgan bo'lsa)",
     "category": "kategoriya (agar aniqlasa bo'lsa)"
   }]
}
3. Agar o'zgarishlarni aniqlay olmasangiz, qaytaring: {"changes": []}
4. Buyruqlarni tanib oling:
   - "qo'sh", "qo'shing", "qo'shmoqchiman", "qo'shish kerak", "yana" → action: "add"
   - "o'chir", "olib tashla", "kerak emas", "yo'q", "chiqarib tashla" → action: "remove" 
   - "almashtir", "o'zgartir", "almash", "o'zgartiring", "o'rniga" → action: "replace"
   - "ko'proq", "kamroq", "miqdorni o'zgartir", "miqdor" → action: "update"
5. Mahsulotlar va miqdorlarni tanib oling

Foydalanuvchi xabaridan o'zgarishlarni aniqlang:
"""
}

# ===== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =====
price_db = EnhancedPriceDatabase()
user_data: Dict[int, Dict] = {}
user_languages: Dict[int, str] = {}
shared_lists: Dict[str, Dict] = {}
user_history: Dict[int, List] = {}
websocket_connections: Dict[int, WebSocket] = {}


# ===== ФУНКЦИИ ДЛЯ АНАЛИТИКИ =====
def load_user_history():
    """Загружает историю пользователей из файла"""
    if os.path.exists(USER_HISTORY_FILE):
        try:
            with open(USER_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    user_history[int(k)] = v
            logger.info(f"Загружено {len(user_history)} историй пользователей")
        except Exception as e:
            logger.error(f"Error loading user history: {e}")


def save_user_history():
    """Сохраняет историю пользователей в файл"""
    try:
        with open(USER_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in user_history.items()},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving user history: {e}")


def add_to_user_history(user_id: int, list_data: Dict, final_amount: Optional[float] = None):
    """Добавляет список в историю пользователя"""
    if user_id not in user_history:
        user_history[user_id] = []

    # Преобразуем список в читаемый формат
    items_list = []
    for category, items in list_data.get("categories", {}).items():
        for item in items:
            items_list.append({
                "name": item.get("name", ""),
                "quantity": item.get("quantity", ""),
                "purchased": item.get("purchased", False)
            })

    history_entry = {
        "list_id": list_data.get("list_id", secrets.token_hex(8)),
        "date": datetime.now().isoformat(),
        "items_count": list_data.get("total_items", 0),
        "estimated_price": list_data.get("total_estimated_price", 0),
        "final_amount": final_amount,
        "items": items_list,
        "purchased_items": list_data.get("purchased_items", 0),
        "categories": {k: len(v) for k, v in list_data.get("categories", {}).items()}
    }

    user_history[user_id].append(history_entry)

    # Сохраняем только последние 100 записей
    if len(user_history[user_id]) > 100:
        user_history[user_id] = user_history[user_id][-100:]

    save_user_history()


def get_user_analytics(user_id: int) -> Dict:
    """Получает аналитику пользователя"""
    if user_id not in user_history or not user_history[user_id]:
        return {
            "total_lists": 0,
            "total_spent": 0,
            "average_spent": 0,
            "min_spent": 0,
            "max_spent": 0,
            "min_date": None,
            "max_date": None,
            "min_list": None,
            "max_list": None,
            "history": [],
            "category_breakdown": {},
            "monthly_trend": {}
        }

    history = user_history[user_id]

    # Рассчитываем статистику по тратам
    spent_entries = [h for h in history if h.get("final_amount") is not None]

    total_spent = sum(h["final_amount"] for h in spent_entries if h.get("final_amount") is not None)

    min_spent_entry = None
    max_spent_entry = None

    if spent_entries:
        min_spent_entry = min(spent_entries, key=lambda x: x["final_amount"])
        max_spent_entry = max(spent_entries, key=lambda x: x["final_amount"])

    # Анализ по категориям
    category_breakdown = {}
    for entry in history:
        if "categories" in entry:
            for category, count in entry["categories"].items():
                if category not in category_breakdown:
                    category_breakdown[category] = 0
                category_breakdown[category] += count

    # Месячный тренд
    monthly_trend = {}
    for entry in history:
        date = datetime.fromisoformat(entry["date"])
        month_key = date.strftime("%Y-%m")
        if month_key not in monthly_trend:
            monthly_trend[month_key] = {"count": 0, "spent": 0}
        monthly_trend[month_key]["count"] += 1
        if entry.get("final_amount") is not None:
            monthly_trend[month_key]["spent"] += entry["final_amount"]

    return {
        "total_lists": len(history),
        "total_spent": total_spent,
        "average_spent": total_spent / len(spent_entries) if spent_entries else 0,
        "min_spent": min_spent_entry["final_amount"] if min_spent_entry else 0,
        "max_spent": max_spent_entry["final_amount"] if max_spent_entry else 0,
        "min_date": min_spent_entry["date"] if min_spent_entry else None,
        "max_date": max_spent_entry["date"] if max_spent_entry else None,
        "min_list": min_spent_entry if min_spent_entry else None,
        "max_list": max_spent_entry if max_spent_entry else None,
        "history": history[-20:],  # Последние 20 записей
        "category_breakdown": category_breakdown,
        "monthly_trend": monthly_trend
    }


def get_expense_history(user_id: int) -> List[Dict]:
    """Получает историю расходов пользователя"""
    if user_id not in user_history:
        return []

    history = user_history[user_id]
    expense_history = []

    for entry in history:
        if entry.get("final_amount") is not None:
            expense_history.append({
                "date": entry["date"],
                "amount": entry["final_amount"],
                "items_count": entry.get("items_count", 0),
                "list_id": entry.get("list_id")
            })

    return expense_history


def get_list_details(user_id: int, list_id: str) -> Optional[Dict]:
    """Получает детали конкретного списка"""
    if user_id not in user_history:
        return None

    for entry in user_history[user_id]:
        if entry.get("list_id") == list_id:
            return entry

    return None


# ===== ФУНКЦИЯ ДЛЯ ПАРСИНГА СУММ =====
def parse_amount_from_text(text: str) -> float:
    """Парсит сумму из текста с учетом форматов: 10к, 10кк, 10 тысяч и т.д."""
    if not text:
        return 0.0

    # Убираем пробелы и приводим к нижнему регистру
    clean_text = text.lower().replace(' ', '')

    # Пытаемся извлечь число и множитель
    try:
        # Парсим 10кк (10 миллионов)
        if 'кк' in clean_text:
            num_part = clean_text.replace('кк', '')
            num = float(num_part) if '.' in num_part else int(num_part)
            return num * 1000000

        # Парсим 10к (10 тысяч)
        if 'к' in clean_text:
            num_part = clean_text.replace('к', '')
            num = float(num_part) if '.' in num_part else int(num_part)
            return num * 1000

        # Парсим русские текстовые форматы
        if 'миллион' in clean_text:
            # Извлекаем число перед "миллион"
            match = re.search(r'([\d.,]+)\s*миллион', text.lower())
            if match:
                num_str = match.group(1).replace(',', '.')
                num = float(num_str) if '.' in num_str else int(num_str)
                return num * 1000000

        if 'тысяч' in clean_text:
            # Извлекаем число перед "тысяч"
            match = re.search(r'([\d.,]+)\s*тысяч', text.lower())
            if match:
                num_str = match.group(1).replace(',', '.')
                num = float(num_str) if '.' in num_str else int(num_str)
                return num * 1000

        # Парсим узбекские текстовые форматы
        if 'million' in clean_text:
            match = re.search(r'([\d.,]+)\s*million', text.lower())
            if match:
                num_str = match.group(1).replace(',', '.')
                num = float(num_str) if '.' in num_str else int(num_str)
                return num * 1000000

        if 'ming' in clean_text:
            match = re.search(r'([\d.,]+)\s*ming', text.lower())
            if match:
                num_str = match.group(1).replace(',', '.')
                num = float(num_str) if '.' in num_str else int(num_str)
                return num * 1000

        # Стандартный парсинг чисел
        # Извлекаем все цифры и точки/запятые
        numbers = re.findall(r'[\d.,]+', clean_text)
        if numbers:
            # Берем первое найденное число
            num_str = numbers[0].replace(',', '.')
            # Если есть точка, это float
            if '.' in num_str:
                return float(num_str)
            else:
                return int(num_str)

        return 0.0

    except Exception as e:
        logger.error(f"Error parsing amount from text '{text}': {e}")
        return 0.0


# ===== ОСНОВНЫЕ ФУНКЦИИ =====
async def format_list_with_gpt(text: str, lang: str = "ru") -> str:
    """Форматирует список покупок с помощью GPT с интеграцией цен"""
    try:
        # Добавляем инструкцию по ценам в промпт
        enhanced_prompt = SYSTEM_PROMPTS[lang]

        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": enhanced_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=1000
        )
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Error formatting list with GPT: {e}")
        if lang == "ru":
            return "Извините, произошла ошибка при обработке запроса."
        else:
            return "Kechirasiz, so'rovni qayta ishlashda xatolik yuz berdi."


async def detect_edit_changes(text: str, lang: str = "ru") -> List[Dict]:
    """Определяет изменения для редактирования списка"""
    try:
        edit_lang = lang
        if any(word in text.lower() for word in ["qo'sh", "o'chir", "almashtir", "mahsulot"]):
            edit_lang = "uz"
        elif any(word in text.lower() for word in ["добавь", "удали", "замени", "продукт"]):
            edit_lang = "ru"

        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_EDIT[edit_lang]},
                {"role": "user", "content": text},
            ],
            temperature=0.1
        )

        response = completion.choices[0].message.content
        data = json.loads(response)
        return data.get("changes", [])
    except Exception as e:
        logger.error(f"Error detecting edit changes: {e}")
        return []


async def transcribe_voice_uzbek(file_path: str) -> str:
    """Транскрибирует голосовое сообщение на узбекском через Aisha STT"""
    if not AISHA_API_KEY:
        return None

    record_id = await aisha_stt_start(file_path, "uz")
    if not record_id:
        return None

    for attempt in range(30):
        result = await aisha_stt_result(record_id)
        if not result:
            await asyncio.sleep(1)
            continue

        status = str(result.get("status", "")).lower()

        candidate_text = (
                result.get("text") or
                result.get("result") or
                result.get("transcription") or
                result.get("transcript")
        )

        if candidate_text and candidate_text.strip():
            return candidate_text.strip()

        if status in ("success", "succeeded", "done", "finished", "completed", "ready"):
            break

        if status in ("failed", "error"):
            break

        await asyncio.sleep(1)

    return None


async def transcribe_voice_ru(file_path: str) -> str:
    """Транскрибирует голосовое сообщение на русском через Whisper-1"""
    try:
        with open(file_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru"
            )
        return transcript.text
    except Exception as e:
        logger.error(f"Error transcribing voice with Whisper: {e}")
        return None


async def transcribe_voice(file_path: str, lang: str = "ru") -> str:
    """Транскрибирует голосовое сообщение в зависимости от языка"""
    if lang == "uz":
        return await transcribe_voice_uzbek(file_path)
    else:
        return await transcribe_voice_ru(file_path)


async def aisha_stt_start(file_path: str, lang: str = "uz"):
    """Отправляет аудио на распознавание в Aisha STT"""
    if not AISHA_API_KEY:
        logger.error("Aisha API key is not set")
        return None

    headers = {"x-api-key": AISHA_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as audio_file:
                form = aiohttp.FormData()
                form.add_field(
                    "audio",
                    audio_file,
                    filename=os.path.basename(file_path),
                    content_type="audio/ogg",
                )
                form.add_field("language", lang)

                async with session.post(AISHA_POST_URL, headers=headers, data=form) as resp:
                    if resp.status != 200:
                        logger.error(f"Aisha STT start failed with status: {resp.status}")
                        return None

                    try:
                        data = await resp.json()
                    except Exception as e:
                        logger.error(f"Error parsing Aisha STT response: {e}")
                        return None

                    return data.get("id")
    except Exception as e:
        logger.error(f"Error in aisha_stt_start: {e}")
        return None


async def aisha_stt_result(record_id: int):
    """Получает результат распознавания от Aisha STT"""
    if not AISHA_API_KEY:
        logger.error("Aisha API key is not set")
        return None

    url = AISHA_GET_URL + f"{record_id}/"
    headers = {"x-api-key": AISHA_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"Aisha STT result failed with status: {resp.status}")
                    return None

                try:
                    return await resp.json()
                except Exception as e:
                    logger.error(f"Error parsing Aisha STT result: {e}")
                    return None
    except Exception as e:
        logger.error(f"Error in aisha_stt_result: {e}")
        return None


def parse_shopping_list(text: str, lang: str = "ru") -> Dict[str, List[Dict]]:
    """Парсит отформатированный список покупок на категории и товары с ценами"""
    categories = {}
    current_category = None

    lines = text.split('\n')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Определяем категорию по эмодзи
        emojis = ["🥕", "🍎", "🥛", "🍖", "📦", "🥤", "🧴", "📝"]
        category_found = False

        for emoji in emojis:
            if line.startswith(emoji) and (':' in line or line.endswith(':')):
                current_category = line.split(':')[0].strip()
                categories[current_category] = []
                category_found = True
                break

        if category_found:
            continue

        # Если это товар (начинается с •)
        if line.startswith('•') and current_category:
            # Убираем маркер списка
            item_text = line[1:].strip()

            # Разделяем название и остальное
            if '—' in item_text:
                parts = item_text.split('—', 1)
                product_name = parts[0].strip()
                rest = parts[1].strip()
            elif '(' in item_text:
                # Если есть цена в скобках
                product_name = item_text.split('(')[0].strip()
                rest = item_text.split('(')[1].replace(')', '').strip()
            else:
                product_name = item_text
                rest = ""

            # Извлекаем количество и цену
            quantity = ""
            estimated_price = None

            # Ищем цену в формате "≈цена сум"
            price_match = re.search(r'≈([\d\s,]+)\s*(сум|so\'m)', rest)
            if price_match:
                price_str = price_match.group(1).replace(' ', '').replace(',', '')
                if price_str.isdigit():
                    estimated_price = int(price_str)
                    # Убираем цену из строки с количеством
                    rest = rest[:price_match.start()].strip()

            # Остальное - это количество
            if rest:
                quantity = rest

            # Получаем информацию о цене из базы данных
            price_info = price_db.get_price_for_product(product_name, quantity, lang)

            # Если не нашли цену в тексте, но нашли в базе данных
            if not estimated_price and price_info and price_info.get("estimated_price"):
                estimated_price = price_info.get("estimated_price")

            item_data = {
                "name": product_name,
                "quantity": quantity,
                "purchased": False,
                "price_info": price_info,
                "estimated_price": estimated_price
            }

            categories[current_category].append(item_data)

    return categories


def format_shopping_list_for_json(categories: Dict[str, List[Dict]]) -> Dict:
    """Форматирует список покупок для JSON ответа"""
    result = {
        "categories": {},
        "items": [],
        "total_items": 0,
        "purchased_items": 0,
        "total_estimated_price": 0,
        "list_id": secrets.token_hex(8),
        "created_at": datetime.now().isoformat(),
        "all_purchased": False
    }

    for category, items in categories.items():
        result["categories"][category] = []
        result["total_items"] += len(items)

        purchased_count = sum(1 for item in items if item.get("purchased", False))
        result["purchased_items"] += purchased_count

        for item in items:
            item_data = {
                "name": item["name"],
                "quantity": item["quantity"],
                "purchased": item.get("purchased", False),
                "category": category,
                "price_info": item.get("price_info"),
                "estimated_price": item.get("estimated_price")
            }

            if item.get("estimated_price"):
                result["total_estimated_price"] += item["estimated_price"]

            result["categories"][category].append(item_data)
            result["items"].append(item_data)

    # Проверяем, все ли товары куплены
    if result["total_items"] > 0 and result["purchased_items"] == result["total_items"]:
        result["all_purchased"] = True

    return result


def apply_edit_changes(categories: Dict[str, List[Dict]], changes: List[Dict], lang: str = "ru") -> Dict[
    str, List[Dict]]:
    """Применяет изменения к списку покупок"""
    updated_categories = {k: v.copy() for k, v in categories.items()}

    for change in changes:
        action = change.get("action")
        target = change.get("target", "").strip()
        new_item = change.get("new_item", "").strip()
        quantity = change.get("quantity", "").strip()
        category = change.get("category", "").strip()

        if not target and action != "add":
            continue

        if action == "remove":
            # Удаляем товар из всех категорий
            for cat_name, items in list(updated_categories.items()):
                updated_items = []
                for item in items:
                    if target.lower() not in item["name"].lower():
                        updated_items.append(item)

                if updated_items:
                    updated_categories[cat_name] = updated_items
                else:
                    del updated_categories[cat_name]

        elif action == "add":
            # Определяем категорию для нового товара
            target_category = category
            if not target_category:
                # Пытаемся определить категорию по названию товара
                for cat_name in updated_categories.keys():
                    if any(keyword in new_item.lower() for keyword in get_category_keywords(cat_name, lang)):
                        target_category = cat_name
                        break

            if not target_category:
                target_category = "📝 Другое" if lang == "ru" else "📝 Boshqalar"

            # Создаем категорию, если её нет
            if target_category not in updated_categories:
                updated_categories[target_category] = []

            # Получаем информацию о цене
            price_info = price_db.get_price_for_product(new_item, quantity, lang)

            # Добавляем товар
            updated_categories[target_category].append({
                "name": new_item,
                "quantity": quantity,
                "purchased": False,
                "price_info": price_info,
                "estimated_price": price_info.get("estimated_price") if price_info else None
            })

        elif action == "replace":
            # Заменяем товар
            for cat_name, items in updated_categories.items():
                for i, item in enumerate(items):
                    if target.lower() in item["name"].lower():
                        price_info = price_db.get_price_for_product(new_item, quantity, lang)
                        updated_categories[cat_name][i] = {
                            "name": new_item,
                            "quantity": quantity,
                            "purchased": False,
                            "price_info": price_info,
                            "estimated_price": price_info.get("estimated_price") if price_info else None
                        }
                        break

        elif action == "update":
            # Обновляем количество
            for cat_name, items in updated_categories.items():
                for item in items:
                    if target.lower() in item["name"].lower():
                        item["quantity"] = quantity
                        # Пересчитываем цену
                        if item.get("price_info"):
                            price_info = price_db.get_price_for_product(item["name"], quantity, lang)
                            item["price_info"] = price_info
                            item["estimated_price"] = price_info.get("estimated_price") if price_info else None

    # Удаляем пустые категории
    updated_categories = {k: v for k, v in updated_categories.items() if v}

    return updated_categories


def get_category_keywords(category_name: str, lang: str = "ru") -> List[str]:
    """Возвращает ключевые слова для категории"""
    keywords = {
        "🥕 Овощи": ["овощ", "карто", "лук", "морков", "помидор", "огур", "капуст", "сабзавот"],
        "🥕 Sabzavotlar": ["sabzavot", "kartoshka", "piyoz", "sabzi", "pomidor", "bodring", "karam"],
        "🍎 Фрукты": ["фрукт", "яблок", "банан", "апельсин", "мандарин", "виноград", "мева"],
        "🍎 Mevalar": ["meva", "olma", "banan", "apelsin", "mandarin", "uzum"],
        "🥛 Молочные продукты": ["молок", "сыр", "творог", "йогурт", "кефир", "сметан", "масло", "сут"],
        "🥛 Sut mahsulotlari": ["sut", "pishloq", "tvorog", "yogurt", "qatiq", "qaymoq", "yog'"],
        "🍖 Мясо и рыба": ["мяс", "говядин", "куриц", "рыб", "колбас", "сосиск", "филе", "go'sht", "baliq"],
        "🍖 Go'sht va baliq": ["go'sht", "mol", "tovuq", "baliq", "kolbasa", "sosiska", "file"],
        "📦 Бакалея": ["макарон", "рис", "гречк", "мука", "сахар", "соль", "масло растительн", "консерв"],
        "📦 Boshqa mahsulotlar": ["makaron", "guruch", "grechka", "un", "shakar", "tuz", "yog'", "konserva"],
        "🥤 Напитки": ["напиток", "вода", "сок", "чай", "кофе", "лимонад", "газировк", "ichimlik"],
        "🥤 Ichimliklar": ["ichimlik", "suv", "sharbat", "choy", "qahva", "limonad", "gazli"],
        "🧴 Химия и быт": ["мыло", "шампунь", "порошок", "паста", "гель", "салфетк", "бумаг", "kukun", "shampun"],
        "🧴 Kimyoviy mahsulotlar": ["sovun", "shampun", "kukun", "pasta", "gel", "salfetka", "qog'oz"],
        "📝 Другое": [],
        "📝 Boshqalar": []
    }

    return keywords.get(category_name, [])


# ===== ЗАГРУЗКА И СОХРАНЕНИЕ ДАННЫХ =====
def load_languages():
    if os.path.exists(LANGUAGES_FILE):
        try:
            with open(LANGUAGES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    user_languages[int(k)] = v
            logger.info(f"Загружено {len(user_languages)} языков пользователей")
        except Exception as e:
            logger.error(f"Error loading languages: {e}")


def save_languages():
    try:
        with open(LANGUAGES_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in user_languages.items()},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving languages: {e}")


def load_shared_lists():
    if os.path.exists(SHARED_LISTS_FILE):
        try:
            with open(SHARED_LISTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for list_id, list_data in data.items():
                    shared_lists[list_id] = list_data
            logger.info(f"Загружено {len(shared_lists)} общих списков")
        except Exception as e:
            logger.error(f"Error loading shared lists: {e}")


def save_shared_lists():
    try:
        with open(SHARED_LISTS_FILE, "w", encoding="utf-8") as f:
            json.dump(shared_lists, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving shared lists: {e}")


# ===== LIFESPAN MANAGER =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for FastAPI"""
    # Startup
    logger.info("Запуск Bozorlik AI Web Backend...")
    load_languages()
    load_shared_lists()
    load_user_history()
    logger.info("Bozorlik AI Web Backend успешно запущен")
    yield
    # Shutdown
    logger.info("Завершение работы Bozorlik AI Web Backend...")
    save_languages()
    save_shared_lists()
    save_user_history()
    logger.info("Данные сохранены")


# ===== ИНИЦИАЛИЗАЦИЯ APP =====
app = FastAPI(
    title="Bozorlik AI Web Backend",
    description="Web интерфейс для Telegram бота Bozorlik AI",
    version="2.2.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== API ENDPOINTS =====
@app.get("/")
async def root():
    return JSONResponse(content={
        "status": "online",
        "service": "Bozorlik AI",
        "version": "2.2.0",
        "features": [
            "Умные списки покупок",
            "Точные цены из базы данных",
            "Голосовой ввод (рус/узб)",
            "Редактирование списков",
            "Расширенная аналитика",
            "История покупок",
            "Inline редактирование в списке"
        ]
    })


@app.get("/health")
async def health_check():
    return JSONResponse(content={
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "users_count": len(user_languages),
        "shared_lists_count": len(shared_lists),
        "price_db_loaded": price_db.data is not None,
        "synonyms_loaded": len(price_db.synonyms_ru) > 0 and len(price_db.synonyms_uz) > 0,
        "openai_available": True
    })


@app.post("/api/chat")
async def chat_message(chat_request: ChatMessage):
    """Обработка текстовых сообщений"""
    try:
        user_id = chat_request.user_id
        text = chat_request.text.strip()
        lang = chat_request.language

        logger.info(f"Получено сообщение от пользователя {user_id}: {text[:100]}...")

        if not text:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Пустое сообщение"}
            )

        # Устанавливаем язык пользователя
        if user_id not in user_languages:
            user_languages[user_id] = lang
            save_languages()

        # Обрабатываем сообщение
        response_text = await format_list_with_gpt(text, lang)

        # Если это список покупок, парсим его
        if any(emoji in response_text for emoji in ["🥕", "🍎", "🥛", "🍖", "📦", "🥤", "🧴", "📝"]):
            categories = parse_shopping_list(response_text, lang)
            list_json = format_shopping_list_for_json(categories)

            # Сохраняем в user_data
            user_data[user_id] = {
                "categories": categories,
                "list_data": list_json,
                "last_message": text,
                "last_response": response_text,
                "created_at": datetime.now().isoformat(),
                "is_shared": False,
            }

            return JSONResponse(content={
                "success": True,
                "type": "shopping_list",
                "message": response_text,
                "data": list_json
            })
        else:
            return JSONResponse(content={
                "success": True,
                "type": "message",
                "message": response_text
            })

    except Exception as e:
        logger.error(f"Ошибка в chat_message: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error",
                "details": str(e) if "dev" in os.environ.get("ENVIRONMENT", "") else None
            }
        )


@app.post("/api/voice")
async def voice_message(
        user_id: int = Form(...),
        language: str = Form("ru"),
        voice_file: UploadFile = File(...)
):
    """Обработка голосовых сообщений"""
    temp_file = None
    try:
        logger.info(f"Получено голосовое сообщение от пользователя {user_id}")

        # Сохраняем временный файл
        temp_file = f"temp_voice_{user_id}_{datetime.now().timestamp()}.ogg"
        with open(temp_file, "wb") as f:
            content = await voice_file.read()
            f.write(content)

        # Транскрибируем голос
        text = await transcribe_voice(temp_file, language)

        if not text:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Не удалось распознать голос"}
            )

        logger.info(f"Распознанный текст: {text}")

        # Обрабатываем как обычное сообщение
        chat_request = ChatMessage(
            user_id=user_id,
            text=text,
            language=language,
            is_voice=True
        )

        return await chat_message(chat_request)

    except Exception as e:
        logger.error(f"Ошибка в voice_message: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )
    finally:
        # Удаляем временный файл
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass


@app.post("/api/list/{user_id}/edit")
async def edit_shopping_list(user_id: int, edit_request: EditRequest):
    """Редактирование списка покупок"""
    try:
        if user_id not in user_data:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "Список покупок не найден"}
            )

        # Получаем текущие категории
        current_categories = user_data[user_id].get("categories", {})

        # Определяем изменения
        changes = await detect_edit_changes(edit_request.text, edit_request.language)

        if not changes:
            return JSONResponse(content={
                "success": True,
                "message": "Не удалось определить изменения",
                "changes": []
            })

        # Применяем изменения
        updated_categories = apply_edit_changes(current_categories, changes, edit_request.language)

        # Обновляем данные пользователя
        user_data[user_id]["categories"] = updated_categories
        user_data[user_id]["list_data"] = format_shopping_list_for_json(updated_categories)

        return JSONResponse(content={
            "success": True,
            "changes": changes,
            "data": user_data[user_id]["list_data"],
            "message": "Список успешно обновлен"
        })

    except Exception as e:
        logger.error(f"Ошибка в edit_shopping_list: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@app.post("/api/list/{user_id}/toggle")
async def toggle_purchase(user_id: int, item_data: Dict = Body(...)):
    """Переключение статуса покупки товара"""
    try:
        if user_id not in user_data:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "Список покупок не найден"}
            )

        category = item_data.get("category")
        item_name = item_data.get("item_name")

        if not category or not item_name:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Не указаны категория или название товара"}
            )

        # Находим и обновляем товар
        categories = user_data[user_id].get("categories", {})

        if category in categories:
            for item in categories[category]:
                if item["name"] == item_name:
                    item["purchased"] = not item.get("purchased", False)
                    break

        # Обновляем список
        user_data[user_id]["list_data"] = format_shopping_list_for_json(categories)

        # Проверяем, все ли товары куплены
        list_data = user_data[user_id]["list_data"]
        all_purchased = list_data.get("all_purchased", False)

        response_data = {
            "success": True,
            "purchased": any(item.get("purchased") for cat in categories.values() for item in cat),
            "all_purchased": all_purchased,
            "data": list_data
        }

        # Если все товары куплены, добавляем флаг для фронтенда
        if all_purchased:
            response_data["show_expense_prompt"] = True

        return JSONResponse(content=response_data)

    except Exception as e:
        logger.error(f"Ошибка в toggle_purchase: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@app.delete("/api/list/{user_id}")
async def clear_shopping_list(user_id: int):
    """Очистить список покупок пользователя"""
    try:
        if user_id in user_data:
            # Сохраняем в историю перед удалением
            list_data = user_data[user_id].get("list_data", {})
            add_to_user_history(user_id, list_data)

            # Удаляем список
            del user_data[user_id]

            return JSONResponse(content={
                "success": True,
                "message": "Список покупок очищен и сохранен в истории"
            })

        return JSONResponse(
            status_code=404,
            content={"success": False, "error": "Список покупок не найден"}
        )

    except Exception as e:
        logger.error(f"Ошибка в clear_shopping_list: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@app.post("/api/expense")
async def add_expense(expense_request: ExpenseRequest):
    """Добавить информацию о потраченной сумме"""
    try:
        user_id = expense_request.user_id

        if user_id not in user_data:
            # Проверяем, есть ли активный список
            if user_id in user_history and user_history[user_id]:
                # Берем последний список из истории
                last_list = user_history[user_id][-1]
                add_to_user_history(user_id, last_list, expense_request.amount)

                return JSONResponse(content={
                    "success": True,
                    "message": "Сумма расходов сохранена для последнего списка",
                    "analytics_available": True
                })

            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "Активный список покупок не найден"}
            )

        # Получаем данные текущего списка
        list_data = user_data[user_id].get("list_data", {})

        # Добавляем в историю
        add_to_user_history(user_id, list_data, expense_request.amount)

        # Очищаем текущий список
        if user_id in user_data:
            del user_data[user_id]

        return JSONResponse(content={
            "success": True,
            "message": "Сумма расходов сохранена",
            "analytics_available": True
        })

    except Exception as e:
        logger.error(f"Ошибка в add_expense: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


# ===== НОВЫЙ ЭНДПОИНТ ДЛЯ ПАРСИНГА СУММ =====
@app.post("/api/parse_amount")
async def parse_amount_endpoint(text: str = Body(..., embed=True)):
    """Парсит сумму из текста"""
    try:
        amount = parse_amount_from_text(text)

        return JSONResponse(content={
            "success": True,
            "amount": amount,
            "text": text,
            "formatted": f"{amount:,.0f}".replace(",", " ") + " сум"
        })
    except Exception as e:
        logger.error(f"Error parsing amount: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@app.get("/api/analytics/{user_id}")
async def get_analytics(user_id: int):
    """Получить аналитику пользователя"""
    try:
        analytics = get_user_analytics(user_id)

        return JSONResponse(content={
            "success": True,
            "data": analytics
        })

    except Exception as e:
        logger.error(f"Ошибка в get_analytics: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@app.get("/api/analytics/{user_id}/expenses")
async def get_expense_history_endpoint(user_id: int):
    """Получить историю расходов пользователя"""
    try:
        expense_history = get_expense_history(user_id)

        return JSONResponse(content={
            "success": True,
            "data": expense_history
        })

    except Exception as e:
        logger.error(f"Ошибка в get_expense_history: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@app.get("/api/analytics/{user_id}/list/{list_id}")
async def get_list_details_endpoint(user_id: int, list_id: str):
    """Получить детали конкретного списка"""
    try:
        list_details = get_list_details(user_id, list_id)

        if not list_details:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "Список не найден"}
            )

        return JSONResponse(content={
            "success": True,
            "data": list_details
        })

    except Exception as e:
        logger.error(f"Ошибка в get_list_details: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@app.post("/api/share")
async def share_list(share_request: ShareRequest):
    """Поделиться списком покупок"""
    try:
        user_id = share_request.user_id
        list_id = share_request.list_id

        if user_id not in user_data:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "Список покупок не найден"}
            )

        # Создаем новый ID для общего списка
        if not list_id or list_id == "new":
            list_id = secrets.token_hex(8)

        # Сохраняем в общих списках
        shared_lists[list_id] = {
            "list_data": user_data[user_id].get("list_data", {}),
            "owner_id": user_id,
            "created_at": datetime.now().isoformat(),
            "lang": user_languages.get(user_id, "ru"),
        }
        save_shared_lists()

        # Отмечаем как общий
        user_data[user_id]["is_shared"] = True

        share_url = f"/shared/{list_id}"

        return JSONResponse(content={
            "success": True,
            "share_url": share_url,
            "list_id": list_id,
            "message": "Список доступен по ссылке"
        })

    except Exception as e:
        logger.error(f"Ошибка в share_list: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@app.get("/api/prices/search")
async def search_prices(
        query: str = Query(...),
        lang: str = Query("ru")
):
    """Поиск товаров в базе цен"""
    try:
        results = []

        # Ищем по названиям товаров
        for product_id, product_info in price_db.items.items():
            display_name = product_info.get("display_name", product_id)
            if query.lower() in product_id.lower() or query.lower() in display_name.lower():
                # Получаем информацию о цене
                price_info = price_db.get_price_for_product(product_id, "", lang)
                if price_info:
                    results.append({
                        "id": product_id,
                        "name": display_name,
                        "price": price_info.get("price"),
                        "unit": price_info.get("unit"),
                        "source": price_info.get("source")
                    })

        # Ищем по синонимам
        synonyms_dict = price_db.synonyms_ru if lang == "ru" else price_db.synonyms_uz
        for synonym, product_id in synonyms_dict.items():
            if query.lower() in synonym.lower():
                if product_id in price_db.items:
                    product_info = price_db.items[product_id]
                    price_info = price_db.get_price_for_product(product_id, "", lang)
                    if price_info:
                        results.append({
                            "id": product_id,
                            "name": synonym,
                            "price": price_info.get("price"),
                            "unit": price_info.get("unit"),
                            "source": price_info.get("source")
                        })

        return JSONResponse(content={
            "success": True,
            "results": results[:10]  # Ограничиваем 10 результатами
        })

    except Exception as e:
        logger.error(f"Ошибка в search_prices: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@app.post("/api/set-language")
async def set_language(user_id: int = Form(...), language: str = Form(...)):
    """Установить язык пользователя"""
    try:
        if language not in ["ru", "uz"]:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Неподдерживаемый язык"}
            )

        user_languages[user_id] = language
        save_languages()

        return JSONResponse(content={
            "success": True,
            "message": f"Язык установлен: {language}"
        })

    except Exception as e:
        logger.error(f"Ошибка в set_language: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


# ===== WebSocket для реального времени =====
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


manager = ConnectionManager()


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    """WebSocket соединение для реального времени"""
    await manager.connect(user_id, websocket)

    try:
        while True:
            data = await websocket.receive_text()

            try:
                message_data = json.loads(data)
                message_type = message_data.get("type")

                if message_type == "chat":
                    text = message_data.get("text", "")
                    lang = message_data.get("language", "ru")

                    response = await format_list_with_gpt(text, lang)

                    # Отправляем ответ обратно
                    await manager.send_personal_message(user_id, {
                        "type": "response",
                        "message": response
                    })

                    # Если это список покупок, сохраняем
                    if any(emoji in response for emoji in ["🥕", "🍎", "🥛", "🍖", "📦", "🥤", "🧴", "📝"]):
                        categories = parse_shopping_list(response, lang)

                        if user_id not in user_data:
                            user_data[user_id] = {}

                        user_data[user_id]["categories"] = categories
                        user_data[user_id]["list_data"] = format_shopping_list_for_json(categories)
                        user_data[user_id]["last_message"] = text
                        user_data[user_id]["last_response"] = response
                        user_data[user_id]["created_at"] = datetime.now().isoformat()
                        user_data[user_id]["is_shared"] = False

                        await manager.send_personal_message(user_id, {
                            "type": "shopping_list",
                            "data": user_data[user_id]["list_data"]
                        })

                elif message_type == "ping":
                    await manager.send_personal_message(user_id, {"type": "pong"})

                elif message_type == "get_list":
                    if user_id in user_data and user_data[user_id].get("list_data"):
                        await manager.send_personal_message(user_id, {
                            "type": "current_list",
                            "data": user_data[user_id]["list_data"]
                        })
                    else:
                        await manager.send_personal_message(user_id, {
                            "type": "error",
                            "message": "Список не найден"
                        })

                elif message_type == "toggle_purchase":
                    category = message_data.get("category")
                    item_name = message_data.get("item_name")

                    if user_id in user_data and category and item_name:
                        categories = user_data[user_id].get("categories", {})
                        if category in categories:
                            for item in categories[category]:
                                if item["name"] == item_name:
                                    item["purchased"] = not item.get("purchased", False)
                                    break

                        user_data[user_id]["list_data"] = format_shopping_list_for_json(categories)

                        # Проверяем, все ли товары куплены
                        list_data = user_data[user_id]["list_data"]
                        all_purchased = list_data.get("all_purchased", False)

                        response = {
                            "type": "list_updated",
                            "data": list_data,
                            "all_purchased": all_purchased
                        }

                        if all_purchased:
                            response["show_expense_prompt"] = True

                        await manager.send_personal_message(user_id, response)

            except json.JSONDecodeError:
                await manager.send_personal_message(user_id, {
                    "type": "error",
                    "message": "Неверный формат JSON"
                })
            except Exception as e:
                logger.error(f"WebSocket processing error: {e}")
                await manager.send_personal_message(user_id, {
                    "type": "error",
                    "message": "Ошибка обработки сообщения"
                })

    except WebSocketDisconnect:
        manager.disconnect(user_id)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(user_id)


# ===== ЗАПУСК СЕРВЕРА =====
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info"
    )