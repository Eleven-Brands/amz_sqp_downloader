"""
Import SU (Seller Utilities) historical data into processed/resultado_final.csv.

SU files use Saturday as Reporting Date (week end).
Pipeline uses Sunday as ob_date (week start) = Saturday - 6 days.

Only imports weeks NOT already covered by the pipeline (before 2025-12-28 for US).
"""
from __future__ import annotations
from datetime import timedelta
from pathlib import Path

import pandas as pd

from config import PROCESSED_DIR, CURRENCY_MAP
from tratamento import RENAME, FINAL_COLS

SU_DIR = Path(
    "G:/Shared drives/OrganiHaus/3.1 - OH Data & Reports"
    "/z_personal_folders/lucca_lanzellotti/Projetos/SQP/SU"
)

SU_FILES = [
    "SQP_US_Y2025.csv",
    "SQP_US_Y2025W47.csv",
    "SQP_US_Y2025W48.csv",
    "SQP_US_Y2025W49.csv",
    "multi-asin-sqp-data-2026-05-13T23_04_41.csv",
    # multi-05-14 overlap com pipeline (2026-01-03+) — ignorado
]

# Pipeline US começa em 2025-12-28 (domingo).
# Correspondente sábado (Reporting Date SU) = 2026-01-03.
# Importar apenas semanas com Reporting Date < 2026-01-03.
PIPELINE_US_START_SAT = pd.Timestamp("2026-01-03")


def run() -> None:
    frames: list[pd.DataFrame] = []

    for fname in SU_FILES:
        fpath = SU_DIR / fname
        if not fpath.exists():
            print(f"[import_su] SKIP (not found): {fname}")
            continue

        df = pd.read_csv(fpath, low_memory=False)
        df["Reporting Date"] = pd.to_datetime(df["Reporting Date"])

        # Filtrar só semanas antes do pipeline
        before = df[df["Reporting Date"] < PIPELINE_US_START_SAT].copy()
        skipped = len(df) - len(before)
        print(f"[import_su] {fname}: {len(df):,} rows -> {len(before):,} kept ({skipped:,} overlap skipped)")

        if before.empty:
            continue

        frames.append(before)

    if not frames:
        print("[import_su] Nenhum dado novo para importar.")
        return

    df = pd.concat(frames, ignore_index=True)

    # Deduplica (mesma semana pode aparecer em mais de um arquivo)
    before_dedup = len(df)
    df = df.drop_duplicates(subset=["ASIN", "Search Query", "Reporting Date"])
    print(f"[import_su] Dedup: {before_dedup:,} -> {len(df):,} rows")

    # Renomear colunas para o schema do pipeline
    df = df.rename(columns=RENAME)

    # ob_date = sábado - 6 dias = domingo (início da semana Amazon)
    reporting_date = pd.to_datetime(df["ob_date"])  # já renomeado de Reporting Date
    df["ob_date"]     = reporting_date - timedelta(days=6)
    df["start_date"]  = df["ob_date"]
    df["end_date"]    = reporting_date

    iso = df["ob_date"].dt.isocalendar()
    df["year"] = iso.year.astype(int)
    df["week"] = iso.week.astype(int)

    df["country_code"]          = "US"
    df["inventory_region_code"] = "US"
    df["price_currency"]        = "USD"

    # Consolidar colunas duplicadas (mesmo padrão do tratamento.py)
    seen: set[str] = set()
    dupes: set[str] = set()
    for col in df.columns:
        (dupes if col in seen else seen).add(col)
    for col in dupes:
        mask   = df.columns == col
        merged = df.loc[:, mask].bfill(axis=1).iloc[:, 0]
        df     = df.loc[:, ~mask].copy()
        df[col] = merged

    # Selecionar colunas finais
    available = [c for c in FINAL_COLS if c in df.columns]
    missing   = [c for c in FINAL_COLS if c not in df.columns]
    if missing:
        print(f"[import_su] Colunas ausentes (serao NaN): {missing}")
        for c in missing:
            df[c] = None

    su_final = df[FINAL_COLS].copy()
    print(f"[import_su] {len(su_final):,} linhas novas | periodo: {su_final['ob_date'].min()} -> {su_final['ob_date'].max()}")

    # Append no resultado_final.csv existente
    out = PROCESSED_DIR / "resultado_final.csv"
    existing = pd.read_csv(out, low_memory=False)
    print(f"[import_su] resultado_final atual: {len(existing):,} linhas")

    combined = pd.concat([existing, su_final], ignore_index=True)

    # Dedup final por marketplace+asin+query+semana
    before_final_dedup = len(combined)
    combined = combined.drop_duplicates(
        subset=["country_code", "asin", "search_query", "ob_date"]
    )
    print(f"[import_su] Dedup final: {before_final_dedup:,} -> {len(combined):,} linhas")

    combined.to_csv(out, index=False)
    print(f"[import_su] Salvo: {out}")
    print(f"[import_su] Total final: {len(combined):,} linhas")


if __name__ == "__main__":
    run()
