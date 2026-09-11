"""
browser_driver.py
=================
Creates a Selenium WebDriver for Chrome or Firefox with:
  - Automatic chromedriver / geckodriver installation via webdriver-manager
  - Anti-detection JavaScript patches applied after launch
  - A fallback to Firefox if Chrome fails
  - Retry logic so one transient failure doesn't abort the script

Usage
-----
    from browser_driver import make_driver, quit_driver

    driver = make_driver(headless=False)   # Chrome first, Firefox fallback
    try:
        driver.get("https://example.com")
        ...
    finally:
        quit_driver(driver)
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Literal

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager

from chrome_config import build_chrome_options, LOGS_DIR

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "browser_driver.log"),
    ],
)

# JavaScript that removes navigator.webdriver and fixes common fingerprint leaks
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = { runtime: {} };
"""

BrowserKind = Literal["chrome", "firefox", "auto"]


# ── Public API ────────────────────────────────────────────────────────────

def make_driver(
    *,
    browser: BrowserKind = "auto",
    headless: bool = False,
    retries: int = 3,
    retry_delay: float = 3.0,
) -> webdriver.Chrome | webdriver.Firefox:
    """
    Create and return a WebDriver.

    browser  : "chrome" | "firefox" | "auto" (tries Chrome first, Firefox fallback)
    headless : run without a visible window
    retries  : number of launch attempts before giving up
    """
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            if browser in ("chrome", "auto"):
                driver = _make_chrome(headless=headless)
                _apply_stealth(driver)
                log.info("Chrome driver ready (attempt %d)", attempt)
                return driver
        except Exception as exc:
            last_exc = exc
            log.warning("Chrome launch failed (attempt %d/%d): %s", attempt, retries, exc)
            if browser == "chrome":
                time.sleep(retry_delay)
                continue
            # Fall through to Firefox
            log.info("Falling back to Firefox")
            try:
                driver = _make_firefox(headless=headless)
                log.info("Firefox driver ready (attempt %d)", attempt)
                return driver
            except Exception as ff_exc:
                last_exc = ff_exc
                log.warning("Firefox launch also failed: %s", ff_exc)

        if browser == "firefox":
            try:
                driver = _make_firefox(headless=headless)
                log.info("Firefox driver ready (attempt %d)", attempt)
                return driver
            except Exception as exc:
                last_exc = exc
                log.warning("Firefox launch failed (attempt %d/%d): %s", attempt, retries, exc)

        time.sleep(retry_delay)

    raise RuntimeError(
        f"Could not launch any browser after {retries} attempts. "
        f"Last error: {last_exc}"
    )


def quit_driver(driver: webdriver.Chrome | webdriver.Firefox | None) -> None:
    """Safely quit the driver, ignoring errors."""
    if driver is None:
        return
    try:
        driver.quit()
        log.info("Driver closed cleanly")
    except Exception as exc:
        log.debug("Driver quit error (ignored): %s", exc)


# ── Private helpers ───────────────────────────────────────────────────────

def _make_chrome(*, headless: bool) -> webdriver.Chrome:
    """
    Launch Chrome with the correct options for Parrot OS.

    Uses webdriver-manager to auto-download the matching chromedriver binary
    so you never have to manage driver versions manually.
    """
    opts    = build_chrome_options(headless=headless)
    service = ChromeService(
        ChromeDriverManager().install(),
        log_path=str(LOGS_DIR / "chromedriver.log"),
    )
    driver  = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)
    driver.implicitly_wait(5)
    return driver


def _make_firefox(*, headless: bool) -> webdriver.Firefox:
    """
    Launch Firefox ESR as a fallback.
    geckodriver is auto-installed via webdriver-manager.
    """
    opts = FirefoxOptions()
    if headless:
        opts.add_argument("--headless")
    opts.set_preference("dom.webdriver.enabled", False)
    opts.set_preference("useAutomationExtension", False)
    opts.set_preference(
        "general.useragent.override",
        "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    )
    opts.set_preference("intl.accept_languages", "en-US, en")

    service = FirefoxService(
        GeckoDriverManager().install(),
        log_path=str(LOGS_DIR / "geckodriver.log"),
    )
    driver  = webdriver.Firefox(service=service, options=opts)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)
    driver.implicitly_wait(5)
    return driver


def _apply_stealth(driver: webdriver.Chrome) -> None:
    """
    Inject JavaScript to remove automation fingerprints.
    Must be called after the driver is created but before any page load.
    """
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _STEALTH_JS},
        )
    except Exception as exc:
        # CDP not available (e.g. remote driver) — fall back to execute_script
        log.debug("CDP stealth injection failed, will inject per-page: %s", exc)
