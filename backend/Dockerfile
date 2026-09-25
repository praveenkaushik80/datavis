FROM python:3.12-slim

ARG INSTALL_MVP2_DRIVERS=false
ARG INSTALL_DOCLING=false

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$INSTALL_MVP2_DRIVERS" = "true" ]; then pip install --no-cache-dir -r requirements-mvp2-drivers.txt; fi \
 && if [ "$INSTALL_DOCLING" = "true" ]; then pip install --no-cache-dir -r requirements-docling.txt; fi
COPY app ./app
COPY index.py start.sh ./

RUN useradd --create-home datafusion && mkdir -p /shared && chown -R datafusion /shared
USER datafusion
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=10 CMD curl -sf http://localhost:${PORT:-8000}/health || exit 1
# start.sh binds "::" (IPv4 + IPv6, needed for Railway private networking) or 0.0.0.0 without IPv6.
# PORT is set by Railway; 8000 elsewhere.
CMD ["./start.sh"]
