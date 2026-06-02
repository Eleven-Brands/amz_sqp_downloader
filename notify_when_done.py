"""Monitora o log e mostra popup Windows quando o backfill terminar."""
import time
import subprocess
from pathlib import Path

LOG = Path(r"C:\SQP\logs\sqp.log")
CHECK_INTERVAL = 10  # segundos


def notify(title: str, message: str) -> None:
    script = f"""
    Add-Type -AssemblyName System.Windows.Forms
    $notify = New-Object System.Windows.Forms.NotifyIcon
    $notify.Icon = [System.Drawing.SystemIcons]::Information
    $notify.Visible = $true
    $notify.ShowBalloonTip(10000, '{title}', '{message}', [System.Windows.Forms.ToolTipIcon]::Info)
    Start-Sleep -Seconds 10
    $notify.Dispose()
    """
    subprocess.Popen(["powershell", "-WindowStyle", "Hidden", "-Command", script])

    # Também abre um MessageBox para garantir
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)


def tail_log(path: Path, last_size: int) -> tuple[str, int]:
    size = path.stat().st_size
    if size <= last_size:
        return "", last_size
    with open(path, encoding="utf-8", errors="replace") as f:
        f.seek(last_size)
        return f.read(), size


def main() -> None:
    print(f"Monitorando {LOG} ...")
    print("Aguardando 'Backfill complete' no log...\n")

    last_size = LOG.stat().st_size if LOG.exists() else 0

    while True:
        time.sleep(CHECK_INTERVAL)
        if not LOG.exists():
            continue
        new_text, last_size = tail_log(LOG, last_size)
        if not new_text:
            continue
        for line in new_text.splitlines():
            print(line)
            if "Backfill complete" in line or "Session expired" in line:
                if "Backfill complete" in line:
                    notify("SQP Downloader", "Backfill DE concluido!")
                else:
                    notify("SQP Downloader - ERRO", "Sessao expirou! Faca login novamente.")
                return


if __name__ == "__main__":
    main()
