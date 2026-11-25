#!/usr/bin/env bash
# Creates the S3 bucket used to hold Lambda deployment zips for
# CloudFormation deploys. Safe to re-run.
#
# Usage: ./scripts/create-deploy-bucket.sh

set -euo pipefail

AWS_REGION="${AWS_REGION:-ap-southeast-2}"
STACK_NAME="${STACK_NAME:-deputy-mcp}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${ARTIFACT_BUCKET:-${STACK_NAME}-artifacts-${ACCOUNT_ID}}"

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "Bucket s3://$BUCKET already exists."
else
  echo "Creating s3://$BUCKET in $AWS_REGION ..."
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" \
      --region "$AWS_REGION" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
  fi
fi

aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration "Status=Enabled"

echo ""
echo "Done. Set GitHub repo variable:"
echo "  ARTIFACT_BUCKET = ${BUCKET}"
echo "Then re-run setup-github-oidc.sh with ARTIFACT_BUCKET=${BUCKET} to scope the deploy role."
