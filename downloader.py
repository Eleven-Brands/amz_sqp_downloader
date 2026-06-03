"""Playwright-based Seller Central SQP downloader.

IMPORTANT — SELECTOR CALIBRATION
---------------------------------
The selectors in this file are best-effort estimates of Seller Central's UI.
Run `python main.py discover --marketplace US` after `setup` to capture
screenshots and the page's interactive elements. Share the debug/ folder with
the developer to fine-tune selectors before running bulk downloads.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from config import (
    CHROME_PROFILE,
    DEBUG_DIR,
    DOWNLOADS_TMP,
    MARKETPLACES,
    last_available_week,
)

logger = logging.getLogger(__name__)

_SQP_PATH   = "/brand-analytics/dashboard/query-performance"
_TIMEOUT    = 15_000   # ms for most waits
_DL_TIMEOUT = 90_000   # ms for download


def _sqp_url(marketplace: str, ws: date) -> str:
    """Build the Brand Analytics SQP ASIN-view URL for a marketplace and week.

    The reporting-range and weekly-week params pre-select the UI filters on
    page load, avoiding the need to interact with non-standard Katal components.
    weekly-week uses the Saturday (end of Amazon week, ws+6 days).
    """
    cfg        = MARKETPLACES[marketplace]
    sc_url     = cfg["sc_url"]
    country_id = cfg["country_id"]
    week_end   = (ws + timedelta(days=6)).isoformat()
    return (
        f"{sc_url}{_SQP_PATH}"
        f"?view-id=query-performance-asin-view"
        f"&country-id={country_id}"
        f"&reporting-range=weekly"
        f"&weekly-week={week_end}"
    )


class SessionExpiredError(Exception):
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _screenshot(page: Page, name: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
    except Exception:
        pass


def _jitter(lo: float = 8.0, hi: float = 20.0) -> None:
    time.sleep(random.uniform(lo, hi))


def _parse_dm_datetime(s: str) -> datetime:
    """Parse DM 'Date Requested' cell (e.g. '14/05/2026, 16:21' or '05/14/2026, 4:21 PM')."""
    s = s.strip()
    for fmt in (
        "%d/%m/%Y, %H:%M", "%m/%d/%Y, %H:%M",
        "%d/%m/%Y, %I:%M %p", "%m/%d/%Y, %I:%M %p",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime(2000, 1, 1)


def _ready(page: Page, timeout: int = _TIMEOUT) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=timeout)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeout:
        pass  # networkidle can hang on pages with live polling


def _is_logged_out(page: Page) -> bool:
    url = page.url.lower()
    return "signin" in url or "ap/login" in url or "ap/signin" in url


def _try_click(page: Page, selectors: list[str], timeout: int = 5_000) -> bool:
    for sel in selectors:
        try:
            page.click(sel, timeout=timeout)
            return True
        except PWTimeout:
            continue
    return False


def _try_fill(page: Page, selectors: list[str], value: str, timeout: int = 5_000) -> bool:
    for sel in selectors:
        try:
            page.fill(sel, value, timeout=timeout)
            return True
        except PWTimeout:
            continue
    return False


# ── Core class ────────────────────────────────────────────────────────────────

class SQPDownloader:
    def __init__(self, headless: bool = False):
        self._headless = headless
        self._pw       = None
        self._ctx: BrowserContext | None = None

    def __enter__(self) -> "SQPDownloader":
        CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
        DOWNLOADS_TMP.mkdir(parents=True, exist_ok=True)
        self._pw  = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir   = str(CHROME_PROFILE),
            headless        = self._headless,
            downloads_path  = str(DOWNLOADS_TMP),
            viewport        = {"width": 1440, "height": 900},
            accept_downloads= True,
            args            = ["--disable-blink-features=AutomationControlled"],
        )
        return self

    def __exit__(self, *_) -> None:
        if self._ctx:
            self._ctx.close()
        if self._pw:
            self._pw.stop()

    @property
    def page(self) -> Page:
        pages = self._ctx.pages
        return pages[0] if pages else self._ctx.new_page()

    # ── Session ───────────────────────────────────────────────────────────────

    def setup_session(self, marketplace: str = "US") -> None:
        """Open the browser so the user can log in manually (run once)."""
        sc_url = MARKETPLACES[marketplace]["sc_url"]
        pg = self.page
        pg.goto(f"{sc_url}/home")
        print("\nBrowser is open. Please:")
        print("  1. Log in to Seller Central")
        print("  2. Complete 2-FA")
        print("  3. Mark this as a trusted device if Amazon asks")
        print("  4. Click OK in the dialog that will appear\n")
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "Log in to Seller Central, then click OK to save the session.",
            "SQP Setup",
            0,
        )
        _screenshot(pg, "session_setup_complete")
        print(f"Session saved to {CHROME_PROFILE}")

    def check_session(self, sc_url: str) -> bool:
        """Return True if Seller Central session is active."""
        pg = self.page
        try:
            pg.goto(f"{sc_url}/home", timeout=30_000)
            _ready(pg)
        except Exception as exc:
            logger.warning(f"check_session navigation error: {exc}")
            return False
        if _is_logged_out(pg):
            _screenshot(pg, "session_expired")
            return False
        return True

    # ── Marketplace switching ─────────────────────────────────────────────────

    def switch_marketplace(self, marketplace: str) -> bool:
        """Switch Seller Central to the target marketplace."""
        sc_name = MARKETPLACES[marketplace]["sc_name"]
        sc_url  = MARKETPLACES[marketplace]["sc_url"]
        pg = self.page

        # Navigate to home of the right regional SC first
        pg.goto(f"{sc_url}/home", timeout=30_000)
        _ready(pg)

        if _is_logged_out(pg):
            raise SessionExpiredError()

        # --- Attempt to click the marketplace switcher ---
        # Selector priority: most specific → most generic
        switcher_selectors = [
            "#sc-mkt-switcher-announce",
            "[data-testid='sc-navbar-marketplace-switcher']",
            "li#sc-mkt-switcher a",
            "span.nav-line-2",
            "a.picker-switch-link",
            "a:has-text('Switch to')",
        ]
        if not _try_click(pg, switcher_selectors, timeout=6_000):
            logger.warning(f"[{marketplace}] Marketplace switcher not found — may already be correct")
            _screenshot(pg, f"switcher_missing_{marketplace}")
            # Assume the current context is acceptable and continue
            return True

        # Wait for the dropdown/modal to appear, then click target
        try:
            pg.wait_for_selector(f"text={sc_name}", timeout=8_000)
            pg.click(f"text={sc_name}")
            _ready(pg)
            logger.info(f"Switched to {marketplace} ({sc_name})")
            _screenshot(pg, f"switched_{marketplace}")
            return True
        except PWTimeout:
            logger.error(f"[{marketplace}] Could not find '{sc_name}' in switcher dropdown")
            _screenshot(pg, f"switch_fail_{marketplace}")
            return False

    # ── Direct API fetch (no Download Manager) ───────────────────────────────

    # Column order matches Seller Utilities extension SQP_ASIN_DATA_COLUMNS.
    # Rows returned by the API are dicts keyed by these IDs.
    _SQP_COLUMNS: "list[tuple[str, str]]" = [
        ("asin",                                 "ASIN"),
        ("qp-asin-query",                        "Search Query"),
        ("qp-asin-query-rank",                   "Search Query Score"),
        ("qp-asin-query-volume",                 "Search Query Volume"),
        ("qp-asin-impressions",                  "Impressions: Total Count"),
        ("qp-asin-count-impressions",            "Impressions: ASIN Count"),
        ("qp-asin-share-impressions",            "Impressions: ASIN Share %"),
        ("qp-asin-clicks",                       "Clicks: Total Count"),
        ("qp-click-rate",                        "Clicks: Click Rate %"),
        ("qp-asin-count-clicks",                 "Clicks: ASIN Count"),
        ("qp-asin-share-clicks",                 "Clicks: ASIN Share %"),
        ("qp-asin-median-query-price-clicks",    "Clicks: Price (Median)"),
        ("qp-asin-median-price-clicks",          "Clicks: ASIN Price (Median)"),
        ("qp-asin-same-day-shipping-clicks",     "Clicks: Same Day Shipping Speed"),
        ("qp-asin-one-day-shipping-clicks",      "Clicks: 1D Shipping Speed"),
        ("qp-asin-two-day-shipping-clicks",      "Clicks: 2D Shipping Speed"),
        ("qp-asin-cart-adds",                    "Cart Adds: Total Count"),
        ("qp-asin-cart-add-rate",                "Cart Adds: Cart Add Rate %"),
        ("qp-asin-count-cart-adds",              "Cart Adds: ASIN Count"),
        ("qp-asin-share-cart-adds",              "Cart Adds: ASIN Share %"),
        ("qp-asin-median-query-price-cart-adds", "Cart Adds: Price (Median)"),
        ("qp-asin-median-price-cart-adds",       "Cart Adds: ASIN Price (Median)"),
        ("qp-asin-same-day-shipping-cart-adds",  "Cart Adds: Same Day Shipping Speed"),
        ("qp-asin-one-day-shipping-cart-adds",   "Cart Adds: 1D Shipping Speed"),
        ("qp-asin-two-day-shipping-cart-adds",   "Cart Adds: 2D Shipping Speed"),
        ("qp-asin-purchases",                    "Purchases: Total Count"),
        ("qp-asin-purchase-rate",                "Purchases: Purchase Rate %"),
        ("qp-asin-count-purchases",              "Purchases: ASIN Count"),
        ("qp-asin-share-purchases",              "Purchases: ASIN Share %"),
        ("qp-asin-median-query-price-purchases", "Purchases: Price (Median)"),
        ("qp-asin-median-price-purchases",       "Purchases: ASIN Price (Median)"),
        ("qp-asin-same-day-shipping-purchases",  "Purchases: Same Day Shipping Speed"),
        ("qp-asin-one-day-shipping-purchases",   "Purchases: 1D Shipping Speed"),
        ("qp-asin-two-day-shipping-purchases",   "Purchases: 2D Shipping Speed"),
        ("marketplace",                          "Marketplace"),
        ("period",                               "Reporting Date"),
    ]

    def _fetch_sqp_api(
        self,
        pg: Page,
        marketplace: str,
        asin: str,
        ws: date,
        out_path: Path,
        tag: str,
    ) -> bool:
        """Fetch SQP data via Amazon's internal Brand Analytics REST API.

        Uses the exact same request format as the Seller Utilities Chrome extension
        (reverse-engineered from inline-script-report-fetcher chunk). The fetch()
        call runs in the page context, so session cookies are inherited automatically.
        CSRF token is read from the page's <meta name="anti-csrftoken-a2z"> tag.
        """
        import json as _json

        cfg      = MARKETPLACES[marketplace]
        sc_url   = cfg["sc_url"]
        country  = cfg["country_id"]
        week_end = (ws + timedelta(days=6)).isoformat()   # Saturday

        # Referer URL mirrors generateReportRefererUrl from the extension
        referer_params = (
            f"asin={asin}"
            f"&reporting-range=weekly"
            f"&weekly-week={week_end}"
            f"&country-id={country}"
            f"&view-id=query-performance-asin-view"
        )
        referer = f"{sc_url}/brand-analytics/dashboard/query-performance?{referer_params}"

        body = {
            "filterSelections": [
                {"id": "asin",            "value": asin,    "valueType": "ASIN"},
                {"id": "reporting-range", "value": "weekly", "valueType": None},
                {"id": "weekly-week",     "value": week_end, "valueType": "weekly"},
            ],
            "reportId": "query-performance-asin-report-table",
            "reportOperations": [{
                "ascending":     True,
                "pageNumber":    1,
                "pageSize":      100,
                "reportId":      "query-performance-asin-report-table",
                "reportType":    "TABLE",
                "sortByColumnId": "qp-asin-query-rank",
            }],
            "selectedCountries": [country],
            "viewId": "query-performance-asin-view",
        }

        api_url  = f"{sc_url}/api/brand-analytics/v1/dashboard/query-performance/reports"
        body_js  = _json.dumps(body)

        js = f"""async () => {{
            const csrfMeta = document.querySelector('meta[name="anti-csrftoken-a2z"]');
            if (!csrfMeta) return {{__error: "no_csrf_meta"}};
            const csrf = csrfMeta.getAttribute("content");
            if (!csrf) return {{__error: "csrf_empty"}};
            try {{
                const resp = await fetch("{api_url}", {{
                    method: "POST",
                    headers: {{
                        "Anti-Csrftoken-A2z": csrf,
                        "Content-Type": "application/json",
                        "Accept": "*/*",
                        "Origin": "{sc_url}",
                        "Referer": "{referer}",
                        "Sec-Fetch-Site": "same-origin",
                    }},
                    credentials: "include",
                    body: JSON.stringify({body_js}),
                }});
                if (!resp.ok) {{
                    let errBody = "";
                    try {{ errBody = await resp.text(); }} catch(_) {{}}
                    return {{__error: "http_" + resp.status, __body: errBody.slice(0, 800)}};
                }}
                return await resp.json();
            }} catch(e) {{
                return {{__error: e.message}};
            }}
        }}"""

        try:
            result = pg.evaluate(js)
        except Exception as exc:
            logger.error(f"[{tag}] API evaluate error: {exc}")
            return False

        if not isinstance(result, dict):
            logger.error(f"[{tag}] API: unexpected response type {type(result)}")
            return False

        if "__error" in result:
            body_snippet = result.get("__body", "")
            logger.error(f"[{tag}] API error: {result['__error']}, body: {body_snippet!r}")
            return False

        reports = result.get("reportsV2") or []
        if not reports:
            # Save full response for diagnosis on first failure
            if not hasattr(self, "_api_empty_logged"):
                self._api_empty_logged = True
                (DEBUG_DIR / f"api_empty_{tag}.json").write_text(
                    _json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            logger.warning(f"[{tag}] API: empty reportsV2")
            return False

        report = reports[0]
        if not isinstance(report, dict):
            logger.error(f"[{tag}] reportsV2[0] is not a dict: {type(report)}")
            return False

        # On first success, log the report keys so we can confirm the shape
        if not hasattr(self, "_api_shape_logged"):
            self._api_shape_logged = True
            logger.info(f"[{tag}] reportsV2[0] keys: {list(report.keys())}")

        raw_rows: list[dict] = report.get("rows") or []
        if not raw_rows:
            # No data for this ASIN/week — write header-only file so it's skipped next run
            logger.info(f"[{tag}] API: 0 rows (no SQP data this week)")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            headers = [label for _, label in self._SQP_COLUMNS]
            with io.open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(headers)
            return True

        headers = [label for _, label in self._SQP_COLUMNS]
        col_ids = [col_id for col_id, _ in self._SQP_COLUMNS]

        mkt_name = cfg.get("sc_name", marketplace)
        period   = f"{ws.isoformat()} to {week_end}"

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with io.open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in raw_rows:
                row["asin"]        = asin
                row["marketplace"] = mkt_name
                row["period"]      = period
                writer.writerow([row.get(c, "") for c in col_ids])

        logger.info(f"[{tag}] API: {len(raw_rows)} rows -> {out_path.name}")
        return True

    # ── SQP page interactions ─────────────────────────────────────────────────

    def _navigate_sqp(self, pg: Page, marketplace: str, ws: date) -> None:
        url = _sqp_url(marketplace, ws)
        logger.info(f"Navigating to: {url}")
        pg.goto(url, timeout=30_000)
        _ready(pg, timeout=20_000)
        if _is_logged_out(pg):
            raise SessionExpiredError()

    def _select_week(self, pg: Page, ws: date, tag: str) -> bool:
        # Week and reporting-range are pre-selected via URL params in _navigate_sqp.
        # No UI interaction needed for the current week; this is a no-op.
        logger.debug(f"[{tag}] Week {ws} pre-selected via URL params")
        return True

    def _select_asin(self, pg: Page, asin: str, tag: str) -> bool:
        # New Katal UI (US): kat-input web component with shadow DOM
        try:
            loc = pg.locator("kat-input").first
            loc.click(timeout=6_000)
            pg.keyboard.press("Control+a")
            pg.keyboard.type(asin)
            pg.keyboard.press("Tab")
            time.sleep(1)
            logger.debug(f"[{tag}] ASIN typed via kat-input")
            return True
        except PWTimeout:
            pass

        # Legacy UI (CA, MX, EU): plain <input class="header-row-text ...">
        try:
            loc = pg.locator("input[class*='header-row-text']").first
            loc.click(timeout=6_000)
            loc.fill(asin)
            pg.keyboard.press("Tab")
            time.sleep(1)
            logger.debug(f"[{tag}] ASIN typed via legacy input")
            return True
        except PWTimeout:
            pass

        logger.error(f"[{tag}] Could not enter ASIN -- check discover/ screenshots")
        _screenshot(pg, f"asin_fail_{tag}")
        return False

    def _apply_filters(self, pg: Page) -> None:
        # The Apply button is a native BUTTON inside KAT-BUTTON's shadow root.
        # get_by_role() searches through shadow DOM and matches on accessible name.
        try:
            pg.get_by_role("button", name="Apply").first.click(timeout=6_000)
            _ready(pg, timeout=20_000)
        except PWTimeout:
            logger.warning("Apply button not found via get_by_role")

    def _request_download(self, pg: Page, marketplace: str, asin: str, ws: date, tag: str) -> bool:
        """Navigate to SQP for one ASIN and queue a Download Manager report. Returns True if queued."""
        try:
            self._navigate_sqp(pg, marketplace, ws)
            if not self._select_asin(pg, asin, tag):
                return False
            self._apply_filters(pg)

            try:
                pg.locator("#GenerateDownloadButton:not([disabled])").click(timeout=8_000)
            except PWTimeout:
                logger.error(f"[{tag}] Generate Download button not found")
                _screenshot(pg, f"req_fail_{tag}")
                return False

            try:
                modal_btn = pg.locator("#downloadModalGenerateDownloadButton")
                modal_btn.wait_for(state="visible", timeout=15_000)
                modal_btn.click(timeout=8_000)
            except PWTimeout:
                logger.error(f"[{tag}] Download modal button not found")
                _screenshot(pg, f"req_fail_{tag}")
                return False

            try:
                pg.locator("kat-button[label='Open Download Manager']").wait_for(
                    state="visible", timeout=30_000
                )
            except PWTimeout:
                logger.warning(f"[{tag}] 'Open Download Manager' did not appear — job may still be queued")

            logger.info(f"[{tag}] Download job queued")
            return True

        except SessionExpiredError:
            raise
        except Exception as exc:
            logger.error(f"[{tag}] Error requesting download: {exc}")
            return False

    def _scrape_table_csv(self, pg: Page, out_path: Path, tag: str) -> bool:
        """Extract the SQP table from the DOM (all pages) and save as CSV.

        Amazon's SQP table uses div[role='table'] / div[role='row'] / div[role='cell']
        instead of standard HTML tables. This scraper iterates through pagination
        and writes one CSV file — no async DM queue required.
        """
        # The SQP table uses div[role="table"] / div[role="row"] / plain div children.
        # Header rows have div[role="columnheader"] cells; data rows have plain div cells.
        _JS_EXTRACT = """() => {
            const tbl = Array.from(document.querySelectorAll('[role="table"]'))
                             .find(t => t.querySelectorAll('[role="row"]').length > 3);
            if (!tbl) return null;
            const allRows = Array.from(tbl.querySelectorAll('[role="row"]'));
            // Last header row = last row that has a columnheader child
            const hdrRows = allRows.filter(r => r.querySelector('[role="columnheader"]'));
            const lastHdr = hdrRows[hdrRows.length - 1];
            const headers = lastHdr
                ? Array.from(lastHdr.children).map(c => c.innerText.trim().split('\\n')[0].trim())
                : [];
            // Data rows: rows with NO columnheader children (plain div cells)
            const dataRows = allRows.filter(r => !r.querySelector('[role="columnheader"]'));
            const rows = dataRows.map(r =>
                Array.from(r.children).map(c => c.innerText.trim().replace(/\\s+/g, ' '))
            ).filter(r => r.some(cell => cell !== ''));
            return { headers, rows };
        }"""

        # Find the enabled "next page" navigation button at the bottom of the table.
        _JS_NEXT_PAGE = """() => {
            const candidates = Array.from(document.querySelectorAll('button'));
            // Match aria-label "Next page" or buttons whose text is exactly ">"
            const nxt = candidates.find(b => {
                if (b.disabled) return false;
                const lbl = (b.getAttribute('aria-label') || '').toLowerCase();
                const txt = (b.innerText || '').trim();
                return lbl.includes('next') || txt === '>';
            });
            if (!nxt) return false;
            nxt.click();
            return true;
        }"""

        all_headers: list[str] = []
        all_rows: list[list[str]] = []
        page_num = 1

        while True:
            # Poll via JS until the data table (>3 rows) is present.
            # wait_for_selector is unreliable here because the element may be
            # "attached" but not pass Playwright's visibility heuristic.
            found = False
            for _ in range(5):  # up to 15s (5 × 3s)
                count = pg.evaluate("""() => {
                    const t = Array.from(document.querySelectorAll('[role="table"]'))
                                   .find(t => t.querySelectorAll('[role="row"]').length > 3);
                    return t ? t.querySelectorAll('[role="row"]').length : 0;
                }""")
                if count > 3:
                    found = True
                    break
                time.sleep(3)

            if not found:
                if page_num == 1:
                    logger.error(f"[{tag}] Table not found on page {page_num}")
                    _screenshot(pg, f"scrape_fail_{tag}")
                    return False
                break

            time.sleep(1)  # let dynamic content finish rendering
            data = pg.evaluate(_JS_EXTRACT)
            if not data or not data.get("rows"):
                logger.warning(f"[{tag}] No rows extracted on page {page_num}")
                break

            if page_num == 1:
                all_headers = data["headers"]
            all_rows.extend(data["rows"])
            logger.debug(f"[{tag}] Page {page_num}: {len(data['rows'])} rows scraped")

            advanced = pg.evaluate(_JS_NEXT_PAGE)
            if not advanced:
                break
            page_num += 1
            time.sleep(1.5)

        if not all_rows:
            logger.error(f"[{tag}] No data extracted from table")
            _screenshot(pg, f"scrape_fail_{tag}")
            return False

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with io.open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if all_headers:
                writer.writerow(all_headers)
            writer.writerows(all_rows)

        logger.info(f"[{tag}] Scraped {len(all_rows)} rows -> {out_path.name}")
        return True

    def _click_download(self, pg: Page, out_path: Path, tag: str) -> bool:
        # Amazon SQP download is a two-step async process:
        # 1. "Generate Download" opens a modal → queues a background CSV generation job
        # 2. After queuing, an "Open Download Manager" button appears → navigate there
        # 3. On the Download Manager page, click the actual download link (expect_download)

        # Step 1: open the download type modal
        try:
            pg.locator("#GenerateDownloadButton:not([disabled])").click(timeout=8_000)
        except PWTimeout:
            logger.error(f"[{tag}] Generate Download button not found -- check discover/ screenshots")
            _screenshot(pg, f"dl_fail_{tag}")
            return False
        except Exception as exc:
            logger.warning(f"[{tag}] Generate Download click failed: {exc}")
            _screenshot(pg, f"dl_fail_{tag}")
            return False

        # Step 2: wait for modal to load, then click modal's "Generate Download"
        try:
            modal_btn = pg.locator("#downloadModalGenerateDownloadButton")
            modal_btn.wait_for(state="visible", timeout=15_000)
            _screenshot(pg, f"dl_modal_{tag}")
            modal_btn.click(timeout=8_000)
        except PWTimeout:
            logger.error(f"[{tag}] Modal Generate Download button did not appear")
            _screenshot(pg, f"dl_fail_{tag}")
            return False
        except Exception as exc:
            logger.warning(f"[{tag}] Modal button click failed: {exc}")
            _screenshot(pg, f"dl_fail_{tag}")
            return False

        # Step 3: wait for "Open Download Manager" button (confirms job was queued).
        # Clicking it opens the Download Manager in a NEW TAB — capture that page.
        try:
            open_dm = pg.locator("kat-button[label='Open Download Manager']")
            open_dm.wait_for(state="visible", timeout=30_000)
            _screenshot(pg, f"dl_dm_ready_{tag}")
        except PWTimeout:
            logger.warning(f"[{tag}] 'Open Download Manager' button did not appear in 30s")
            _screenshot(pg, f"dl_fail_{tag}")
            return False

        try:
            with self._ctx.expect_page(timeout=15_000) as new_page_info:
                open_dm.click(timeout=8_000)
            dm_page = new_page_info.value
            _ready(dm_page, timeout=20_000)
        except Exception:
            # Fallback: button may navigate in same tab
            _ready(pg, timeout=20_000)
            dm_page = pg

        _screenshot(dm_page, f"dl_dm_page_{tag}")

        # Step 4: poll the Download Manager page until a "Download" link appears.
        # Amazon generates the CSV asynchronously — "In Progress" may last several minutes.
        dm_url = dm_page.url
        logger.info(f"[{tag}] Download Manager URL: {dm_url}")

        _DM_POLL_INTERVAL = 30   # seconds between page reloads
        _DM_MAX_WAIT      = 600  # 10-minute ceiling
        elapsed = 0

        # Selectors that indicate a download is ready (in priority order).
        # Scoped to the table rows to avoid matching header/nav links.
        _dl_ready_sels = [
            "table a[href*='.csv'], tr a[href*='.csv']",
            "kat-button[label='Download']",
            "tr button:has-text('Download')",
            "tr a:has-text('Download')",
        ]

        while elapsed <= _DM_MAX_WAIT:
            for sel in _dl_ready_sels:
                try:
                    loc = dm_page.locator(sel).first
                    loc.wait_for(state="visible", timeout=3_000)
                    with dm_page.expect_download(timeout=_DL_TIMEOUT) as dl_info:
                        loc.click(timeout=8_000)
                    dl = dl_info.value
                    dl.save_as(str(out_path))
                    logger.info(f"[{tag}] Downloaded -> {out_path.name}")
                    return True
                except PWTimeout:
                    continue
                except Exception as exc:
                    logger.warning(f"[{tag}] DM link click failed ({sel}): {exc}")

            # Not ready yet — wait and reload
            logger.debug(f"[{tag}] DM: all items still In Progress, waiting {_DM_POLL_INTERVAL}s (elapsed={elapsed}s)")
            time.sleep(_DM_POLL_INTERVAL)
            elapsed += _DM_POLL_INTERVAL
            try:
                dm_page.reload(timeout=20_000)
                _ready(dm_page, timeout=20_000)
            except Exception:
                pass

        _screenshot(dm_page, f"dl_dm_timeout_{tag}")
        logger.error(f"[{tag}] Download Manager: no ready file after {_DM_MAX_WAIT}s")
        logger.error(f"[{tag}] Download did not complete -- check discover/ screenshots")
        return False

    # ── Brand Catalog Performance sniffer ────────────────────────────────────

    _CATALOG_PATH = "/brand-analytics/dashboard/brand-catalog-performance"

    def sniff_catalog(self, marketplace: str, ws: date) -> dict | None:
        """Navigate to the Brand Catalog Performance dashboard and intercept
        the internal API call the page makes. Saves request + response to
        debug/catalog_sniff_<marketplace>.json and returns the parsed response.

        Run this once to discover the exact reportId, column names, and body
        shape before implementing a full downloader.
        """
        cfg      = MARKETPLACES[marketplace]
        sc_url   = cfg["sc_url"]
        country  = cfg["country_id"]
        week_end = (ws + timedelta(days=6)).isoformat()

        url = (
            f"{sc_url}{self._CATALOG_PATH}"
            f"?reporting-range=weekly"
            f"&weekly-week={week_end}"
            f"&view-id=brand-catalog-performance-default-view"
            f"&country-id={country}"
        )

        pg = self.page
        sniff: dict = {}

        api_pattern = "**/brand-catalog-performance/reports"

        def _handle_route(route):
            req = route.request
            try:
                body_text = req.post_data or ""
                try:
                    body_parsed = json.loads(body_text)
                except Exception:
                    body_parsed = body_text

                sniff["request_url"]  = req.url
                sniff["request_body"] = body_parsed
                sniff["request_headers"] = dict(req.headers)

                # Forward the real request and capture the response
                response = route.fetch()
                try:
                    resp_json = response.json()
                except Exception:
                    resp_json = response.text()

                sniff["response_status"] = response.status
                sniff["response_body"]   = resp_json

                route.fulfill(
                    status=response.status,
                    headers=dict(response.headers),
                    body=response.body(),
                )
            except Exception as exc:
                logger.error(f"[sniff_catalog] route handler error: {exc}")
                route.continue_()

        pg.route(api_pattern, _handle_route)

        logger.info(f"[sniff_catalog] Navigating to: {url}")
        pg.goto(url, timeout=30_000)
        _ready(pg, timeout=25_000)

        if _is_logged_out(pg):
            raise SessionExpiredError()

        # Wait up to 15s for the API call to be intercepted
        deadline = time.time() + 15
        while "response_body" not in sniff and time.time() < deadline:
            pg.wait_for_timeout(500)

        pg.unroute(api_pattern, _handle_route)
        _screenshot(pg, f"catalog_sniff_{marketplace}")

        if not sniff:
            logger.error("[sniff_catalog] No API call intercepted — page may not have loaded data")
            return None

        out_path = DEBUG_DIR / f"catalog_sniff_{marketplace}.json"
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(sniff, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        logger.info(f"[sniff_catalog] Saved to {out_path}")

        # Print a summary so the dev can see the shape immediately
        req_body = sniff.get("request_body", {})
        resp_body = sniff.get("response_body", {})
        print("\n── REQUEST BODY ──────────────────────────────")
        print(json.dumps(req_body, indent=2, ensure_ascii=False))
        reports = (resp_body or {}).get("reportsV2") or []
        if reports:
            first = reports[0]
            rows  = first.get("rows") or []
            print(f"\n── RESPONSE: {len(rows)} rows, totalItems={first.get('totalItems')}")
            if rows:
                print("── FIRST ROW KEYS:")
                print(json.dumps(list(rows[0].keys()), indent=2))
                print("── FIRST ROW VALUES:")
                print(json.dumps(rows[0], indent=2, ensure_ascii=False))
        else:
            print(f"\n── RESPONSE STATUS: {sniff.get('response_status')}")
            print("── RESPONSE BODY (truncated):")
            body_str = json.dumps(resp_body, ensure_ascii=False)
            print(body_str[:1000])

        return sniff

    # ── Brand Catalog Performance downloader ─────────────────────────────────

    _CATALOG_COLUMNS: "list[tuple[str, str]]" = [
        ("asin",                            "ASIN"),
        ("asin-title",                      "Product Title"),
        ("category",                        "Category"),
        ("impressions-count",               "Impressions"),
        ("clicks",                          "Clicks"),
        ("ctr-clicks",                      "CTR %"),
        ("cart-adds-count",                 "Cart Adds"),
        ("purchases-count",                 "Purchases"),
        ("conversion-rate",                 "Conversion Rate %"),
        ("total-sales-purchases",           "Total Sales"),
        ("impression-price",                "Impression Price"),
        ("click-price",                     "Click Price"),
        ("cart-adds-price",                 "Cart Add Price"),
        ("purchase-price",                  "Purchase Price"),
        ("same-day-shipping-impressions",   "Impressions: Same Day"),
        ("one-day-shipping-impressions",    "Impressions: 1D"),
        ("two-day-shipping-impressions",    "Impressions: 2D"),
        ("same-day-shipping-clicks",        "Clicks: Same Day"),
        ("one-day-shipping-clicks",         "Clicks: 1D"),
        ("two-day-shipping-clicks",         "Clicks: 2D"),
        ("same-day-shipping-cart-adds",     "Cart Adds: Same Day"),
        ("one-day-shipping-cart-adds",      "Cart Adds: 1D"),
        ("two-day-shipping-cart-adds",      "Cart Adds: 2D"),
        ("same-day-shipping-purchases",     "Purchases: Same Day"),
        ("one-day-shipping-purchases",      "Purchases: 1D"),
        ("two-day-shipping-purchases",      "Purchases: 2D"),
        ("marketplace",                     "Marketplace"),
        ("period",                          "Reporting Date"),
    ]

    def _fetch_catalog_page(
        self,
        pg: Page,
        marketplace: str,
        ws: date,
        page_number: int,
        page_size: int,
        tag: str,
    ) -> dict | None:
        """Fetch one page of Brand Catalog Performance data via the internal API."""
        cfg      = MARKETPLACES[marketplace]
        sc_url   = cfg["sc_url"]
        country  = cfg["country_id"]
        week_end = (ws + timedelta(days=6)).isoformat()

        body = {
            "viewId": "brand-catalog-performance-default-view",
            "filterSelections": [
                {"id": "reporting-range", "value": "weekly", "valueType": None},
                {"id": "weekly-week",     "value": week_end, "valueType": "weekly"},
            ],
            "selectedCountries": [country],
            "reportId": "brand-catalog-performance-report-table",
            "reportOperations": [{
                "ascending":      False,
                "pageNumber":     page_number,
                "pageSize":       page_size,
                "reportId":       "brand-catalog-performance-report-table",
                "reportType":     "TABLE",
                "sortByColumnId": "impressions-count",
            }],
        }

        api_url  = f"{sc_url}/api/brand-analytics/v1/dashboard/brand-catalog-performance/reports"
        referer  = (
            f"{sc_url}/brand-analytics/dashboard/brand-catalog-performance"
            f"?reporting-range=weekly&weekly-week={week_end}"
            f"&view-id=brand-catalog-performance-default-view&country-id={country}"
        )
        body_js  = json.dumps(body)

        js = f"""async () => {{
            const csrfMeta = document.querySelector('meta[name="anti-csrftoken-a2z"]');
            if (!csrfMeta) return {{__error: "no_csrf_meta"}};
            const csrf = csrfMeta.getAttribute("content");
            if (!csrf) return {{__error: "csrf_empty"}};
            try {{
                const resp = await fetch("{api_url}", {{
                    method: "POST",
                    headers: {{
                        "Anti-Csrftoken-A2z": csrf,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Origin": "{sc_url}",
                        "Referer": "{referer}",
                        "X-Requested-With": "XMLHttpRequest",
                        "Sec-Fetch-Site": "same-origin",
                    }},
                    credentials: "include",
                    body: JSON.stringify({body_js}),
                }});
                if (!resp.ok) {{
                    let errBody = "";
                    try {{ errBody = await resp.text(); }} catch(_) {{}}
                    return {{__error: "http_" + resp.status, __body: errBody.slice(0, 800)}};
                }}
                return await resp.json();
            }} catch(e) {{
                return {{__error: e.message}};
            }}
        }}"""

        try:
            result = pg.evaluate(js)
        except Exception as exc:
            logger.error(f"[{tag}] catalog API evaluate error: {exc}")
            return None

        if not isinstance(result, dict):
            logger.error(f"[{tag}] catalog API: unexpected type {type(result)}")
            return None

        if "__error" in result:
            logger.error(f"[{tag}] catalog API error: {result['__error']} body={result.get('__body','')!r}")
            return None

        reports = result.get("reportsV2") or []
        if not reports:
            logger.info(f"[{tag}] catalog API: empty reportsV2 (0-item week)")
            return {}

        return reports[0]

    def download_catalog_week(
        self,
        marketplace: str,
        ws: date,
        save_dir: Path,
    ) -> bool:
        """Download Brand Catalog Performance for one marketplace + week.

        Fetches all pages and writes a single CSV to save_dir.
        Returns True on success (including 0-row weeks).
        """
        save_dir.mkdir(parents=True, exist_ok=True)
        week_end = (ws + timedelta(days=6)).isoformat()
        out      = save_dir / f"catalog_{marketplace}_{ws.isoformat()}.csv"
        tag      = f"catalog_{marketplace}_{ws}"

        if out.exists():
            logger.info(f"Skipping (already exists): {out.name}")
            return True

        cfg      = MARKETPLACES[marketplace]
        mkt_name = cfg.get("sc_name", marketplace)
        period   = f"{ws.isoformat()} to {week_end}"

        try:
            # Navigate to the catalog page so CSRF token is available
            catalog_url = (
                f"{cfg['sc_url']}/brand-analytics/dashboard/brand-catalog-performance"
                f"?reporting-range=weekly&weekly-week={week_end}"
                f"&view-id=brand-catalog-performance-default-view&country-id={cfg['country_id']}"
            )
            logger.info(f"[{tag}] Navigating to catalog page")
            pg = self.page
            pg.goto(catalog_url, timeout=30_000)
            _ready(pg, timeout=20_000)
            if _is_logged_out(pg):
                raise SessionExpiredError()

            # Fetch first page to discover totalItems
            _PAGE_SIZE = 100
            first = self._fetch_catalog_page(pg, marketplace, ws, 1, _PAGE_SIZE, tag)
            if first is None:
                logger.error(f"[{tag}] First page fetch failed")
                return False

            total_items = first.get("totalItems", 0)
            all_rows: list[dict] = list(first.get("rows") or [])
            logger.info(f"[{tag}] totalItems={total_items}, got {len(all_rows)} on page 1")

            # Fetch remaining pages
            total_pages = math.ceil(total_items / _PAGE_SIZE)
            for page_num in range(2, total_pages + 1):
                _jitter(1.0, 2.5)
                page_data = self._fetch_catalog_page(pg, marketplace, ws, page_num, _PAGE_SIZE, tag)
                if page_data is None:
                    logger.error(f"[{tag}] Failed on page {page_num}, aborting")
                    return False
                rows = page_data.get("rows") or []
                all_rows.extend(rows)
                logger.info(f"[{tag}] Page {page_num}/{total_pages}: +{len(rows)} rows (total {len(all_rows)})")

            headers = [label for _, label in self._CATALOG_COLUMNS]
            col_ids = [col_id for col_id, _ in self._CATALOG_COLUMNS]

            tmp = out.with_suffix(".tmp")
            with io.open(tmp, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                for row in all_rows:
                    row["marketplace"] = mkt_name
                    row["period"]      = period
                    writer.writerow([row.get(c, "") for c in col_ids])
            tmp.rename(out)

            logger.info(f"[{tag}] {len(all_rows)} rows -> {out.name}")
            return True

        except SessionExpiredError:
            raise
        except Exception as exc:
            logger.error(f"[{tag}] Unexpected error: {exc}", exc_info=True)
            _screenshot(self.page, f"error_{tag}")
            return False

    # ── Public: single download ───────────────────────────────────────────────

    def download_one(
        self,
        marketplace: str,
        asin: str,
        ws: date,
        save_dir: Path,
    ) -> bool:
        """Download SQP CSV for one ASIN + week. Returns True on success."""
        save_dir.mkdir(parents=True, exist_ok=True)
        out = save_dir / f"{asin}_{ws.isoformat()}.csv"
        if out.exists():
            logger.info(f"Skipping (already exists): {out.name}")
            return True

        tag = f"{marketplace}_{asin}_{ws}"

        try:
            # Ensure we're on a Seller Central page so session cookies + CSRF are available
            self._navigate_sqp(self.page, marketplace, ws)

            # Try direct API call first (fast, full data format)
            ok = self._fetch_sqp_api(self.page, marketplace, asin, ws, out, tag)
            if ok:
                _jitter(2.0, 4.0)
                return True

            # Fallback: DOM scrape (no price/shipping columns)
            logger.warning(f"[{tag}] API fetch failed, falling back to DOM scrape")
            if not self._select_asin(self.page, asin, tag):
                return False
            self._apply_filters(self.page)
            ok = self._scrape_table_csv(self.page, out, tag)
            if ok:
                _jitter(8, 18)
            return ok

        except SessionExpiredError:
            raise
        except Exception as exc:
            logger.error(f"[{tag}] Unexpected error: {exc}", exc_info=True)
            _screenshot(self.page, f"error_{tag}")
            return False

    def download_week_batch(
        self,
        marketplace: str,
        asins: list[str],
        ws: date,
        save_dir: Path,
        done_set: set,
    ) -> dict[str, bool]:
        """Queue all downloads for a week, then bulk-download from Download Manager.

        Phase 1: navigate to each ASIN's SQP page and queue a download job (no waiting).
        Phase 2: go to Download Manager, poll until reports are ready, download each file.
        Matches downloaded files to ASINs via the 'ASIN' column in the CSV content.

        Returns {asin: success_bool}.
        """
        import pandas as pd

        save_dir.mkdir(parents=True, exist_ok=True)
        cfg    = MARKETPLACES[marketplace]
        sc_url = cfg["sc_url"]
        pg     = self.page

        pending = [
            a for a in asins
            if not (save_dir / f"{a}_{ws.isoformat()}.csv").exists()
            and (marketplace, a, ws.isoformat()) not in done_set
        ]
        if not pending:
            logger.info(f"[{marketplace}/{ws}] All ASINs already done")
            return {a: True for a in asins}

        logger.info(f"[{marketplace}/{ws}] Requesting {len(pending)} DM downloads")
        queued: list[str] = []

        # ── Phase 1: queue download jobs ──────────────────────────────────────
        for asin in pending:
            tag = f"{marketplace}_{asin}_{ws}"
            _jitter(2.0, 5.0)
            if self._request_download(pg, marketplace, asin, ws, tag):
                queued.append(asin)

        if not queued:
            logger.error(f"[{marketplace}/{ws}] No downloads queued")
            return {a: False for a in pending}

        logger.info(f"[{marketplace}/{ws}] {len(queued)}/{len(pending)} queued — going to Download Manager")

        # ── Phase 2: poll DM and download ready files ─────────────────────────
        # The DM is a SPA: kat-button elements only render when the page is opened
        # via Amazon's own SPA navigation (clicking "Open Download Manager"), NOT via
        # a direct goto(). We click the button that appeared after the last queued
        # job, which opens DM in a new tab with kat-buttons properly rendered.
        dm_url = f"{sc_url}/brand-analytics/download-manager"
        dm_page: Page = pg

        try:
            open_dm_btn = pg.locator("kat-button[label='Open Download Manager']")
            open_dm_btn.wait_for(state="visible", timeout=10_000)
            with self._ctx.expect_page(timeout=15_000) as new_pg_info:
                open_dm_btn.click(timeout=8_000)
            dm_page = new_pg_info.value
            _ready(dm_page, timeout=20_000)
            logger.info(f"[{marketplace}/{ws}] DM opened via SPA button (new tab)")
        except Exception as exc:
            logger.warning(f"[{marketplace}/{ws}] 'Open Download Manager' button not found ({exc}), using direct URL")
            pg.goto(f"{sc_url}/brand-analytics/home", timeout=30_000)
            _ready(pg)
            pg.goto(dm_url, timeout=30_000)
            _ready(pg)
            dm_page = pg

        # Wait for any DM content to render
        try:
            dm_page.wait_for_selector("div[role='row'], kat-button, table tr td", timeout=30_000)
        except PWTimeout:
            logger.warning(f"[{marketplace}/{ws}] DM table did not render after 30s — proceeding anyway")
        if _is_logged_out(dm_page):
            raise SessionExpiredError()
        _screenshot(dm_page, f"dm_{marketplace}_{ws}")

        our_country     = cfg["country_id"].upper()
        results: dict[str, bool] = {}
        already_clicked: set[str] = set()  # keyed by 'Date Requested' cell text

        _DM_POLL  = 30
        _DM_LIMIT = 1200  # 20 minutes max

        def _process_dm_page() -> None:
            """Scan the current DM page and download any ready rows that belong to our batch."""
            # DM uses div[role='table']/div[role='row'] — NOT standard <table>/<tr>/<td>
            all_rows = dm_page.locator("div[role='row']").all()
            # Skip header rows (those that contain columnheader children)
            data_rows = [
                r for r in all_rows
                if r.locator("[role='columnheader']").count() == 0
            ]
            logger.info(f"[{marketplace}/{ws}] DM: {len(data_rows)} data rows (country={our_country})")

            # Build the expected report start-date strings for this week in both
            # DD/MM/YYYY and MM/DD/YYYY formats (Amazon uses either depending on locale).
            ws_dd = ws.strftime("%d/%m/%Y")
            ws_md = ws.strftime("%m/%d/%Y")

            for row in data_rows:
                try:
                    # Try ARIA cells first, fall back to direct div children
                    cells = row.locator("div[role='cell']").all()
                    if not cells:
                        cells = row.locator("> div").all()
                    if len(cells) < 4:
                        continue

                    country    = cells[1].text_content(timeout=2_000).strip().upper()
                    start_date = cells[3].text_content(timeout=2_000).strip() if len(cells) > 3 else ""
                    requested_s = cells[5].text_content(timeout=2_000).strip() if len(cells) > 5 else ""

                    if country != our_country:
                        continue
                    # Filter by report's own start date to isolate rows for the target week.
                    # This is more reliable than batch_start: it works across sessions and
                    # lets us pick up ready reports from previous (failed) runs of this week.
                    if start_date and start_date not in (ws_dd, ws_md):
                        continue
                    if requested_s in already_clicked:
                        continue

                    # DM Download buttons are <kat-button label="Download"> web components.
                    # Also try JS shadow-DOM pierce as fallback.
                    last_cell = cells[-1]
                    dl_el = last_cell.locator(
                        "kat-button[label='Download'], a:has-text('Download'), button:has-text('Download')"
                    )
                    if dl_el.count() == 0:
                        logger.info(f"[{marketplace}/{ws}] Row {requested_s} ({country}): in-progress or no dl button")
                        continue

                    safe_ts  = requested_s.replace("/", "").replace(":", "").replace(", ", "_").replace(" ", "")
                    tmp_path = DOWNLOADS_TMP / f"dm_{marketplace}_{safe_ts}.csv"
                    DOWNLOADS_TMP.mkdir(parents=True, exist_ok=True)

                    with dm_page.expect_download(timeout=_DL_TIMEOUT) as dl_info:
                        dl_el.first.click(timeout=8_000)
                    dl_info.value.save_as(str(tmp_path))
                    already_clicked.add(requested_s)
                    logger.info(f"[{marketplace}/{ws}] DM file downloaded -> {tmp_path.name}")

                    try:
                        df = pd.read_csv(tmp_path, encoding="utf-8-sig", low_memory=False)
                        if "ASIN" not in df.columns or df.empty:
                            logger.warning(f"[{marketplace}/{ws}] No ASIN data in {tmp_path.name}")
                        else:
                            for asin_val, asin_df in df.groupby("ASIN"):
                                out = save_dir / f"{asin_val}_{ws.isoformat()}.csv"
                                asin_df.to_csv(out, index=False, encoding="utf-8-sig")
                                results[str(asin_val)] = True
                                logger.info(f"[{marketplace}/{ws}] Saved {asin_val} -> {out.name}")
                    except Exception as exc:
                        logger.error(f"[{marketplace}/{ws}] Error reading {tmp_path.name}: {exc}")
                    finally:
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass

                except PWTimeout:
                    continue
                except Exception as exc:
                    logger.debug(f"[{marketplace}/{ws}] DM row error: {exc}")
                    continue

        _JS_ALL_BUTTONS = """() => {
            const kbs = Array.from(document.querySelectorAll('kat-button')).map(el => ({
                tag: 'KAT', label: el.getAttribute('label'), disabled: el.getAttribute('disabled'),
                text: el.innerText?.trim(), shadowBtn: !!el.shadowRoot?.querySelector('button'),
            }));
            const btns = Array.from(document.querySelectorAll('button')).map(el => ({
                tag: 'BTN', label: el.getAttribute('aria-label'), disabled: el.disabled,
                text: el.innerText?.trim(),
            }));
            return [...kbs, ...btns];
        }"""

        _JS_NEXT_PAGE = """() => {
            // Snapshot all kat-buttons and regular buttons for debugging
            const allKat = Array.from(document.querySelectorAll('kat-button'));
            const allBtn = Array.from(document.querySelectorAll('button'));

            // Try regular button selectors first
            const btnCandidates = [
                document.querySelector('button[aria-label="Next page"]'),
                document.querySelector('button[aria-label="next"]'),
                allBtn.find(b => !b.disabled && (b.innerText.trim() === '>' || b.innerText.trim() === '»')),
                allBtn.find(b => !b.disabled && /next/i.test(b.getAttribute('aria-label') || '')),
            ];
            for (const el of btnCandidates) {
                if (!el || el.disabled) continue;
                el.click();
                return 'BTN:' + (el.getAttribute('aria-label') || el.innerText?.trim());
            }

            // Try kat-button selectors
            const katCandidates = [
                document.querySelector('kat-button[label="Next"]'),
                document.querySelector('kat-button[label="next"]'),
                document.querySelector('kat-button[label=">"]'),
                document.querySelector('kat-button[label="›"]'),
                allKat.find(b => !b.getAttribute('disabled') && /next/i.test(b.getAttribute('label') || '')),
                allKat.find(b => !b.getAttribute('disabled') && (b.getAttribute('label') === '>' || b.getAttribute('label') === '›')),
            ];
            for (const el of katCandidates) {
                if (!el || el.getAttribute('disabled') != null) continue;
                const inner = el.shadowRoot?.querySelector('button:not([disabled])');
                (inner || el).click();
                return 'KAT:' + el.getAttribute('label');
            }

            // Last resort: look for a pagination container and click the last non-disabled button
            const paginationContainer = document.querySelector('[class*="pagination"], [aria-label*="pagination"], [role="navigation"]');
            if (paginationContainer) {
                const btnsInPager = Array.from(paginationContainer.querySelectorAll('button, kat-button'));
                const lastBtn = btnsInPager[btnsInPager.length - 1];
                if (lastBtn && !lastBtn.disabled && !lastBtn.getAttribute('disabled')) {
                    lastBtn.click();
                    return 'PAGER:' + (lastBtn.innerText?.trim() || lastBtn.getAttribute('label'));
                }
            }

            // Return inventory of all buttons for debugging
            return null;
        }"""

        elapsed = 0
        while elapsed <= _DM_LIMIT and len(already_clicked) < len(queued):
            # Process all pages of the DM
            while True:
                _process_dm_page()
                # Navigate to next page using JS (handles kat-button and shadow DOM)
                try:
                    result = dm_page.evaluate(_JS_NEXT_PAGE)
                    if result and result != 'disabled':
                        logger.info(f"[{marketplace}/{ws}] DM paginated: {result}")
                        _ready(dm_page, timeout=10_000)
                        time.sleep(1)
                    else:
                        if result is None:
                            # Log all buttons so we can diagnose the pagination selector
                            try:
                                all_btns = dm_page.evaluate(_JS_ALL_BUTTONS)
                                logger.info(f"[{marketplace}/{ws}] DM buttons on page: {all_btns}")
                            except Exception:
                                pass
                        break
                except Exception as exc:
                    logger.debug(f"[{marketplace}/{ws}] DM pagination error: {exc}")
                    break

            remaining = len(queued) - len(already_clicked)
            if remaining > 0:
                logger.info(f"[{marketplace}/{ws}] {remaining} reports still in-progress ({elapsed}s elapsed)")
                time.sleep(_DM_POLL)
                elapsed += _DM_POLL
                # Navigate back to page 1 of DM using the SPA nav link.
                # reload() / goto() destroy kat-button rendering in the SPA context —
                # clicking the "Download Manager" link within the live SPA preserves it.
                try:
                    dm_page.locator("a.css-14njiag, a:has-text('Download Manager')").first.click(timeout=5_000)
                    _ready(dm_page, timeout=10_000)
                    dm_page.wait_for_selector("div[role='row']", timeout=15_000)
                except Exception:
                    # Last resort: try going back to first page via Previous/First button
                    try:
                        first_btn = dm_page.locator(
                            "button[aria-label='First page'], button[aria-label='first']"
                        ).first
                        if first_btn.is_visible(timeout=2_000):
                            first_btn.click(timeout=5_000)
                            _ready(dm_page, timeout=10_000)
                    except Exception:
                        pass

        if len(already_clicked) < len(queued):
            logger.warning(
                f"[{marketplace}/{ws}] Only {len(already_clicked)}/{len(queued)} files downloaded after {elapsed}s"
            )

        for asin in queued:
            if asin not in results:
                results[asin] = False

        return results

    # ── Discover mode ─────────────────────────────────────────────────────────

    def discover(self, marketplace: str) -> None:
        """Navigate to the SQP page and dump screenshots + interactive elements.

        Run this after setup to capture what the page actually looks like.
        Share the debug/ folder so selectors can be calibrated.
        """
        pg  = self.page
        url = _sqp_url(marketplace, last_available_week())

        print(f"[discover] Navigating to: {url}")
        pg.goto(url, timeout=30_000)
        _ready(pg)

        if _is_logged_out(pg):
            print("[discover] Session expired. Run setup first.")
            return

        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        _screenshot(pg, f"discover_{marketplace}")

        # Dump interactive elements — including those inside open shadow roots
        # (Amazon's Katal components render inputs/buttons in shadow DOM).
        elements = pg.evaluate("""() => {
            function collectEls(root) {
                const sel = 'input, button, select, a[href], [role="combobox"], [role="listbox"], [aria-label], kat-input, kat-button, kat-select';
                const found = Array.from(root.querySelectorAll(sel));
                for (const el of Array.from(root.querySelectorAll('*'))) {
                    if (el.shadowRoot) found.push(...collectEls(el.shadowRoot));
                }
                return found;
            }
            return collectEls(document).slice(0, 200).map(el => ({
                tag:         el.tagName,
                type:        el.type || null,
                id:          el.id || null,
                name:        el.name || null,
                class:       el.className?.toString().slice(0, 80) || null,
                ariaLabel:   el.getAttribute('aria-label'),
                placeholder: el.placeholder || el.getAttribute('placeholder') || null,
                text:        el.innerText?.trim().slice(0, 60) || null,
                testId:      el.getAttribute('data-testid'),
                href:        el.href || null,
                inShadow:    el.getRootNode() !== document,
            }));
        }""")

        out_json = DEBUG_DIR / f"discover_{marketplace}_elements.json"
        out_json.write_text(json.dumps(elements, indent=2, ensure_ascii=False), encoding="utf-8")

        # Inspect kat-select shadow DOM to understand how to set values
        kat_info = pg.evaluate("""() => {
            return Array.from(document.querySelectorAll('kat-select')).map(sel => ({
                label:      sel.getAttribute('label'),
                value:      sel.value,
                innerHTML:  sel.innerHTML.slice(0, 400),
                shadowHtml: sel.shadowRoot ? sel.shadowRoot.innerHTML.slice(0, 400) : null,
            }));
        }""")
        kat_json = DEBUG_DIR / f"discover_{marketplace}_katselects.json"
        kat_json.write_text(json.dumps(kat_info, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"\n[discover] Done for {marketplace}:")
        print(f"  Screenshot -> {DEBUG_DIR}/discover_{marketplace}.png")
        print(f"  Elements   -> {out_json}")
        print(f"  KatSelects -> {kat_json}")
        print(f"  Page URL   -> {pg.url}")
        print("\nShare the debug/ folder so selectors can be calibrated.")
