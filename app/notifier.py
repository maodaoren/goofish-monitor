"""Hermes webhook notifier for Xianyu monitor."""
from __future__ import annotations
import hashlib
import hmac
import json
import time
import httpx
from typing import Optional
from .config import config
from .types import Item, Subscription


class Notifier:
    """Send notifications via Hermes webhook."""
    
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
    
    async def init(self):
        self._client = httpx.AsyncClient(timeout=10)
    
    async def close(self):
        if self._client:
            await self._client.aclose()
    
    def _sign(self, payload: bytes, timestamp: str) -> str:
        """Generate HMAC-SHA256 V2 signature."""
        signed_data = timestamp.encode() + b"." + payload
        return hmac.new(
            config.webhook_secret.encode(),
            signed_data,
            hashlib.sha256
        ).hexdigest()
    
    async def send(self, message: str) -> bool:
        """Send message via webhook."""
        if not self._client:
            await self.init()
        
        payload = json.dumps({"message": message}).encode()
        timestamp = str(int(time.time()))
        signature = self._sign(payload, timestamp)
        
        try:
            resp = await self._client.post(
                config.webhook_url,
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Timestamp": timestamp,
                    "X-Webhook-Signature-V2": signature,
                },
            )
            return resp.status_code == 200
        except Exception as e:
            print(f"[notifier] webhook error: {e}")
            return False
    
    async def notify_new_item(self, sub: Subscription, item: Item) -> bool:
        """Notify about a new item."""
        msg = f"🆕 闲鱼新上架\n"
        msg += "━━━━━━━━━━━━━━━━━━━\n"
        msg += f"🔍 {sub.keyword}\n"
        msg += f"📦 {item.title}\n"
        msg += f"💰 ¥{item.price:.0f}\n"
        if item.seller_name:
            msg += f"👤 {item.seller_name}\n"
        if item.location:
            msg += f"📍 {item.location}\n"
        msg += f"🔗 {item.detail_url}\n"
        
        return await self.send(msg)
    
    async def notify_price_drop(
        self, sub: Subscription, item: Item,
        old_price: float, new_price: float,
        drop_abs: float, drop_pct: float
    ) -> bool:
        """Notify about a price drop."""
        msg = f"📉 闲鱼降价提醒\n"
        msg += "━━━━━━━━━━━━━━━━━━━\n"
        msg += f"🔍 {sub.keyword}\n"
        msg += f"📦 {item.title}\n"
        msg += f"💰 ¥{old_price:.0f} → ¥{new_price:.0f}\n"
        msg += f"🔻 降了 ¥{drop_abs:.0f} ({drop_pct:.1f}%)\n"
        if item.seller_name:
            msg += f"👤 {item.seller_name}\n"
        msg += f"🔗 {item.detail_url}\n"
        
        return await self.send(msg)
    
    async def notify_error(self, error_code: str, message: str) -> bool:
        """Notify about an error."""
        msg = f"⚠️ 闲鱼监控异常\n"
        msg += "━━━━━━━━━━━━━━━━━━━\n"
        msg += f"❌ {error_code}\n"
        msg += f"📝 {message}\n"
        
        return await self.send(msg)


# Singleton
notifier = Notifier()
