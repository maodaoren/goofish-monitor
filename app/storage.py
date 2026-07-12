"""SQLite storage for Xianyu monitor."""
from __future__ import annotations
import aiosqlite
from datetime import datetime
from typing import List, Optional
from .types import Item, Subscription, Notification
from .config import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    min_price REAL DEFAULT 0,
    max_price REAL DEFAULT 999999,
    interval_sec INTEGER DEFAULT 600,
    pages INTEGER DEFAULT 3,
    drop_abs REAL DEFAULT 50,
    drop_pct REAL DEFAULT 5.0,
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    last_run_at TEXT,
    next_run_at TEXT
);

CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    title TEXT,
    price REAL,
    url TEXT,
    image_url TEXT,
    seller_name TEXT,
    location TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT,
    price REAL,
    recorded_at TEXT,
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id INTEGER,
    item_id TEXT,
    event_type TEXT,
    payload_hash TEXT,
    sent_at TEXT,
    UNIQUE(sub_id, item_id, event_type, payload_hash)
);
"""


class Storage:
    """Async SQLite storage."""
    
    def __init__(self):
        self._db: Optional[aiosqlite.Connection] = None
    
    async def init(self):
        """Initialize database."""
        config.ensure_dirs()
        self._db = await aiosqlite.connect(config.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
    
    async def close(self):
        """Close database."""
        if self._db:
            await self._db.close()
    
    # ── Subscriptions ──
    
    async def add_subscription(self, sub: Subscription) -> int:
        """Add a new subscription."""
        now = datetime.now().isoformat()
        cursor = await self._db.execute(
            """INSERT INTO subscriptions 
               (keyword, min_price, max_price, interval_sec, pages, 
                drop_abs, drop_pct, enabled, created_at, next_run_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sub.keyword, sub.min_price, sub.max_price, sub.interval_sec,
             sub.pages, sub.drop_abs, sub.drop_pct, 1, now, now)
        )
        await self._db.commit()
        return cursor.lastrowid
    
    async def get_subscriptions(self, enabled_only: bool = True) -> List[Subscription]:
        """Get all subscriptions."""
        query = "SELECT * FROM subscriptions"
        if enabled_only:
            query += " WHERE enabled = 1"
        async with self._db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_subscription(row) for row in rows]
    
    async def get_due_subscriptions(self) -> List[Subscription]:
        """Get subscriptions that are due for polling."""
        now = datetime.now().isoformat()
        async with self._db.execute(
            "SELECT * FROM subscriptions WHERE enabled = 1 AND next_run_at <= ?",
            (now,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_subscription(row) for row in rows]
    
    async def update_subscription_run(self, sub_id: int):
        """Update last_run_at and next_run_at."""
        now = datetime.now()
        sub = await self._get_subscription(sub_id)
        if sub:
            next_run = datetime.now().timestamp() + sub.interval_sec
            from datetime import timedelta
            next_dt = (now + timedelta(seconds=sub.interval_sec)).isoformat()
            await self._db.execute(
                "UPDATE subscriptions SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                (now.isoformat(), next_dt, sub_id)
            )
            await self._db.commit()
    
    async def _get_subscription(self, sub_id: int) -> Optional[Subscription]:
        async with self._db.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return self._row_to_subscription(row) if row else None
    
    def _row_to_subscription(self, row) -> Subscription:
        return Subscription(
            id=row["id"],
            keyword=row["keyword"],
            min_price=row["min_price"],
            max_price=row["max_price"],
            interval_sec=row["interval_sec"],
            pages=row["pages"],
            drop_abs=row["drop_abs"],
            drop_pct=row["drop_pct"],
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            last_run_at=datetime.fromisoformat(row["last_run_at"]) if row["last_run_at"] else None,
            next_run_at=datetime.fromisoformat(row["next_run_at"]) if row["next_run_at"] else None,
        )
    
    # ── Items ──
    
    async def upsert_item(self, item: Item) -> bool:
        """Insert or update item. Returns True if new."""
        now = datetime.now().isoformat()
        existing = await self._get_item(item.item_id)
        
        if existing:
            await self._db.execute(
                "UPDATE items SET price = ?, last_seen_at = ? WHERE item_id = ?",
                (item.price, now, item.item_id)
            )
            await self._db.commit()
            return False
        else:
            await self._db.execute(
                """INSERT INTO items 
                   (item_id, title, price, url, image_url, seller_name, 
                    location, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item.item_id, item.title, item.price, item.url,
                 item.image_url, item.seller_name, item.location, now, now)
            )
            await self._db.commit()
            return True
    
    async def _get_item(self, item_id: str) -> Optional[Item]:
        async with self._db.execute(
            "SELECT * FROM items WHERE item_id = ?", (item_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return Item(
                    item_id=row["item_id"],
                    title=row["title"],
                    price=row["price"],
                    url=row["url"],
                    image_url=row["image_url"],
                    seller_name=row["seller_name"],
                    location=row["location"],
                )
            return None
    
    async def record_price(self, item_id: str, price: float):
        """Record a price point."""
        await self._db.execute(
            "INSERT INTO price_history (item_id, price, recorded_at) VALUES (?, ?, ?)",
            (item_id, price, datetime.now().isoformat())
        )
        await self._db.commit()
    
    async def get_last_price(self, item_id: str) -> Optional[float]:
        """Get the most recent price for an item."""
        async with self._db.execute(
            "SELECT price FROM price_history WHERE item_id = ? ORDER BY recorded_at DESC LIMIT 1",
            (item_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["price"] if row else None
    
    # ── Notifications ──
    
    async def was_notified(self, sub_id: int, item_id: str, event_type: str, payload_hash: str) -> bool:
        """Check if notification was already sent."""
        async with self._db.execute(
            "SELECT 1 FROM notifications WHERE sub_id = ? AND item_id = ? AND event_type = ? AND payload_hash = ?",
            (sub_id, item_id, event_type, payload_hash)
        ) as cursor:
            return await cursor.fetchone() is not None
    
    async def mark_notified(self, sub_id: int, item_id: str, event_type: str, payload_hash: str):
        """Mark notification as sent."""
        await self._db.execute(
            "INSERT OR IGNORE INTO notifications (sub_id, item_id, event_type, payload_hash, sent_at) VALUES (?, ?, ?, ?, ?)",
            (sub_id, item_id, event_type, payload_hash, datetime.now().isoformat())
        )
        await self._db.commit()


# Singleton
storage = Storage()
