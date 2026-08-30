"""Structured (logfmt-style) logging, shared by the web app and the scheduler.

No dependency: a plain logging.Formatter that emits `key=value` pairs, plus
`configure_logging()` to install it on the root logger and quiet uvicorn's
own access log (requests are logged by our middleware instead).

Level comes from CADENCE_LOG_LEVEL (default INFO). Pass structured fields with
`logger.info("msg", extra={"fields": {...}})`.
"""

from __future__ import annotations

import logging
import os
import time


def _needs_quoting(s: str) -> bool:
    return s == "" or any(c in s for c in ' ="\n')


def _fmt_value(value: object) -> str:
    s = str(value)
    if _needs_quoting(s):
        s = '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'
    return s


class LogfmtFormatter(logging.Formatter):
    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            f"ts={self.formatTime(record)}",
            f"level={record.levelname.lower()}",
            f"logger={record.name}",
            f"msg={_fmt_value(record.getMessage())}",
        ]
        for key, val in getattr(record, "fields", {}).items():
            parts.append(f"{key}={_fmt_value(val)}")
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging() -> None:
    level = os.environ.get("CADENCE_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(LogfmtFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)

    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # We log every request ourselves (app.api.log_requests middleware).
    logging.getLogger("uvicorn.access").disabled = True
