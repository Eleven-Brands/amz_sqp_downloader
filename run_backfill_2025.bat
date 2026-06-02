@echo off
cd /d "%~dp0"
echo [%DATE% %TIME%] Starting 2025 backfill — US
python main.py backfill --from-date 2024-12-29 --to-date 2025-12-21 --marketplace US
echo [%DATE% %TIME%] US done. Starting GB + DE
python main.py backfill --from-date 2024-12-29 --to-date 2025-12-21 --marketplace GB DE
echo [%DATE% %TIME%] Backfill 2025 complete.
