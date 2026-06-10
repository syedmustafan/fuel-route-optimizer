# Deploy: GCP infra setup

Run these against the project to provision the infra the deployed service needs:
an always-on instance (no cold starts) and a persistent shared cache.

```
PROJECT=fuel-route-011732
REGION=us-central1
SERVICE=spotter-backend
```

After this: first request ~3–4s (no scale-from-zero) and repeat requests
sub-second (`meta.cached: true`).

---

## 1. Memorystore Redis (persistent, shared cache)

Without this, prod uses `LocMemCache`: per-gunicorn-worker and wiped on every cold
start, so the geocode/route/trip cache layers never survive. Memorystore fixes both.

```bash
gcloud redis instances create spotter-redis \
  --size=1 --region="$REGION" --project="$PROJECT" \
  --redis-version=redis_7_2
```

Takes ~5–10 min. Then capture host/port:

```bash
REDIS_HOST=$(gcloud redis instances describe spotter-redis \
  --region="$REGION" --project="$PROJECT" --format='value(host)')
REDIS_PORT=$(gcloud redis instances describe spotter-redis \
  --region="$REGION" --project="$PROJECT" --format='value(port)')
echo "REDIS_URL=redis://${REDIS_HOST}:${REDIS_PORT}"
```

## 2. Serverless VPC Access connector (Cloud Run → Memorystore)

Memorystore exposes a private IP only, so Cloud Run has no route to it without a
VPC connector; skip this and the redeploy in step 3 connect-timeouts to Redis.
Use a `/28` range that doesn't overlap your VPC subnets.

```bash
gcloud compute networks vpc-access connectors create spotter-redis-conn \
  --region="$REGION" --project="$PROJECT" \
  --range=10.8.0.0/28
```

(If the API isn't enabled: `gcloud services enable vpcaccess.googleapis.com --project="$PROJECT"`.)

## 3. Redeploy Cloud Run (always-on + CPU boost + Redis + concurrency)

`--min-instances=1` kills cold starts; `--cpu-boost` speeds the boot that does
happen; `--concurrency=16` matches gunicorn's 2 workers × 8 threads.

```bash
gcloud run deploy "$SERVICE" --source . \
  --region="$REGION" --project="$PROJECT" \
  --min-instances=1 --cpu-boost \
  --cpu=1 --memory=512Mi --concurrency=16 \
  --vpc-connector=spotter-redis-conn \
  --set-env-vars="REDIS_URL=redis://${REDIS_HOST}:${REDIS_PORT},DEBUG=False"
```

> Keep your existing `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS`, `DATABASE_URL`,
> `CORS_ALLOWED_ORIGINS` env/secrets — add them to the same `--set-env-vars` /
> `--set-secrets` so the redeploy doesn't drop them.

## 4. Verify the CityCoordinate table is seeded in prod

Common "City, ST" endpoints should resolve from the DB (`_parallel_geocode` →
`resolve_query`) with **zero** external geocode calls. Confirm rows exist:

```bash
# from a shell with prod DATABASE_URL:
python manage.py shell -c "from fuelroute.models import CityCoordinate; print(CityCoordinate.objects.count())"
```

If it's 0, run whatever import/seed command this repo ships (see README / management
commands) against the prod DB before relying on the DB fast-path.

---

## Verify the result

```bash
URL=https://spotter-backend-54klkk2f5a-uc.a.run.app/api/route/
DATA='{"start":"Seattle, WA","finish":"Miami, FL"}'

# First call (warms the trip cache):
curl -s -X POST "$URL" -H 'Content-Type: application/json' -d "$DATA" \
  -w '\n--> %{time_total}s  %{size_download} bytes\n' -o /dev/null

# Second call — should be sub-second and meta.cached:true (Redis is live & shared):
curl -s -X POST "$URL" -H 'Content-Type: application/json' -d "$DATA" \
  -w '\n--> %{time_total}s\n' | python -c "import sys,json; d=json.load(sys.stdin); print('cached =', d['meta']['cached'])"
```

Check the per-phase timing line in Cloud Run logs to see which phase dominates:

```bash
gcloud run services logs read "$SERVICE" --region="$REGION" --project="$PROJECT" --limit=20 \
  | grep 'geocode='
# e.g. INFO fuelroute.views: route 'Seattle, WA'->'Miami, FL' geocode=180ms osrm=900ms optimize=60ms total=1140ms calls=1 cached=False
```

If OSRM dominates after this, self-host OSRM or swap in a paid routing provider
behind the same `RoutingService` interface.
