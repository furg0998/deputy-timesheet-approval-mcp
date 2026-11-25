#!/usr/bin/env bash
# Build + deploy the stack from your machine (same steps the GitHub
# Action runs). Requires: python 3.12, zip, aws CLI logged in.
#
# Usage:
#   DEPUTY_CLIENT_ID=... DEPUTY_CLIENT_SECRET=... ARTIFACT_BUCKET=... \
#     ./scripts/deploy-local.sh

set -euo pipefail
cd "$(dirname "$0")/.."

AWS_REGION="${AWS_REGION:-ap-southeast-2}"
STACK_NAME="${STACK_NAME:-deputy-mcp}"
: "${DEPUTY_CLIENT_ID:?set DEPUTY_CLIENT_ID}"
: "${DEPUTY_CLIENT_SECRET:?set DEPUTY_CLIENT_SECRET}"
: "${ARTIFACT_BUCKET:?set ARTIFACT_BUCKET}"

KEY="builds/$(date +%Y%m%d%H%M%S)-$$.zip"

echo "==> Installing deps"
rm -rf build
mkdir -p build
pip install -r requirements.txt -t build/ --quiet --platform manylinux2014_aarch64 \
  --python-version 3.12 --only-binary=:all: || \
  pip install -r requirements.txt -t build/ --quiet
cp src/*.py build/

echo "==> Packaging"
(cd build && zip -qr ../package.zip .)
aws s3 cp package.zip "s3://${ARTIFACT_BUCKET}/${KEY}" --region "$AWS_REGION"

echo "==> Deploying stack ${STACK_NAME}"
aws cloudformation deploy \
  --template-file infra/template.yaml \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
    "ArtifactBucket=${ARTIFACT_BUCKET}" \
    "ArtifactKey=${KEY}" \
    "DeputyClientId=${DEPUTY_CLIENT_ID}" \
    "DeputyClientSecret=${DEPUTY_CLIENT_SECRET}"

aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
  --query "Stacks[0].Outputs" --output table
