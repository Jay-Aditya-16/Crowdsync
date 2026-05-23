#!/usr/bin/env bash
# Deploy CrowdSync to Google Cloud Run.
# Usage: ./deploy.sh
#
# Prereqs:
#   gcloud auth login
#   gcloud config set project $GCP_PROJECT_ID
#   gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com

set -euo pipefail
set -a; source .env; set +a

PROJECT="${GCP_PROJECT_ID:-platinum-loop-497205-a3}"
REGION="${GCP_REGION:-asia-south1}"
SERVICE="${GCP_SERVICE_NAME:-crowdsync}"

echo "==> Project: $PROJECT  Region: $REGION  Service: $SERVICE"

# 1) Put secrets in Secret Manager (idempotent — ignores 'already exists').
for kv in \
  "openrouter-api-key:$OPENROUTER_API_KEY" \
  "agentmail-api-key:$AGENTMAIL_API_KEY" \
  "virustotal-api-key:$VIRUSTOTAL_API_KEY" \
  "firecrawl-api-key:$FIRECRAWL_API_KEY" \
  "browseruse-api-key:$BROWSER_USE_API_KEY" ; do
  NAME="${kv%%:*}"
  VAL="${kv#*:}"
  if gcloud secrets describe "$NAME" --project "$PROJECT" >/dev/null 2>&1; then
    echo "$VAL" | gcloud secrets versions add "$NAME" --data-file=- --project "$PROJECT"
  else
    echo "$VAL" | gcloud secrets create "$NAME" --data-file=- --replication-policy=automatic --project "$PROJECT"
  fi
done

# 2) Grant Cloud Run service account access to the secrets.
SA="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
for NAME in openrouter-api-key agentmail-api-key virustotal-api-key firecrawl-api-key browseruse-api-key; do
  gcloud secrets add-iam-policy-binding "$NAME" \
    --member="serviceAccount:$SA" \
    --role="roles/secretmanager.secretAccessor" \
    --project "$PROJECT" >/dev/null
done

# 3) Build via Cloud Build and deploy.
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300 \
  --max-instances 5 \
  --set-env-vars "FAN_CONCIERGE_CLIENT_ID=$FAN_CONCIERGE_CLIENT_ID,FAN_CONCIERGE_INBOX_ADDR=$FAN_CONCIERGE_INBOX_ADDR,COMMANDER_CLIENT_ID=$COMMANDER_CLIENT_ID,COMMANDER_INBOX_ADDR=$COMMANDER_INBOX_ADDR,DEMO_FAN_EMAILS=$DEMO_FAN_EMAILS,OPERATOR_EMAIL=$OPERATOR_EMAIL,LIVE_SCOREBOARD_URL=$LIVE_SCOREBOARD_URL,LIVE_WEATHER_URL=$LIVE_WEATHER_URL,OPENROUTER_TEXT_MODEL=$OPENROUTER_TEXT_MODEL,OPENROUTER_VISION_MODEL=$OPENROUTER_VISION_MODEL" \
  --set-secrets "OPENROUTER_API_KEY=openrouter-api-key:latest,AGENTMAIL_API_KEY=agentmail-api-key:latest,VIRUSTOTAL_API_KEY=virustotal-api-key:latest,FIRECRAWL_API_KEY=firecrawl-api-key:latest,BROWSER_USE_API_KEY=browseruse-api-key:latest"

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')"
echo
echo "==> Live: $URL"
