"""
Regression tests for the Bozorlik AI parser / matching pipeline.

Run with:  python test_parser.py
No external test framework required — uses only stdlib unittest.

These tests cover:
  - quantity extraction (both orderings)
  - filler word filtering
  - comma / conjunction splitting
  - unit normalization
  - noise/garbage rejection
  - duplicate prevention
  - unknown product preservation
  - mixed RU/UZ input
  - edit command patterns
"""

import sys
import os
import unittest
import unittest.mock as mock

# Allow importing app from the same directory without package setup.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# ──────────────────────────────────────────────────────────────────────────────
# Lightweight stubs so tests can run without a live DB / OpenAI key.
# We set POSTGRES_URL before import so the module-level guard passes, then
# replace the DB constructor with a MagicMock so no real connection is made.
# ──────────────────────────────────────────────────────────────────────────────
os.environ["POSTGRES_URL"] = "postgresql://stub:stub@localhost/stub"
os.environ.setdefault("PRICES_FILE", os.path.join(_HERE, "prices.json"))

# Pre-populate sys.modules with stubs for all heavy dependencies.
_postgres_stub_mod = mock.MagicMock()
_aiohttp_stub = mock.MagicMock()
_openai_stub = mock.MagicMock()
_dotenv_stub = mock.MagicMock()
_dotenv_stub.load_dotenv = mock.MagicMock()

for _mod in [
    "postgres_db", "postgres_shared_repository",
    "mini_app.postgres_db", "mini_app.postgres_shared_repository",
    "shared_storage", "mini_app.shared_storage",
]:
    sys.modules.setdefault(_mod, _postgres_stub_mod)

sys.modules.setdefault("aiohttp", _aiohttp_stub)
sys.modules.setdefault("openai", _openai_stub)
sys.modules.setdefault("dotenv", _dotenv_stub)

# Stub FastAPI and Pydantic so app.py loads without the full web stack.
_fastapi_stub = mock.MagicMock()
_fastapi_stub.FastAPI = mock.MagicMock(return_value=mock.MagicMock())
_fastapi_stub.HTTPException = Exception
_fastapi_stub.UploadFile = mock.MagicMock()
_fastapi_stub.File = mock.MagicMock(return_value=None)
_fastapi_stub.Form = mock.MagicMock(return_value=None)
_fastapi_stub.Query = mock.MagicMock(return_value=None)
_fastapi_stub.Body = mock.MagicMock(return_value=None)
_fastapi_stub.WebSocket = mock.MagicMock()
_fastapi_stub.WebSocketDisconnect = Exception
_fastapi_stub.Request = mock.MagicMock()
_fastapi_middleware_stub = mock.MagicMock()
_fastapi_responses_stub = mock.MagicMock()
_pydantic_stub = mock.MagicMock()
_pydantic_stub.BaseModel = object  # classes that inherit from BaseModel must be real classes

for _mod, _stub in [
    ("fastapi", _fastapi_stub),
    ("fastapi.middleware", _fastapi_middleware_stub),
    ("fastapi.middleware.cors", _fastapi_middleware_stub),
    ("fastapi.responses", _fastapi_responses_stub),
    ("pydantic", _pydantic_stub),
]:
    sys.modules.setdefault(_mod, _stub)

# app.py uses relative imports (from .postgres_db import ...).
# We make it importable standalone by loading it as part of a fake package.
import importlib
import importlib.util
import types

_import_error = None
_app_module = None

try:
    # Build a fake "mini_app" package in sys.modules.
    _pkg = types.ModuleType("mini_app")
    _pkg.__path__ = [_HERE]
    _pkg.__package__ = "mini_app"
    sys.modules["mini_app"] = _pkg
    sys.modules["mini_app.postgres_db"] = _postgres_stub_mod
    sys.modules["mini_app.postgres_shared_repository"] = _postgres_stub_mod
    sys.modules["mini_app.shared_storage"] = _postgres_stub_mod

    # Load app.py as mini_app.app so relative imports resolve.
    _spec = importlib.util.spec_from_file_location(
        "mini_app.app",
        os.path.join(_HERE, "app.py"),
        submodule_search_locations=[],
    )
    _app_module = importlib.util.module_from_spec(_spec)
    _app_module.__package__ = "mini_app"
    sys.modules["mini_app.app"] = _app_module
    _spec.loader.exec_module(_app_module)
except Exception as _e:
    _import_error = _e


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get(name):
    """Get symbol from app module (skip test if module not importable)."""
    if _app_module is None:
        reason = f"app module could not be imported: {_import_error}"
        raise unittest.SkipTest(reason)
    obj = getattr(_app_module, name, None)
    if obj is None:
        raise unittest.SkipTest(f"Symbol '{name}' not found in app module")
    return obj


# ──────────────────────────────────────────────────────────────────────────────
# Unit-level tests for pure functions (no DB required)
# ──────────────────────────────────────────────────────────────────────────────

class TestUnitMapping(unittest.TestCase):
    """UNIT_MAPPING must contain all expected variants."""

    def _map(self):
        return _get("UNIT_MAPPING")  # returns a dict, not a function — no binding issue

    def test_kg_variants(self):
        m = self._map()
        for key in ["кг", "килограмм", "kilo", "kg"]:
            self.assertEqual(m.get(key), "kg", f"Expected {key!r} → 'kg'")

    def test_gram_variants(self):
        m = self._map()
        for key in ["г", "гр", "грамм", "gram", "g"]:
            self.assertEqual(m.get(key), "g", f"Expected {key!r} → 'g'")

    def test_litre_variants(self):
        m = self._map()
        for key in ["л", "литр", "litr", "l"]:
            self.assertEqual(m.get(key), "l", f"Expected {key!r} → 'l'")

    def test_ml_variants(self):
        m = self._map()
        for key in ["мл", "ml"]:
            self.assertEqual(m.get(key), "ml", f"Expected {key!r} → 'ml'")

    def test_pcs_variants(self):
        m = self._map()
        for key in ["шт", "штук", "dona", "ta"]:
            self.assertEqual(m.get(key), "pcs", f"Expected {key!r} → 'pcs'")


class TestFillerWords(unittest.TestCase):
    """SHOPPING_FILLER_WORDS must include all common command/polite tokens."""

    def _fillers(self):
        return _get("SHOPPING_FILLER_WORDS")

    def test_russian_polite(self):
        f = self._fillers()
        for word in ["пожалуйста", "добавь", "купи", "нужно", "еще", "ещё", "мне"]:
            self.assertIn(word, f, f"Filler word {word!r} should be in SHOPPING_FILLER_WORDS")

    def test_uzbek_command(self):
        f = self._fillers()
        for word in ["yana", "ol", "ber", "menga", "iltimos"]:
            self.assertIn(word, f, f"Filler word {word!r} should be in SHOPPING_FILLER_WORDS")


class TestIsValidProductCandidate(unittest.TestCase):
    """_is_valid_product_candidate must reject numbers, units, and fillers."""

    def _fn(self):
        return _get("_is_valid_product_candidate")

    def test_rejects_pure_number(self):
        fn = self._fn()
        self.assertFalse(fn("15"), "Pure number '15' must not be a valid product")
        self.assertFalse(fn("1.5"), "Pure float '1.5' must not be a valid product")

    def test_rejects_pure_unit(self):
        fn = self._fn()
        self.assertFalse(fn("кг"), "Pure unit 'кг' must not be a valid product")
        self.assertFalse(fn("kg"), "Pure unit 'kg' must not be a valid product")
        self.assertFalse(fn("шт"), "Pure unit 'шт' must not be a valid product")
        self.assertFalse(fn("литр"), "Pure unit 'литр' must not be a valid product")

    def test_rejects_filler(self):
        fn = self._fn()
        self.assertFalse(fn("пожалуйста"), "Polite word must not be a valid product")
        self.assertFalse(fn("добавь"), "Command word must not be a valid product")

    def test_accepts_product_names(self):
        fn = self._fn()
        for name in ["картошка", "молоко", "хлеб", "tuxum", "kartoshka", "сок"]:
            self.assertTrue(fn(name), f"Product name {name!r} should be valid")


class TestExtractNameAndQuantity(unittest.TestCase):
    """_extract_name_and_quantity must handle both qty orderings."""

    def _fn(self):
        return _get("_extract_name_and_quantity")

    def _ru(self, fragment):
        return self._fn()(fragment, "ru")

    def test_product_then_qty_unit(self):
        name, qty = self._ru("картошка 15 кг")
        self.assertIn("картошк", name.lower(), f"Name should contain 'картошк', got {name!r}")
        self.assertIn("15", qty, f"Qty should contain '15', got {qty!r}")
        self.assertIn("кг", qty.lower() + "kg", f"Qty should contain unit, got {qty!r}")

    def test_qty_unit_then_product(self):
        name, qty = self._ru("2 кг картошки")
        self.assertIn("картошк", name.lower(), f"Name should contain 'картошк', got {name!r}")
        self.assertIn("2", qty, f"Qty should contain '2', got {qty!r}")

    def test_latin_product_with_qty(self):
        name, qty = self._fn()("kartoshka 15 kilo", "ru")
        self.assertIn("kartoshka", name.lower(), f"Name should contain 'kartoshka', got {name!r}")
        self.assertIn("15", qty, f"Qty should contain '15', got {qty!r}")

    def test_cola_15l(self):
        name, qty = self._fn()("cola 1.5l", "ru")
        self.assertIn("cola", name.lower(), f"Name should contain 'cola', got {name!r}")
        self.assertIn("1.5", qty, f"Qty should contain '1.5', got {qty!r}")

    def test_milk_1000ml(self):
        name, qty = self._ru("сок 1000 мл")
        self.assertIn("сок", name.lower(), f"Name should contain 'сок', got {name!r}")
        self.assertIn("1000", qty, f"Qty should contain '1000', got {qty!r}")

    def test_eggs_pcs_uz(self):
        name, qty = self._fn()("10 ta tuxum", "uz")
        self.assertIn("tuxum", name.lower(), f"Name should contain 'tuxum', got {name!r}")
        self.assertIn("10", qty, f"Qty should contain '10', got {qty!r}")

    def test_no_qty_returns_empty_qty(self):
        name, qty = self._ru("молоко")
        self.assertIn("молоко", name.lower())
        self.assertEqual(qty, "", f"Qty should be empty, got {qty!r}")


class TestSmartSplitFragments(unittest.TestCase):
    """_smart_split_fragments must not break product, qty, unit into separate items."""

    def _fn(self):
        return _get("_smart_split_fragments")

    def test_normal_comma_list(self):
        fn = self._fn()
        result = fn("молоко, хлеб, яйца")
        self.assertEqual(len(result), 3, f"Expected 3 fragments, got {result}")

    def test_broken_qty_unit(self):
        fn = self._fn()
        # "сыр, 15, кг" must not produce 3 fragments — qty and unit must merge.
        result = fn("сыр, 15, кг")
        self.assertLessEqual(len(result), 2,
                             f"'сыр, 15, кг' should merge qty/unit, got {result}")
        joined = " ".join(result).lower()
        self.assertIn("15", joined, "Quantity '15' must survive merge")
        self.assertIn("кг", joined, "Unit 'кг' must survive merge")

    def test_conjunction_separation(self):
        fn = self._fn()
        result = fn("молоко 1 л\nхлеб\nбанан 2 кг")
        self.assertGreaterEqual(len(result), 2, f"Expected ≥2 fragments, got {result}")


# ──────────────────────────────────────────────────────────────────────────────
# Integration-level tests (require prices.json — skipped if not present)
# ──────────────────────────────────────────────────────────────────────────────

class TestDirectParserIntegration(unittest.TestCase):
    """Integration tests for try_parse_direct_shopping_input.

    Skipped when prices.json is absent (CI / unit-only mode).
    """

    @classmethod
    def setUpClass(cls):
        import os
        prices_file = os.environ.get("PRICES_FILE", "prices.json")
        if not os.path.exists(prices_file):
            raise unittest.SkipTest(f"prices.json not found at {prices_file!r} — skipping integration tests")
        # Wrap in staticmethod so self is not injected when called via instance.
        cls.parse = staticmethod(_get("try_parse_direct_shopping_input"))

    def _all_items(self, result):
        items = []
        for cat_items in result.values():
            items.extend(cat_items)
        return items

    # ── Basic single-product cases ──────────────────────────────────────────

    def test_potato_15kg_ru(self):
        result = self.parse("картошка 15 кг", "ru")
        items = self._all_items(result)
        self.assertEqual(len(items), 1, f"Expected 1 item, got {items}")
        item = items[0]
        self.assertIn("картошк", item["name"].lower(), f"Name mismatch: {item['name']!r}")
        self.assertIn("15", item.get("quantity", ""), f"Qty mismatch: {item.get('quantity')!r}")

    def test_potato_15kg_lat(self):
        result = self.parse("kartoshka 15 kilo", "ru")
        items = self._all_items(result)
        self.assertEqual(len(items), 1, f"Expected 1 item, got {items}")
        self.assertIn("15", items[0].get("quantity", ""))

    def test_potato_2kg_prefix(self):
        result = self.parse("2 кг картошки", "ru")
        items = self._all_items(result)
        self.assertEqual(len(items), 1, f"Expected 1 item, got {items}")
        self.assertIn("2", items[0].get("quantity", ""))

    def test_eggs_10pcs_ru(self):
        result = self.parse("яйца 10 шт", "ru")
        items = self._all_items(result)
        self.assertEqual(len(items), 1, f"Expected 1 item, got {items}")
        self.assertIn("10", items[0].get("quantity", ""))

    def test_eggs_10ta_uz(self):
        result = self.parse("10 ta tuxum", "uz")
        items = self._all_items(result)
        self.assertEqual(len(items), 1, f"Expected 1 item, got {items}")
        self.assertIn("10", items[0].get("quantity", ""))

    # ── Multi-product / comma-separated ─────────────────────────────────────

    def test_comma_list(self):
        result = self.parse("молоко 1 л, хлеб, банан 2 кг", "ru")
        items = self._all_items(result)
        self.assertGreaterEqual(len(items), 2,
                                f"Expected ≥2 items from comma list, got {items}")

    def test_no_broken_products_from_comma_qty_unit(self):
        result = self.parse("сыр, 15, кг", "ru")
        items = self._all_items(result)
        names = [i["name"].lower() for i in items]
        for bad in ["15", "кг", "kg"]:
            self.assertNotIn(bad, names,
                             f"Garbage token {bad!r} must not become a product name. Items: {names}")

    def test_conjunction_and(self):
        result = self.parse("картошку 3 кг и лук", "ru")
        items = self._all_items(result)
        self.assertGreaterEqual(len(items), 1,
                                f"Expected ≥1 item, got {items}")
        names = " ".join(i["name"].lower() for i in items)
        self.assertIn("картошк", names, f"'картошка' not found. Items: {names}")

    # ── Filler word / command noise filtering ────────────────────────────────

    def test_strip_pozhaluysta_kupi(self):
        result = self.parse("пожалуйста купи картошку 5 кг и лук", "ru")
        items = self._all_items(result)
        names = [i["name"].lower() for i in items]
        for garbage in ["пожалуйста", "купи", "и"]:
            self.assertNotIn(garbage, names,
                             f"Filler {garbage!r} must not become a product. Items: {names}")
        self.assertTrue(any("картошк" in n for n in names),
                        f"'картошка' should be in list. Items: {names}")

    def test_dobav_command_stripped(self):
        result = self.parse("добавь картошку 3 кг", "ru")
        items = self._all_items(result)
        names = [i["name"].lower() for i in items]
        self.assertNotIn("добавь", names, f"'добавь' must not be a product. Items: {names}")
        self.assertTrue(any("картошк" in n for n in names),
                        f"'картошка' should be in list. Items: {names}")

    # ── Cola / drinks with float qty ─────────────────────────────────────────

    def test_cola_1_5l(self):
        result = self.parse("cola 1.5l", "ru")
        items = self._all_items(result)
        self.assertEqual(len(items), 1, f"Expected 1 item, got {items}")
        self.assertIn("1.5", items[0].get("quantity", ""))

    def test_juice_1000ml(self):
        result = self.parse("сок 1000 мл", "ru")
        items = self._all_items(result)
        self.assertEqual(len(items), 1, f"Expected 1 item, got {items}")
        self.assertIn("1000", items[0].get("quantity", ""))

    # ── Isolated numbers / units must not become products ────────────────────

    def test_isolated_number_not_product(self):
        result = self.parse("15", "ru")
        items = self._all_items(result)
        names = [i["name"] for i in items]
        self.assertNotIn("15", names, f"Isolated number '15' must not be a product. Got: {names}")

    def test_isolated_unit_not_product(self):
        result = self.parse("кг", "ru")
        items = self._all_items(result)
        names = [i["name"] for i in items]
        self.assertNotIn("кг", names, f"Isolated unit 'кг' must not be a product. Got: {names}")

    # ── Unknown products preserved without price ─────────────────────────────

    def test_unknown_product_kept_no_price(self):
        result = self.parse("qoraqaragat 1 кг", "uz")
        items = self._all_items(result)
        # Unknown product should still be in the list, just without a price.
        self.assertGreaterEqual(len(items), 1,
                                "Unknown product should still produce a list item")
        item = items[0]
        # Price must be None for unknown products
        self.assertIsNone(item.get("estimated_price"),
                          f"Unknown product must have no price, got {item.get('estimated_price')}")

    # ── No duplicates ────────────────────────────────────────────────────────

    def test_no_duplicates_in_same_parse(self):
        result = self.parse("картошка 2 кг, картошка 2 кг", "ru")
        items = self._all_items(result)
        names = [i["name"].lower() for i in items]
        potato_count = sum(1 for n in names if "картошк" in n)
        self.assertLessEqual(potato_count, 1, f"Duplicate items found: {names}")

    # ── Mixed RU / UZ input ──────────────────────────────────────────────────

    def test_mixed_ru_uz(self):
        result = self.parse("kartoshka 3 кг, молоко 1 л", "ru")
        items = self._all_items(result)
        self.assertGreaterEqual(len(items), 1,
                                f"Expected ≥1 item from mixed input, got {items}")


class TestNormalizeQuantityDisplay(unittest.TestCase):
    """normalize_quantity_display must localize units correctly."""

    def _fn(self):
        return _get("normalize_quantity_display")

    def test_kg_ru(self):
        result = self._fn()("15 кг", "ru")
        self.assertIn("15", result)
        self.assertIn("кг", result)

    def test_kg_uz(self):
        result = self._fn()("15 кг", "uz")
        self.assertIn("15", result)
        # Uzbek locale maps "kg" → "kg"
        self.assertIn("kg", result.lower())

    def test_float_qty(self):
        result = self._fn()("1.5 л", "ru")
        self.assertIn("1.5", result)
        self.assertIn("л", result)


class TestScoreMatchConfidence(unittest.TestCase):
    """_score_match_confidence must return high score for exact match, low for mismatch."""

    @classmethod
    def setUpClass(cls):
        prices_file = os.environ.get("PRICES_FILE", "prices.json")
        if not os.path.exists(prices_file):
            raise unittest.SkipTest("prices.json not found")
        cls.fn = staticmethod(_get("_score_match_confidence"))

    def test_exact_match_high_score(self):
        product = {"name_ru": "Картошка", "name_uz": "Kartoshka"}
        score = self.fn("Картошка", product, "ru")
        self.assertGreaterEqual(score, 900,
                                f"Exact match should score ≥900, got {score}")

    def test_completely_different_low_score(self):
        product = {"name_ru": "Арбуз", "name_uz": "Tarvuz"}
        score = self.fn("Картошка", product, "ru")
        self.assertLess(score, 200,
                        f"Completely different products should score <200, got {score}")


# ──────────────────────────────────────────────────────────────────────────────
# Edit flow tests (deterministic patterns)
# ──────────────────────────────────────────────────────────────────────────────

class TestApplyEditChanges(unittest.TestCase):

    def _fn(self):
        return _get("apply_edit_changes")

    def _base_categories(self):
        return {
            "🥕 Овощи": [
                {"name": "Картошка", "quantity": "2 кг", "purchased": False,
                 "estimated_price": None, "original_name": "Картошка", "user_specified_quantity": True},
                {"name": "Бананы", "quantity": "1 кг", "purchased": False,
                 "estimated_price": None, "original_name": "Бананы", "user_specified_quantity": True},
            ]
        }

    def test_remove_item(self):
        fn = self._fn()
        changes = [{"action": "remove", "target": "Бананы", "new_item": "", "quantity": ""}]
        result = fn(self._base_categories(), changes, "ru")
        all_names = [i["name"].lower() for items in result.values() for i in items]
        self.assertNotIn("бананы", all_names, f"'Бананы' should be removed. Got: {all_names}")

    def test_add_item(self):
        fn = self._fn()
        changes = [{"action": "add", "target": "", "new_item": "Молоко", "quantity": "1 л"}]
        result = fn(self._base_categories(), changes, "ru")
        all_names = [i["name"].lower() for items in result.values() for i in items]
        self.assertIn("молоко", all_names, f"'Молоко' should be added. Got: {all_names}")

    def test_replace_item(self):
        fn = self._fn()
        changes = [{"action": "replace", "target": "Бананы", "new_item": "Яблоки", "quantity": "2 кг"}]
        result = fn(self._base_categories(), changes, "ru")
        all_names = [i["name"].lower() for items in result.values() for i in items]
        self.assertNotIn("бананы", all_names, f"'Бананы' should be replaced")
        self.assertIn("яблоки", all_names, f"'Яблоки' should appear after replace. Got: {all_names}")

    def test_update_quantity(self):
        fn = self._fn()
        changes = [{"action": "update", "target": "Картошка", "new_item": "", "quantity": "5 кг"}]
        result = fn(self._base_categories(), changes, "ru")
        all_items = [i for items in result.values() for i in items]
        potato = next((i for i in all_items if "картошк" in i["name"].lower()), None)
        self.assertIsNotNone(potato, "Картошка should still be in list")
        self.assertIn("5", potato.get("quantity", ""), f"Qty should be updated to '5 кг', got {potato.get('quantity')!r}")


# ──────────────────────────────────────────────────────────────────────────────
# Number-word conversion (voice often returns "два" / "ikki" instead of digits)
# ──────────────────────────────────────────────────────────────────────────────

class TestNumberWords(unittest.TestCase):

    def _fn(self):
        return _get("_convert_number_words")

    def test_russian_words(self):
        fn = self._fn()
        self.assertEqual(fn("картошка два килограмма"), "картошка 2 килограмма")
        self.assertEqual(fn("три помидора"), "3 помидора")

    def test_uzbek_words(self):
        fn = self._fn()
        self.assertEqual(fn("kartoshka ikki kilo"), "kartoshka 2 kilo")

    def test_leaves_real_words(self):
        fn = self._fn()
        # Should not touch unrelated words.
        self.assertEqual(fn("молоко хлеб"), "молоко хлеб")


# ──────────────────────────────────────────────────────────────────────────────
# Obscene / illegal content filtering (Tasks.md #4)
# ──────────────────────────────────────────────────────────────────────────────

class TestBlockedProducts(unittest.TestCase):

    def _fn(self):
        return _get("_is_blocked_product")

    def test_blocks_profanity(self):
        fn = self._fn()
        for word in ["хуй", "пизда", "сиськи"]:
            self.assertTrue(fn(word), f"{word!r} must be blocked")

    def test_blocks_weapons_drugs(self):
        fn = self._fn()
        for word in ["автомат калашникова", "наркотики", "героин", "qurol"]:
            self.assertTrue(fn(word), f"{word!r} must be blocked")

    def test_allows_real_products(self):
        fn = self._fn()
        for word in ["картошка", "молоко", "куриное филе", "tovuq", "kartoshka"]:
            self.assertFalse(fn(word), f"{word!r} must NOT be blocked")

    def test_blocked_not_added_to_list(self):
        parse = _get("try_parse_direct_shopping_input")
        result = parse("сиськи хуй пизда наркотики", "ru")
        items = [i["name"] for cat in result.values() for i in cat]
        self.assertEqual(items, [], f"Blocked words must not produce items, got {items}")

    def test_blocked_mixed_keeps_safe(self):
        parse = _get("try_parse_direct_shopping_input")
        result = parse("картошка наркотики молоко", "ru")
        names = " ".join(i["name"].lower() for cat in result.values() for i in cat)
        self.assertIn("картошк", names)
        self.assertIn("молоко", names)
        self.assertNotIn("наркот", names)


# ──────────────────────────────────────────────────────────────────────────────
# Greedy multi-product parsing (Tasks.md #1 — voice stream without punctuation)
# ──────────────────────────────────────────────────────────────────────────────

class TestGreedyMultiProduct(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        prices_file = os.environ.get("PRICES_FILE", "prices.json")
        if not os.path.exists(prices_file):
            raise unittest.SkipTest("prices.json not found")
        cls.parse = staticmethod(_get("try_parse_direct_shopping_input"))

    def _items(self, result):
        return [(i["name"], i.get("quantity", "")) for cat in result.values() for i in cat]

    def test_potato_qty_unit_single(self):
        items = self._items(self.parse("Картошка 2 кг", "ru"))
        self.assertEqual(len(items), 1)
        self.assertIn("2", items[0][1])

    def test_multi_product_with_quantities(self):
        items = self._items(self.parse("картошка 2 кг помидор 3 кг лук", "ru"))
        names = [n.lower() for n, _ in items]
        self.assertGreaterEqual(len(items), 3, f"Expected ≥3 products, got {items}")
        self.assertTrue(any("картошк" in n for n in names))
        self.assertTrue(any("помидор" in n for n in names))
        self.assertTrue(any("лук" in n for n in names))

    def test_multi_product_no_quantities(self):
        items = self._items(self.parse("помидоры огурцы картошка молоко", "ru"))
        self.assertGreaterEqual(len(items), 4, f"Expected 4 products, got {items}")

    def test_number_word_quantity(self):
        items = self._items(self.parse("картошка два килограмма", "ru"))
        self.assertEqual(len(items), 1, f"Expected 1 item, got {items}")
        self.assertIn("2", items[0][1], f"'два' should become 2, got {items}")

    def test_unknown_multiword_stays_single(self):
        items = self._items(self.parse("куриное филе", "ru"))
        self.assertEqual(len(items), 1, f"'куриное филе' must stay one item, got {items}")

    def test_uz_multi_product(self):
        items = self._items(self.parse("kartoshka 2 kg bodring pomidor", "uz"))
        self.assertGreaterEqual(len(items), 3, f"Expected ≥3, got {items}")


# ──────────────────────────────────────────────────────────────────────────────
# Meat-category detection (Tasks.md #5 — chicken filet must not stay in vegetables)
# ──────────────────────────────────────────────────────────────────────────────

class TestMeatCategory(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        prices_file = os.environ.get("PRICES_FILE", "prices.json")
        if not os.path.exists(prices_file):
            raise unittest.SkipTest("prices.json not found")
        cls.fn = staticmethod(_get("get_display_category_for_product"))

    def test_chicken_filet_ru(self):
        for name in ["куриное филе", "куриная грудка", "фарш", "говядина"]:
            cat = self.fn(name, "ru")
            self.assertIn("Мясные", cat, f"{name!r} should be meat, got {cat!r}")

    def test_chicken_filet_uz(self):
        cat = self.fn("tovuq file", "uz")
        self.assertIn("Go'sht", cat, f"'tovuq file' should be meat, got {cat!r}")


# ──────────────────────────────────────────────────────────────────────────────
# Deterministic voice commands: purchase / remove / replace (Tasks.md #2, #3)
# ──────────────────────────────────────────────────────────────────────────────

class TestVoiceCommands(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        prices_file = os.environ.get("PRICES_FILE", "prices.json")
        if not os.path.exists(prices_file):
            raise unittest.SkipTest("prices.json not found")
        cls.detect = staticmethod(_get("detect_voice_list_command"))
        cls.apply = staticmethod(_get("apply_voice_list_command"))
        cls.build = staticmethod(_get("try_parse_direct_shopping_input"))
        cls.to_json = staticmethod(_get("format_shopping_list_for_json"))

    def _list(self, text, lang):
        return self.to_json(self.build(text, lang), 1, lang)

    def _names(self, ld):
        return [i["name"] for cat in ld["categories"].values() for i in cat]

    def _purchased(self, ld):
        return {i["name"]: i["purchased"] for cat in ld["categories"].values() for i in cat}

    # ── purchase ──
    def test_purchase_marks_items_ru(self):
        ld = self._list("картошка 2 кг огурцы молоко", "ru")
        cmd = self.detect("купил картошку огурцы", "ru")
        self.assertEqual(cmd["type"], "purchase")
        ld, changed = self.apply(ld, cmd, "ru")
        self.assertTrue(changed)
        p = self._purchased(ld)
        self.assertTrue(p.get("Картошка"))
        self.assertTrue(p.get("Огурец"))
        self.assertFalse(p.get("Молоко"))

    def test_purchase_uz(self):
        ld = self._list("kartoshka bodring sut", "uz")
        cmd = self.detect("kartoshka bodring sotib oldim", "uz")
        self.assertEqual(cmd["type"], "purchase")
        ld, changed = self.apply(ld, cmd, "uz")
        self.assertTrue(changed)
        p = self._purchased(ld)
        self.assertTrue(p.get("Kartoshka"))
        self.assertTrue(p.get("Bodring"))

    # ── remove ──
    def test_remove_ru(self):
        ld = self._list("картошка огурцы молоко", "ru")
        cmd = self.detect("удали картошку", "ru")
        self.assertEqual(cmd["type"], "remove")
        ld, changed = self.apply(ld, cmd, "ru")
        self.assertTrue(changed)
        names = [n.lower() for n in self._names(ld)]
        self.assertFalse(any("картошк" in n for n in names), f"картошка should be gone, got {names}")

    def test_remove_uz(self):
        ld = self._list("kartoshka bodring sut", "uz")
        cmd = self.detect("sutni o'chir", "uz")
        self.assertEqual(cmd["type"], "remove")
        ld, changed = self.apply(ld, cmd, "uz")
        self.assertTrue(changed)
        names = [n.lower() for n in self._names(ld)]
        self.assertNotIn("sut", names, f"sut should be gone, got {names}")

    # ── replace + category move ──
    def test_replace_moves_category_ru(self):
        ld = self._list("картошка 2 кг огурцы молоко", "ru")
        cmd = self.detect("замени картошку на куриное филе", "ru")
        self.assertEqual(cmd["type"], "replace")
        ld, changed = self.apply(ld, cmd, "ru")
        self.assertTrue(changed)
        cats = {c: [i["name"].lower() for i in its] for c, its in ld["categories"].items()}
        meat = [c for c in cats if "Мясные" in c]
        self.assertTrue(meat, f"Meat category should exist, got {cats}")
        self.assertTrue(any("филе" in n for n in cats[meat[0]]))
        veg = [c for c in cats if "Овощи" in c]
        if veg:
            self.assertFalse(any("картошк" in n for n in cats[veg[0]]),
                             "картошка must leave vegetables after replace")

    def test_replace_uz_suffix_form(self):
        ld = self._list("kartoshka bodring sut", "uz")
        cmd = self.detect("kartoshkani pomidorga almashtir", "uz")
        self.assertEqual(cmd["type"], "replace")
        ld, changed = self.apply(ld, cmd, "uz")
        self.assertTrue(changed)
        names = [n.lower() for n in self._names(ld)]
        self.assertNotIn("kartoshka", names)
        self.assertTrue(any("pomidor" in n for n in names), f"pomidor should appear, got {names}")

    # ── normal product input must NOT be treated as a command ──
    def test_plain_products_not_command(self):
        self.assertIsNone(self.detect("картошка лук молоко", "ru"))


# ──────────────────────────────────────────────────────────────────────────────
# Smart price matching (TODO #2/#3 — synonyms, multi-word products, unit math)
# ──────────────────────────────────────────────────────────────────────────────

class TestSmartPriceMatching(unittest.TestCase):
    """Synonyms and inflections must resolve to DB products with correct prices."""

    @classmethod
    def setUpClass(cls):
        prices_file = os.environ.get("PRICES_FILE", "prices.json")
        if not os.path.exists(prices_file):
            raise unittest.SkipTest("prices.json not found")
        cls.parse = staticmethod(_get("try_parse_direct_shopping_input"))
        cls.to_json = staticmethod(_get("format_shopping_list_for_json"))

    def _items(self, text, lang="ru"):
        data = self.to_json(self.parse(text, lang), 1, lang)
        return [i for cat in data["categories"].values() for i in cat]

    def _single(self, text, lang="ru"):
        items = self._items(text, lang)
        self.assertEqual(len(items), 1, f"Expected exactly 1 item for {text!r}, got {items}")
        return items[0]

    # ── synonyms → price ──────────────────────────────────────────────────────

    def test_kartofel_synonym_price_scaled(self):
        # Картошка = 8000/кг → картофель 5 кг = 40000
        item = self._single("картофель 5 кг")
        self.assertEqual(item["estimated_price"], 40000,
                         f"'картофель 5 кг' must cost 40000, got {item}")

    def test_morkovka_synonym_price(self):
        # Морковь красная = 6000/кг → морковка 2 кг = 12000
        item = self._single("морковка 2 кг")
        self.assertEqual(item["estimated_price"], 12000,
                         f"'морковка 2 кг' must cost 12000, got {item}")

    def test_tomat_synonym_price(self):
        # Помидоры = 38000/кг
        item = self._single("томаты 1 кг")
        self.assertEqual(item["estimated_price"], 38000,
                         f"'томаты 1 кг' must cost 38000, got {item}")

    def test_generic_milk_priced_keeps_user_name(self):
        # "молоко" получает цену Lactel 3,2%, но имя остаётся "Молоко".
        item = self._single("молоко 1 л")
        self.assertEqual(item["name"].lower(), "молоко", f"Name must stay 'Молоко', got {item['name']!r}")
        self.assertIsNotNone(item["estimated_price"], f"'молоко' must be priced via alias, got {item}")

    # ── multi-word products stay whole ────────────────────────────────────────

    def test_krasnaya_repa_single_item_priced(self):
        # Красная репа = 12000/кг → 2 кг = 24000, и это ОДИН товар.
        item = self._single("красная репа 2 кг")
        self.assertIn("репа", item["name"].lower())
        self.assertEqual(item["estimated_price"], 24000, f"Wrong price: {item}")

    def test_krasnaya_repa_inflected(self):
        # Родительный падеж: "2 кг красной репы" — тоже один товар с ценой.
        item = self._single("2 кг красной репы")
        self.assertEqual(item["estimated_price"], 24000, f"Wrong price: {item}")

    def test_green_salad_leaf_single_item(self):
        # Зеленый лист салата = 5000 за 1 шт, три слова — один товар.
        item = self._single("зеленый лист салата")
        self.assertEqual(item["estimated_price"], 5000, f"Wrong price: {item}")

    def test_chicken_fillet_priced_and_meat(self):
        # Куриное филе = 48000/кг → 2 кг = 96000, категория Мясные продукты.
        items = self._items("куриное филе 2 кг")
        self.assertEqual(len(items), 1, f"Expected 1 item, got {items}")
        self.assertEqual(items[0]["estimated_price"], 96000, f"Wrong price: {items[0]}")
        self.assertIn("Мясные", items[0]["category"], f"Wrong category: {items[0]}")

    # ── unit conversion ───────────────────────────────────────────────────────

    def test_grams_converted_to_kg(self):
        # 500 г картошки = 0.5 × 8000 = 4000
        item = self._single("500 г картошки")
        self.assertEqual(item["estimated_price"], 4000, f"Wrong price: {item}")

    def test_incompatible_units_no_price(self):
        # Хлеб продаётся за 1 шт; "2 кг хлеба" не должен дать бессмысленную цену.
        item = self._single("хлеб 2 кг")
        self.assertIsNone(item["estimated_price"],
                          f"Incompatible units must give no price, got {item}")

    # ── package size preference ───────────────────────────────────────────────

    def test_flour_5kg_uses_5kg_pack(self):
        # Мука 5 кг стоит 65000 (фасовка), а не 5 × 14000 = 70000.
        item = self._single("мука 5 кг")
        self.assertEqual(item["estimated_price"], 65000, f"Wrong price: {item}")

    # ── uzbek synonyms ────────────────────────────────────────────────────────

    def test_uz_sabzi_priced(self):
        # sabzi → Qizil sabzi (Морковь красная) = 6000/кг → 3 kg = 18000
        item = self._single("sabzi 3 kg", "uz")
        self.assertEqual(item["estimated_price"], 18000, f"Wrong price: {item}")

    def test_uz_gosht_priced(self):
        # go'sht → Mol go'shti = 100000/кг
        item = self._single("go'sht 1 kg", "uz")
        self.assertEqual(item["estimated_price"], 100000, f"Wrong price: {item}")


class TestSpiceDetectionFix(unittest.TestCase):
    """"мак" — приправа, но "макароны" не должны попадать в приправы."""

    @classmethod
    def setUpClass(cls):
        prices_file = os.environ.get("PRICES_FILE", "prices.json")
        if not os.path.exists(prices_file):
            raise unittest.SkipTest("prices.json not found")
        cls.price_db = _get("price_db")

    def test_mak_is_spice(self):
        self.assertTrue(self.price_db.is_spice("мак"))

    def test_makarony_not_spice(self):
        self.assertFalse(self.price_db.is_spice("макароны"),
                         "'макароны' must not be detected as spice via 'мак' substring")


def _get_bot(name):
    """Get symbol from the standalone aiogram bot module (bot.py); skip if
    aiogram is not installed in the test environment."""
    try:
        # app.py was imported above with stubbed pydantic/aiohttp; aiogram needs
        # the real packages, so drop the stubs before importing bot.py.
        for _m in list(sys.modules):
            if (_m == "pydantic" or _m.startswith("pydantic.")
                    or _m == "aiohttp" or _m.startswith("aiohttp.")):
                if isinstance(sys.modules[_m], mock.MagicMock):
                    del sys.modules[_m]
        import bot as bot_module
    except Exception as e:
        raise unittest.SkipTest(f"bot module could not be imported: {e}")
    obj = getattr(bot_module, name, None)
    if obj is None:
        raise unittest.SkipTest(f"Symbol '{name}' not found in bot module")
    return obj


class TestTelegramBotBridge(unittest.TestCase):
    """Бот-чат → мини-приложение: определение языка и текст ответа (bot.py)."""

    def test_detect_ru_cyrillic(self):
        detect = _get_bot("detect_message_language")
        self.assertEqual(detect("2 кг картошки, молоко и хлеб"), "ru")

    def test_detect_uz_latin(self):
        detect = _get_bot("detect_message_language")
        self.assertEqual(detect("2 kg kartoshka, sut va non"), "uz")

    def test_detect_empty_uses_fallback(self):
        detect = _get_bot("detect_message_language")
        self.assertEqual(detect("", "uz"), "uz")
        self.assertEqual(detect("", "ru"), "ru")
        self.assertEqual(detect("123", "xx"), "ru")

    def test_detect_digits_only_uses_fallback(self):
        detect = _get_bot("detect_message_language")
        self.assertEqual(detect("2 100", "uz"), "uz")

    def test_saved_text_contains_items_and_totals(self):
        build = _get_bot("build_saved_list_text")
        list_data = {
            "categories": {"🥕 Овощи": [
                {"name": "Картошка", "quantity": "2 кг"},
                {"name": "Лук", "quantity": "1 кг"},
            ]},
            "total_items": 2,
            "total_estimated_price": 15000,
        }
        text = build(list_data, "ru")
        self.assertIn("Картошка", text)
        self.assertIn("2 кг", text)
        self.assertIn("15 000", text)
        self.assertIn("Сохранил", text)

    def test_saved_text_escapes_html(self):
        build = _get_bot("build_saved_list_text")
        list_data = {"categories": {"📝 Другое": [{"name": "<script>alert(1)</script>", "quantity": ""}]},
                     "total_items": 1, "total_estimated_price": 0}
        text = build(list_data, "ru")
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_saved_text_truncates_long_lists(self):
        build = _get_bot("build_saved_list_text")
        items = [{"name": f"Товар{i}", "quantity": "1 шт"} for i in range(20)]
        list_data = {"categories": {"📝 Другое": items}, "total_items": 20, "total_estimated_price": 0}
        text = build(list_data, "ru")
        self.assertIn("ещё 8", text)
        self.assertNotIn("Товар19", text)

    def test_open_app_markup_deeplink(self):
        markup_fn = _get_bot("open_app_markup")
        markup = markup_fn("ru")
        btn = markup.inline_keyboard[0][0]
        self.assertIn("startapp=botlist", btn.url)
        self.assertIn("t.me/", btn.url)


# ──────────────────────────────────────────────────────────────────────────────
# "Я на базаре": цена голосом, тысячные суммы, финиш-команды, план vs факт
# ──────────────────────────────────────────────────────────────────────────────

class TestBazaarMode(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        prices_file = os.environ.get("PRICES_FILE", "prices.json")
        if not os.path.exists(prices_file):
            raise unittest.SkipTest("prices.json not found")
        cls.parse = staticmethod(_get("parse_bazaar_purchases"))
        cls.finish = staticmethod(_get("detect_bazaar_finish"))
        cls.apply = staticmethod(_get("apply_bazaar_purchases"))
        cls.enable = staticmethod(_get("enable_bazaar_mode"))
        cls.summary = staticmethod(_get("bazaar_summary"))
        cls.build = staticmethod(_get("try_parse_direct_shopping_input"))
        cls.to_json = staticmethod(_get("format_shopping_list_for_json"))

    def _list(self, text, lang="ru"):
        return self.enable(self.to_json(self.build(text, lang), 1, lang))

    def _items(self, ld):
        return {i["name"]: i for cat in ld["categories"].values() for i in cat}

    # ── price interpretation ──
    def test_short_number_is_thousands(self):
        p = self.parse("Картошка 38", "ru")
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["price"], 38000)

    def test_explicit_thousands_word(self):
        p = self.parse("Яблоки 25 тысяч", "ru")
        self.assertEqual(p[0]["price"], 25000)

    def test_large_number_kept_as_is(self):
        p = self.parse("Хлеб 3500", "ru")
        self.assertEqual(p[0]["price"], 3500)

    def test_purchase_verb_and_preposition(self):
        p = self.parse("Купил мясо за 120 тысяч", "ru")
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["price"], 120000)
        self.assertIn("мясо", p[0]["name"].lower())

    def test_multiple_purchases_comma_separated(self):
        p = self.parse("Морковь 15, лук 12", "ru")
        self.assertEqual([x["price"] for x in p], [15000, 12000])

    def test_multiple_purchases_without_commas(self):
        p = self.parse("картошка 38 морковь 15", "ru")
        self.assertEqual(len(p), 2)
        self.assertEqual(p[0]["price"], 38000)
        self.assertEqual(p[1]["price"], 15000)

    def test_quantity_before_price_not_confused(self):
        p = self.parse("2 кг картошки за 38 тысяч", "ru")
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["price"], 38000)
        self.assertIn("картошк", p[0]["name"].lower())

    def test_uz_ming(self):
        p = self.parse("Kartoshka 38 ming", "uz")
        self.assertEqual(p[0]["price"], 38000)

    def test_spoken_number_words(self):
        p = self.parse("картошка тридцать восемь", "ru")
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["price"], 38000)

    def test_no_price_returns_empty(self):
        self.assertEqual(self.parse("купил картошку", "ru"), [])

    # ── finish command detection ──
    def test_finish_ru(self):
        for phrase in ["Всё куплено", "Закончил покупки", "Завершить список", "Сохранить список"]:
            self.assertTrue(self.finish(phrase, "ru"), phrase)

    def test_finish_uz(self):
        for phrase in ["Hammasi olindi", "Xaridni tugatdim", "Ro'yxatni yakunla"]:
            self.assertTrue(self.finish(phrase, "uz"), phrase)

    def test_regular_purchase_is_not_finish(self):
        self.assertFalse(self.finish("Картошка 38", "ru"))

    # ── applying purchases to the list ──
    def test_apply_marks_item_and_sets_actual_price(self):
        ld = self._list("картошка 2 кг, молоко")
        ld, applied = self.apply(ld, [{"name": "картошка", "price": 38000}], "ru")
        self.assertEqual(len(applied), 1)
        items = self._items(ld)
        potato = items.get("Картошка")
        self.assertIsNotNone(potato)
        self.assertTrue(potato["purchased"])
        self.assertEqual(potato["actual_price"], 38000)
        self.assertEqual(potato["estimated_price"], 38000)

    def test_apply_keeps_forecast_as_planned_price(self):
        ld = self._list("картошка 2 кг")
        planned_before = self._items(ld)["Картошка"].get("estimated_price")
        ld, applied = self.apply(ld, [{"name": "картошка", "price": 38000}], "ru")
        self.assertEqual(self._items(ld)["Картошка"].get("planned_price"), planned_before)

    def test_apply_adds_offlist_item_as_purchased(self):
        ld = self._list("картошка")
        ld, applied = self.apply(ld, [{"name": "гранат", "price": 45000}], "ru")
        self.assertEqual(len(applied), 1)
        self.assertTrue(applied[0]["added"])
        added = [i for n, i in self._items(ld).items() if "гранат" in n.lower()]
        self.assertTrue(added)
        self.assertTrue(added[0]["purchased"])
        self.assertEqual(added[0]["actual_price"], 45000)
        self.assertIsNone(added[0].get("planned_price"))

    # ── plan vs actual summary ──
    def test_summary_savings_positive_and_negative(self):
        ld = self.enable({
            "categories": {"🥕 Овощи": [
                {"name": "Картошка", "quantity": "2 кг", "purchased": False, "estimated_price": 40000},
                {"name": "Лук", "quantity": "1 кг", "purchased": False, "estimated_price": 10000},
            ]},
            "total_items": 2, "purchased_items": 0, "total_estimated_price": 50000,
        })
        self.assertEqual(ld["bazaar_planned_total"], 50000)
        ld, _ = self.apply(ld, [{"name": "картошка", "price": 38000}], "ru")
        s = self.summary(ld)
        self.assertEqual(s["actual_total"], 38000)
        self.assertEqual(s["savings"], 2000)   # экономия
        self.assertEqual(s["purchased_items"], 1)
        ld, _ = self.apply(ld, [{"name": "лук", "price": 13000}], "ru")
        s = self.summary(ld)
        self.assertEqual(s["actual_total"], 51000)
        self.assertEqual(s["savings"], -1000)  # переплата 3000 по луку минус 2000 экономии

    def test_recalculate_prices_keeps_actual_price(self):
        recalc = _get("recalculate_list_prices")
        ld = self._list("картошка 2 кг")
        ld, _ = self.apply(ld, [{"name": "картошка", "price": 38000}], "ru")
        ld = recalc(ld, "ru")
        self.assertEqual(self._items(ld)["Картошка"]["estimated_price"], 38000)


class TestBudgetValidation(unittest.TestCase):
    """validate_budget_amount: месячный бюджет аналитики (сум)."""

    def setUp(self):
        self.validate = _get("validate_budget_amount")

    def test_normal_amounts(self):
        self.assertEqual(self.validate(3_000_000), 3_000_000)
        self.assertEqual(self.validate("2500000"), 2_500_000)
        self.assertEqual(self.validate(1499999.6), 1_500_000)

    def test_zero_clears_budget(self):
        self.assertEqual(self.validate(0), 0)

    def test_negative_rejected(self):
        self.assertIsNone(self.validate(-1))
        self.assertIsNone(self.validate(-3_000_000))

    def test_garbage_rejected(self):
        self.assertIsNone(self.validate(None))
        self.assertIsNone(self.validate("три миллиона"))
        self.assertIsNone(self.validate(float("nan")))
        self.assertIsNone(self.validate(float("inf")))

    def test_absurdly_large_rejected(self):
        self.assertIsNone(self.validate(10_000_000_001))


class TestFamilyShareSnapshot(unittest.TestCase):
    """Семейная синхронизация (Pro): create_shared_snapshot(live=True) должен
    помечать снапшот live_sync + source_list_id, а обычный шаринг — нет.
    shared_storage замокан для app.py, поэтому грузим настоящий модуль напрямую."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "shared_storage_real", os.path.join(_HERE, "shared_storage.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class DictRepo:
            def __init__(self):
                self.store = {}

            def save(self, token, record):
                self.store[token] = record
                return record

            def get(self, token):
                return self.store.get(token)

            def delete_expired(self):
                return 0

        cls.service = module.SharedListService(DictRepo())
        cls.list_data = {
            "list_id": "abc123", "total_items": 2, "purchased_items": 0,
            "categories": {"🥦 Овощи": [{"name": "Картошка", "purchased": False}]},
        }

    def test_live_snapshot_marks_sync(self):
        record = self.service.create_shared_snapshot(self.list_data, owner_id=7, lang="ru", live=True)
        snapshot = record["list_data"]
        self.assertTrue(snapshot.get("live_sync"))
        self.assertEqual(snapshot.get("source_list_id"), "abc123")
        self.assertEqual(record["owner_id"], 7)

    def test_regular_snapshot_has_no_sync(self):
        record = self.service.create_shared_snapshot(self.list_data, owner_id=7, lang="ru")
        snapshot = record["list_data"]
        self.assertNotIn("live_sync", snapshot)
        self.assertNotIn("source_list_id", snapshot)

    def test_snapshot_does_not_mutate_source(self):
        self.service.create_shared_snapshot(self.list_data, owner_id=7, lang="ru", live=True)
        self.assertNotIn("live_sync", self.list_data)


class TestProSubscriptionStatus(unittest.TestCase):
    """compute_pro_status: триал 7 дней, оплаченная подписка, сервисный сбор.
    Сбор 2490 снимается только у АКТИВНОЙ оплаченной подписки — триал платит."""

    def setUp(self):
        from datetime import datetime, timedelta
        self.compute = _get("compute_pro_status")
        self.fee = _get("PRO_SERVICE_FEE")
        self.now = datetime(2026, 7, 13, 12, 0, 0)
        self.delta = timedelta

    def test_no_row_means_no_pro_and_fee(self):
        s = self.compute(None, now=self.now)
        self.assertFalse(s["is_pro"])
        self.assertEqual(s["plan"], "none")
        self.assertEqual(s["service_fee"], self.fee)

    def test_active_trial_is_pro_but_pays_fee(self):
        row = {"plan": "trial", "is_pro": True,
               "trial_ends_at": self.now + self.delta(days=5, hours=3), "paid_until": None}
        s = self.compute(row, now=self.now)
        self.assertTrue(s["is_pro"])
        self.assertEqual(s["plan"], "trial")
        self.assertEqual(s["days_left"], 6)  # 5д3ч → округляем вверх
        self.assertEqual(s["service_fee"], self.fee)

    def test_trial_last_hours_show_one_day(self):
        row = {"plan": "trial", "is_pro": True,
               "trial_ends_at": self.now + self.delta(hours=2), "paid_until": None}
        s = self.compute(row, now=self.now)
        self.assertTrue(s["is_pro"])
        self.assertEqual(s["days_left"], 1)

    def test_expired_trial_loses_pro(self):
        row = {"plan": "trial", "is_pro": True,
               "trial_ends_at": self.now - self.delta(days=1), "paid_until": None}
        s = self.compute(row, now=self.now)
        self.assertFalse(s["is_pro"])
        self.assertEqual(s["plan"], "trial_expired")
        self.assertEqual(s["service_fee"], self.fee)

    def test_paid_subscription_no_fee(self):
        row = {"plan": "paid", "is_pro": True,
               "trial_ends_at": None, "paid_until": self.now + self.delta(days=20)}
        s = self.compute(row, now=self.now)
        self.assertTrue(s["is_pro"])
        self.assertEqual(s["plan"], "paid")
        self.assertEqual(s["service_fee"], 0)

    def test_paid_expired_pays_fee_again(self):
        row = {"plan": "paid", "is_pro": True,
               "trial_ends_at": None, "paid_until": self.now - self.delta(days=1)}
        s = self.compute(row, now=self.now)
        self.assertFalse(s["is_pro"])
        self.assertEqual(s["plan"], "expired")
        self.assertEqual(s["service_fee"], self.fee)

    def test_legacy_manual_pro_treated_as_paid_forever(self):
        row = {"plan": "none", "is_pro": True, "trial_ends_at": None, "paid_until": None}
        s = self.compute(row, now=self.now)
        self.assertTrue(s["is_pro"])
        self.assertEqual(s["plan"], "paid")
        self.assertEqual(s["service_fee"], 0)


class TestWebPayments(unittest.TestCase):
    """Веб-оплата подписки: подпись Click SHOP API и ссылки на чекаут."""

    def setUp(self):
        self.sig = _get("click_signature")
        self.payme_url = _get("build_payme_checkout_url")
        self.click_url = _get("build_click_checkout_url")

    def test_click_prepare_signature(self):
        import hashlib
        params = {"click_trans_id": "12345", "service_id": "777", "merchant_trans_id": "42",
                  "amount": "19990.0", "action": "0", "sign_time": "2026-07-14 12:00:00"}
        expected = hashlib.md5(
            ("12345" + "777" + "secret" + "42" + "19990.0" + "0" + "2026-07-14 12:00:00").encode()).hexdigest()
        self.assertEqual(self.sig(params, "secret"), expected)

    def test_click_complete_signature_includes_prepare_id(self):
        import hashlib
        params = {"click_trans_id": "12345", "service_id": "777", "merchant_trans_id": "42",
                  "merchant_prepare_id": "42", "amount": "19990.0", "action": "1",
                  "sign_time": "2026-07-14 12:00:00"}
        expected = hashlib.md5(
            ("12345" + "777" + "secret" + "42" + "42" + "19990.0" + "1" + "2026-07-14 12:00:00").encode()).hexdigest()
        self.assertEqual(self.sig(params, "secret"), expected)
        # без merchant_prepare_id подпись complete не совпадает
        self.assertNotEqual(self.sig({**params, "action": "0"}, "secret"), expected)

    def test_payme_checkout_url_encodes_order(self):
        import base64 as b64
        url = self.payme_url("MERCHANT1", 42, 19990, lang="ru", test_mode=True)
        self.assertTrue(url.startswith("https://checkout.test.paycom.uz/"))
        decoded = b64.b64decode(url.rsplit("/", 1)[1]).decode()
        self.assertEqual(decoded, "m=MERCHANT1;ac.order_id=42;a=1999000;l=ru")

    def test_payme_prod_host(self):
        url = self.payme_url("M", 1, 19990, test_mode=False)
        self.assertTrue(url.startswith("https://checkout.paycom.uz/"))

    def test_click_checkout_url(self):
        url = self.click_url("777", "888", 19990, 42, return_url="https://bozorlikai.uz")
        self.assertIn("service_id=777", url)
        self.assertIn("merchant_id=888", url)
        self.assertIn("amount=19990", url)
        self.assertIn("transaction_param=42", url)
        self.assertIn("return_url=https%3A%2F%2Fbozorlikai.uz", url)

    def test_price_is_19990(self):
        self.assertEqual(_get("PRO_PRICE_MONTHLY"), 19990)


class TestCategoryAssignment(unittest.TestCase):
    """Продукты должны попадать в свои категории — даже когда в каталоге цен
    есть только составной продукт с этим словом («Мохито клубника» ≠ клубника)."""

    @classmethod
    def setUpClass(cls):
        prices_file = os.environ.get("PRICES_FILE", "prices.json")
        if not os.path.exists(prices_file):
            raise unittest.SkipTest("prices.json not found")
        cls.price_db = _get("price_db")

    def _cat(self, name, lang="ru"):
        return self.price_db.determine_category(name, lang)

    # ── ягоды и фрукты не должны утекать в «Напитки» (мохито/соки) ────────────
    def test_strawberry_is_fruit(self):
        self.assertIn("Фрукты", self._cat("клубника"))

    def test_strawberry_uz_is_fruit(self):
        self.assertIn("Mevalar", self._cat("qulupnay", "uz"))

    def test_berries_are_fruit(self):
        for berry in ["малина", "вишня", "черешня", "персик", "смородина",
                      "виноград", "абрикос", "слива", "хурма", "инжир",
                      "арбуз", "дыня"]:
            self.assertIn("Фрукты", self._cat(berry), f"{berry!r} must be a fruit")

    # ── овощи, которых раньше не было ни в БД, ни в ключевых словах ──────────
    def test_vegetables(self):
        for veg in ["капуста", "баклажан", "свёкла", "свекла", "тыква", "шпинат"]:
            self.assertIn("Овощи", self._cat(veg), f"{veg!r} must be a vegetable")

    # ── составной продукт целиком — всё ещё напиток ──────────────────────────
    def test_mojito_strawberry_is_drink(self):
        self.assertIn("Напитки", self._cat("мохито клубника"))

    # ── фрукт как вкус-модификатор не делает продукт фруктом ─────────────────
    def test_strawberry_yogurt_is_dairy(self):
        self.assertIn("Молочные", self._cat("йогурт клубничный"))

    def test_cream_is_dairy_not_plum(self):
        # «сливки» не должны попадать во фрукты из-за ключа «слива»
        self.assertIn("Молочные", self._cat("сливки"))

    # ── редактирование позиции пересортировывает её ──────────────────────────
    def test_edit_moves_item_to_correct_category(self):
        update = _get("update_item_in_list")
        list_data = {"categories": {"🥤 Напитки": [
            {"name": "Кола", "quantity": "1 шт", "purchased": False,
             "estimated_price": None, "original_name": "Кола",
             "user_specified_quantity": True}]}}
        list_data = update(list_data, "🥤 Напитки", "Кола", "Клубника", "1 кг", "ru")
        cats = list_data["categories"]
        self.assertNotIn("🥤 Напитки", cats, f"Кола must be gone: {cats}")
        self.assertIn("🍎 Фрукты", cats, f"Клубника must be a fruit: {cats}")
        self.assertEqual(cats["🍎 Фрукты"][0]["name"], "Клубника")

    # ── каждый продукт каталога сортируется в свою собственную категорию ─────
    def test_every_catalog_product_self_categorizes(self):
        import json
        with open(os.environ.get("PRICES_FILE", "prices.json"), encoding="utf-8") as f:
            data = json.load(f)
        cat_defs = data["categories"]
        for product in data["products"]:
            for lang in ("ru", "uz"):
                name = product.get(lang)
                if not name:
                    continue
                # яйца — спец-правило (всегда Бакалея), пропускаем
                if any(k in name.lower() for k in ("яйц", "tuxum")):
                    continue
                expected = cat_defs[product["category"]][lang]
                got = self._cat(name, lang)
                self.assertIn(expected, got,
                              f"[{lang}] {name!r}: expected *{expected}*, got {got!r}")


class TestRecipePricing(unittest.TestCase):
    """Цены ингредиентов рецептов = цена за единицу × количество."""

    @classmethod
    def setUpClass(cls):
        for f in ("prices.json", "recipes.json"):
            if not os.path.exists(os.path.join(_HERE, f)):
                raise unittest.SkipTest(f"{f} not found")
        cls.app = _app_module
        # детерминированный путь (без GPT)
        cls._client = cls.app.client
        cls.app.client = None

    @classmethod
    def tearDownClass(cls):
        cls.app.client = cls._client

    def _build_list(self, text):
        res = self.app.build_recipe_shopping_list(text, "ru")
        self.assertTrue(res["found"], f"Recipe not found for {text!r}")
        list_data = {"categories": {}, "list_id": "test", "created_at": "now"}
        return res, self.app.add_recipe_ingredients_to_list(list_data, res["ingredients"], "ru")

    def _items(self, list_data):
        return {i["name"]: i for cat in list_data["categories"].values() for i in cat}

    def test_plov_12_servings_scale(self):
        res, _ = self._build_list("хочу приготовить плов на 12 человек")
        self.assertEqual(res["servings"], 12)
        amounts = {i["name"]: i["amount"] for i in res["ingredients"]}
        self.assertEqual(amounts["Рис лазер"], 2400)  # 800 г × 3
        self.assertEqual(amounts["Говядина"], 1800)   # 600 г × 3

    def test_plov_12_rice_price_is_unit_price_times_qty(self):
        # Рис лазер = 23000/кг → 2400 г = 2.4 кг → 55200
        _, list_data = self._build_list("хочу приготовить плов на 12 человек")
        items = self._items(list_data)
        self.assertEqual(items["Рис лазер"]["estimated_price"], 55200,
                         f"Рис лазер 2.4 кг must cost 23000×2.4=55200, got {items['Рис лазер']}")

    def test_plov_all_ingredients_priced(self):
        _, list_data = self._build_list("хочу приготовить плов на 12 человек")
        for name, item in self._items(list_data).items():
            self.assertIsNotNone(item["estimated_price"],
                                 f"{name!r} in plov must have a price: {item}")

    def test_plov_total_is_sum_of_items(self):
        _, list_data = self._build_list("плов на 12 человек")
        items = self._items(list_data)
        self.assertEqual(list_data["total_estimated_price"],
                         sum(i["estimated_price"] for i in items.values()))

    def test_small_spice_amount_costs_one_package(self):
        # Соль 45 г при цене «за пачку» — покупается одна пачка (4000),
        # отсутствие цены хуже честной цены упаковки.
        _, list_data = self._build_list("плов на 12 человек")
        items = self._items(list_data)
        self.assertEqual(items["Соль"]["estimated_price"], 4000, items["Соль"])

    def test_every_recipe_every_ingredient_priced(self):
        recipes = self.app.load_recipes()
        for key, recipe in recipes.items():
            aliases = recipe.get("aliases") or [key]
            _, list_data = self._build_list(f"хочу приготовить {aliases[0]}")
            for name, item in self._items(list_data).items():
                self.assertIsNotNone(
                    item["estimated_price"],
                    f"{key}: ingredient {name!r} has no price: {item}")

    def test_proportional_scaling_against_catalog(self):
        # Для каждого ингредиента в г/мл с совместимой упаковкой цена должна
        # быть пропорциональна количеству (units конвертируются).
        pdb = self.app.price_db
        _, list_data = self._build_list("плов на 12 человек")
        for name, item in self._items(list_data).items():
            qty_text = item["quantity"]
            matches = pdb.find_products(item.get("original_name") or name, "ru")
            self.assertTrue(matches, f"{name!r} must exist in prices.json")
            best = pdb.choose_best_product_match(
                matches, name, "ru", requested_quantity_text=qty_text) or matches[0]
            expected, _, _ = pdb.calculate_price_for_product(best, qty_text, "ru")
            self.assertEqual(item["estimated_price"], expected,
                             f"{name!r}: list price {item['estimated_price']} != "
                             f"calculated {expected} from {best['name_ru']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
