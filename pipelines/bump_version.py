"""kb/VERSION 遞增（2026.09.1 → 2026.09.2；跨月重設為 1）。新增或修改畫作後執行。"""

from datetime import date
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "kb" / "VERSION"
old = path.read_text(encoding="utf-8").strip()
year, month, n = old.split(".")
today = date.today()
prefix = f"{today.year}.{today.month:02d}"
new = f"{prefix}.{int(n) + 1}" if f"{year}.{month}" == prefix else f"{prefix}.1"
path.write_text(new + "\n", encoding="utf-8")
print(f"kb/VERSION：{old} → {new}")
