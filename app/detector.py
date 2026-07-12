"""Price drop detection for Xianyu monitor."""
from __future__ import annotations
import hashlib
from datetime import datetime
from typing import Optional, Tuple
from .types import Item, Subscription


def hash_payload(event_type: str, keyword: str, item_id: str, 
                 title: str, price: float, url: str) -> str:
    """Generate SHA-256 hash for deduplication."""
    payload = f"{event_type}:{keyword}:{item_id}:{title}:{price}:{url}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def check_price_drop(
    current_price: float,
    last_price: Optional[float],
    sub: Subscription
) -> Tuple[bool, float, float]:
    """
    Check if price dropped significantly.
    Returns (is_drop, abs_drop, pct_drop).
    """
    if last_price is None or last_price <= 0:
        return False, 0, 0
    
    if current_price >= last_price:
        return False, 0, 0
    
    abs_drop = last_price - current_price
    pct_drop = (abs_drop / last_price) * 100
    
    # Trigger if absolute drop OR percentage drop exceeds threshold
    is_drop = (abs_drop >= sub.drop_abs) or (pct_drop >= sub.drop_pct)
    
    return is_drop, abs_drop, pct_drop


def is_new_item(item: Item, first_seen: Optional[datetime]) -> bool:
    """Check if item is newly published (within 1 hour)."""
    if first_seen is not None:
        return False
    
    if item.publish_time is None:
        # No publish time available, treat as new
        return True
    
    age = (datetime.now() - item.publish_time).total_seconds()
    return age < 3600  # Within 1 hour
