# Redis Setup (Memorystore)

The API uses Redis for its three cache layers (`geocode:*`, `route:*`,
`trip:*`). Without `REDIS_URL` the cache falls back to per-process
`LocMemCache`, which is wiped on every cold start, so cached geocode/route/trip
results don't survive across requests or instances. Memorystore gives a
persistent, shared cache so repeat requests are served from `trip:*` with zero
external calls.

The steps below provision Memorystore and point Cloud Run at it.

```
PROJECT=fuel-route-011732
REGION=us-central1
SERVICE=spotter-backend
```

## 1. Create the Redis instance

```bash
gcloud redis instances create spotter-redis \
  --size=1 --region="$REGION" --project="$PROJECT" \
  --redis-version=redis_7_2
```

Provisioning takes ~5–10 minutes.

## 2. Get the connection string

```bash
REDIS_HOST=$(gcloud redis instances describe spotter-redis \
  --region="$REGION" --project="$PROJECT" --format='value(host)')
REDIS_PORT=$(gcloud redis instances describe spotter-redis \
  --region="$REGION" --project="$PROJECT" --format='value(port)')
echo "redis://${REDIS_HOST}:${REDIS_PORT}"
```

Memorystore exposes a **private IP only**, so Cloud Run needs a Serverless VPC
Access connector to reach it (see `deploy/optimize.md`).

## 3. Store the URL as a secret

```bash
echo -n "redis://${REDIS_HOST}:${REDIS_PORT}" | gcloud secrets create REDIS_URL \
  --data-file=- --project="$PROJECT"
# if it already exists, add a new version instead:
# echo -n "redis://${REDIS_HOST}:${REDIS_PORT}" | gcloud secrets versions add REDIS_URL --data-file=- --project="$PROJECT"

gcloud secrets add-iam-policy-binding REDIS_URL \
  --member=serviceAccount:901897067349-compute@developer.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor --project="$PROJECT"
```

## 4. Point Cloud Run at Redis

```bash
gcloud run services update "$SERVICE" \
  --update-env-vars "REDIS_URL=redis://${REDIS_HOST}:${REDIS_PORT}" \
  --vpc-connector=spotter-redis-conn \
  --region="$REGION" --project="$PROJECT"
```

## Monitoring

```bash
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" INFO stats | grep hits
```
