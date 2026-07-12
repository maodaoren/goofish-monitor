"""Playwright-based Xianyu search provider.
Adapted from astrbot_plugin_goofish_catcher (MIT-licensed portions).
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
    Error as PlaywrightError,
)

from .config import config
from .types import Item

logger = logging.getLogger("goofish-monitor")


class PlaywrightProvider:
    """Xianyu search via Playwright browser automation."""
    
    def __init__(self):
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page = None
        self._lock = asyncio.Lock()
        self._ready = False
        self._auth_required = False
    
    async def start(self):
        """Launch browser with persistent context."""
        if self._ready:
            return
        
        self._pw = await async_playwright().start()
        
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-proxy-server",
        ]
        
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=config.browser_profile_dir,
            headless=config.headless,
            args=launch_args,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        
        # Attach page state watchers
        self._context.on("page", self._on_page)
        
        # Try to load existing page or create new
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()
        
        # Block assets if configured
        if config.block_assets:
            await self._page.route("**/*", self._route_handler)
        
        self._ready = True
        logger.info("Browser started with profile: %s", config.browser_profile_dir)
    
    async def stop(self):
        """Close browser."""
        if self._context:
            await self._context.close()
        if self._pw:
            await self._pw.stop()
        self._ready = False
    
    async def _route_handler(self, route):
        """Block static assets to speed up loading."""
        if route.request.resource_type in ("image", "stylesheet", "font", "media"):
            await route.abort()
        else:
            await route.continue_()
    
    def _on_page(self, page):
        """Watch page for auth/captcha issues."""
        page.on("response", self._on_response)
    
    async def _on_response(self, response):
        """Monitor responses for auth/captcha markers."""
        url = response.url
        if "passport" in url or "login" in url:
            self._auth_required = True
            logger.warning("Auth redirect detected: %s", url[:100])
        if "captcha" in url.lower():
            self._auth_required = True
            logger.warning("Captcha detected: %s", url[:100])
    
    async def ensure_logged_in(self) -> bool:
        """Check if logged in, trigger QR login if not."""
        if self._auth_required:
            return False
        
        try:
            await self._page.goto("https://www.goofish.com/", timeout=15000)
            await self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            await asyncio.sleep(2)
            
            # Check for login wall
            content = await self._page.content()
            if "passport.goofish.com" in content or "alibaba-login-box" in content:
                logger.warning("Login required - capturing QR code")
                self._auth_required = True
                # Try to capture QR code
                await self.capture_login_qr()
                return False
            
            # Check if we can access search results
            await self._page.goto("https://www.goofish.com/search?q=test", timeout=15000)
            await self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            await asyncio.sleep(2)
            
            content = await self._page.content()
            if "passport.goofish.com" in content:
                logger.warning("Login required for search")
                self._auth_required = True
                await self.capture_login_qr()
                return False
            
            # Try quick login
            for selector in ["text=快速进入", "text=快速登录", "text=一键登录"]:
                try:
                    btn = self._page.locator(selector)
                    if await btn.count() > 0:
                        await btn.first.click()
                        await self._page.wait_for_load_state("networkidle", timeout=5000)
                        logger.info("Quick login successful")
                        await self._persist_state()
                        return True
                except Exception:
                    continue
            
            logger.info("Login state OK")
            await self._persist_state()
            return True
            
        except Exception as e:
            logger.error("Login check failed: %s", e)
            return False
    
    async def _persist_state(self):
        """Save browser state (cookies, localStorage)."""
        try:
            state = await self._context.storage_state()
            Path(config.storage_state_path).write_text(json.dumps(state))
            logger.info("Browser state saved")
        except Exception as e:
            logger.warning("Failed to save state: %s", e)
    
    async def search(self, keyword: str, pages: int = 3,
                     min_price: float = 0, max_price: float = 999999) -> List[Item]:
        """Search Xianyu for items matching keyword."""
        if not self._ready:
            await self.start()
        
        if self._auth_required:
            logged_in = await self.ensure_logged_in()
            if not logged_in:
                raise RuntimeError("AUTH_REQUIRED")
        
        async with self._lock:
            items = []
            for page_num in range(1, pages + 1):
                try:
                    page_items = await self._search_page(
                        keyword, page_num, min_price, max_price
                    )
                    items.extend(page_items)
                    
                    if page_num < pages:
                        await asyncio.sleep(1.5)  # Polite delay
                    
                except PlaywrightTimeout:
                    logger.warning("Timeout on page %d", page_num)
                    break
                except Exception as e:
                    logger.error("Search error on page %d: %s", page_num, e)
                    if "captcha" in str(e).lower() or "auth" in str(e).lower():
                        self._auth_required = True
                        raise RuntimeError("AUTH_REQUIRED")
                    break
            
            # Deduplicate by item_id
            seen = set()
            unique = []
            for item in items:
                if item.item_id not in seen:
                    seen.add(item.item_id)
                    unique.append(item)
            
            return unique
    
    async def _search_page(self, keyword: str, page_num: int,
                           min_price: float, max_price: float) -> List[Item]:
        """Search a single page."""
        url = f"https://www.goofish.com/search?q={keyword}&page={page_num}"
        if min_price > 0:
            url += f"&priceLower={min_price}"
        if max_price < 999999:
            url += f"&priceUpper={max_price}"
        
        logger.info("Navigating to: %s", url)
        await self._page.goto(url, timeout=config.browser_timeout)
        await self._page.wait_for_load_state("domcontentloaded", timeout=10000)
        
        # Wait for content to load
        await asyncio.sleep(3)
        
        # Check current URL
        current_url = self._page.url
        logger.info("Current URL: %s", current_url)
        
        # Check for login wall
        content = await self._page.content()
        if "passport.goofish.com" in content:
            logger.warning("Login wall detected on search page")
            self._auth_required = True
            return []
        
        # Try to extract items from page
        items = await self._extract_items()
        logger.info("Extracted %d items", len(items))
        
        return items
    
    async def _extract_items(self) -> List[Item]:
        """Extract items from current page."""
        items = []
        
        logger.info("Starting item extraction...")
        
        # Method 1: Extract from JSON API responses (intercepted)
        # Method 2: Extract from DOM
        try:
            # Find all item links
            item_links = await self._page.query_selector_all("a[href*='/item/']")
            logger.info("Found %d links with /item/", len(item_links))
            
            for link in item_links:
                try:
                    href = await link.get_attribute("href")
                    if not href or "/item/" not in href:
                        continue
                    
                    # Extract item_id from URL
                    match = re.search(r"/item/(\d+)", href)
                    if not match:
                        continue
                    item_id = match.group(1)
                    
                    # Get title and price
                    title = await link.inner_text()
                    title = title.strip()[:200]  # Limit length
                    
                    # Try to find price near the link
                    price = 0
                    parent = await link.evaluate("el => el.closest('[class*=item]') || el.parentElement")
                    if parent:
                        price_el = await self._page.query_selector(
                            f"[class*=item] >> text=/¥\\d/"
                        )
                        if price_el:
                            price_text = await price_el.inner_text()
                            price_match = re.search(r"(\d+\.?\d*)", price_text)
                            if price_match:
                                price = float(price_match.group(1))
                    
                    if item_id and title:
                        item_url = href if href.startswith("http") else f"https://www.goofish.com{href}"
                        items.append(Item(
                            item_id=item_id,
                            title=title,
                            price=price,
                            url=item_url,
                        ))
                        
                except Exception as e:
                    continue
            
        except Exception as e:
            logger.error("DOM extraction failed: %s", e)
        
        return items
    
    def get_login_qr_path(self) -> Optional[str]:
        """Get path to QR code screenshot for login."""
        qr_path = Path(config.data_dir) / "login_qr.png"
        if qr_path.exists():
            return str(qr_path)
        return None
    
    async def capture_login_qr(self) -> Optional[str]:
        """Navigate to login page and capture QR code screenshot."""
        if not self._ready:
            await self.start()
        
        try:
            # Navigate to search which triggers login wall
            await self._page.goto(
                "https://www.goofish.com/search?q=test",
                timeout=config.browser_timeout
            )
            await self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            await asyncio.sleep(2)
            
            # Look for QR code
            qr_selectors = [
                "img[src*='qrcode']",
                "img[src*='qr']",
                "[class*=qrcode] img",
                "[class*=QRCode] img",
                "#J_QRCodeImg img",
            ]
            
            for selector in qr_selectors:
                try:
                    qr_el = self._page.locator(selector)
                    if await qr_el.count() > 0:
                        qr_path = Path(config.data_dir) / "login_qr.png"
                        await qr_el.first.screenshot(path=str(qr_path))
                        logger.info("QR code captured: %s", qr_path)
                        return str(qr_path)
                except Exception:
                    continue
            
            # Fallback: screenshot the whole page
            qr_path = Path(config.data_dir) / "login_page.png"
            await self._page.screenshot(path=str(qr_path))
            logger.info("Login page screenshot: %s", qr_path)
            return str(qr_path)
            
        except Exception as e:
            logger.error("Failed to capture QR: %s", e)
            return None
    
    async def wait_for_login(self, timeout: int = 60) -> bool:
        """Wait for user to scan QR code and login."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                content = await self._page.content()
                # Check if login wall is gone
                if "passport.goofish.com" not in content and "alibaba-login-box" not in content:
                    logger.info("Login successful!")
                    await self._persist_state()
                    self._auth_required = False
                    return True
                await asyncio.sleep(2)
            except Exception:
                await asyncio.sleep(2)
        
        logger.warning("Login timeout after %ds", timeout)
        return False


# Singleton
provider = PlaywrightProvider()
