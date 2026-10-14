# Build context is the repo root (compose: build.context ..). The image holds the application only:
# jobscan/ (tracker library + board adapters) and jobboard/ (server, auth, MCP, static pages), plus the
# Chromium that board adapters drive through Selenium + undetected-chromedriver (browser.py).
# Dev (compose.yaml) bind-mounts the same two directories over the copies for live reload.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 JOBSCAN_DIR=/app/jobscan \
    JOBSCAN_CHROME=/usr/bin/chromium JOBSCAN_CHROMEDRIVER=/usr/bin/chromedriver HOME=/tmp
RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-driver fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "psycopg[binary]>=3.2" "selenium>=4.20" "undetected-chromedriver>=3.5.5" setuptools \
    "curl_cffi>=0.10"
ENV JOBSCAN_OWN_CHROME=1
COPY jobscan /app/jobscan
COPY jobboard /app/jobboard
WORKDIR /app/jobboard
USER 65534:65534
CMD ["python3", "server.py"]
