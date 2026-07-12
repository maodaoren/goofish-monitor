"""Xianyu Monitor - Standalone service for Hermes integration."""
from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.config import config
from app.storage import storage
from app.notifier import notifier
from app.provider import provider
from app.scheduler import scheduler
from app.qr_handler import setup_qr_routes

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger("goofish-monitor")


# ── Lifespan ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    config.ensure_dirs()
    await storage.init()
    await notifier.init()
    
    # Start browser
    try:
        await provider.start()
        logged_in = await provider.ensure_logged_in()
        if logged_in:
            logger.info("✅ 浏览器已启动，登录状态正常")
        else:
            logger.warning("⚠️ 浏览器已启动，需要扫码登录")
    except Exception as e:
        logger.error("❌ 浏览器启动失败: %s", e)
    
    # Start scheduler
    await scheduler.start()
    
    logger.info("Goofish Monitor started. Data dir: %s", config.data_dir)
    yield
    
    # Shutdown
    await scheduler.stop()
    await notifier.close()
    await storage.close()
    logger.info("Goofish Monitor stopped.")


app = FastAPI(
    title="Xianyu Monitor",
    description="闲鱼关键词监控 + 好价推送",
    version="0.1.0",
    lifespan=lifespan
)

# Setup QR code routes
setup_qr_routes(app)


# ── Models ──

class SubscriptionCreate(BaseModel):
    keyword: str
    min_price: float = 0
    max_price: float = 999999
    interval_sec: int = 600
    pages: int = 3
    drop_abs: float = 50
    drop_pct: float = 5.0


class SubscriptionResponse(BaseModel):
    id: int
    keyword: str
    min_price: float
    max_price: float
    interval_sec: int
    enabled: bool
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None


# ── Subscription API ──

@app.get("/api/subscriptions")
async def list_subscriptions():
    """List all subscriptions."""
    subs = await storage.get_subscriptions(enabled_only=False)
    return [
        SubscriptionResponse(
            id=s.id, keyword=s.keyword, min_price=s.min_price,
            max_price=s.max_price, interval_sec=s.interval_sec,
            enabled=s.enabled,
            last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
            next_run_at=s.next_run_at.isoformat() if s.next_run_at else None,
        )
        for s in subs
    ]


@app.post("/api/subscriptions", response_model=SubscriptionResponse)
async def create_subscription(req: SubscriptionCreate):
    """Create a new subscription."""
    from app.types import Subscription
    sub = Subscription(
        keyword=req.keyword,
        min_price=req.min_price,
        max_price=req.max_price,
        interval_sec=req.interval_sec,
        pages=req.pages,
        drop_abs=req.drop_abs,
        drop_pct=req.drop_pct,
    )
    sub_id = await storage.add_subscription(sub)
    sub.id = sub_id
    return SubscriptionResponse(
        id=sub_id, keyword=sub.keyword, min_price=sub.min_price,
        max_price=sub.max_price, interval_sec=sub.interval_sec,
        enabled=True,
    )


@app.delete("/api/subscriptions/{sub_id}")
async def delete_subscription(sub_id: int):
    """Delete a subscription."""
    await storage._db.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    await storage._db.commit()
    return {"ok": True}


# ── Search API ──

@app.get("/api/search/{keyword}")
async def search_items(keyword: str, pages: int = 1):
    """Search Xianyu for items."""
    try:
        items = await provider.search(keyword, pages=pages)
        return [
            {
                "item_id": item.item_id,
                "title": item.title,
                "price": item.price,
                "url": item.detail_url,
                "seller": item.seller_name,
                "location": item.location,
            }
            for item in items
        ]
    except RuntimeError as e:
        if str(e) == "AUTH_REQUIRED":
            raise HTTPException(status_code=401, detail="需要重新登录")
        raise HTTPException(status_code=500, detail=str(e))


# ── Login API ──

@app.post("/api/login")
async def trigger_login():
    """Trigger QR code login - captures QR screenshot."""
    qr_path = await provider.capture_login_qr()
    if qr_path:
        return {
            "status": "waiting",
            "message": "请扫描二维码登录",
            "qr_path": qr_path,
        }
    return {"status": "error", "message": "无法获取二维码"}


@app.post("/api/login/wait")
async def wait_for_login(timeout: int = 60):
    """Wait for QR code scan and login completion."""
    logged_in = await provider.wait_for_login(timeout=timeout)
    return {"logged_in": logged_in}


# ── Login Wait ──

@app.post("/api/login/wait")
async def wait_for_login(timeout: int = 120):
    """Wait for login and check status."""
    logged_in = await provider.wait_for_login(timeout=timeout)
    return {"logged_in": logged_in}


# ── Health ──

@app.get("/health")
async def health():
    """Health check."""
    return {
        "status": "ok",
        "service": "goofish-monitor",
        "browser_ready": provider._ready,
        "auth_required": provider._auth_required,
    }


# ── Main ──

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
