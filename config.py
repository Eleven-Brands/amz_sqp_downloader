import os
from pathlib import Path
from datetime import date, timedelta

# ── Paths ─────────────────────────────────────────────────────────────────────
# DRIVE_BASE resolves automatically to wherever this repo lives.
# No env var needed — just clone/move the folder and it works.
DRIVE_BASE = Path(__file__).parent

# LOCAL_BASE holds Chrome profile, logs, and temp downloads.
# Override with SQP_LOCAL_DIR if you want a different location.
LOCAL_BASE = Path(os.getenv("SQP_LOCAL_DIR", r"C:\SQP"))

RAW_DIR        = DRIVE_BASE / "raw"
PROCESSED_DIR  = DRIVE_BASE / "processed"
STATE_DIR      = DRIVE_BASE / "state"
DEBUG_DIR      = DRIVE_BASE / "debug"

LOG_DIR         = LOCAL_BASE / "logs"
CHROME_PROFILE  = LOCAL_BASE / "chrome_profile"
DOWNLOADS_TMP   = LOCAL_BASE / "downloads_tmp"

# Service account for BigQuery. Override with SQP_SERVICE_ACCOUNT.
# Default assumes the return_badge_predictor repo is a sibling of sqp_downloader.
SERVICE_ACCOUNT = Path(
    os.getenv(
        "SQP_SERVICE_ACCOUNT",
        str(DRIVE_BASE.parent / "return_badge_predictor" / "service_account.json"),
    )
)

# ── BigQuery ──────────────────────────────────────────────────────────────────
BQ_PROJECT       = "amazon-sp-api-openbridge"
BQ_LISTINGS_VIEW = "amazon-sp-api-openbridge.2_Silver_Aux.vw_all_listings_report"

BQ_SQP_DATASET         = "3_Bronze_Business_Reports"
BQ_SQP_TABLE           = "td_search_query_performance"
BQ_SQP_TABLE_ID        = f"{BQ_PROJECT}.{BQ_SQP_DATASET}.{BQ_SQP_TABLE}"

# Silver — combined + deduplicated (OpenBridge Silver + Bronze scraped)
BQ_SQP_SILVER_DATASET       = "2_Silver_Business_Reports"
BQ_SQP_OPENBRIDGE_VIEW      = f"{BQ_PROJECT}.{BQ_SQP_SILVER_DATASET}.vw_search_query_performance"
BQ_SQP_SILVER_VIEW          = "vw_sqp_combined"
BQ_SQP_SILVER_VIEW_ID       = f"{BQ_PROJECT}.{BQ_SQP_SILVER_DATASET}.{BQ_SQP_SILVER_VIEW}"

# Gold — ready for consumption
BQ_SQP_GOLD_DATASET         = "1_Gold_Business_Reports"
BQ_SQP_GOLD_VIEW            = "vw_search_query_performance"
BQ_SQP_GOLD_VIEW_ID         = f"{BQ_PROJECT}.{BQ_SQP_GOLD_DATASET}.{BQ_SQP_GOLD_VIEW}"

# ── Marketplaces ──────────────────────────────────────────────────────────────
# sc_url   : Seller Central base URL for this region
# sc_name  : Display name used in Seller Central's marketplace switcher
MARKETPLACES: dict[str, dict] = {
    "US": {"region": "NA", "sc_url": "https://sellercentral.amazon.com",    "sc_name": "United States",   "country_id": "us"},
    "CA": {"region": "NA", "sc_url": "https://sellercentral.amazon.com",    "sc_name": "Canada",          "country_id": "ca"},
    "MX": {"region": "NA", "sc_url": "https://sellercentral.amazon.com",    "sc_name": "Mexico",          "country_id": "mx"},
    "DE": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "Germany",         "country_id": "de"},
    "FR": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "France",          "country_id": "fr"},
    "IT": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "Italy",           "country_id": "it"},
    "ES": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "Spain",           "country_id": "es"},
    "GB": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "United Kingdom",  "country_id": "gb"},
    "IE": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "Ireland",         "country_id": "ie"},
    "NL": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "Netherlands",     "country_id": "nl"},
    "BE": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "Belgium",         "country_id": "be"},
    "PL": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "Poland",          "country_id": "pl"},
    "SE": {"region": "EU", "sc_url": "https://sellercentral.amazon.co.uk",  "sc_name": "Sweden",          "country_id": "se"},
}

# Marketplaces where Amazon SQP is available via Brand Analytics URL.
# IE, BE, PL return "Page not found" for the SQP dashboard — excluded.
SQP_MARKETPLACES: list[str] = [
    "US", "CA", "MX",
    "DE", "FR", "IT", "ES", "GB", "NL", "SE",
]

CURRENCY_MAP: dict[str, str] = {
    "US": "USD", "CA": "CAD", "MX": "MXN",
    "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
    "IE": "EUR", "NL": "EUR", "BE": "EUR",
    "PL": "PLN", "SE": "SEK", "GB": "GBP",
}

# ── Week helpers ──────────────────────────────────────────────────────────────

def week_start(d: date) -> date:
    """Sunday that opens the Amazon week containing d (Sun–Sat)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def last_available_week() -> date:
    """Most recently completed Amazon week.

    Amazon SQP data lags ~1–2 days after the week closes (Saturday).
    We conservatively return the week ending on the last Saturday.
    """
    today = date.today()
    days_since_sat = (today.weekday() - 5) % 7   # 0 if today is Sat
    if days_since_sat == 0:
        days_since_sat = 7                        # if today IS Saturday, use previous
    last_sat = today - timedelta(days=days_since_sat)
    return week_start(last_sat)


def weeks_in_range(start: date, end: date):
    """Yield all Amazon week-start Sundays from start to end, inclusive."""
    cur = week_start(start)
    while cur <= end:
        yield cur
        cur += timedelta(weeks=1)
