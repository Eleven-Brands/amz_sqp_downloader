@echo off
setlocal

set WORKDIR=G:\Shared drives\OrganiHaus\3.1 - OH Data & Reports\code_repository\sqp_downloader

cd /d "%WORKDIR%"

echo [%DATE% %TIME%] === SQP Backfill 2025 US/DE/GB + ingestao BQ iniciando ===
python main.py backfill --from-date 2025-06-01 --to-date 2025-12-28 --marketplace US DE GB
echo [%DATE% %TIME%] === SQP Backfill 2025 concluido ===

echo [%DATE% %TIME%] === SCP Backfill 2026 todos os paises iniciando ===
python main.py catalog-backfill --from-date 2026-01-01
echo [%DATE% %TIME%] === SCP Backfill 2026 concluido ===

echo [%DATE% %TIME%] === Tudo pronto ===
endlocal
