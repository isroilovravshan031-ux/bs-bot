FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY nakladnoy_bot ./nakladnoy_bot
COPY migrations ./migrations
COPY config ./config

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p logs data \
    && chown -R bot /app
USER bot

# Sozlamalar Railway Variables'dan keladi (.env konteynerga kirmaydi).
CMD ["python", "-m", "nakladnoy_bot", "run"]
