FROM node:22-bookworm-slim AS provider
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /provider
WORKDIR /provider/server
RUN npm ci && npx tsc && npm prune --omit=dev

FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates chromium xvfb xauth fonts-liberation && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=provider /provider/server /provider/server
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY requirements-wpc.txt .
RUN python3 -m venv /opt/wpc && /opt/wpc/bin/pip install --no-cache-dir -r requirements-wpc.txt
ENV WPC_ENABLED=1 WPC_PYTHON=/opt/wpc/bin/python WPC_BROWSER_PATH=/app/chromium-server
COPY server.py start.sh chromium-server ./
RUN chmod 755 /app/chromium-server
USER node
CMD ["bash", "start.sh"]
