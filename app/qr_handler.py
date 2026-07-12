"""QR code serving and login notification."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse

from .config import config
from .provider import provider
from .notifier import notifier

logger = logging.getLogger("goofish-monitor")


async def notify_login_required():
    """Capture QR code and push to QQ."""
    qr_path = await provider.capture_login_qr()
    if qr_path:
        # Send QR code via webhook (as text for now, image support TODO)
        msg = "🔐 闲鱼登录\n"
        msg += "━━━━━━━━━━━━━━━━━━━\n"
        msg += "需要扫码登录闲鱼账号\n"
        msg += f"QR 截图已保存到: {qr_path}\n"
        msg += "请在手机上打开闲鱼 App 扫码\n"
        msg += "扫码后回复任意消息继续"
        
        await notifier.send(msg)
        logger.info("Login notification sent")
        return True
    return False


def setup_qr_routes(app: FastAPI):
    """Add QR code serving routes."""
    
    @app.get("/login/qr")
    async def get_qr_code():
        """Serve the QR code image."""
        qr_path = Path(config.data_dir) / "login_qr.png"
        if qr_path.exists():
            return FileResponse(str(qr_path), media_type="image/png")
        
        # Try to capture
        path = await provider.capture_login_qr()
        if path:
            return FileResponse(path, media_type="image/png")
        
        return {"error": "QR code not available"}
    
    @app.get("/login/page")
    async def get_login_page():
        """Serve the full login page screenshot."""
        page_path = Path(config.data_dir) / "login_page.png"
        if page_path.exists():
            return FileResponse(str(page_path), media_type="image/png")
        return {"error": "Login page screenshot not available"}
