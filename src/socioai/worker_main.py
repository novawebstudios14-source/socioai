import logging
import time
from pathlib import Path

from .database import assert_schema_current
from .main import app


def main():
    logging.basicConfig(level=logging.INFO)
    assert_schema_current(app.state.engine)
    heartbeat = Path(app.state.inbound_service.media_store.root) / "worker.heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    while True:
        heartbeat.touch()
        processed = app.state.worker.run_once()
        time.sleep(1 if processed else 5)


if __name__ == "__main__":
    main()
