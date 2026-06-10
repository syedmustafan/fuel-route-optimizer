FROM python:3.12-slim

# Faster, cleaner Python in containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY . .

# Collect static files at build time (whitenoise serves them).
# DEBUG defaults off here; SECRET_KEY not needed for collectstatic.
RUN DJANGO_SECRET_KEY=build-time-only DEBUG=False \
    python manage.py collectstatic --noinput

# Cloud Run injects $PORT (default 8080). gunicorn binds to it.
# Threaded workers suit this I/O-bound app (geocode + OSRM calls). --preload
# imports the app once before forking, so numpy + the city-centroid CSV load a
# single time and are shared read-only across workers (faster, lighter spawn).
# Pair --workers x --threads (2x8=16) with Cloud Run --concurrency=16.
ENV PORT=8080
CMD exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT}" \
    --worker-class gthread \
    --workers 2 \
    --threads 8 \
    --timeout 60 \
    --graceful-timeout 30 \
    --preload
