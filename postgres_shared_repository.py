from typing import Dict, Any, Optional
from postgres_db import PostgresDatabaseManager

class PostgresSharedListRepository:
    def __init__(self, db: PostgresDatabaseManager):
        self.db = db

    def save(self, token: str, record: Dict[str, Any]) -> Dict[str, Any]:
        # record expected to contain owner_id, lang, list_data, expires_at
        owner = record.get('owner_id', 0)
        lang = record.get('lang', 'ru')
        payload = record.get('list_data') or record.get('payload') or {}
        # calculate expires from created_at if present: default handled by db
        # persist as permanent shared list (no expiry) unless repository passes an expires value
        expires = None
        try:
            expires_val = record.get('expires_at')
            if expires_val:
                # if ISO timestamp is present, don't override
                expires = None
        except Exception:
            expires = None
        self.db.create_shared_list(token, payload, owner, lang, expires_days=expires)
        return record

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        row = self.db.get_shared_list(token)
        if not row:
            return None
        return {
            'token': token,
            'owner_id': row.get('owner_id'),
            'lang': row.get('lang'),
            'list_data': row.get('list_data'),
            'storage_type': 'postgres',
            'storage_version': 1,
        }

    def delete_expired(self) -> int:
        return self.db.cleanup_expired_shared_lists()
