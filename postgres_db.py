from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import QueuePool
from typing import Optional, Dict, Any
import json
import logging
from datetime import datetime, timedelta

from postgres_models import Base, ActiveList, SharedList, UserHistory, UserLanguage, Receipt, PurchaseHistoryItem, UserBudget, UserPro, PaymentOrder

logger = logging.getLogger(__name__)

class PostgresDatabaseManager:
    def __init__(self, database_url: str, echo: bool = False):
        self.database_url = database_url
        url = make_url(database_url)
        # Use pool settings suitable for small apps
        self.engine = create_engine(database_url, poolclass=QueuePool, pool_size=5, max_overflow=10, echo=echo)
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        # Ensure tables exist
        Base.metadata.create_all(self.engine)
        self._ensure_bilingual_purchase_columns()
        self._ensure_pro_columns()

    def _ensure_bilingual_purchase_columns(self) -> None:
        """create_all doesn't alter existing tables; add the bilingual name
        columns to purchase_history when upgrading an older database."""
        try:
            columns = {c['name'] for c in inspect(self.engine).get_columns('purchase_history')}
            missing = [c for c in ('name_ru', 'name_uz') if c not in columns]
            if not missing:
                return
            with self.engine.begin() as conn:
                for column in missing:
                    conn.execute(text(f'ALTER TABLE purchase_history ADD COLUMN {column} VARCHAR(255)'))
            logger.info(f"Added bilingual columns to purchase_history: {missing}")
        except Exception as e:
            logger.error(f"Could not ensure bilingual purchase columns: {e}")

    def _ensure_pro_columns(self) -> None:
        """Upgrade the user_pro table created before the trial/subscription
        system: add plan/trial_ends_at/paid_until and convert legacy manual
        Pro flags into paid-with-no-expiry."""
        try:
            columns = {c['name'] for c in inspect(self.engine).get_columns('user_pro')}
            statements = []
            if 'plan' not in columns:
                statements.append("ALTER TABLE user_pro ADD COLUMN plan VARCHAR(16) DEFAULT 'none'")
            if 'trial_ends_at' not in columns:
                statements.append('ALTER TABLE user_pro ADD COLUMN trial_ends_at TIMESTAMP')
            if 'paid_until' not in columns:
                statements.append('ALTER TABLE user_pro ADD COLUMN paid_until TIMESTAMP')
            if not statements:
                return
            with self.engine.begin() as conn:
                for statement in statements:
                    conn.execute(text(statement))
                conn.execute(text("UPDATE user_pro SET plan='paid' WHERE is_pro AND (plan IS NULL OR plan='none')"))
            logger.info("Upgraded user_pro table with subscription columns")
        except Exception as e:
            logger.error(f"Could not ensure user_pro columns: {e}")

    def _session(self):
        return self.Session()

    # User languages
    def get_user_language(self, user_id: int) -> str:
        session = self._session()
        try:
            row = session.query(UserLanguage).get(user_id)
            return row.language if row else 'ru'
        finally:
            session.close()

    def set_user_language(self, user_id: int, language: str) -> None:
        session = self._session()
        try:
            row = session.query(UserLanguage).get(user_id)
            if row:
                row.language = language
            else:
                row = UserLanguage(user_id=user_id, language=language)
                session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Pro subscription (trial / paid). Status is computed in app.compute_pro_status;
    # the DB layer only stores and returns raw rows.
    def get_pro_row(self, user_id: int) -> Optional[Dict[str, Any]]:
        session = self._session()
        try:
            row = session.query(UserPro).get(user_id)
            if not row:
                return None
            return {
                'plan': row.plan or 'none',
                'is_pro': bool(row.is_pro),
                'trial_ends_at': row.trial_ends_at,
                'paid_until': row.paid_until,
            }
        finally:
            session.close()

    def ensure_trial(self, user_id: int, days: int) -> None:
        """First-time user: start the free trial. No-op if a row already exists
        (the trial is granted once, никогда повторно)."""
        session = self._session()
        try:
            if session.query(UserPro).get(user_id):
                return
            session.add(UserPro(user_id=user_id, is_pro=True, plan='trial',
                                trial_ends_at=datetime.now() + timedelta(days=days)))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def start_paid_subscription(self, user_id: int, days: int) -> None:
        session = self._session()
        try:
            row = session.query(UserPro).get(user_id)
            if not row:
                row = UserPro(user_id=user_id)
                session.add(row)
            row.plan = 'paid'
            row.is_pro = True
            row.paid_until = datetime.now() + timedelta(days=days)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def set_user_pro(self, user_id: int, is_pro: bool) -> None:
        """Manual override (dev/admin): True → paid with no expiry, False → cancel."""
        session = self._session()
        try:
            row = session.query(UserPro).get(user_id)
            if not row:
                row = UserPro(user_id=user_id)
                session.add(row)
            row.is_pro = is_pro
            row.plan = 'paid' if is_pro else 'none'
            row.paid_until = None
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Payment orders (веб-оплата подписки: Payme Merchant API / Click SHOP API)
    @staticmethod
    def _order_to_dict(row) -> Dict[str, Any]:
        return {
            'id': row.id, 'user_id': row.user_id, 'provider': row.provider or '',
            'amount': row.amount, 'state': row.state or 'pending',
            'payme_txn_id': row.payme_txn_id, 'payme_state': row.payme_state or 0,
            'payme_create_time': row.payme_create_time, 'payme_perform_time': row.payme_perform_time,
            'payme_cancel_time': row.payme_cancel_time, 'payme_reason': row.payme_reason,
            'click_trans_id': row.click_trans_id,
        }

    def create_payment_order(self, user_id: int, provider: str, amount: int) -> Dict[str, Any]:
        session = self._session()
        try:
            row = PaymentOrder(user_id=user_id, provider=provider, amount=amount)
            session.add(row)
            session.commit()
            return self._order_to_dict(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_payment_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        session = self._session()
        try:
            row = session.query(PaymentOrder).get(order_id)
            return self._order_to_dict(row) if row else None
        finally:
            session.close()

    def get_order_by_payme_txn(self, txn_id: str) -> Optional[Dict[str, Any]]:
        session = self._session()
        try:
            row = session.query(PaymentOrder).filter_by(payme_txn_id=txn_id).first()
            return self._order_to_dict(row) if row else None
        finally:
            session.close()

    def update_payment_order(self, order_id: int, **fields) -> None:
        session = self._session()
        try:
            row = session.query(PaymentOrder).get(order_id)
            if row:
                for key, value in fields.items():
                    setattr(row, key, value)
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_payme_transactions(self, from_ms: int, to_ms: int):
        """GetStatement: все Payme-транзакции в интервале времени создания."""
        session = self._session()
        try:
            rows = (session.query(PaymentOrder)
                    .filter(PaymentOrder.payme_txn_id.isnot(None),
                            PaymentOrder.payme_create_time >= from_ms,
                            PaymentOrder.payme_create_time <= to_ms)
                    .order_by(PaymentOrder.payme_create_time).all())
            return [self._order_to_dict(row) for row in rows]
        finally:
            session.close()

    # Monthly budget (Analytics)
    def get_user_budget(self, user_id: int) -> float:
        session = self._session()
        try:
            row = session.query(UserBudget).get(user_id)
            return row.amount if row else 0
        finally:
            session.close()

    def set_user_budget(self, user_id: int, amount: float) -> None:
        """amount == 0 clears the budget."""
        session = self._session()
        try:
            row = session.query(UserBudget).get(user_id)
            if row:
                if amount:
                    row.amount = amount
                else:
                    session.delete(row)
            elif amount:
                session.add(UserBudget(user_id=user_id, amount=amount))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Active lists
    def get_active_list(self, user_id: int) -> Optional[Dict[str, Any]]:
        session = self._session()
        try:
            row = session.query(ActiveList).get(user_id)
            if row:
                return json.loads(row.list_data) if isinstance(row.list_data, str) else row.list_data
            return None
        finally:
            session.close()

    def save_active_list(self, user_id: int, list_data: Dict[str, Any]) -> None:
        session = self._session()
        try:
            list_id = list_data.get('list_id') or list_data.get('listid') or None
            if not list_id:
                list_id = (list_data.get('list_id') or '')
            row = session.query(ActiveList).get(user_id)
            payload = list_data
            if row:
                row.list_id = list_id
                row.list_data = payload
            else:
                row = ActiveList(user_id=user_id, list_id=list_id, list_data=payload)
                session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_active_list(self, user_id: int) -> None:
        session = self._session()
        try:
            row = session.query(ActiveList).get(user_id)
            if row:
                session.delete(row)
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Shared lists
    def create_shared_list(self, list_id: str, list_data: Dict[str, Any], owner_id: int, lang: str, expires_days: Optional[int] = None) -> None:
        """Create a shared list. If expires_days is None the shared list is permanent.
        """
        session = self._session()
        try:
            expires_at = None
            if isinstance(expires_days, int):
                expires_at = datetime.now() + timedelta(days=expires_days)
            row = SharedList(list_id=list_id, list_data=list_data, owner_id=owner_id, lang=lang, expires_at=expires_at)
            session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_shared_list(self, list_id: str) -> Optional[Dict[str, Any]]:
        session = self._session()
        try:
            row = session.query(SharedList).get(list_id)
            if row and (row.expires_at is None or row.expires_at > datetime.now()):
                return {'list_data': row.list_data, 'owner_id': row.owner_id, 'lang': row.lang}
            return None
        finally:
            session.close()

    def get_shared_list_with_owner_validation(self, list_id: str, owner_id: int) -> Optional[Dict[str, Any]]:
        session = self._session()
        try:
            row = session.query(SharedList).get(list_id)
            if row and row.owner_id == owner_id and (row.expires_at is None or row.expires_at > datetime.now()):
                return {'list_data': row.list_data, 'owner_id': row.owner_id, 'lang': row.lang}
            return None
        finally:
            session.close()

    def cleanup_expired_shared_lists(self) -> int:
        session = self._session()
        try:
            now = datetime.now()
            deleted = session.query(SharedList).filter(SharedList.expires_at <= now).delete()
            session.commit()
            return deleted
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # History
    def add_history_entry(self, user_id: int, list_data: Dict[str, Any]) -> None:
        session = self._session()
        try:
            list_id = list_data.get('list_id') or list_data.get('listid') or ''
            # insert or replace
            existing = session.query(UserHistory).filter_by(user_id=user_id, list_id=list_id).first()
            if existing:
                existing.list_data = list_data
                existing.created_at = datetime.now()
            else:
                row = UserHistory(user_id=user_id, list_id=list_id, list_data=list_data)
                session.add(row)
            # keep last 50
            session.commit()
            # delete older entries
            rows = session.query(UserHistory).filter_by(user_id=user_id).order_by(UserHistory.created_at.desc()).all()
            if len(rows) > 50:
                for r in rows[50:]:
                    session.delete(r)
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_history_item(self, user_id: int, list_id: str):
        """Get a specific history entry by user_id and list_id"""
        session = self._session()
        try:
            row = session.query(UserHistory).filter_by(user_id=user_id, list_id=list_id).first()
            if not row:
                return None
            normalized = json.loads(row.list_data) if isinstance(row.list_data, str) else row.list_data
            return normalized
        finally:
            session.close()

    def get_user_history(self, user_id: int):
        session = self._session()
        try:
            rows = session.query(UserHistory).filter_by(user_id=user_id).order_by(UserHistory.created_at.desc()).all()
            result = []
            for row in rows:
                normalized = json.loads(row.list_data) if isinstance(row.list_data, str) else row.list_data
                # Flatten list_data to top-level fields expected by frontend
                entry = {
                    'list_id': row.list_id,
                    'created_at': row.created_at.isoformat(),
                }
                # Copy common fields from normalized payload
                if isinstance(normalized, dict):
                    entry['categories'] = normalized.get('categories', {})
                    # total price fields
                    entry['total_estimated_price'] = normalized.get('total_estimated_price') or normalized.get('estimated_price') or normalized.get('total_price') or 0
                    entry['estimated_price'] = entry['total_estimated_price']
                    entry['total_items'] = normalized.get('total_items') or sum(len(v) for v in normalized.get('categories', {}).values()) if normalized.get('categories') else 0
                    entry.update({k: v for k, v in normalized.items() if k not in entry})
                else:
                    entry['categories'] = {}

                result.append(entry)
            return result
        finally:
            session.close()

    def get_user_history_raw(self, user_id: int):
        """History entries as (list_id, list_data) pairs — the stored payloads
        themselves, for in-place rewrites (e.g. language translation)."""
        session = self._session()
        try:
            rows = session.query(UserHistory).filter_by(user_id=user_id).all()
            result = []
            for row in rows:
                data = json.loads(row.list_data) if isinstance(row.list_data, str) else row.list_data
                if isinstance(data, dict):
                    result.append((row.list_id, data))
            return result
        finally:
            session.close()

    def update_history_entry(self, user_id: int, list_id: str, list_data: Dict[str, Any]) -> None:
        """Replace a history entry's payload without touching created_at
        (unlike add_history_entry, which re-dates the entry)."""
        session = self._session()
        try:
            row = session.query(UserHistory).filter_by(user_id=user_id, list_id=list_id).first()
            if row:
                row.list_data = list_data
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def clear_user_history(self, user_id: int) -> bool:
        session = self._session()
        try:
            deleted = session.query(UserHistory).filter_by(user_id=user_id).delete()
            session.commit()
            return deleted > 0
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def clear_user_receipts(self, user_id: int) -> bool:
        """Delete all scanned receipts and their per-item purchase history."""
        session = self._session()
        try:
            deleted_items = session.query(PurchaseHistoryItem).filter_by(user_id=user_id).delete()
            deleted_receipts = session.query(Receipt).filter_by(user_id=user_id).delete()
            session.commit()
            return (deleted_items + deleted_receipts) > 0
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Receipts (Premium receipt scanning)
    def save_receipt(self, user_id: int, receipt: Dict[str, Any]) -> int:
        """Persist a normalized receipt; returns the new receipt id."""
        session = self._session()
        try:
            row = Receipt(
                user_id=user_id,
                store=receipt.get('store', ''),
                purchase_date=receipt.get('date', ''),
                currency=receipt.get('currency', ''),
                total=float(receipt.get('total') or 0),
                items=receipt.get('items', []),
            )
            session.add(row)
            session.commit()
            return row.id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _receipt_to_dict(row) -> Dict[str, Any]:
        items = row.items
        if isinstance(items, str):
            items = json.loads(items)
        return {
            'id': row.id,
            'store': row.store or '',
            'date': row.purchase_date or '',
            'currency': row.currency or '',
            'total': row.total or 0,
            'items': items or [],
            'created_at': row.created_at.isoformat() if row.created_at else '',
        }

    def get_user_receipts(self, user_id: int):
        session = self._session()
        try:
            rows = session.query(Receipt).filter_by(user_id=user_id).order_by(Receipt.created_at.desc()).all()
            return [self._receipt_to_dict(row) for row in rows]
        finally:
            session.close()

    def get_receipt(self, user_id: int, receipt_id: int) -> Optional[Dict[str, Any]]:
        session = self._session()
        try:
            row = session.query(Receipt).filter_by(user_id=user_id, id=receipt_id).first()
            return self._receipt_to_dict(row) if row else None
        finally:
            session.close()

    def update_receipt_items(self, receipt_id: int, items) -> None:
        """Rewrite a receipt's items JSON (e.g. bilingual name backfill)."""
        session = self._session()
        try:
            row = session.query(Receipt).get(receipt_id)
            if row:
                row.items = items
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Purchase history (per-item; foundation for repeat purchases / recommendations)
    def add_purchase_history_items(self, user_id: int, receipt_id: Optional[int], receipt: Dict[str, Any]) -> int:
        """Append every receipt item to the user's purchase history. Returns count."""
        session = self._session()
        try:
            count = 0
            for item in receipt.get('items', []):
                session.add(PurchaseHistoryItem(
                    user_id=user_id,
                    receipt_id=receipt_id,
                    name=item.get('name', ''),
                    name_ru=item.get('name_ru') or item.get('name', ''),
                    name_uz=item.get('name_uz') or None,
                    category=item.get('category', 'Другое'),
                    quantity=float(item.get('quantity') or 1),
                    unit=item.get('unit', 'шт'),
                    price=float(item.get('price') or 0),
                    currency=receipt.get('currency', ''),
                    store=receipt.get('store', ''),
                    purchase_date=receipt.get('date', ''),
                ))
                count += 1
            session.commit()
            return count
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_purchase_history(self, user_id: int, limit: int = 200):
        session = self._session()
        try:
            rows = (session.query(PurchaseHistoryItem)
                    .filter_by(user_id=user_id)
                    .order_by(PurchaseHistoryItem.created_at.desc())
                    .limit(limit).all())
            return [{
                'id': row.id,
                'receipt_id': row.receipt_id,
                'name': row.name,
                'name_ru': row.name_ru or row.name,
                'name_uz': row.name_uz or '',
                'category': row.category or 'Другое',
                'quantity': row.quantity or 1,
                'unit': row.unit or 'шт',
                'price': row.price or 0,
                'currency': row.currency or '',
                'store': row.store or '',
                'date': row.purchase_date or '',
                'created_at': row.created_at.isoformat() if row.created_at else '',
            } for row in rows]
        finally:
            session.close()

    def update_purchase_item_names(self, item_id: int, name_ru: Optional[str] = None,
                                   name_uz: Optional[str] = None) -> None:
        """Backfill bilingual names on a purchase history row."""
        session = self._session()
        try:
            row = session.query(PurchaseHistoryItem).get(item_id)
            if row:
                if name_ru:
                    row.name_ru = name_ru
                if name_uz:
                    row.name_uz = name_uz
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()