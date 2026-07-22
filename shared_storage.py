"""Shared shopping list storage.

This module isolates share persistence behind a repository/service split so the
storage backend can move from JSON to PostgreSQL later without changing route
or UI business logic.

Future PostgreSQL mapping example:
- shared_lists(token PRIMARY KEY, owner_id, lang, payload JSONB, created_at,
  expires_at, updated_at, storage_version)
"""

from __future__ import annotations

import copy
import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Protocol


class SharedListRepository(Protocol):
    def save(self, token: str, record: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        ...

    def delete_expired(self) -> int:
        ...


class JsonSharedListRepository:
    """JSON file repository for shared lists.

    File structure example:
    {
      "version": 1,
      "shared_lists": {
        "token123": {
          "token": "token123",
          "owner_id": 42,
          "lang": "ru",
          "created_at": "2026-05-07T12:00:00",
          "expires_at": "2026-05-14T12:00:00",
          "list_data": { ... full shopping list snapshot ... }
        }
      }
    }
    """

    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self._lock = threading.RLock()
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self._write_state({"version": 1, "shared_lists": {}})

    def _read_state(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            return {"version": 1, "shared_lists": {}}
        with self.file_path.open("r", encoding="utf-8") as file:
            state = json.load(file)
        if "shared_lists" not in state or not isinstance(state["shared_lists"], dict):
            state["shared_lists"] = {}
        if "version" not in state:
            state["version"] = 1
        return state

    def _write_state(self, state: Dict[str, Any]) -> None:
        temp_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        temp_path.replace(self.file_path)

    def save(self, token: str, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            state = self._read_state()
            state["shared_lists"][token] = copy.deepcopy(record)
            self._write_state(state)
            return copy.deepcopy(state["shared_lists"][token])

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._read_state()
            record = state.get("shared_lists", {}).get(token)
            return copy.deepcopy(record) if record else None

    def delete_expired(self) -> int:
        with self._lock:
            state = self._read_state()
            shared_lists = state.get("shared_lists", {})
            now = datetime.now()
            removed = 0
            for token, record in list(shared_lists.items()):
                expires_at = record.get("expires_at")
                if not expires_at:
                    continue
                try:
                    if datetime.fromisoformat(expires_at) <= now:
                        del shared_lists[token]
                        removed += 1
                except ValueError:
                    continue
            if removed:
                self._write_state(state)
            return removed


class SharedListService:
    def __init__(self, repository: SharedListRepository):
        self.repository = repository

    def generate_unique_token(self, length: int = 12) -> str:
        # URL-safe token; keep it compact for share links.
        return secrets.token_urlsafe(length).rstrip("=").replace("-", "").replace("_", "")

    def create_shared_snapshot(
        self,
        list_data: Dict[str, Any],
        owner_id: int,
        lang: str,
        expires_days: int = 7,
        live: bool = False,
    ) -> Dict[str, Any]:
        snapshot = copy.deepcopy(list_data)
        token = self.generate_unique_token()
        while self.repository.get(token) is not None:
            token = self.generate_unique_token()

        created_at = datetime.now().isoformat()
        expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat()

        snapshot["list_id"] = snapshot.get("list_id") or token
        snapshot["created_at"] = snapshot.get("created_at") or created_at
        snapshot["is_shared_snapshot"] = True
        snapshot["original_owner_id"] = owner_id
        snapshot["shared_token"] = token
        if live:
            # Pro family sync: the link resolves to the owner's ACTIVE list while
            # it is still the same list; the snapshot stays as a static fallback.
            snapshot["live_sync"] = True
            snapshot["source_list_id"] = snapshot.get("list_id")

        record = {
            "token": token,
            "owner_id": owner_id,
            "lang": lang,
            "created_at": created_at,
            "expires_at": expires_at,
            "storage_type": "json",
            "storage_version": 1,
            "list_data": snapshot,
        }

        return self.repository.save(token, record)

    def get_shared_snapshot(self, token: str) -> Optional[Dict[str, Any]]:
        return self.repository.get(token)

    def cleanup_expired(self) -> int:
        return self.repository.delete_expired()