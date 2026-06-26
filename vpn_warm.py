"""
VPN Playwright pre-warm for jenkinsbot.

On OSE-Tools (duty bot + jenkinsbot on same host), reuses ``osedutybot/jenkinsupdate.py``
so the same VPN form browser logic applies. Falls back to a minimal local warm if import fails.

Enable: ``VPN_WARM_BROWSER=1`` (default). Disable: ``VPN_WARM_BROWSER=0``.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("jenkinsbot.vpn_warm")

_VPN_BUILD_URL = (
    "https://ose-jenkinsaliyun.bewen.me/job/DEVOPS_CP/job/VPN_CONFIGURATION/job/VPN_CREATION/"
    "build?delay=0sec"
)


def _vpn_warm_enabled() -> bool:
    return (os.environ.get("VPN_WARM_BROWSER", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _prewarm_on_startup_enabled() -> bool:
    return (os.environ.get("VPN_WARM_PREWARM_ON_STARTUP", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _startup_wait_sec() -> float:
    raw = (os.environ.get("JENKINS_WARM_STARTUP_WAIT_SEC") or "0").strip()
    try:
        return max(0.0, float(raw or "0"))
    except ValueError:
        return 0.0


def _duty_bot_root() -> Path:
    raw = (os.environ.get("DUTY_BOT_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent.parent / "osedutybot"


def _import_jenkinsupdate():
    root = _duty_bot_root()
    ju_path = root / "jenkinsupdate.py"
    if not ju_path.is_file():
        return None
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    try:
        import jenkinsupdate as ju  # noqa: WPS433

        return ju
    except Exception as exc:
        logger.warning("import jenkinsupdate from %s failed: %s", root, exc)
        return None


def _vpn_credentials() -> tuple[str, str]:
    u = (os.environ.get("createvpnid") or os.environ.get("JENKINS_USER") or "").strip()
    p = (os.environ.get("createvpnpass") or os.environ.get("JENKINS_PASSWORD") or "").strip()
    return u, p


def _profile_dir() -> str:
    d = (os.environ.get("JENKINSBOT_VPN_WARM_PROFILE") or os.environ.get("VPN_PLAYWRIGHT_USER_DATA_DIR") or "").strip()
    if d:
        return str(Path(d).expanduser())
    return os.path.join(tempfile.gettempdir(), "jenkinsbot_vpn_warm_profile")


def _standalone_prewarm() -> None:
    """Minimal VPN Jenkins login + form when osedutybot is not on disk."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning(
            "VPN warm skipped — pip install playwright && playwright install chromium "
            "(or set DUTY_BOT_ROOT to osedutybot)"
        )
        return

    user, pw = _vpn_credentials()
    if not user or not pw:
        logger.warning("VPN warm skipped — createvpnid / createvpnpass not set")
        return

    headless = (os.environ.get("JENKINSUPDATE_BOT_HEADLESS", "1") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    profile = Path(_profile_dir())
    profile.mkdir(parents=True, exist_ok=True)
    url = (os.environ.get("VPN_CREATION_BUILD_URL") or _VPN_BUILD_URL).strip()

    logger.info("[vpn-warm] standalone prewarm → %s", url)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            viewport={"width": 1400, "height": 900},
            ignore_https_errors=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        if page.locator("input[name='j_username']").count():
            page.locator("input[name='j_username']").fill(user)
            page.locator("input[name='j_password']").fill(pw)
            page.locator(
                "button[name='Submit'], input[name='Submit'][type='submit']"
            ).first.click()
            page.wait_for_load_state("domcontentloaded", timeout=60_000)
        page.wait_for_selector("div.jenkins-form-item", timeout=60_000)
        logger.info("[vpn-warm] standalone form ready")
        time.sleep(2)
        ctx.close()


def _prewarm_worker() -> None:
    if not _vpn_warm_enabled() or not _prewarm_on_startup_enabled():
        logger.info("[vpn-warm] disabled — not pre-warming")
        return

    ju = _import_jenkinsupdate()
    if ju is not None:
        try:
            logger.info("[vpn-warm] using osedutybot jenkinsupdate from %s", _duty_bot_root())
            ju.prewarm_vpn_browser_on_startup()
            wait = _startup_wait_sec()
            if wait > 0 and ju._vpn_warm_enabled():
                ok = ju._vpn_warm_get().wait_ready(wait)
                if ok:
                    logger.info("[vpn-warm] startup ready (shared duty-bot warm browser)")
                else:
                    logger.warning("[vpn-warm] startup wait timed out (%.0fs)", wait)
            return
        except Exception as exc:
            logger.exception("[vpn-warm] duty-bot jenkinsupdate prewarm failed: %s", exc)

    try:
        _standalone_prewarm()
    except Exception as exc:
        logger.exception("[vpn-warm] standalone prewarm failed: %s", exc)


def prewarm_vpn_browser_on_startup() -> None:
    """Fire-and-forget VPN browser warm (does not block Flask / Lark WS)."""
    t = threading.Thread(
        target=_prewarm_worker,
        daemon=True,
        name="jenkinsbot-vpn-warm",
    )
    t.start()
