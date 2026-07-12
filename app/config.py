"""Configuration for Xianyu monitor."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    """Main configuration."""
    
    # Data directory
    data_dir: str = os.environ.get("GOOFISH_DATA_DIR", "/app/data")
    
    # Hermes Webhook
    webhook_url: str = os.environ.get(
        "GOOFISH_WEBHOOK_URL",
        "http://host.docker.internal:8644/webhooks/goofish"
    )
    webhook_secret: str = os.environ.get(
        "GOOFISH_WEBHOOK_SECRET",
        "goofish_hmac_secret_2026"
    )
    
    # Polling
    poll_interval: int = int(os.environ.get("GOOFISH_POLL_INTERVAL", "60"))
    max_pages: int = int(os.environ.get("GOOFISH_MAX_PAGES", "3"))
    
    # Playwright
    headless: bool = os.environ.get("GOOFISH_HEADLESS", "true").lower() == "true"
    browser_timeout: int = int(os.environ.get("GOOFISH_BROWSER_TIMEOUT", "30000"))
    block_assets: bool = os.environ.get("GOOFISH_BLOCK_ASSETS", "true").lower() == "true"
    
    # Price drop detection
    default_drop_abs: float = float(os.environ.get("GOOFISH_DROP_ABS", "50"))
    default_drop_pct: float = float(os.environ.get("GOOFISH_DROP_PCT", "5.0"))
    
    # Paths
    @property
    def storage_state_path(self) -> str:
        return str(Path(self.data_dir) / "storage_state.json")
    
    @property
    def browser_profile_dir(self) -> str:
        return str(Path(self.data_dir) / "browser_profile")
    
    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "goofish.db")
    
    def ensure_dirs(self):
        """Create necessary directories."""
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.browser_profile_dir).mkdir(parents=True, exist_ok=True)


# Singleton
config = Config()
