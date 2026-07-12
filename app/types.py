"""Core data models for Xianyu monitoring."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Item:
    """A single Xianyu listing."""
    item_id: str
    title: str
    price: float
    url: str
    image_url: str = ""
    seller_name: str = ""
    seller_credit: int = 0
    location: str = ""
    publish_time: Optional[datetime] = None
    want_count: int = 0
    browse_count: int = 0
    
    @property
    def detail_url(self) -> str:
        if self.url.startswith("http"):
            return self.url
        return f"https://www.goofish.com/item/{self.item_id}"


@dataclass
class Subscription:
    """A keyword subscription."""
    id: Optional[int] = None
    keyword: str = ""
    min_price: float = 0
    max_price: float = float("inf")
    interval_sec: int = 600
    pages: int = 3
    drop_abs: float = 50
    drop_pct: float = 5.0
    enabled: bool = True
    created_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None


@dataclass
class PriceRecord:
    """Price history for an item."""
    item_id: str
    price: float
    recorded_at: Optional[datetime] = None


@dataclass
class Notification:
    """A sent notification."""
    id: Optional[int] = None
    sub_id: int = 0
    item_id: str = ""
    event_type: str = ""  # "new" | "price_drop"
    payload_hash: str = ""
    sent_at: Optional[datetime] = None
