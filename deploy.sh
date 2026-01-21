#!/bin/bash

# Deploy script for Shopify Sync Service
PROJECT_ID="shopify-473015"
SERVICE_NAME="shopify-sync-service-staging"
REGION="us-central1"
IMAGE="gcr.io/${PROJECT_ID}/shopify-sync-service:latest"

echo "Building and pushing Docker image..."
gcloud builds submit --tag $IMAGE --project $PROJECT_ID

echo "Updating Cloud Run service..."
gcloud run services update $SERVICE_NAME \
  --image $IMAGE \
  --region $REGION \
  --project $PROJECT_ID

echo "Deployment complete!"
echo "Service URL: https://shopify-sync-service-staging-vgcxyi5qqa-uc.a.run.app"
