"""kb/VERSION 遞增（2026.09.1 → 2026.09.2；跨月重設為 1）。新增或修改畫作後執行。

後端的照片建檔收錄時也呼叫 next_version()（docs/adr/013）。
"""

from datetime import date
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "kb" / "VERSION"


def next_version(old: str, today: date | None = None) -> str:
    year, month, n = old.strip().split(".")
    today = today or date.today()
    prefix = f"{today.year}.{today.month:02d}"
    return f"{prefix}.{int(n) + 1}" if f"{year}.{month}" == prefix else f"{prefix}.1"


if __name__ == "__main__":
    old = PATH.read_text(encoding="utf-8").strip()
    new = next_version(old)
    PATH.write_text(new + "\n", encoding="utf-8")
    print(f"kb/VERSION：{old} → {new}")
