from __future__ import annotations
from functools import lru_cache
from datetime import date

from google.oauth2 import service_account
from google.cloud import bigquery

from config import BQ_PROJECT, BQ_LISTINGS_VIEW, SERVICE_ACCOUNT


@lru_cache(maxsize=1)
def _client() -> bigquery.Client:
    creds = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT),
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    return bigquery.Client(credentials=creds, project=BQ_PROJECT)


# Cache by (marketplace, year-month) to avoid redundant BQ calls during backfill.
# ASINs are re-queried at most once per marketplace per calendar month.
@lru_cache(maxsize=256)
def _get_asins_cached(marketplace: str, year_month: str) -> tuple[str, ...]:
    ref = date.fromisoformat(f"{year_month}-01")
    return tuple(_fetch_asins(marketplace, ref))


def _fetch_asins(marketplace: str, reference_date: date) -> list[str]:
    query = f"""
    WITH latest AS (
        SELECT MAX(ob_date) AS ob_date
        FROM `{BQ_LISTINGS_VIEW}`
        WHERE sales_country = @mkt
          AND ob_date <= @ref
    )
    SELECT DISTINCT l.asin
    FROM `{BQ_LISTINGS_VIEW}` l
    JOIN latest USING (ob_date)
    WHERE l.sales_country = @mkt
      AND l.status = 'Active'
    ORDER BY l.asin
    """
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("mkt", "STRING", marketplace),
        bigquery.ScalarQueryParameter("ref", "DATE",   reference_date.isoformat()),
    ])
    return [row.asin for row in _client().query(query, job_config=cfg).result()]


def get_asins(marketplace: str, reference_date: date) -> list[str]:
    """Return active ASINs for a marketplace, using the snapshot closest to reference_date."""
    year_month = reference_date.strftime("%Y-%m")
    return list(_get_asins_cached(marketplace, year_month))
