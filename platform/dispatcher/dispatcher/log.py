"""JSON log lines on stdout (CloudWatch on AWS). Never log a token or a whole match spec."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


def fields(**values: Any) -> dict[str, Any]:
    """`extra=` for a log call: `log.info("match running", extra=fields(match_id=...))`."""
    return {"fields": values}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        line.update(getattr(record, "fields", {}))
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def setup(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
