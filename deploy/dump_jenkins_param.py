"""
Dump the real markup of one 'Build with Parameters' row so a failing Playwright locator can be
fixed against facts instead of guesses.

Usage (on the server, in the venv that has playwright):
    python dump_jenkins_param.py "<job-url>/build?delay=0sec" Branch

Credentials: JENKINS_USER / JENKINS_PASSWORD (falls back to createvpnid / createvpnpass).
Prints, for the matched form item: its own visibility, then every <input>/<select>/<textarea>
inside it with tag, name, class, type, and whether Playwright considers it visible.
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else "Branch"

    user = (os.environ.get("JENKINS_USER") or os.environ.get("createvpnid") or "").strip()
    pw = (os.environ.get("JENKINS_PASSWORD") or os.environ.get("createvpnpass") or "").strip()
    if not user or not pw:
        print("set JENKINS_USER / JENKINS_PASSWORD (or createvpnid / createvpnpass)")
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)

        if page.locator("input[name='j_username']").count():
            page.locator("input[name='j_username']").fill(user)
            page.locator("input[name='j_password']").fill(pw)
            page.locator(
                "button[name='Submit'], input[name='Submit'][type='submit']"
            ).first.click()
            page.wait_for_load_state("domcontentloaded", timeout=60_000)

        page.wait_for_selector("div.jenkins-form-item", timeout=60_000)
        page.wait_for_timeout(3000)  # let reactive/Active-Choices parameters render

        # Every parameter row on the page, so you can see what the job actually has.
        print("\n=== all parameter labels on this page ===")
        for i in range(page.locator("div.jenkins-form-item").count()):
            item = page.locator("div.jenkins-form-item").nth(i)
            lab = item.locator("div.jenkins-form-label, div.setting-name").first
            name = (lab.inner_text().strip() if lab.count() else "(no label)")
            print(f"  [{i}] {name!r:40} item_visible={item.is_visible()}")

        row = (
            page.locator("div.jenkins-form-item")
            .filter(
                has=page.locator("div.jenkins-form-label, div.setting-name").filter(
                    has_text=__import__("re").compile(rf"^\s*{label}\s*$", __import__("re").I)
                )
            )
            .first
        )
        if not row.count():
            print(f"\nno form item labelled {label!r} — see the list above")
            browser.close()
            return 1

        print(f"\n=== form item for {label!r} ===")
        print("item is_visible :", row.is_visible())
        print("item bounding   :", row.bounding_box())

        print("\n=== controls inside it (DOM order — this is the order .first sees) ===")
        ctrls = row.locator("input, select, textarea")
        for i in range(ctrls.count()):
            c = ctrls.nth(i)
            info = c.evaluate(
                "e => ({tag: e.tagName, name: e.name || '', cls: e.className || '',"
                " type: e.type || '', disp: getComputedStyle(e).display,"
                " vis: getComputedStyle(e).visibility,"
                " parentHidden: !!e.closest('[style*=\"display: none\"]')})"
            )
            print(f"  [{i}] {info}  playwright_visible={c.is_visible()}")

        print("\n=== raw HTML of the row ===")
        print(row.evaluate("e => e.outerHTML")[:4000])

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
