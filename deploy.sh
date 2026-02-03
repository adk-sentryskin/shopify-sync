#!/bin/bash
set -e

# =============================================================================
# Cloud Run Deployment Script for Shopify Sync Service
# Usage: ./deploy.sh [staging|production]
# =============================================================================

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get environment from argument (default: staging)
ENVIRONMENT="${1:-staging}"

# Validate environment
if [ "$ENVIRONMENT" != "staging" ] && [ "$ENVIRONMENT" != "production" ]; then
    echo -e "${RED}Error: Invalid environment '$ENVIRONMENT'${NC}"
    echo "Usage: $0 [staging|production]"
    exit 1
fi

echo -e "${BLUE}=== Shopify Sync Service - Cloud Run Deployment ===${NC}"
echo -e "${YELLOW}Environment: ${ENVIRONMENT}${NC}"
echo ""

# =============================================================================
# Configuration
# =============================================================================

PROJECT_ID="${GCP_PROJECT_ID:-shopify-473015}"
REGION="${GCP_REGION:-us-central1}"

# Environment-specific configuration
if [ "$ENVIRONMENT" = "staging" ]; then
    SERVICE_NAME="shopify-sync-service-staging"
    MEMORY="512Mi"
    CPU="1"
    MIN_INSTANCES="0"
    MAX_INSTANCES="5"
    LOG_LEVEL="INFO"
    DEBUG="false"
else  # production
    SERVICE_NAME="shopify-sync-service"
    MEMORY="1Gi"
    CPU="2"
    MIN_INSTANCES="1"
    MAX_INSTANCES="20"
    LOG_LEVEL="WARNING"
    DEBUG="false"
fi

IMAGE_NAME="gcr.io/${PROJECT_ID}/shopify-sync-service"

# Load environment variables from .env file
if [ -f ".env" ]; then
    echo -e "${YELLOW}Loading environment variables from .env file...${NC}"
    set -a
    source <(grep -v '^#' .env | grep -v '^$' | sed 's/\r$//')
    set +a
else
    echo -e "${YELLOW}Warning: .env file not found. Using environment variables.${NC}"
fi

# =============================================================================
# Production Confirmation
# =============================================================================

if [ "$ENVIRONMENT" = "production" ]; then
    echo -e "${RED}WARNING: You are about to deploy to PRODUCTION!${NC}"
    echo ""
    read -p "Type 'yes' to confirm production deployment: " -r
    echo
    if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
        echo -e "${YELLOW}Deployment cancelled.${NC}"
        exit 0
    fi
fi

# =============================================================================
# Pre-deployment Checks
# =============================================================================

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo -e "${RED}Error: gcloud CLI is not installed.${NC}"
    echo "Visit: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# Check authentication
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" &> /dev/null; then
    echo -e "${YELLOW}Not authenticated. Running gcloud auth login...${NC}"
    gcloud auth login
fi

# =============================================================================
# Deployment Summary
# =============================================================================

echo ""
echo -e "${BLUE}Deployment Configuration:${NC}"
echo "   Environment:    $ENVIRONMENT"
echo "   Service Name:   $SERVICE_NAME"
echo "   Project:        $PROJECT_ID"
echo "   Region:         $REGION"
echo "   Resources:      ${MEMORY} RAM, ${CPU} CPU"
echo "   Scaling:        ${MIN_INSTANCES}-${MAX_INSTANCES} instances"
echo "   Log Level:      $LOG_LEVEL"
echo ""

# =============================================================================
# Build Environment Variables
# =============================================================================

ENV_FILE=$(mktemp)
cat > ${ENV_FILE} << EOF
ENVIRONMENT: ${ENVIRONMENT}
DEBUG: "${DEBUG}"
LOG_LEVEL: "${LOG_LEVEL}"
DB_DSN: "${DB_DSN}"
ENCRYPTION_KEY: "${ENCRYPTION_KEY}"
API_KEY: "${API_KEY}"
SHOPIFY_API_KEY: "${SHOPIFY_API_KEY}"
SHOPIFY_API_SECRET: "${SHOPIFY_API_SECRET}"
SHOPIFY_SCOPES: "${SHOPIFY_SCOPES:-read_products,read_orders,read_customers}"
SHOPIFY_API_VERSION: "${SHOPIFY_API_VERSION:-2024-01}"
OAUTH_REDIRECT_URL: "${OAUTH_REDIRECT_URL}"
APP_URL: "${APP_URL}"
ENABLE_SCHEDULER: "${ENABLE_SCHEDULER:-true}"
RECONCILIATION_HOUR: "${RECONCILIATION_HOUR:-3}"
RECONCILIATION_MINUTE: "${RECONCILIATION_MINUTE:-0}"
ENABLE_EMBEDDINGS: "${ENABLE_EMBEDDINGS:-true}"
GCP_PROJECT_ID: "${PROJECT_ID}"
GCP_REGION: "${REGION}"
EOF

trap "rm -f $ENV_FILE" EXIT

# =============================================================================
# Build and Deploy
# =============================================================================

# Set project
echo -e "${YELLOW}Setting project to: ${PROJECT_ID}${NC}"
gcloud config set project ${PROJECT_ID}

# Build and push Docker image
echo -e "${YELLOW}Building and pushing Docker image...${NC}"
gcloud builds submit --tag ${IMAGE_NAME}:latest --project ${PROJECT_ID}

# Deploy to Cloud Run
echo -e "${YELLOW}Deploying to Cloud Run...${NC}"
gcloud run deploy ${SERVICE_NAME} \
    --image ${IMAGE_NAME}:latest \
    --platform managed \
    --region ${REGION} \
    --port 8000 \
    --memory ${MEMORY} \
    --cpu ${CPU} \
    --timeout 300 \
    --min-instances ${MIN_INSTANCES} \
    --max-instances ${MAX_INSTANCES} \
    --allow-unauthenticated \
    --env-vars-file ${ENV_FILE}

# =============================================================================
# Post-deployment
# =============================================================================

# Get the service URL
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} \
    --region=${REGION} \
    --project=${PROJECT_ID} \
    --format='value(status.url)' 2>/dev/null)

# Update APP_URL with deployed service URL
echo -e "${YELLOW}Updating APP_URL with deployed service URL...${NC}"
gcloud run services update ${SERVICE_NAME} \
    --region ${REGION} \
    --update-env-vars "APP_URL=${SERVICE_URL}"

# Health check
echo -e "${YELLOW}Testing health endpoint...${NC}"
sleep 5
if curl -sf "${SERVICE_URL}/health" > /dev/null 2>&1; then
    echo -e "${GREEN}Health check passed!${NC}"
elif curl -sf "${SERVICE_URL}/" > /dev/null 2>&1; then
    echo -e "${GREEN}Service is responding!${NC}"
else
    echo -e "${YELLOW}Warning: Health check failed - service may still be starting${NC}"
fi

# Print summary
echo ""
echo -e "${GREEN}=============================================="
echo "Deployment Complete!"
echo "=============================================="
echo "Environment:  ${ENVIRONMENT}"
echo "Service:      ${SERVICE_NAME}"
echo "URL:          ${SERVICE_URL}"
echo "Project:      ${PROJECT_ID}"
echo "Region:       ${REGION}"
echo "==============================================${NC}"
