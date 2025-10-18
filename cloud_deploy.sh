#!/bin/bash

PROJECT_ID="firm-braid-475420-p9"
FUNCTION_NAME="calendar-sync"
REGION="us-central1"

# Environment variables - These will be read from your environment
# Make sure to set these before running the script:
# export ICLOUD_USERNAME="your_email@icloud.com"
# export ICLOUD_PASSWORD="your_app_specific_password"

echo "Deploying calendar sync function to Google Cloud Functions..."

# Check if required environment variables are set
if [ -z "$ICLOUD_USERNAME" ]; then
    echo "ERROR: ICLOUD_USERNAME environment variable is not set"
    echo "Please run: export ICLOUD_USERNAME='your_email@icloud.com'"
    exit 1
fi

if [ -z "$ICLOUD_PASSWORD" ]; then
    echo "ERROR: ICLOUD_PASSWORD environment variable is not set"
    echo "Please run: export ICLOUD_PASSWORD='your_app_specific_password'"
    exit 1
fi

# Copy the cloud-optimized main file
cp cloud_main.py main.py

# Deploy the function
gcloud functions deploy $FUNCTION_NAME \
  --runtime python312 \
  --trigger-http \
  --entry-point main \
  --source . \
  --region $REGION \
  --project $PROJECT_ID \
  --set-env-vars GOOGLE_CALENDAR_ID=primary,ICLOUD_USERNAME=$ICLOUD_USERNAME,ICLOUD_PASSWORD=$ICLOUD_PASSWORD,SYNC_DIRECTION=two_way,DRY_RUN=false,WINDOW_PAST_DAYS=90,WINDOW_FUTURE_DAYS=365 \
  --memory 512MB \
  --timeout 540s \
  --no-gen2

echo "Function deployed successfully!"

# Set up Cloud Scheduler to run every hour
echo "Setting up Cloud Scheduler to run every hour..."

gcloud scheduler jobs create http calendar-sync-job \
  --schedule="0 * * * *" \
  --uri="https://$REGION-$PROJECT_ID.cloudfunctions.net/$FUNCTION_NAME" \
  --http-method=POST \
  --location=$REGION \
  --project $PROJECT_ID

echo "Scheduler job created! The function will now run every hour."
echo "You can view logs with: gcloud functions logs read $FUNCTION_NAME --region $REGION"
