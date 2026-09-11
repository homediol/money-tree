"""
test_browser.py
===============
Verifies the browser setup works correctly on this Parrot OS machine.

Tests
-----
1. Chrome launches without sandbox warning
2. navigator.webdriver is hidden (anti-detection)
3. User-agent contains no "HeadlessChrome"
4. A real public page loads (httpbin.org/get)
5. Firefox fallback launches and loads a page
6. Persistent profile directory is created and reused

Run:
    python3 test_browser.py
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

# Add project root to path so imports work from any working directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from browser_driver import make_driver, quit_driver


# ── Colour helpers ────────────────────────────────────────────────────────
def _green(s: str) -> str:  return f"\033[32m{s}\033[0m"
def _red(s: str)   -> str:  return f"\033[31m{s}\033[0m"
def _yellow(s: str)-> str:  return f"\033[33m{s}\033[0m"

PASS = _green("  PASS")
FAIL = _red("  FAIL")

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    results.append((name, condition, detail))
    icon = PASS if condition else FAIL
    print(f"{icon}  {name}" + (f"\n        {detail}" if detail and not condition else ""))
    return condition


# ── Test runner ───────────────────────────────────────────────────────────

def run_chrome_tests() -> None:
    print("\n" + "═" * 60)
    print("  Chrome Tests")
    print("═" * 60)
    driver = None
    try:
        driver = make_driver(browser="chrome", headless=True)

        # 1. Driver created
        check("Chrome driver created", driver is not None)

        # 2. navigator.webdriver hidden
        wd = driver.execute_script("return navigator.webdriver")
        check(
            "navigator.webdriver is hidden",
            wd is None or wd is False,
            f"navigator.webdriver = {wd!r}",
        )

        # 3. User-agent has no HeadlessChrome
        ua: str = driver.execute_script("return navigator.userAgent")
        check(
            "User-agent has no 'HeadlessChrome'",
            "HeadlessChrome" not in ua,
            f"UA = {ua}",
        )
        check(
            "User-agent contains Chrome/150",
            "Chrome/150" in ua,
            f"UA = {ua}",
        )

        # 4. Load a real page
        driver.get("https://httpbin.org/get")
        time.sleep(3)
        src = driver.page_source
        check(
            "httpbin.org/get loaded successfully",
            '"url"' in src,
            "Page source did not contain expected JSON" if '"url"' not in src else "",
        )

        # 5. Check that IP / headers are visible (no AccessDenied)
        check(
            "No 'Access Denied' on httpbin.org",
            "Access Denied" not in src and "access denied" not in src.lower(),
            "Page returned an access denied response",
        )

        # 6. Profile directory exists
        from chrome_config import PROFILE_DIR
        check(
            "Persistent profile directory created",
            PROFILE_DIR.exists(),
            str(PROFILE_DIR),
        )

        # 7. Plugins not empty (anti-bot check)
        plugin_count = driver.execute_script("return navigator.plugins.length")
        check(
            "navigator.plugins is not empty",
            plugin_count is not None and plugin_count > 0,
            f"plugins.length = {plugin_count}",
        )

    except Exception:
        check("Chrome test suite", False, traceback.format_exc())
    finally:
        quit_driver(driver)


def run_firefox_tests() -> None:
    print("\n" + "═" * 60)
    print("  Firefox Fallback Tests")
    print("═" * 60)
    driver = None
    try:
        driver = make_driver(browser="firefox", headless=True)
        check("Firefox driver created", driver is not None)

        ua: str = driver.execute_script("return navigator.userAgent")
        check(
            "Firefox user-agent is set",
            "Firefox" in ua,
            f"UA = {ua}",
        )

        driver.get("https://httpbin.org/get")
        time.sleep(3)
        src = driver.page_source
        check(
            "httpbin.org/get loaded (Firefox)",
            '"url"' in src,
        )

    except Exception:
        check("Firefox test suite", False, traceback.format_exc())
    finally:
        quit_driver(driver)


def run_auto_fallback_test() -> None:
    print("\n" + "═" * 60)
    print("  Auto-fallback Test  (Chrome → Firefox)")
    print("═" * 60)
    driver = None
    try:
        driver = make_driver(browser="auto", headless=True)
        check("Auto-fallback driver created", driver is not None)
        driver.get("https://httpbin.org/ip")
        time.sleep(2)
        src = driver.page_source
        check("httpbin.org/ip loaded via auto-fallback", '"origin"' in src)
    except Exception:
        check("Auto-fallback test", False, traceback.format_exc())
    finally:
        quit_driver(driver)


def print_summary() -> None:
    total  = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print("\n" + "═" * 60)
    print(f"  Results: {_green(str(passed))} passed  {_red(str(failed)) if failed else '0'} failed  / {total} total")
    print("═" * 60)
    if failed:
        print(_yellow("\nFailed tests:"))
        for name, ok, detail in results:
            if not ok:
                print(f"  ✗  {name}")
                if detail:
                    print(f"     {detail}")
    else:
        print(_green("\nAll tests passed — browser setup is working correctly.\n"))


if __name__ == "__main__":
    print(_yellow("Parrot OS — Browser Setup Test Suite"))
    print(f"Python {sys.version.split()[0]}  |  Selenium")

    run_chrome_tests()
    run_firefox_tests()
    run_auto_fallback_test()
    print_summary()

    sys.exit(0 if all(ok for _, ok, _ in results) else 1)
