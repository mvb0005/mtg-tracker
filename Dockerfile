FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --home-dir /app mtg

WORKDIR /app
COPY . /app

# Editable install on purpose: SEED_DIR and scryfall.CACHE_DIR are resolved
# relative to the source tree (/app/seed, /app/.scryfall_cache), which a
# regular site-packages install would break.
RUN pip install -e . \
    && mkdir -p /app/.scryfall_cache /data \
    && chown mtg:mtg /app/.scryfall_cache /data

USER mtg
EXPOSE 8000

# Bind all interfaces inside the pod so kubelet probes and port-forward reach
# it; the cluster Service is ClusterIP-only, so nothing is exposed beyond it.
CMD ["mtg", "serve", "--db", "/data/mtgtracker.db", "--host", "0.0.0.0", "--port", "8000"]
