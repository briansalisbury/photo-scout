# Heart service for the Photo Scout gallery.
#
#   docker compose build hearts && docker compose up -d hearts
#
# The image holds no data: hearts.sqlite3 lives on a mounted volume, so the
# container can be rebuilt or replaced without losing a single heart.
FROM python:3.12-slim

# Run as a non-root user. Compose also sets read_only and drops capabilities;
# this is the layer that survives if someone forgets those.
RUN useradd --create-home --uid 10001 hearts

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

USER hearts
EXPOSE 8091

# Waitress, not Flask's development server: the built-in one is single
# threaded and explicitly not for production use.
ENV HEARTS_DB=/data/hearts.sqlite3 \
    HEARTS_PORT=8091 \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8091/api/hearts/_health',timeout=4).status==200 else 1)"

CMD ["python", "app.py"]
