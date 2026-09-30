"""JSON 格式日誌；固定欄位見共用層 §七。不記錄 API key 與照片內容。"""

import json
import logging
import sys
import uuid
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("artrag")
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    root.propagate = False


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:16]


log = logging.getLogger("artrag")
