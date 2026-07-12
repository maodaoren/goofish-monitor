"""Scheduler for Xianyu monitor - polls subscriptions."""
from __future__ import annotations
import asyncio
import logging
import hashlib
from datetime import datetime, timedelta
from typing import List

from .config import config
from .storage import storage
from .provider import provider
from .notifier import notifier
from .detector import check_price_drop, hash_payload
from .types import Subscription, Item

logger = logging.getLogger("goofish-monitor")


class Scheduler:
    """Polling scheduler for subscriptions."""
    
    def __init__(self):
        self._running = False
        self._task: asyncio.Task = None
    
    async def start(self):
        """Start the polling loop."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Scheduler started")
    
    async def stop(self):
        """Stop the polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await provider.stop()
        logger.info("Scheduler stopped")
    
    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                # Get subscriptions due for polling
                due_subs = await storage.get_due_subscriptions()
                
                # Skip polling if login session is active (don't disturb the browser)
                if provider._login_session_active:
                    logger.debug("Login session active, skipping poll")
                    await asyncio.sleep(config.poll_interval)
                    continue
                
                if due_subs:
                    logger.info("Polling %d subscriptions", len(due_subs))
                    
                    for sub in due_subs:
                        try:
                            await self._poll_subscription(sub)
                        except Exception as e:
                            logger.error("Error polling '%s': %s", sub.keyword, e)
                
                # Wait before next cycle
                await asyncio.sleep(config.poll_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Poll loop error: %s", e)
                await asyncio.sleep(30)
    
    async def _poll_subscription(self, sub: Subscription):
        """Poll a single subscription."""
        logger.info("Polling: %s", sub.keyword)
        
        try:
            # Search for items
            items = await provider.search(
                keyword=sub.keyword,
                pages=sub.pages,
                min_price=sub.min_price,
                max_price=sub.max_price,
            )
            
            logger.info("Found %d items for '%s'", len(items), sub.keyword)
            
            # Process items
            for item in items:
                await self._process_item(sub, item)
            
            # Update run time
            await storage.update_subscription_run(sub.id)
            
        except RuntimeError as e:
            if str(e) == "AUTH_REQUIRED":
                logger.warning("Auth required for subscription %d", sub.id)
                await notifier.notify_error("AUTH_REQUIRED", 
                    f"订阅 '{sub.keyword}' 需要重新登录")
            else:
                raise
    
    async def _process_item(self, sub: Subscription, item: Item):
        """Process a single item - check for new/price drop."""
        # Upsert item (returns True if new)
        is_new = await storage.upsert_item(item)
        
        # Record price
        await storage.record_price(item.item_id, item.price)
        
        # Check for new item
        if is_new:
            payload_hash = hash_payload("new", sub.keyword, item.item_id, 
                                        item.title, item.price, item.url)
            
            if not await storage.was_notified(sub.id, item.item_id, "new", payload_hash):
                await notifier.notify_new_item(sub, item)
                await storage.mark_notified(sub.id, item.item_id, "new", payload_hash)
                logger.info("New item: %s - ¥%.0f", item.title[:30], item.price)
        
        # Check for price drop
        else:
            last_price = await storage.get_last_price(item.item_id)
            if last_price is not None and item.price < last_price:
                is_drop, abs_drop, pct_drop = check_price_drop(
                    item.price, last_price, sub
                )
                
                if is_drop:
                    payload_hash = hash_payload("price_drop", sub.keyword, item.item_id,
                                                item.title, item.price, item.url)
                    
                    if not await storage.was_notified(sub.id, item.item_id, "price_drop", payload_hash):
                        await notifier.notify_price_drop(
                            sub, item, last_price, item.price, abs_drop, pct_drop
                        )
                        await storage.mark_notified(sub.id, item.item_id, "price_drop", payload_hash)
                        logger.info("Price drop: %s ¥%.0f→¥%.0f", 
                                   item.title[:30], last_price, item.price)


# Singleton
scheduler = Scheduler()
