"""
WIPO Madrid Gazette scraper — Stage 1.

Uses Playwright (headless Chromium) to drive the public Gazette browse-by-chapter
UI for each LATAM Madrid Protocol member, download the weekly XLS of
"Notifications of provisional refusals", and return parsed rows ready to be
appended to a Google Sheet.

It does NOT yet visit each IRN's detail page to pull representative name or
refusal PDF — that's Stage 2. For now the weekly output is:
    country_code, country_name, gazette_issue, irn, mark, holder, origin, transaction

Run locally:
    pip install -r requirements.txt
    python -m playwright install chromium
    python scraper.py            # prints rows, no sheet push

Run in CI: see .github/workflows/weekly.yml
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
import xlrd  # the XLS from WIPO is the old BIFF format

LATAM_MEMBERS = {
    "AG": "Antigua and Barbuda",
    "BR": "Brazil",
    "BZ": "Belize",
    "CL": "Chile",
    "CO": "Colombia",
    "CU": "Cuba",
    "GD": "Grenada",
    "JM": "Jamaica",
    "MX": "Mexico",
    "TT": "Trinidad and Tobago",
}

GAZETTE_URL = "https://www3.wipo.int/madrid/monitor/gazette/en/"

SHOT_DIR = Path(os.environ.get("SHOT_DIR", "/tmp/wipo-shots"))
SHOT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Refusal:
    country_code: str
    country_name: str
    gazette_issue: str
    irn: str
    mark: str
    holder: str
    origin: str
    transaction: str
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    monitor_url: str = ""

    def to_sheet_row(self) -> list[str]:
        return [
            self.fetched_at,
            self.country_code,
            self.country_name,
            self.gazette_issue,
            self.irn,
            self.mark,
            self.holder,
            self.origin,
            self.transaction,
            self.monitor_url or f"https://www3.wipo.int/madrid/monitor/en/",
        ]


SHEET_HEADERS = [
    "fetched_at", "country_code", "country_name", "gazette_issue",
    "irn", "mark", "holder", "origin", "transaction", "monitor_url",
]


async def _shot(page, label: str):
    """Save a screenshot for post-mortem debugging."""
    path = SHOT_DIR / f"{label}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        print(f"  [shot] {path}", flush=True)
    except Exception as e:
        print(f"  [shot-fail] {label}: {e}", flush=True)


async def _wait_gazette_form(page):
    """The Gazette form is inside a portlet; wait for its well-known fields."""
    # Try several signals in order — whichever appears first wins.
    for sel in [
        'select[name*="year" i]',
        'select:has(option:has-text("2026"))',
        'text=Publication date',
    ]:
        try:
            await page.wait_for_selector(sel, timeout=15000, state="visible")
            return
        except PlaywrightTimeout:
            continue
    raise RuntimeError("Gazette form did not load — selectors did not match.")


async def _select_by_label(page, label_text: str, option_text_or_value: str):
    """Set a <select> whose label (or nearby text) is `label_text`."""
    # Playwright's get_by_label works when the <label> is correctly associated.
    try:
        locator = page.get_by_label(re.compile(label_text, re.I))
        await locator.first.select_option(label=option_text_or_value)
        return
    except Exception:
        pass
    # Fallback: find select near label text.
    locator = page.locator(f'xpath=//label[contains(., "{label_text}")]/following::select[1]')
    await locator.select_option(label=option_text_or_value)


async def _check_by_label(page, label_text: str, checked: bool):
    try:
        locator = page.get_by_label(re.compile(rf"^{label_text}$", re.I))
        if checked:
            await locator.first.check()
        else:
            await locator.first.uncheck()
        return
    except Exception:
        pass
    locator = page.locator(f'xpath=//label[normalize-space()="{label_text}"]/preceding::input[@type="checkbox"][1]')
    if checked:
        await locator.check()
    else:
        await locator.uncheck()


async def scrape_country(page, country_code: str, gazette_issue_label: str | None = None) -> list[Refusal]:
    """Fill the Gazette form for one country, download the XLS, parse it."""
    country_name = LATAM_MEMBERS[country_code]
    print(f"\n→ {country_code} {country_name}", flush=True)

    await page.goto(GAZETTE_URL, wait_until="domcontentloaded")
    await _wait_gazette_form(page)
    await _shot(page, f"01-{country_code}-loaded")

    # Summary selector: Notifications of provisional refusals
    await _select_by_label(page, "Summary", "Notifications of provisional refusals")

    # State/IGO = target country. The dropdown option text is "Brazil (BR)" etc.
    state_label = f"{country_name} ({country_code})"
    await _select_by_label(page, "State/IGO", state_label)

    # Filter checkboxes: we only want "Designated" (where the LATAM office issued the refusal)
    await _check_by_label(page, "Origin", False)
    await _check_by_label(page, "Interested", False)
    await _check_by_label(page, "Designated", True)

    # If caller pinned an issue, set it; otherwise leave default (latest).
    if gazette_issue_label:
        await _select_by_label(page, "No", gazette_issue_label)

    await _shot(page, f"02-{country_code}-filtered")

    # Submit
    await page.get_by_role("button", name=re.compile("Submit", re.I)).click()

    # Wait for either results or a "no results" message
    try:
        await page.wait_for_selector(
            'text=/download report|Notifications of provisional refusals|no result/i',
            timeout=30000,
        )
    except PlaywrightTimeout:
        await _shot(page, f"03-{country_code}-submit-timeout")
        raise

    await _shot(page, f"03-{country_code}-results")

    # Detect the "no results" case gracefully
    if await page.locator('text=/no result|0 results/i').count() > 0:
        print(f"  no refusals for {country_code} this issue", flush=True)
        return []

    # Read the gazette issue label that's now showing (e.g. "15 - 23/04/2026")
    issue = await _read_issue_label(page)

    # Click "download report XLS"
    async with page.expect_download() as dl_info:
        # The button is often an image link; match by accessible name or text.
        btn = page.get_by_role("link", name=re.compile("download report", re.I))
        if await btn.count() == 0:
            btn = page.get_by_text(re.compile("download report", re.I))
        await btn.first.click()
    download = await dl_info.value

    tmp = Path(tempfile.gettempdir()) / f"gazette-{country_code}.xls"
    await download.save_as(str(tmp))
    print(f"  saved {tmp.name} ({tmp.stat().st_size:,} bytes)", flush=True)

    rows = _parse_xls(tmp, country_code=country_code, country_name=country_name, gazette_issue=issue)
    print(f"  parsed {len(rows)} rows", flush=True)
    return rows


async def _read_issue_label(page) -> str:
    """Best-effort: read the current 'No.' dropdown value so we can tag rows."""
    try:
        sel = page.locator('select:has(option[selected])').first
        val = await sel.evaluate("el => el.options[el.selectedIndex]?.label || el.value")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return datetime.now(timezone.utc).strftime("%Y-wk%V")


def _parse_xls(path: Path, *, country_code: str, country_name: str, gazette_issue: str) -> list[Refusal]:
    wb = xlrd.open_workbook(str(path))
    ws = wb.sheet_by_index(0)
    if ws.nrows < 2:
        return []

    header = [str(ws.cell_value(0, c)).strip().upper() for c in range(ws.ncols)]
    def col(name):
        return header.index(name) if name in header else -1

    ci_irn = col("IRN")
    ci_mark = col("MARK")
    ci_holder = col("HOLDER")
    ci_origin = col("ORIGIN")
    ci_txn = col("TRANSACTION")

    rows = []
    for r in range(1, ws.nrows):
        def val(ci):
            if ci < 0: return ""
            v = ws.cell_value(r, ci)
            # xlrd returns floats for numeric cells; IRNs must stay as ints.
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            return str(v).strip()

        irn = val(ci_irn)
        if not irn:
            continue
        rows.append(Refusal(
            country_code=country_code,
            country_name=country_name,
            gazette_issue=gazette_issue,
            irn=irn,
            mark=val(ci_mark),
            holder=val(ci_holder),
            origin=val(ci_origin),
            transaction=val(ci_txn),
        ))
    return rows


async def scrape_all(countries: Iterable[str] | None = None) -> list[Refusal]:
    countries = list(countries or LATAM_MEMBERS.keys())
    all_rows: list[Refusal] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900})
        ctx.set_default_timeout(45000)
        page = await ctx.new_page()
        for cc in countries:
            try:
                rows = await scrape_country(page, cc)
                all_rows.extend(rows)
            except Exception as exc:
                print(f"  !! failed {cc}: {exc}", flush=True)
                await _shot(page, f"FAIL-{cc}")
                continue
        await browser.close()
    return all_rows


async def _main():
    rows = await scrape_all()
    print(f"\n=== {len(rows)} total rows across {len(LATAM_MEMBERS)} countries ===")
    for r in rows[:5]:
        print(" ", r.country_code, r.irn, r.mark[:40], "—", r.holder[:40])


if __name__ == "__main__":
    asyncio.run(_main())
