# Build context is the repo root (compose: build.context ..). The image holds the application only:
# jobscan/ (tracker library + board adapters) and jobboard/ (server, auth, MCP, static pages).
# Dev (compose.yaml) bind-mounts the same two directories over the copies for live reload.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 JOBSCAN_DIR=/app/jobscan
RUN pip install --no-cache-dir "psycopg[binary]>=3.2"
COPY jobscan /app/jobscan
COPY jobboard /app/jobboard
WORKDIR /app/jobboard
USER 65534:65534
CMD ["python3", "server.py"]
