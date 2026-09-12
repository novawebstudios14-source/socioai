FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
CMD ["sh", "-c", "alembic upgrade head && uvicorn socioai.main:app --host 0.0.0.0 --port 8000"]
