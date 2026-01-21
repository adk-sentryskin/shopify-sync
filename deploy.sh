#!/bin/bash

# Deploy script for Shopify Sync Service
# Configuration - override via environment variables or edit defaults below
PROJECT_ID="${GCP_PROJECT_ID:-shopify-473015}"
SERVICE_NAME="${SERVICE_NAME:-shopify-sync-service}-staging"
REGION="${GCP_REGION:-us-central1}"
IMAGE="gcr.io/${PROJECT_ID}/shopify-sync-service:latest"

echo "=== Deployment Configuration ==="
echo "Project ID: $PROJECT_ID"
echo "Service Name: $SERVICE_NAME"
echo "Region: $REGION"
echo "Image: $IMAGE"
echo "================================"

echo "Building and pushing Docker image..."
gcloud builds submit --tag $IMAGE --project $PROJECT_ID

echo "Updating Cloud Run service..."
gcloud run services update $SERVICE_NAME \
  --image $IMAGE \
  --region $REGION \
  --project $PROJECT_ID

# Get the actual service URL dynamically
SERVICE_URL=$(gcloud run services describe $SERVICE_NAME \
  --region=$REGION \
  --project=$PROJECT_ID \
  --format='value(status.url)' 2>/dev/null)

echo "Deployment complete!"
echo "Service URL: ${SERVICE_URL:-Unable to retrieve URL}"
