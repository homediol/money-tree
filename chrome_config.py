"""
chrome_config.py
================
Builds a ChromeOptions object that works correctly on Parrot OS.

Root cause of your two errors
------------------------------
1) --disable-setuid-sandbox
   /opt/google/chrome/chrome-sandbox is already SUID root (permissions 4755).
   The sandbox works without that flag.  Passing it anyway triggers Chrome's
   deprecation warning and can actually degrade security.  Remove it.

2) AccessDenied (website blocks the browser)
   Sites detect headless / automated Chrome via:
     - navigator.webdriver = true
     - User-Agent containing "HeadlessChrome"
     - Missing browser fingerprint properties
   Fix: disable automation indicators, use a real user-agent string, and keep
   a persistent profile so cookies and fingerprints accumulate naturally.
"""
from __future__ import annotations

from pathlib import Path

from selenium.webdriver.chrome.options import Options


# ── Project paths ─────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).resolve().parent
PROFILE_DIR   = BASE_DIR / "data" / "bot" / "chrome-profile-new-email"
DOWNLOADS_DIR = BASE_DIR / "downloads"
LOGS_DIR      = BASE_DIR / "logs"

for _d in (PROFILE_DIR, DOWNLOADS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Chrome 150 user-agent matching the installed version — no "HeadlessChrome"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


def build_chrome_options(
    *,
    headless: bool = False,
    profile_dir: Path | None = None,
    extra_args: list[str] | None = None,
) -> Options:
    """
    Return a ChromeOptions object that:
      - Uses the system SUID sandbox — NO --no-sandbox needed
      - Hides automation indicators (navigator.webdriver, UA string, switches)
      - Keeps a persistent profile for cookies / session state
      - Works on Parrot OS with Chrome 150

    Parameters
    ----------
    headless    : run without a visible window (uses --headless=new)
    profile_dir : persistent Chrome profile directory (default: ./data/bot/chrome-profile-new-email)
    extra_args  : any additional Chrome flags
    """
    opts    = Options()
    profile = profile_dir or PROFILE_DIR

    # Persistent profile — accumulates cookies, localStorage, fingerprints
    opts.add_argument(f"--user-data-dir={profile}")

    # Headless: use the new implementation (Chrome 112+).
    # The old --headless puts "HeadlessChrome" in the UA string → instant ban.
    if headless:
        opts.add_argument("--headless=new")

    # ── Anti-detection ────────────────────────────────────────────────────
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-agent={_USER_AGENT}")

    # ── Stability (Parrot OS / Docker / low-memory environments) ─────────
    # NOTE: --no-sandbox and --disable-setuid-sandbox are deliberately absent.
    # The SUID sandbox binary at /opt/google/chrome/chrome-sandbox works fine.
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--lang=en-US")

    # Download preferences
    opts.add_experimental_option("prefs", {
        "download.default_directory":       str(DOWNLOADS_DIR),
        "download.prompt_for_download":     False,
        "download.directory_upgrade":       True,
        "safebrowsing.enabled":             True,
        "credentials_enable_service":       False,
        "profile.password_manager_enabled": False,
    })

    for arg in (extra_args or []):
        opts.add_argument(arg)

    return opts
