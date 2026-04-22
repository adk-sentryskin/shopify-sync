#!/bin/bash
set -e

# =============================================================================
# Cloud Run Deployment Script for Shopify Sync Service
# Usage: ./deploy.sh [development|production]
#
# This script is aligned with .github/workflows/deploy.yml for consistency
# between manual and CI/CD deployments.
#
# Secrets are managed via Google Cloud Secret Manager (not .env file)
# =============================================================================

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get environment from argument (default: development)
ENVIRONMENT="${1:-development}"

# Validate environment
if [ "$ENVIRONMENT" != "development" ] && [ "$ENVIRONMENT" != "production" ]; then
    echo -e "${RED}Error: Invalid environment '$ENVIRONMENT'${NC}"
    echo "Usage: $0 [development|production]"
    exit 1
fi

echo -e "${BLUE}=== Shopify Sync Service - Cloud Run Deployment ===${NC}"
echo -e "${YELLOW}Environment: ${ENVIRONMENT}${NC}"
echo ""

# =============================================================================
# Configuration
# =============================================================================

# Load local environment variables from .env.local (preferred) or .env
if [ -f ".env.local" ]; then
    echo -e "${YELLOW}Loading local overrides from .env.local file...${NC}"
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
        export "$key=$value"
    done < <(grep -v '^#' .env.local | grep -v '^$' | sed 's/\r$//')
elif [ -f ".env" ]; then
    echo -e "${YELLOW}Loading local overrides from .env file...${NC}"
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
        export "$key=$value"
    done < <(grep -v '^#' .env | grep -v '^$' | sed 's/\r$//')
else
    echo -e "${YELLOW}Note: No .env file found. Using defaults and Secret Manager for secrets.${NC}"
fi

ENVIRONMENT="${1:-development}"  # re-assert: .env.local must not override the CLI arg
if [ "$ENVIRONMENT" = "production" ]; then
    PROJECT_ID="production-aibuilder"
else
    PROJECT_ID="${GCP_PROJECT_ID:-shopify-473015}"
fi
REGION="${GCP_REGION:-us-central1}"
unset SERVICE_NAME  # always set by environment block below, never from .env files

# Environment-specific configuration
if [ "$ENVIRONMENT" = "development" ]; then
    SERVICE_NAME="shopify-sync-service-staging"
    MEMORY="512Mi"
    CPU="1"
    MIN_INSTANCES="0"
    MAX_INSTANCES="10"
    LOG_LEVEL="INFO"
    DEBUG="false"
    CONCURRENCY=""
    CHATBOT_SRC="${CHATBOT_SRC:-https://ai-builder.chekout.ai/chatbot.js}"
    # Secret names for development
    DB_DSN_SECRET="DB_DSN"
    API_KEY_SECRET="API_KEY"
    SHOPIFY_API_KEY_SECRET="SHOPIFY_API_KEY"
    SHOPIFY_API_SECRET_SECRET="SHOPIFY_API_SECRET"
    OAUTH_REDIRECT_URL_SECRET="OAUTH_REDIRECT_URL"
    APP_URL_SECRET="APP_URL"
else  # production
    CHATBOT_SRC="${CHATBOT_SRC:-https://app.chekout.ai/chatbot.js}"
    SERVICE_NAME="shopify-sync"
    MEMORY="1Gi"
    CPU="2"
    MIN_INSTANCES="1"
    MAX_INSTANCES="100"
    LOG_LEVEL="WARNING"
    DEBUG="false"
    CONCURRENCY="80"
    # Secret names for production (with _PROD suffix)
    DB_DSN_SECRET="DB_DSN_PROD"
    API_KEY_SECRET="API_KEY_PROD"
    SHOPIFY_API_KEY_SECRET="SHOPIFY_API_KEY_PROD"
    SHOPIFY_API_SECRET_SECRET="SHOPIFY_API_SECRET_PROD"
    OAUTH_REDIRECT_URL_SECRET="OAUTH_REDIRECT_URL_PROD"
    APP_URL_SECRET="APP_URL_PROD"
fi

IMAGE_NAME="gcr.io/${PROJECT_ID}/shopify-sync"

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
# Build Environment Variables (aligned with deploy.yml)
# =============================================================================

# Write env vars to a temp YAML file to avoid escaping issues with commas
ENV_VARS_FILE=$(mktemp /tmp/cloudrun-env-XXXXXX).yaml
cat > "${ENV_VARS_FILE}" <<EOF
ENVIRONMENT: "${ENVIRONMENT}"
DEBUG: "${DEBUG}"
LOG_LEVEL: "${LOG_LEVEL}"
SHOPIFY_SCOPES: "${SHOPIFY_SCOPES:-read_products}"
SHOPIFY_API_VERSION: "${SHOPIFY_API_VERSION:-2024-01}"
ENABLE_SCHEDULER: "${ENABLE_SCHEDULER:-true}"
RECONCILIATION_HOUR: "${RECONCILIATION_HOUR:-3}"
RECONCILIATION_MINUTE: "${RECONCILIATION_MINUTE:-0}"
GCP_PROJECT_ID: "${PROJECT_ID}"
GCP_REGION: "${REGION}"
ENABLE_EMBEDDINGS: "${ENABLE_EMBEDDINGS:-true}"
CHATBOT_SRC: "${CHATBOT_SRC}"
EOF

# Secrets (using Google Cloud Secret Manager - aligned with deploy.yml)
SECRETS="DB_DSN=${DB_DSN_SECRET}:latest"
SECRETS="${SECRETS},ENCRYPTION_KEY=ENCRYPTION_KEY:latest"
SECRETS="${SECRETS},API_KEY=${API_KEY_SECRET}:latest"
SECRETS="${SECRETS},SHOPIFY_API_KEY=${SHOPIFY_API_KEY_SECRET}:latest"
SECRETS="${SECRETS},SHOPIFY_API_SECRET=${SHOPIFY_API_SECRET_SECRET}:latest"
SECRETS="${SECRETS},OAUTH_REDIRECT_URL=${OAUTH_REDIRECT_URL_SECRET}:latest"
SECRETS="${SECRETS},APP_URL=${APP_URL_SECRET}:latest"

# =============================================================================
# Build and Deploy
# =============================================================================

# Set project
echo -e "${YELLOW}Setting project to: ${PROJECT_ID}${NC}"
gcloud config set project ${PROJECT_ID}

# Build and push Docker image
echo -e "${YELLOW}Building and pushing Docker image...${NC}"
gcloud builds submit --tag ${IMAGE_NAME}:latest --project ${PROJECT_ID}

# Deploy to Cloud Run (aligned with deploy.yml)
echo -e "${YELLOW}Deploying to Cloud Run...${NC}"

# Build deployment command
DEPLOY_CMD="gcloud run deploy ${SERVICE_NAME} \
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
    --env-vars-file "${ENV_VARS_FILE}" \
    --set-secrets ${SECRETS}"

# Add concurrency for production (aligned with deploy.yml)
if [ -n "$CONCURRENCY" ]; then
    DEPLOY_CMD="${DEPLOY_CMD} --concurrency ${CONCURRENCY}"
fi

# Execute deployment
eval ${DEPLOY_CMD}
rm -f "${ENV_VARS_FILE}"

# =============================================================================
# Post-deployment (aligned with deploy.yml)
# =============================================================================

# Get the service URL
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} \
    --region=${REGION} \
    --project=${PROJECT_ID} \
    --format='value(status.url)' 2>/dev/null)

# Health check (aligned with deploy.yml - 10 second wait)
echo -e "${YELLOW}Verifying deployment...${NC}"
echo "Service URL: ${SERVICE_URL}"
sleep 10
if curl -f "${SERVICE_URL}/health" > /dev/null 2>&1; then
    echo -e "${GREEN}Health check passed!${NC}"
elif curl -f "${SERVICE_URL}/" > /dev/null 2>&1; then
    echo -e "${GREEN}Service is responding!${NC}"
else
    echo -e "${YELLOW}Health check endpoint not available${NC}"
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
