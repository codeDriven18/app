from sqlalchemy import Column, Integer, BigInteger, String, Text, DateTime, Float, func, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Boolean

Base = declarative_base()

class ActiveList(Base):
    __tablename__ = 'active_lists'
    user_id = Column(Integer, primary_key=True)
    list_id = Column(String(128), nullable=False)
    list_data = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class SharedList(Base):
    __tablename__ = 'shared_lists'
    list_id = Column(String(128), primary_key=True)
    list_data = Column(JSONB, nullable=False)
    owner_id = Column(Integer, nullable=False)
    lang = Column(String(8), default='ru')
    created_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime, nullable=True)
    storage_version = Column(Integer, default=1)

class UserHistory(Base):
    __tablename__ = 'user_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    list_id = Column(String(128), nullable=False)
    list_data = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

class UserLanguage(Base):
    __tablename__ = 'user_languages'
    user_id = Column(Integer, primary_key=True)
    language = Column(String(8), nullable=False, default='ru')
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Receipt(Base):
    """A scanned store receipt (Premium 'Сканирование чека'). Items are stored
    as structured JSON already normalized/categorized by the vision pipeline."""
    __tablename__ = 'receipts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    store = Column(String(255), default='')
    purchase_date = Column(String(32), default='')
    currency = Column(String(16), default='')
    total = Column(Float, default=0)
    items = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime, server_default=func.now())


class UserPro(Base):
    """Pro subscription state. plan: 'none' | 'trial' | 'paid'.
    New users get a free trial on first open (row created by GET /api/pro).
    Billing is not integrated yet — 'subscribe' activates instantly; the real
    payment provider (Click/Payme) plugs into /api/pro/{id}/subscribe later.
    Legacy rows with is_pro=True and plan='none' (manual activation from before
    the trial system) are treated as paid with no expiry."""
    __tablename__ = 'user_pro'
    user_id = Column(Integer, primary_key=True)
    is_pro = Column(Boolean, nullable=False, default=False)
    plan = Column(String(16), nullable=False, default='none')
    trial_ends_at = Column(DateTime, nullable=True)
    paid_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class PaymentOrder(Base):
    """Счёт на оплату подписки через веб-чекаут Payme/Click.

    id заказа передаётся провайдеру (Payme: ac.order_id, Click:
    transaction_param) и возвращается в колбэках. state: pending → paid /
    cancelled. Поля payme_* хранят состояние транзакции по протоколу
    Merchant API (времена в миллисекундах, state: 1 создана, 2 проведена,
    -1 отменена, -2 отменена после проведения)."""
    __tablename__ = 'payment_orders'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    provider = Column(String(16), default='')          # payme | click
    amount = Column(Integer, nullable=False)           # сумы (не тийины)
    state = Column(String(16), default='pending')      # pending | paid | cancelled
    payme_txn_id = Column(String(64), nullable=True, index=True)
    payme_state = Column(Integer, default=0)
    payme_create_time = Column(BigInteger, nullable=True)
    payme_perform_time = Column(BigInteger, nullable=True)
    payme_cancel_time = Column(BigInteger, nullable=True)
    payme_reason = Column(Integer, nullable=True)
    click_trans_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class UserBudget(Base):
    """Monthly grocery budget (in sums) the user sets on the Analytics page.
    amount == 0 means "no budget set"."""
    __tablename__ = 'user_budgets'
    user_id = Column(Integer, primary_key=True)
    amount = Column(Float, nullable=False, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class PurchaseHistoryItem(Base):
    """One purchased product, denormalized per item so future features
    (repeat purchases, 'buy again', AI recommendations) can query directly."""
    __tablename__ = 'purchase_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    receipt_id = Column(Integer, nullable=True, index=True)
    name = Column(String(255), nullable=False)
    # Bilingual product names so the UI can switch language without losing data.
    name_ru = Column(String(255), nullable=True)
    name_uz = Column(String(255), nullable=True)
    category = Column(String(64), default='Другое')
    quantity = Column(Float, default=1)
    unit = Column(String(32), default='шт')
    price = Column(Float, default=0)
    currency = Column(String(16), default='')
    store = Column(String(255), default='')
    purchase_date = Column(String(32), default='')
    created_at = Column(DateTime, server_default=func.now())
