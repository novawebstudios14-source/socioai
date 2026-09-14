import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                   "logger": record.name, "message": record.getMessage()}
        for key in ("action", "company_id", "job_id", "kind", "count", "mode"):
            if hasattr(record, key): payload[key] = getattr(record, key)
        if record.exc_info: payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging():
    handler = logging.StreamHandler(); handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    if not any(isinstance(x.formatter, JsonFormatter) for x in root.handlers):
        root.handlers = [handler]; root.setLevel(logging.INFO)
