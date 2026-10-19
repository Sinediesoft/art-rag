"""把 services 產生的 SSE 字串（event: x\\ndata: {...}\\n\\n）拆回 (事件名, 資料)。"""

import json


def parse(raw: str) -> tuple[str, dict]:
    event, data = "message", "{}"
    for line in raw.strip().splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
    return event, json.loads(data)
