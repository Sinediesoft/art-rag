"""把 FastAPI 的 OpenAPI 規格寫到 shared/openapi.json（CI 比對是否與程式碼一致）。"""

import json
import sys

from app.core.config import REPO_ROOT
from app.main import app

out = REPO_ROOT / "shared" / "openapi.json"
spec = json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"
if "--check" in sys.argv:
    if out.read_text(encoding="utf-8") != spec:
        sys.exit("shared/openapi.json 已過期，請執行 make openapi")
    print("openapi.json 與程式碼一致")
else:
    out.write_text(spec, encoding="utf-8")
    print(f"已寫入 {out.relative_to(REPO_ROOT)}")
