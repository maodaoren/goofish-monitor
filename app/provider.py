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
        self._login_sessions: dict = {}
    
    async def start(self):
        """Launch browser with persistent context."""
        if self._ready:
            return
        
        self._pw = await async_playwright().start()
        
        # Start Xvfb for headed browser (bypasses headless detection)
        import subprocess, os
        try:
            subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1280x800x24"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            os.environ["DISPLAY"] = ":99"
            await asyncio.sleep(1)
            headless = False
            logger.info("Using headed browser with Xvfb display :99")
        except Exception as e:
            logger.warning("Xvfb failed, falling back to headless: %s", e)
            headless = config.headless
        
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--no-proxy-server",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]
        
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=config.browser_profile_dir,
            headless=headless,
            args=launch_args,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        
        # Attach page state watchers
        self._context.on("page", self._on_page)
        
        # Try to load existing page or create new
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()
        
        # Add stealth scripts to every page
        await self._context.add_init_script("""
            // Override webdriver property
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            
            // Override chrome property
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };
            
            // Override permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            
            // Override plugins length
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            
            // Override languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['zh-CN', 'zh', 'en']
            });
        """)
        
        # Intercept JSON API responses for item data
        self._api_items = []
        
        async def _on_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                payload = await response.json()
                if not isinstance(payload, dict):
                    return
                # Log API calls
                if "mtop" in url:
                    logger.debug("API: %s", url[:100])
                # Search for item data in nested structures
                before = len(self._api_items)
                self._extract_items_from_payload(payload)
                after = len(self._api_items)
                if after > before:
                    logger.info("Found %d items from API: %s", after - before, url[:80])
            except Exception:
                pass
        
        self._page.on("response", _on_response)
        
        # Block assets if configured
        if config.block_assets:
            await self._page.route("**/*", self._route_handler)
        
        # Load saved cookies from storage_state.json if available
        storage_path = Path(config.storage_state_path)
        if storage_path.exists():
            try:
                import json as _json
                state = _json.loads(storage_path.read_text())
                cookies = state.get("cookies", [])
                if cookies:
                    await self._context.add_cookies(cookies)
                    logger.info("Loaded %d cookies from %s", len(cookies), storage_path)
            except Exception as e:
                logger.warning("Failed to load storage state: %s", e)
        
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
    
    # ── Login Session Management (ref: original project) ──
    
    _login_session_active = False
    _login_session_id = None
    
    async def start_login_session(self) -> dict:
        """Start login session using main browser.
        
        Navigates main browser to search page (triggers login wall),
        captures screenshot, returns session_id for confirm step.
        """
        import uuid
        
        try:
            if not self._ready:
                await self.start()
            
            # Navigate main browser to search page → triggers login wall
            await self._page.goto(
                "https://www.goofish.com/search?q=%E9%97%B2%E9%B1%BC",
                wait_until="domcontentloaded",
                timeout=30000
            )
            await self._page.wait_for_timeout(3000)
            
            # Capture screenshot (login wall with QR code)
            import base64
            screenshot_bytes = await self._page.screenshot(type="jpeg", quality=70)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
            
            session_id = uuid.uuid4().hex
            self._login_sessions[session_id] = {
                "created_at": time.time(),
                "status": "waiting",
            }
            self._auth_required = True
            self._login_session_active = True
            self._login_session_id = session_id
            
            # Pause scheduler so it doesn't navigate browser during login
            from .scheduler import scheduler
            scheduler.pause()
            
            logger.info("Login session started (using main browser): %s", session_id)
            
            return {
                "ok": True,
                "session_id": session_id,
                "screenshot_base64": screenshot_b64,
                "page_url": self._page.url,
                "timeout_sec": 120,
            }
            
        except Exception as e:
            logger.error("Failed to start login session: %s", e)
            return {"ok": False, "error": str(e)}
    
    async def confirm_login(self, session_id: str) -> dict:
        """Confirm login after user scans QR code.
        
        Waits for page to settle, checks main page AND all iframes
        for login success markers.
        """
        if session_info := self._login_sessions.get(session_id):
            session_info["status"] = "confirming"
        
        try:
            if not self._ready:
                return {"ok": False, "error": "Browser not ready"}
            
            # Wait for page to settle after QR scan
            await self._page.wait_for_timeout(5000)
            
            page_url = self._page.url
            logger.info("Confirm: current URL = %s", page_url)
            
            # Check 1: If already redirected away from login, we're good
            if "passport" not in page_url:
                logger.info("Confirm: redirected away from passport, checking content...")
                content = await self._page.content()
                if "passport.goofish.com" not in content:
                    return await self._save_login(session_id, "redirect")
            
            # Check 2: Check all frames for login API success markers
            for frame in self._page.frames:
                frame_url = str(getattr(frame, "url", "") or "")
                if any(marker in frame_url for marker in [
                    "mtop.taobao.idlemessage.pc.loginuser.get",
                    "mtop.idle.web.user.page.nav",
                ]):
                    logger.info("Confirm: found login API in frame: %s", frame_url[:80])
                    return await self._save_login(session_id, "api_marker")
            
            # Check 3: Try navigating to search page
            logger.info("Confirm: navigating to search to verify...")
            await self._page.goto(
                "https://www.goofish.com/search?q=test",
                wait_until="domcontentloaded",
                timeout=15000
            )
            await self._page.wait_for_timeout(3000)
            
            content = await self._page.content()
            page_url = self._page.url
            logger.info("Confirm: search URL = %s, has passport = %s", page_url, "passport.goofish.com" in content)
            
            if "passport.goofish.com" not in content:
                return await self._save_login(session_id, "search_verify")
            
            return {"ok": False, "error": "Login not successful", "url": page_url}
            
        except Exception as e:
            logger.error("Login confirmation failed: %s", e)
            try:
                from .scheduler import scheduler
                scheduler.resume()
            except:
                pass
            return {"ok": False, "error": str(e)}
    
    async def _save_login(self, session_id: str, method: str) -> dict:
        """Save login state and cleanup."""
        try:
            storage_state = await self._context.storage_state()
            storage_path = Path(config.storage_state_path)
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            storage_path.write_text(json.dumps(storage_state))
            
            self._login_sessions.pop(session_id, None)
            self._auth_required = False
            self._login_session_active = False
            self._login_session_id = None
            
            from .scheduler import scheduler
            scheduler.resume()
            
            logger.info("Login saved via %s", method)
            return {"ok": True, "status": "saved", "method": method}
        except Exception as e:
            logger.error("Failed to save login: %s", e)
            return {"ok": False, "error": str(e)}
    
    async def check_login_status(self) -> dict:
        """Check if main browser is logged in."""
        if not self._ready:
            return {"logged_in": False, "reason": "browser not ready"}
        
        try:
            await self._page.goto(
                "https://www.goofish.com/search?q=test",
                timeout=15000
            )
            await self._page.wait_for_timeout(2000)
            
            content = await self._page.content()
            if "passport.goofish.com" in content:
                return {"logged_in": False, "reason": "login wall detected"}
            
            return {"logged_in": True}
            
        except Exception as e:
            return {"logged_in": False, "reason": str(e)}
    
    def _extract_items_from_payload(self, data, depth=0):
        """Extract items from Xianyu API response.
        
        Handles two structures:
        1. resultList[].data.item.main - PC search API format
        2. Generic nested dict with title/itemId/price keys
        """
        if depth > 10 or len(self._api_items) > 200:
            return
        
        if isinstance(data, dict):
            # Structure 1: Xianyu PC search API - resultList format
            if "resultList" in data and isinstance(data["resultList"], list):
                for entry in data["resultList"]:
                    self._extract_search_item(entry)
                return
            
            # Structure 2: Generic recursive search
            # Check if this dict looks like an item
            title = None
            item_id = None
            price = None
            url = None
            
            for key in ("title", "item_title", "name", "itemName", "subject"):
                val = data.get(key)
                if isinstance(val, str) and len(val) > 3:
                    title = val
                    break
            
            for key in ("item_id", "itemId", "id", "auctionId", "targetId"):
                val = data.get(key)
                if val:
                    item_id = str(val)
                    break
            
            for key in ("price", "price_text", "priceText", "soldPrice"):
                val = data.get(key)
                if val is not None:
                    try:
                        p = str(val).replace("¥", "").replace(",", "").strip()
                        if p:
                            price = float(p)
                    except (ValueError, TypeError):
                        pass
                    if price is not None:
                        break
            
            for key in ("url", "item_url", "detail_url", "jumpUrl"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    url = val
                    break
            
            if title and item_id and price is not None:
                if not url:
                    url = f"https://www.goofish.com/item?id={item_id}"
                elif not url.startswith("http"):
                    url = f"https://www.goofish.com{url}"
                self._api_items.append(Item(
                    item_id=item_id, title=title, price=price, url=url
                ))
            
            # Recurse into values
            for v in data.values():
                if isinstance(v, (dict, list)):
                    self._extract_items_from_payload(v, depth + 1)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    self._extract_items_from_payload(item, depth + 1)
    
    def _extract_search_item(self, entry):
        """Extract a single item from Xianyu search resultList entry."""
        try:
            if not isinstance(entry, dict):
                return
            item_data = entry.get("data", {}).get("item", {})
            if not item_data:
                return
            
            main = item_data.get("main", {})
            ex = main.get("exContent", {})
            
            # Get item ID
            item_id = str(ex.get("itemId", "")).strip()
            if not item_id:
                return
            
            # Get title from detailParams
            detail_params = ex.get("detailParams", {})
            title = detail_params.get("title", "")
            if not title:
                title = ex.get("richTitle", "")
            if not title:
                return
            
            # Get price
            price = None
            click_args = main.get("clickParam", {}).get("args", {})
            price_str = click_args.get("price", "")
            if price_str:
                try:
                    price = float(str(price_str).replace("¥", "").replace(",", "").strip())
                except (ValueError, TypeError):
                    pass
            
            if price is None:
                # Try oriPrice from exContent
                ori_price = ex.get("oriPrice", "")
                if ori_price:
                    try:
                        price = float(str(ori_price).replace("¥", "").replace(",", "").strip())
                    except (ValueError, TypeError):
                        pass
            
            if price is None:
                price = 0
            
            # Get URL
            target_url = main.get("targetUrl", "")
            if target_url and not target_url.startswith("http"):
                url = f"https://www.goofish.com/item?id={item_id}"
            else:
                url = f"https://www.goofish.com/item?id={item_id}"
            
            self._api_items.append(Item(
                item_id=item_id,
                title=title,
                price=price,
                url=url,
                seller_name=detail_params.get("userNick", ""),
                location=ex.get("area", ""),
            ))
        except Exception as e:
            logger.debug("Error extracting search item: %s", e)
    
    async def search(self, keyword: str, pages: int = 3,
                     min_price: float = 0, max_price: float = 999999) -> List[Item]:
        """Search Xianyu for items matching keyword."""
        if not self._ready:
            await self.start()
        
        # Check login state before searching
        if self._auth_required:
            raise RuntimeError("AUTH_REQUIRED")
        
        async with self._lock:
            items = []
            for page_num in range(1, pages + 1):
                try:
                    page_items = await self._search_page(
                        keyword, page_num, min_price, max_price
                    )
                    items.extend(page_items)
                    
                    # If we got items on first page, we're logged in
                    if page_num == 1 and len(page_items) > 0:
                        logger.info("Login confirmed - got %d items", len(page_items))
                    
                    if page_num < pages:
                        await asyncio.sleep(1.5)  # Polite delay
                    
                except PlaywrightTimeout:
                    logger.warning("Timeout on page %d", page_num)
                    break
                except Exception as e:
                    logger.error("Search error on page %d: %s", page_num, e)
                    if "captcha" in str(e).lower() or "auth" in str(e).lower():
                        self._auth_required = True
                        await self.capture_login_qr()
                        raise RuntimeError("AUTH_REQUIRED")
                    break
            
            # If no items found, might need login
            if len(items) == 0:
                logger.warning("No items found - checking if login is required")
                # Try to detect login wall
                content = await self._page.content()
                if "passport" in content or "login" in content.lower():
                    self._auth_required = True
                    await self.capture_login_qr()
                    raise RuntimeError("AUTH_REQUIRED")
            
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
        
        # Wait for dynamic content to load (items are rendered by JS)
        try:
            await self._page.wait_for_selector("a[href*=/item/]", timeout=10000)
            logger.info("Item links found")
        except Exception:
            logger.info("No item links found after 10s, waiting extra 5s")
            await asyncio.sleep(5)
        
        # Check current URL
        current_url = self._page.url
        logger.info("Current URL: %s", current_url)
        
        # Check for login wall
        content = await self._page.content()
        if "passport.goofish.com" in content:
            logger.warning("Login wall detected on search page")
            self._auth_required = True
            return []
        
        # Use API-intercepted items (more reliable than DOM parsing)
        items = list(self._api_items)
        self._api_items.clear()
        logger.info("Got %d items from API interceptor", len(items))
        
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
        """Navigate to login page and capture screenshot for QR login.
        
        Following the original project's approach: capture the full page
        screenshot (including QR code) and save as JPEG.
        """
        if not self._ready:
            await self.start()
        
        try:
            # Navigate to search which triggers login wall
            await self._page.goto(
                "https://www.goofish.com/search?q=闲鱼",
                timeout=config.browser_timeout
            )
            await self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            
            # Wait for page to settle and QR code to load
            await asyncio.sleep(5)
            
            # Capture full page screenshot as JPEG (like original project)
            qr_path = Path(config.data_dir) / "login_qr.jpg"
            await self._page.screenshot(
                path=str(qr_path),
                type="jpeg",
                quality=80,
                full_page=False
            )
            logger.info("Login page screenshot saved: %s", qr_path)
            
            # Also save as PNG for web serving
            png_path = Path(config.data_dir) / "login_qr.png"
            await self._page.screenshot(path=str(png_path))
            logger.info("Login page PNG saved: %s", png_path)
            
            return str(png_path)
            
        except Exception as e:
            logger.error("Failed to capture QR: %s", e)
            return None
    
    async def capture_login_qr_base64(self) -> Optional[str]:
        """Capture login screenshot and return as base64 string."""
        if not self._ready:
            await self.start()
        
        try:
            await self._page.goto(
                "https://www.goofish.com/search?q=闲鱼",
                timeout=config.browser_timeout
            )
            await self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            await asyncio.sleep(5)
            
            # Capture as JPEG base64 (like original project)
            image_bytes = await self._page.screenshot(
                type="jpeg",
                quality=70,
                full_page=False
            )
            return base64.b64encode(image_bytes).decode("ascii")
            
        except Exception as e:
            logger.error("Failed to capture QR base64: %s", e)
            return None
    
    async def wait_for_login(self, timeout: int = 120) -> bool:
        """Wait for user to scan QR code and login.
        
        This method keeps the browser session open and monitors for login success.
        The user should scan the QR code displayed at /login/qr endpoint.
        """
        logger.info("Waiting for login (timeout=%ds)...", timeout)
        start = time.time()
        
        while time.time() - start < timeout:
            try:
                # Check current page content
                content = await self._page.content()
                
                # Check if login wall is gone
                if "passport.goofish.com" not in content and "alibaba-login-box" not in content:
                    # Check if we can access search results
                    await self._page.goto(
                        "https://www.goofish.com/search?q=test",
                        timeout=15000
                    )
                    await asyncio.sleep(2)
                    
                    new_content = await self._page.content()
                    if "passport.goofish.com" not in new_content:
                        logger.info("Login successful!")
                        await self._persist_state()
                        self._auth_required = False
                        return True
                
                # Check for auth success markers in any frame
                for frame in self._page.frames:
                    frame_url = str(getattr(frame, "url", "") or "")
                    if "mtop.taobao.idlemessage.pc.loginuser.get" in frame_url:
                        logger.info("Login API detected!")
                        await self._persist_state()
                        self._auth_required = False
                        return True
                
                await asyncio.sleep(3)
                
            except Exception as e:
                logger.debug("Login check error: %s", e)
                await asyncio.sleep(3)
        
        logger.warning("Login timeout after %ds", timeout)
        return False


# Singleton
provider = PlaywrightProvider()
