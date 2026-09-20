"""Persistent SQLite subscriber storage for Telegram chat IDs.

Manages subscriber registrations with thread-safe and async-friendly operations.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import logging
import sqlite3
from pathlib import Path
from typing import Generator, List

logger = logging.getLogger(__name__)


class SubscribersDB:
    """Manages the SQLite database of subscribed Telegram chat IDs."""

    def __init__(self, db_path: str | Path = "subscribers.db") -> None:
        """Initialize the database path and ensure tables exist."""
        self.db_path = Path(db_path)
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Create, configure, and safely close a database connection."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create the subscribers table if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notified_acp (
                    event_id TEXT PRIMARY KEY,
                    notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        logger.info("Initialized subscribers database at %s", self.db_path)

    # Synchronous methods
    def add_subscriber_sync(self, chat_id: int) -> bool:
        """Add a subscriber.

        Returns:
            True if newly added, False if already subscribed.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)",
                (chat_id,),
            )
            is_new = cursor.rowcount > 0
        if is_new:
            logger.info("Registered new subscriber: chat_id=%s", chat_id)
        else:
            logger.debug("Subscriber already exists: chat_id=%s", chat_id)
        return is_new

    def remove_subscriber_sync(self, chat_id: int) -> bool:
        """Remove a subscriber.

        Returns:
            True if removed, False if was not subscribed.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            was_removed = cursor.rowcount > 0
        if was_removed:
            logger.info("Removed subscriber: chat_id=%s", chat_id)
        else:
            logger.debug("Subscriber not found for removal: chat_id=%s", chat_id)
        return was_removed

    def get_all_subscribers_sync(self) -> List[int]:
        """Retrieve all active subscriber chat IDs."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT chat_id FROM subscribers ORDER BY created_at ASC")
            rows = cursor.fetchall()
        return [row[0] for row in rows]

    def is_subscribed_sync(self, chat_id: int) -> bool:
        """Check if a chat ID is subscribed."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT 1 FROM subscribers WHERE chat_id = ? LIMIT 1", (chat_id,))
            return cursor.fetchone() is not None

    def get_count_sync(self) -> int:
        """Return total subscriber count."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM subscribers")
            row = cursor.fetchone()
            return row[0] if row else 0

    def is_acp_notified_sync(self, event_id: str) -> bool:
        """Check if an ACP event ID was already broadcast."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT 1 FROM notified_acp WHERE event_id = ?", (str(event_id),))
            return cursor.fetchone() is not None

    def mark_acp_notified_sync(self, event_id: str) -> None:
        """Record an ACP event ID as notified."""
        with self._get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO notified_acp (event_id) VALUES (?)", (str(event_id),))

    def get_all_notified_acp_sync(self) -> List[str]:
        """Return all previously notified ACP event IDs."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT event_id FROM notified_acp")
            return [row[0] for row in cursor.fetchall()]

    def prune_old_acp_sync(self, hours: int = 24) -> int:
        """Prune ACP event IDs older than specified hours."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"DELETE FROM notified_acp WHERE notified_at < datetime('now', '-{hours} hours')"
            )
            return cursor.rowcount

    # Asynchronous non-blocking wrappers
    async def add_subscriber(self, chat_id: int) -> bool:
        """Async wrapper for add_subscriber_sync."""
        return await asyncio.to_thread(self.add_subscriber_sync, chat_id)

    async def remove_subscriber(self, chat_id: int) -> bool:
        """Async wrapper for remove_subscriber_sync."""
        return await asyncio.to_thread(self.remove_subscriber_sync, chat_id)

    async def get_all_subscribers(self) -> List[int]:
        """Async wrapper for get_all_subscribers_sync."""
        return await asyncio.to_thread(self.get_all_subscribers_sync)

    async def is_subscribed(self, chat_id: int) -> bool:
        """Async wrapper for is_subscribed_sync."""
        return await asyncio.to_thread(self.is_subscribed_sync, chat_id)

    async def get_count(self) -> int:
        """Async wrapper for get_count_sync."""
        return await asyncio.to_thread(self.get_count_sync)
