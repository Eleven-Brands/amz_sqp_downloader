"""Consolidate all raw CSVs (one per ASIN/week) into processed/resultado_final.csv."""
from __future__ import annotations
from datetime import timedelta
from pathlib import Path

import pandas as pd

from config import RAW_DIR, PROCESSED_DIR, CURRENCY_MAP

RENAME: dict[str, str] = {
    # Full Amazon export format (kept for backwards compatibility)
    "ASIN":                              "asin",
    "Reporting Date":                    "ob_date",
    "Impressions: Total Count":          "IMP_total_count",
    "Impressions: ASIN Count":           "IMP_asin_count",
    "Impressions: ASIN Share %":         "IMP_asin_share",
    "Clicks: Total Count":               "CLK_total_count",
    "Clicks: Click Rate %":              "CLK_rate",
    "Clicks: ASIN Count":                "CLK_asin_count",
    "Clicks: ASIN Share %":              "CLK_asin_share",
    "Clicks: Price (Median)":            "CLK_price_median",
    "Clicks: ASIN Price (Median)":       "CLK_asin_price_median",
    "Clicks: Same Day Shipping Speed":   "CLK_same_day_shipping_count",
    "Clicks: 1D Shipping Speed":         "CLK_one_day_shipping_count",
    "Clicks: 2D Shipping Speed":         "CLK_two_day_shipping_count",
    "Cart Adds: Total Count":            "CART_total_count",
    "Cart Adds: Cart Add Rate %":        "CART_rate",
    "Cart Adds: ASIN Count":             "CART_asin_count",
    "Cart Adds: ASIN Share %":           "CART_asin_share",
    "Cart Adds: Price (Median)":         "CART_price_median",
    "Cart Adds: ASIN Price (Median)":    "CART_asin_price_median",
    "Cart Adds: Same Day Shipping Speed":"CART_same_day_shipping_count",
    "Cart Adds: 1D Shipping Speed":      "CART_one_day_shipping_count",
    "Cart Adds: 2D Shipping Speed":      "CART_two_day_shipping_count",
    "Purchases: Total Count":            "PUR_total_count",
    "Purchases: Purchase Rate %":        "PUR_rate",
    "Purchases: ASIN Count":             "PUR_asin_count",
    "Purchases: ASIN Share %":           "PUR_asin_share",
    "Purchases: Price (Median)":         "PUR_price_median",
    "Purchases: ASIN Price (Median)":    "PUR_asin_price_median",
    "Purchases: Same Day Shipping Speed":"PUR_same_day_shipping_count",
    "Purchases: 1D Shipping Speed":      "PUR_one_day_shipping_count",
    "Purchases: 2D Shipping Speed":      "PUR_two_day_shipping_count",
    # Scraped DOM format (pandas auto-deduped column names)
    "Search Query":                      "search_query",
    "Search Query Score":                "search_query_score",
    "Search Query Volume":               "search_query_volume",
    "Total Count":                       "IMP_total_count",
    "ASIN Count":                        "IMP_asin_count",
    "ASIN Share":                        "IMP_asin_share",
    "Total Count.1":                     "CLK_total_count",
    "Click Rate":                        "CLK_rate",
    "ASIN Count.1":                      "CLK_asin_count",
    "ASIN Share.1":                      "CLK_asin_share",
    "Total Count.2":                     "CART_total_count",
    "Cart Add Rate":                     "CART_rate",
    "ASIN Count.2":                      "CART_asin_count",
    "ASIN Share.2":                      "CART_asin_share",
    "Total Count.3":                     "PUR_total_count",
    "Purchase Rate":                     "PUR_rate",
    "ASIN Count.3":                      "PUR_asin_count",
    "ASIN Share.3":                      "PUR_asin_share",
}

FINAL_COLS: list[str] = [
    "year", "week",
    "inventory_region_code", "country_code", "price_currency",
    "start_date", "end_date", "asin", "ob_date",
    "search_query", "search_query_score", "search_query_volume",
    "IMP_total_count", "IMP_asin_count", "IMP_asin_share",
    "CLK_total_count", "CLK_rate", "CLK_asin_count", "CLK_asin_share",
    "CLK_price_median", "CLK_asin_price_median",
    "CLK_same_day_shipping_count", "CLK_one_day_shipping_count", "CLK_two_day_shipping_count",
    "CART_total_count", "CART_rate", "CART_asin_count", "CART_asin_share",
    "CART_price_median", "CART_asin_price_median",
    "CART_same_day_shipping_count", "CART_one_day_shipping_count", "CART_two_day_shipping_count",
    "PUR_total_count", "PUR_rate", "PUR_asin_count", "PUR_asin_share",
    "PUR_price_median", "PUR_asin_price_median",
    "PUR_same_day_shipping_count", "PUR_one_day_shipping_count", "PUR_two_day_shipping_count",
]


def run() -> None:
    all_files = list(RAW_DIR.glob("*/*.csv"))
    if not all_files:
        print("[tratamento] No CSV files found in raw/ — nothing to process.")
        return

    print(f"[tratamento] Reading {len(all_files)} files...")
    frames: list[pd.DataFrame] = []
    for f in all_files:
        mkt = f.parent.name.upper()
        # filename: {ASIN}_{YYYY-MM-DD}.csv — extract week-start date
        week_date = f.stem.split("_", 1)[1] if "_" in f.stem else None
        asin      = f.stem.split("_")[0]
        df = pd.read_csv(f, low_memory=False)
        df["_mkt"]       = mkt
        df["_week_date"] = week_date
        df = df.drop(columns=["ASIN", "Marketplace", "Reporting Date"], errors="ignore")
        df["asin"]       = asin
        frames.append(df)

    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    df = df.rename(columns=RENAME)

    # Consolidate duplicate column names that arise when API and DOM files are mixed.
    # Both formats map different source names to the same target ("Impressions: Total Count"
    # and "Total Count" both rename to "IMP_total_count"). After concat+rename the DataFrame
    # has two columns with the identical string name; merge them by coalescing non-null values.
    seen: set[str] = set()
    dupes: set[str] = set()
    for col in df.columns:
        (dupes if col in seen else seen).add(col)
    for col in dupes:
        mask = df.columns == col
        cols = df.loc[:, mask]
        merged = cols.iloc[:, 0]
        for i in range(1, cols.shape[1]):
            merged = merged.combine_first(cols.iloc[:, i])
        df = df.loc[:, ~mask].copy()
        df[col] = merged

    # Always use filename-derived date (reliable ISO format regardless of CSV content)
    df["ob_date"] = pd.to_datetime(df["_week_date"])
    df["start_date"] = df["ob_date"]
    df["end_date"]   = df["ob_date"] + timedelta(days=6)

    iso = df["start_date"].dt.isocalendar()
    df["year"] = iso.year.astype(int)
    df["week"] = iso.week.astype(int)

    df["country_code"]          = df["_mkt"]
    df["inventory_region_code"] = df["_mkt"]
    df["price_currency"]        = df["_mkt"].map(CURRENCY_MAP).fillna("USD")

    # Keep only columns defined in FINAL_COLS that actually exist
    available = [c for c in FINAL_COLS if c in df.columns]
    missing   = [c for c in FINAL_COLS if c not in df.columns]
    if missing:
        print(f"[tratamento] Warning — missing columns (will be skipped): {missing}")

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / "resultado_final.csv"
    df[available].to_csv(out, index=False)
    print(f"[tratamento] {len(df):,} rows -> {out}")


if __name__ == "__main__":
    run()
