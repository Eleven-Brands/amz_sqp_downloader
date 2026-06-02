"""Upload processed/resultado_final.csv to BigQuery and maintain the combined Gold view.

Modes:
  incremental  (default) — appends only weeks not yet in BQ (per country)
  full                   — truncates the table and re-uploads everything

After uploading, always creates/replaces vw_sqp_combined, which UNIONs OpenBridge
(authoritative) with our scraped table (fills historical / coverage gaps).
"""
from __future__ import annotations

import logging
from typing import Literal

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account

from config import (
    BQ_PROJECT,
    BQ_SQP_TABLE_ID,
    BQ_SQP_OPENBRIDGE_VIEW,
    BQ_SQP_SILVER_VIEW_ID,
    BQ_SQP_GOLD_VIEW_ID,
    SERVICE_ACCOUNT,
    PROCESSED_DIR,
)

logger = logging.getLogger(__name__)

# ── Table schema (scraped data) ────────────────────────────────────────────────

SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("year",                         "INTEGER"),
    bigquery.SchemaField("week",                         "INTEGER"),
    bigquery.SchemaField("inventory_region_code",        "STRING"),
    bigquery.SchemaField("country_code",                 "STRING"),
    bigquery.SchemaField("price_currency",               "STRING"),
    bigquery.SchemaField("start_date",                   "DATE"),
    bigquery.SchemaField("end_date",                     "DATE"),
    bigquery.SchemaField("asin",                         "STRING"),
    bigquery.SchemaField("ob_date",                      "DATE"),
    bigquery.SchemaField("search_query",                 "STRING"),
    bigquery.SchemaField("search_query_score",           "FLOAT64"),
    bigquery.SchemaField("search_query_volume",          "INTEGER"),
    bigquery.SchemaField("IMP_total_count",              "INTEGER"),
    bigquery.SchemaField("IMP_asin_count",               "INTEGER"),
    bigquery.SchemaField("IMP_asin_share",               "FLOAT64"),
    bigquery.SchemaField("CLK_total_count",              "INTEGER"),
    bigquery.SchemaField("CLK_rate",                     "FLOAT64"),
    bigquery.SchemaField("CLK_asin_count",               "INTEGER"),
    bigquery.SchemaField("CLK_asin_share",               "FLOAT64"),
    bigquery.SchemaField("CLK_price_median",             "FLOAT64"),
    bigquery.SchemaField("CLK_asin_price_median",        "FLOAT64"),
    bigquery.SchemaField("CLK_same_day_shipping_count",  "INTEGER"),
    bigquery.SchemaField("CLK_one_day_shipping_count",   "INTEGER"),
    bigquery.SchemaField("CLK_two_day_shipping_count",   "INTEGER"),
    bigquery.SchemaField("CART_total_count",             "INTEGER"),
    bigquery.SchemaField("CART_rate",                    "FLOAT64"),
    bigquery.SchemaField("CART_asin_count",              "INTEGER"),
    bigquery.SchemaField("CART_asin_share",              "FLOAT64"),
    bigquery.SchemaField("CART_price_median",            "FLOAT64"),
    bigquery.SchemaField("CART_asin_price_median",       "FLOAT64"),
    bigquery.SchemaField("CART_same_day_shipping_count", "INTEGER"),
    bigquery.SchemaField("CART_one_day_shipping_count",  "INTEGER"),
    bigquery.SchemaField("CART_two_day_shipping_count",  "INTEGER"),
    bigquery.SchemaField("PUR_total_count",              "INTEGER"),
    bigquery.SchemaField("PUR_rate",                     "FLOAT64"),
    bigquery.SchemaField("PUR_asin_count",               "INTEGER"),
    bigquery.SchemaField("PUR_asin_share",               "FLOAT64"),
    bigquery.SchemaField("PUR_price_median",             "FLOAT64"),
    bigquery.SchemaField("PUR_asin_price_median",        "FLOAT64"),
    bigquery.SchemaField("PUR_same_day_shipping_count",  "INTEGER"),
    bigquery.SchemaField("PUR_one_day_shipping_count",   "INTEGER"),
    bigquery.SchemaField("PUR_two_day_shipping_count",   "INTEGER"),
]

_SCHEMA_COLS = [f.name for f in SCHEMA]

_PCT_COLS = [
    "IMP_asin_share",
    "CLK_rate", "CLK_asin_share",
    "CART_rate", "CART_asin_share",
    "PUR_rate", "PUR_asin_share",
]

_INT_COLS = [
    "year", "week", "search_query_volume",
    "IMP_total_count", "IMP_asin_count",
    "CLK_total_count", "CLK_asin_count",
    "CLK_same_day_shipping_count", "CLK_one_day_shipping_count", "CLK_two_day_shipping_count",
    "CART_total_count", "CART_asin_count",
    "CART_same_day_shipping_count", "CART_one_day_shipping_count", "CART_two_day_shipping_count",
    "PUR_total_count", "PUR_asin_count",
    "PUR_same_day_shipping_count", "PUR_one_day_shipping_count", "PUR_two_day_shipping_count",
]

_DATE_COLS = ["ob_date", "start_date", "end_date"]

# ── View SQL ───────────────────────────────────────────────────────────────────
# Silver: combines OpenBridge (authoritative) with our Bronze scraped table.
# - ob_date is normalised to start_date (Sunday) in both sources for consistency.
#   OpenBridge stores ob_date = end_date (Saturday); our scraper stores ob_date = start_date.
# - OpenBridge rows take priority; scraped rows fill historical / coverage gaps.
# - data_source column kept for auditability.

_SILVER_VIEW_SQL = f"""
/*****************************************************************************************************************
Create Date:  2026-06-02
Author:       Lucca Lanzellotti
Description:  Combines weekly Search Query Performance data from two sources:
              (1) OpenBridge connector (authoritative, live feed) and
              (2) td_search_query_performance (scraped backfill covering history
              not yet available via OpenBridge).
              OpenBridge rows take priority; scraped rows are included only when
              no matching row exists in OpenBridge for the same
              (asin, start_date, country_code, search_query) key.
              ob_date is normalised to start_date (Sunday) across both sources.
              A data_source column ('openbridge' | 'scraped') is retained for auditability.
******************************************************************************************************************/
CREATE OR REPLACE VIEW `{BQ_SQP_SILVER_VIEW_ID}` AS

WITH openbridge AS (
  SELECT
    year, week,
    inventory_region_code, country_code, price_currency,
    start_date, end_date,
    asin,
    start_date                          AS ob_date,
    search_query,
    CAST(search_query_score AS FLOAT64) AS search_query_score,
    search_query_volume,
    IMP_total_count, IMP_asin_count, IMP_asin_share,
    CLK_total_count, CLK_rate, CLK_asin_count, CLK_asin_share,
    CLK_price_median, CLK_asin_price_median,
    CLK_same_day_shipping_count, CLK_one_day_shipping_count, CLK_two_day_shipping_count,
    CART_total_count, CART_rate, CART_asin_count, CART_asin_share,
    CART_price_median, CART_asin_price_median,
    CART_same_day_shipping_count, CART_one_day_shipping_count, CART_two_day_shipping_count,
    PUR_total_count, PUR_rate, PUR_asin_count, PUR_asin_share,
    PUR_price_median, PUR_asin_price_median,
    PUR_same_day_shipping_count, PUR_one_day_shipping_count, PUR_two_day_shipping_count,
    'openbridge' AS data_source
  FROM `{BQ_SQP_OPENBRIDGE_VIEW}`
),

scraped AS (
  SELECT
    year, week,
    inventory_region_code, country_code, price_currency,
    start_date, end_date,
    asin,
    start_date AS ob_date,
    search_query,
    search_query_score,
    search_query_volume,
    IMP_total_count, IMP_asin_count, IMP_asin_share,
    CLK_total_count, CLK_rate, CLK_asin_count, CLK_asin_share,
    CLK_price_median, CLK_asin_price_median,
    CLK_same_day_shipping_count, CLK_one_day_shipping_count, CLK_two_day_shipping_count,
    CART_total_count, CART_rate, CART_asin_count, CART_asin_share,
    CART_price_median, CART_asin_price_median,
    CART_same_day_shipping_count, CART_one_day_shipping_count, CART_two_day_shipping_count,
    PUR_total_count, PUR_rate, PUR_asin_count, PUR_asin_share,
    PUR_price_median, PUR_asin_price_median,
    PUR_same_day_shipping_count, PUR_one_day_shipping_count, PUR_two_day_shipping_count,
    'scraped' AS data_source
  FROM `{BQ_SQP_TABLE_ID}`
),

scraped_only AS (
  SELECT s.*
  FROM scraped s
  LEFT JOIN openbridge ob
    ON  s.asin         = ob.asin
    AND s.start_date   = ob.start_date
    AND s.country_code = ob.country_code
    AND s.search_query = ob.search_query
  WHERE ob.asin IS NULL
)

SELECT * FROM openbridge
UNION ALL
SELECT * FROM scraped_only
"""

# Gold: clean consumption layer — strips data_source, exposes the Silver combined view.

_GOLD_VIEW_SQL = f"""
/*****************************************************************************************************************
Create Date:  2026-06-02
Author:       Lucca Lanzellotti
Description:  Gold consumption layer for Search Query Performance data.
              Exposes the deduplicated Silver view (vw_sqp_combined) without the
              internal data_source column, providing a clean, unified weekly SQP
              dataset ready for reporting in Power BI and other downstream tools.
              Source priority: OpenBridge (live) > scraped backfill.
******************************************************************************************************************/
CREATE OR REPLACE VIEW `{BQ_SQP_GOLD_VIEW_ID}` AS
SELECT * EXCEPT (data_source)
FROM `{BQ_SQP_SILVER_VIEW_ID}`
"""


# ── BQ helpers ─────────────────────────────────────────────────────────────────

def _bq_client() -> bigquery.Client:
    creds = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT),
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    return bigquery.Client(credentials=creds, project=BQ_PROJECT)


def _ensure_dataset(client: bigquery.Client, dataset_id: str) -> None:
    ds = bigquery.Dataset(dataset_id)
    ds.location = "US"
    client.create_dataset(ds, exists_ok=True)


# ── Data cleaning ──────────────────────────────────────────────────────────────

def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in _PCT_COLS:
        if col in df.columns:
            df[col] = (
                df[col].astype(str)
                       .str.replace("%", "", regex=False)
                       .str.strip()
                       .replace({"": None, "nan": None, "--": None, "None": None})
            )
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in _INT_COLS:
        if col in df.columns:
            df[col] = (
                df[col].astype(str)
                       .str.replace(",", "", regex=False)
                       .str.strip()
                       .replace({"": None, "nan": None, "--": None, "None": None})
            )
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    for col in _DATE_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    for col in ("search_query_score", "CLK_price_median", "CLK_asin_price_median",
                "CART_price_median", "CART_asin_price_median",
                "PUR_price_median",  "PUR_asin_price_median"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def _max_ob_date_per_country(client: bigquery.Client) -> dict[str, object]:
    """Return {country_code: max(ob_date)} from the scraped table, or {} if it doesn't exist."""
    try:
        rows = client.query(
            f"SELECT country_code, MAX(ob_date) AS max_date FROM `{BQ_SQP_TABLE_ID}` GROUP BY 1"
        ).result()
        return {row.country_code: row.max_date for row in rows}
    except Exception:
        return {}


# ── Public API ─────────────────────────────────────────────────────────────────

def create_views(client: bigquery.Client | None = None) -> None:
    """Create or replace the Silver combined view and Gold consumption view."""
    if client is None:
        client = _bq_client()
    silver_dataset = BQ_SQP_SILVER_VIEW_ID.rsplit(".", 1)[0]
    gold_dataset   = BQ_SQP_GOLD_VIEW_ID.rsplit(".", 1)[0]
    _ensure_dataset(client, silver_dataset)
    _ensure_dataset(client, gold_dataset)
    client.query(_SILVER_VIEW_SQL).result()
    logger.info(f"[bq_ingest] Silver view updated: {BQ_SQP_SILVER_VIEW_ID}")
    client.query(_GOLD_VIEW_SQL).result()
    logger.info(f"[bq_ingest] Gold view updated: {BQ_SQP_GOLD_VIEW_ID}")


def run(mode: Literal["full", "incremental"] = "incremental") -> int:
    """Upload resultado_final.csv to BQ, then refresh the combined view.

    Returns number of rows uploaded (0 if nothing new).
    """
    csv_path = PROCESSED_DIR / "resultado_final.csv"
    if not csv_path.exists():
        logger.error("[bq_ingest] resultado_final.csv not found — run 'process' first.")
        return 0

    logger.info(f"[bq_ingest] Reading {csv_path} ...")
    df = pd.read_csv(csv_path, low_memory=False)
    df = _clean(df)

    available = [c for c in _SCHEMA_COLS if c in df.columns]
    missing   = [c for c in _SCHEMA_COLS if c not in df.columns]
    if missing:
        logger.warning(f"[bq_ingest] Missing columns (will be NULL in BQ): {missing}")
    df = df[available].copy()

    client = _bq_client()
    dataset_id = BQ_SQP_TABLE_ID.rsplit(".", 1)[0]
    _ensure_dataset(client, dataset_id)

    if mode == "full":
        write_disposition = bigquery.WriteDisposition.WRITE_TRUNCATE
        logger.info(f"[bq_ingest] Mode: full — truncating {BQ_SQP_TABLE_ID}")
    else:
        max_dates = _max_ob_date_per_country(client)
        if max_dates:
            original_len = len(df)
            for country, cutoff in max_dates.items():
                if cutoff is None:
                    continue
                mask_old = (df["country_code"] == country) & (df["ob_date"] <= cutoff)
                df = df[~mask_old]
            logger.info(
                f"[bq_ingest] Incremental: skipped {original_len - len(df):,} already-loaded rows."
            )

        if df.empty:
            logger.info("[bq_ingest] Nothing new to upload — refreshing view anyway.")
            create_views(client)
            return 0

        write_disposition = bigquery.WriteDisposition.WRITE_APPEND
        logger.info(f"[bq_ingest] Mode: incremental — appending {len(df):,} rows.")

    schema_for_upload = [f for f in SCHEMA if f.name in available]
    job_config = bigquery.LoadJobConfig(
        schema=schema_for_upload,
        write_disposition=write_disposition,
    )

    logger.info(f"[bq_ingest] Uploading {len(df):,} rows to {BQ_SQP_TABLE_ID} ...")
    job = client.load_table_from_dataframe(df, BQ_SQP_TABLE_ID, job_config=job_config)
    job.result()

    rows_uploaded = len(df)
    logger.info(f"[bq_ingest] Upload done — {rows_uploaded:,} rows.")

    create_views(client)
    return rows_uploaded


if __name__ == "__main__":
    import sys
    _mode = sys.argv[1] if len(sys.argv) > 1 else "incremental"
    run(_mode)
