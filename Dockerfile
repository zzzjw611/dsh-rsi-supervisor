FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8787 \
    LOOPGRAPH_DATA_DIR=/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 loopgraph \
    && mkdir -p /data/workspace \
    && chown -R loopgraph:loopgraph /app /data

USER loopgraph

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8787') + '/health', timeout=3)"

CMD ["sh", "-c", "loopgraph --db \"${LOOPGRAPH_DATA_DIR}/loopgraph.db\" dashboard --host 0.0.0.0 --port \"${PORT}\" --workspace \"${LOOPGRAPH_DATA_DIR}/workspace\""]
