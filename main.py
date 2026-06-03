"""SQP Downloader — orchestrator.

Usage examples
--------------
# First time: log in and save session
python main.py setup

# Capture screenshots of the SQP page (do this before bulk runs)
python main.py discover --marketplace US

# Smoke test: one ASIN, last available week
python main.py test --marketplace US

# Download last week for all marketplaces
python main.py weekly

# Backfill from 2026-W1 onwards, skip US (already done)
python main.py backfill --from-date 2025-12-28 --skip US

# Re-run consolidation only
python main.py process
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from datetime import date
from pathlib import Path

from config import (
    DRIVE_BASE, LOCAL_BASE, LOG_DIR, MARKETPLACES, SQP_MARKETPLACES,
    RAW_DIR, CATALOG_RAW_DIR, STATE_DIR, last_available_week, week_start, weeks_in_range,
)
from bq_client import get_asins
from downloader import SQPDownloader, SessionExpiredError
import tratamento
import bq_ingest
import notifier

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "sqp.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")

# ── Progress tracking ─────────────────────────────────────────────────────────
PROGRESS_FILE = STATE_DIR / "progress.json"


def _load_progress() -> set[tuple]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if PROGRESS_FILE.exists():
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        return {tuple(x) for x in data}
    return set()


def _save_progress(done: set[tuple]) -> None:
    PROGRESS_FILE.write_text(
        json.dumps(sorted(list(done)), indent=2),
        encoding="utf-8",
    )


def _is_done(done: set, mkt: str, asin: str, ws: date) -> bool:
    return (mkt, asin, ws.isoformat()) in done


def _mark_done(done: set, mkt: str, asin: str, ws: date) -> None:
    done.add((mkt, asin, ws.isoformat()))


# ── Modes ─────────────────────────────────────────────────────────────────────

def cmd_setup(marketplace: str) -> None:
    with SQPDownloader(headless=False) as dl:
        dl.setup_session(marketplace)


def cmd_discover(marketplace: str) -> None:
    sc_url = MARKETPLACES[marketplace]["sc_url"]
    with SQPDownloader(headless=False) as dl:
        if not dl.check_session(sc_url):
            print("Session expired. Run 'python main.py setup' first.")
            return
        dl.discover(marketplace)


def cmd_test(marketplace: str, asin: str | None) -> None:
    ws = last_available_week()
    asins = get_asins(marketplace, ws)
    if not asins:
        print(f"No active ASINs found for {marketplace} around {ws}")
        return

    target_asin = asin or asins[0]
    save_dir = RAW_DIR / marketplace
    logger.info(f"TEST: {marketplace} | {target_asin} | week starting {ws}")

    sc_url = MARKETPLACES[marketplace]["sc_url"]
    with SQPDownloader(headless=False) as dl:
        if not dl.check_session(sc_url):
            print("Session expired. Run setup first.")
            return
        ok = dl.switch_marketplace(marketplace)
        if ok:
            ok = dl.download_one(marketplace, target_asin, ws, save_dir)

    if ok:
        print(f"\nSUCCESS — file saved to: {save_dir}")
        print("Run 'python main.py process' to consolidate into resultado_final.csv")
    else:
        print(f"\nFAILED — check screenshots in: {DRIVE_BASE / 'debug'}")


_NA = {"US", "CA", "MX"}


def _weekly_region(
    markets: list[str],
    ws: date,
    done: set,
    lock: threading.Lock,
    errors: list[str],
) -> None:
    """Download one region (NA or EU) in its own browser instance."""
    setup_mkt = "US" if markets[0] in _NA else "DE"
    with SQPDownloader(headless=False) as dl:
        for mkt in markets:
            sc_url = MARKETPLACES[mkt]["sc_url"]
            if not dl.check_session(sc_url):
                errors.append(
                    f"Session expired before {mkt}. "
                    f"Run: python main.py setup --marketplace {setup_mkt}"
                )
                with lock:
                    _save_progress(done)
                return

            dl.switch_marketplace(mkt)
            asins = get_asins(mkt, ws)
            logger.info(f"  {mkt}: {len(asins)} ASINs")

            try:
                for asin in asins:
                    with lock:
                        if (mkt, asin, ws.isoformat()) in done:
                            continue
                    ok = dl.download_one(mkt, asin, ws, RAW_DIR / mkt)
                    if ok:
                        with lock:
                            _mark_done(done, mkt, asin, ws)
                with lock:
                    _save_progress(done)
            except SessionExpiredError:
                errors.append(
                    f"Session expired during {mkt}. "
                    f"Run: python main.py setup --marketplace {setup_mkt}"
                )
                with lock:
                    _save_progress(done)
                return


def cmd_weekly(
    markets: list[str] | None,
    na_week: date | None = None,
    eu_week: date | None = None,
) -> None:
    default_week = last_available_week()
    ws_na = na_week or default_week
    ws_eu = eu_week or default_week
    targets = markets or list(SQP_MARKETPLACES)
    done    = _load_progress()
    lock    = threading.Lock()
    errors: list[str] = []

    na = [m for m in targets if m in _NA]
    eu = [m for m in targets if m not in _NA]

    week_info = []
    if na:
        week_info.append(f"NA week {ws_na}")
    if eu:
        week_info.append(f"EU week {ws_eu}")
    logger.info(f"Weekly run — {', '.join(week_info)} — {targets} (parallel NA + EU)")

    threads = [
        threading.Thread(target=_weekly_region, args=(grp, ws, done, lock, errors), daemon=True)
        for grp, ws in [(na, ws_na), (eu, ws_eu)] if grp
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        for err in errors:
            logger.error(err)
        setup_hint = "\n".join(f"`{e}`" for e in errors)
        notifier.send("Session expired", "\n".join(errors))
        notifier.send_clickup(f"⚠️ **SQP Download — sessão expirada**\n{setup_hint}")
        return

    tratamento.run()
    rows = bq_ingest.run("incremental")
    week_summary = " / ".join(dict.fromkeys(str(w) for w in [ws_na, ws_eu] if w))
    notifier.send("Weekly run complete", f"Week {week_summary} done for: {', '.join(targets)}")
    notifier.send_clickup(
        f"✅ **SQP Download concluído** — semana `{week_summary}`\n"
        f"Marketplaces: {', '.join(targets)}\n"
        f"BigQuery: `{rows:,}` linhas carregadas → `vw_sqp_combined` atualizado"
    )
    logger.info("Weekly run complete.")


def cmd_backfill(
    from_date: date,
    to_date: date,
    markets: list[str] | None,
    skip: list[str] | None,
    no_ingest: bool = False,
) -> None:
    all_weeks = list(weeks_in_range(from_date, to_date))
    skip_set  = set(skip or [])
    targets   = markets or [m for m in SQP_MARKETPLACES if m not in skip_set]
    done      = _load_progress()

    logger.info(
        f"Backfill: {from_date} -> {to_date} | {len(all_weeks)} weeks | markets: {targets}"
    )

    with SQPDownloader(headless=False) as dl:
        for mkt in targets:
            sc_url = MARKETPLACES[mkt]["sc_url"]

            if not dl.check_session(sc_url):
                msg = f"Session expired before {mkt} backfill. Log in and re-run."
                logger.error(msg)
                notifier.send("Session expired", msg)
                _save_progress(done)
                return

            dl.switch_marketplace(mkt)

            for ws in all_weeks:
                asins = get_asins(mkt, ws)
                logger.info(f"  {mkt} week {ws}: {len(asins)} ASINs")

                try:
                    for asin in asins:
                        if (mkt, asin, ws.isoformat()) in done:
                            continue
                        ok = dl.download_one(mkt, asin, ws, RAW_DIR / mkt)
                        if ok:
                            _mark_done(done, mkt, asin, ws)
                    _save_progress(done)
                except SessionExpiredError:
                    msg = f"Session expired at {mkt}/{ws}. Log in and re-run."
                    logger.error(msg)
                    notifier.send("Session expired", msg)
                    _save_progress(done)
                    return

    tratamento.run()
    if no_ingest:
        logger.info("Backfill complete (BQ ingest skipped).")
        notifier.send("Backfill complete (no ingest)", f"{from_date} -> {to_date} | {', '.join(targets)}")
        return
    rows = bq_ingest.run("incremental")
    notifier.send(
        "Backfill complete",
        f"{from_date} → {to_date} | markets: {', '.join(targets)} | BQ: {rows:,} rows"
    )
    logger.info("Backfill complete.")


def cmd_catalog_weekly(markets: list[str] | None, ws: date | None) -> None:
    """Download Brand Catalog Performance for one week across all (or specified) marketplaces."""
    target_week = ws or last_available_week()
    targets     = markets or list(SQP_MARKETPLACES)
    errors: list[str] = []

    logger.info(f"Catalog weekly run — week {target_week} — {targets}")

    with SQPDownloader(headless=False) as dl:
        for mkt in targets:
            sc_url = MARKETPLACES[mkt]["sc_url"]
            if not dl.check_session(sc_url):
                errors.append(f"Session expired before {mkt}. Run: python main.py setup --marketplace {mkt}")
                break
            dl.switch_marketplace(mkt)
            save_dir = CATALOG_RAW_DIR / mkt
            try:
                ok = dl.download_catalog_week(mkt, target_week, save_dir)
                if not ok:
                    errors.append(f"{mkt} week {target_week}: download failed")
            except SessionExpiredError:
                errors.append(f"Session expired at {mkt}. Run setup and retry.")
                break

    if errors:
        for err in errors:
            logger.error(err)
        setup_hint = "\n".join(f"`{e}`" for e in errors)
        notifier.send("Session expired (catalog)", "\n".join(errors))
        notifier.send_clickup(f"⚠️ **Catalog Download — sessão expirada**\n{setup_hint}")
    else:
        logger.info(f"Catalog weekly done — week {target_week} — {targets}")
        notifier.send("Catalog weekly complete", f"Week {target_week} | {', '.join(targets)}")
        notifier.send_clickup(
            f"✅ **Catalog Download concluído** — semana `{target_week}`\n"
            f"Marketplaces: {', '.join(targets)}"
        )
        print(f"\nFiles saved to: {CATALOG_RAW_DIR}")


def cmd_catalog_backfill(from_date: date, to_date: date, markets: list[str] | None) -> None:
    """Download Brand Catalog Performance for all weeks in a date range."""
    all_weeks = list(weeks_in_range(from_date, to_date))
    targets   = markets or list(SQP_MARKETPLACES)

    logger.info(f"Catalog backfill: {from_date} -> {to_date} | {len(all_weeks)} weeks | {targets}")

    with SQPDownloader(headless=False) as dl:
        for mkt in targets:
            sc_url = MARKETPLACES[mkt]["sc_url"]
            if not dl.check_session(sc_url):
                msg = f"Session expired before {mkt} catalog backfill. Log in and re-run."
                logger.error(msg)
                notifier.send("Session expired (catalog backfill)", msg)
                notifier.send_clickup(f"⚠️ **Catalog Backfill — sessão expirada**\n`{msg}`")
                return
            dl.switch_marketplace(mkt)
            save_dir = CATALOG_RAW_DIR / mkt
            for ws in all_weeks:
                try:
                    dl.download_catalog_week(mkt, ws, save_dir)
                except SessionExpiredError:
                    msg = f"Session expired at {mkt}/{ws}. Log in and re-run."
                    logger.error(msg)
                    notifier.send("Session expired (catalog backfill)", msg)
                    notifier.send_clickup(f"⚠️ **Catalog Backfill — sessão expirada**\n`{msg}`")
                    return

    logger.info("Catalog backfill complete.")
    notifier.send("Catalog backfill complete", f"{from_date} -> {to_date} | {', '.join(targets)}")
    notifier.send_clickup(
        f"✅ **Catalog Backfill concluído** — `{from_date}` → `{to_date}`\n"
        f"Marketplaces: {', '.join(targets)}"
    )
    print(f"\nFiles saved to: {CATALOG_RAW_DIR}")


def cmd_sniff_catalog(marketplace: str, ws: date | None) -> None:
    """Navigate to Brand Catalog Performance and capture the internal API call."""
    target_week = ws or last_available_week()
    sc_url = MARKETPLACES[marketplace]["sc_url"]
    with SQPDownloader(headless=False) as dl:
        if not dl.check_session(sc_url):
            print("Session expired. Run 'python main.py setup' first.")
            return
        result = dl.sniff_catalog(marketplace, target_week)
        if result:
            print(f"\nFull sniff saved to: debug/catalog_sniff_{marketplace}.json")
        else:
            print("\nNo API call intercepted. Check debug/ for screenshot.")


def cmd_process() -> None:
    tratamento.run()


def cmd_ingest(mode: str) -> None:
    rows = bq_ingest.run(mode)
    notifier.send_clickup(
        f"✅ **SQP → BigQuery** — `{rows:,}` linhas carregadas (`{mode}`)\n"
        f"Silver `vw_sqp_combined` e Gold `vw_search_query_performance` atualizados"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="SQP Downloader — automates Seller Central Search Query Performance downloads",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # setup
    s = sub.add_parser("setup", help="First-time login (run once per region)")
    s.add_argument("--marketplace", default="US", choices=list(MARKETPLACES))

    # discover
    d = sub.add_parser("discover", help="Capture screenshots for selector calibration")
    d.add_argument("--marketplace", default="US", choices=list(MARKETPLACES))

    # test
    t = sub.add_parser("test", help="Smoke test: one ASIN, last available week")
    t.add_argument("--marketplace", default="US", choices=list(MARKETPLACES))
    t.add_argument("--asin", default=None, help="Specific ASIN (uses first active ASIN if omitted)")

    # weekly
    w = sub.add_parser("weekly", help="Download last week for all (or specified) marketplaces")
    w.add_argument("--marketplace", nargs="*", default=None, choices=list(MARKETPLACES))
    w.add_argument("--na-week", default=None,
                   help="Override week for NA markets (YYYY-MM-DD, any date in the week)")
    w.add_argument("--eu-week", default=None,
                   help="Override week for EU/GB markets (YYYY-MM-DD, any date in the week)")

    # backfill
    b = sub.add_parser("backfill", help="Download all weeks in a date range")
    b.add_argument("--from-date", required=True,
                   help="Start date (any date in first week), format: YYYY-MM-DD")
    b.add_argument("--to-date", default=None,
                   help="End date (defaults to last available week)")
    b.add_argument("--marketplace", nargs="*", default=None, choices=list(MARKETPLACES))
    b.add_argument("--skip", nargs="*", default=None, choices=list(MARKETPLACES),
                   help="Marketplaces to skip (e.g. US if already done)")
    b.add_argument("--no-ingest", action="store_true",
                   help="Skip BigQuery ingestion after download")

    # catalog-weekly
    cw = sub.add_parser("catalog-weekly", help="Download Brand Catalog Performance for last week (all or specified marketplaces)")
    cw.add_argument("--marketplace", nargs="*", default=None, choices=list(MARKETPLACES))
    cw.add_argument("--week", default=None,
                    help="Override week (any date in the week, YYYY-MM-DD). Defaults to last available week.")

    # catalog-backfill
    cb = sub.add_parser("catalog-backfill", help="Download Brand Catalog Performance for all weeks in a date range")
    cb.add_argument("--from-date", required=True, help="Start date (YYYY-MM-DD)")
    cb.add_argument("--to-date", default=None, help="End date (defaults to last available week)")
    cb.add_argument("--marketplace", nargs="*", default=None, choices=list(MARKETPLACES))

    # sniff-catalog
    sc = sub.add_parser("sniff-catalog", help="Intercept Brand Catalog Performance API call to discover request/response shape")
    sc.add_argument("--marketplace", default="US", choices=list(MARKETPLACES))
    sc.add_argument("--week", default=None,
                    help="Week to sniff (any date in the week, YYYY-MM-DD). Defaults to last available week.")

    # process
    sub.add_parser("process", help="Re-run consolidation (tratamento) only")

    # ingest
    i = sub.add_parser("ingest", help="Upload resultado_final.csv to BigQuery and refresh vw_sqp_combined")
    i.add_argument(
        "--mode", choices=["incremental", "full"], default="incremental",
        help="incremental (default): append new weeks only. full: truncate and reload.",
    )

    return p


def main() -> None:
    args = _build_parser().parse_args()

    if args.cmd == "setup":
        cmd_setup(args.marketplace)

    elif args.cmd == "discover":
        cmd_discover(args.marketplace)

    elif args.cmd == "test":
        cmd_test(args.marketplace, args.asin)

    elif args.cmd == "weekly":
        na_week = week_start(date.fromisoformat(args.na_week)) if args.na_week else None
        eu_week = week_start(date.fromisoformat(args.eu_week)) if args.eu_week else None
        cmd_weekly(args.marketplace, na_week=na_week, eu_week=eu_week)

    elif args.cmd == "backfill":
        from_date = week_start(date.fromisoformat(args.from_date))
        to_date   = (
            week_start(date.fromisoformat(args.to_date))
            if args.to_date
            else last_available_week()
        )
        cmd_backfill(from_date, to_date, args.marketplace, args.skip, no_ingest=args.no_ingest)

    elif args.cmd == "catalog-weekly":
        ws = week_start(date.fromisoformat(args.week)) if args.week else None
        cmd_catalog_weekly(args.marketplace, ws)

    elif args.cmd == "catalog-backfill":
        from_date = week_start(date.fromisoformat(args.from_date))
        to_date   = (
            week_start(date.fromisoformat(args.to_date))
            if args.to_date
            else last_available_week()
        )
        cmd_catalog_backfill(from_date, to_date, args.marketplace)

    elif args.cmd == "sniff-catalog":
        ws = week_start(date.fromisoformat(args.week)) if args.week else None
        cmd_sniff_catalog(args.marketplace, ws)

    elif args.cmd == "process":
        cmd_process()

    elif args.cmd == "ingest":
        cmd_ingest(args.mode)


if __name__ == "__main__":
    main()
