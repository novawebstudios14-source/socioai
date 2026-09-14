import logging
import time

from .database import assert_schema_current
from .main import app


def main():
    logging.basicConfig(level=logging.INFO)
    assert_schema_current(app.state.engine)
    while True:
        processed = app.state.worker.run_once()
        time.sleep(1 if processed else 5)


if __name__ == "__main__":
    main()
