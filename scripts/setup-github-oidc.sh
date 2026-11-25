#!/usr/bin/env bash
# Creates the GitHub Actions OIDC provider (if missing) and the IAM role
# GitHub Actions assumes to deploy the deputy-mcp CloudFormation stack.
#
# Usage:
#   GITHUB_REPO=owner/repo ./scripts/setup-github-oidc.sh
#
# Prerequisites: aws CLI v2, logged in (aws login) with admin-ish IAM rights.

set -euo pipefail

GITHUB_REPO="${GITHUB_REPO:-furg0998/deputy-timesheet-approval-mcp}"
ROLE_NAME="${ROLE_NAME:-deputy-mcp-github-deploy}"
STACK_NAME="${STACK_NAME:-deputy-mcp}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-}"
AWS_REGION="${AWS_REGION:-ap-southeast-2}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Account:  $ACCOUNT_ID"
echo "Repo:     $GITHUB_REPO"
echo "Role:     $ROLE_NAME"

# --- OIDC provider ---
PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null 2>&1; then
  echo "OIDC provider already exists."
else
  echo "Creating GitHub OIDC provider..."
  aws iam create-open-id-connect-provider \
    --url "https://token.actions.githubusercontent.com" \
    --client-id-list "sts.amazonaws.com" >/dev/null
  echo "Created."
fi

# --- Trust policy ---
TRUST=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Federated": "${PROVIDER_ARN}"},
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
        "StringLike": {"token.actions.githubusercontent.com:sub": "repo:${GITHUB_REPO}:*"}
      }
    }
  ]
}
EOF
)

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "Role exists - updating trust policy."
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST"
else
  echo "Creating role..."
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST" >/dev/null
  echo "Created."
fi

# --- Deploy policy ---
if [ -n "$ARTIFACT_BUCKET" ]; then
  BUCKET_RESOURCE="[\"arn:aws:s3:::${ARTIFACT_BUCKET}\", \"arn:aws:s3:::${ARTIFACT_BUCKET}/*\"]"
else
  BUCKET_RESOURCE="\"*\""
fi

POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormation",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack",
        "cloudformation:UpdateStack",
        "cloudformation:DeleteStack",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResources",
        "cloudformation:GetTemplate",
        "cloudformation:GetTemplateSummary",
        "cloudformation:ValidateTemplate",
        "cloudformation:CreateChangeSet",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:DeleteChangeSet",
        "cloudformation:DescribeChangeSet",
        "cloudformation:ListChangeSets",
        "cloudformation:TagResource",
        "cloudformation:UntagResource"
      ],
      "Resource": "arn:aws:cloudformation:${AWS_REGION}:${ACCOUNT_ID}:stack/${STACK_NAME}/*"
    },
    {
      "Sid": "Lambda",
      "Effect": "Allow",
      "Action": "lambda:*",
      "Resource": [
        "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${STACK_NAME}",
        "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${STACK_NAME}-*"
      ]
    },
    {
      "Sid": "ApiGateway",
      "Effect": "Allow",
      "Action": "apigateway:*",
      "Resource": "*"
    },
    {
      "Sid": "DynamoDb",
      "Effect": "Allow",
      "Action": "dynamodb:*",
      "Resource": [
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${STACK_NAME}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${STACK_NAME}-*"
      ]
    },
    {
      "Sid": "Secrets",
      "Effect": "Allow",
      "Action": "secretsmanager:*",
      "Resource": "arn:aws:secretsmanager:${AWS_REGION}:${ACCOUNT_ID}:secret:${STACK_NAME}/*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": "logs:*",
      "Resource": "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${STACK_NAME}*:*"
    },
    {
      "Sid": "IamScoped",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:GetRole",
        "iam:DeleteRole",
        "iam:UpdateRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
        "iam:PassRole"
      ],
      "Resource": [
        "arn:aws:iam::${ACCOUNT_ID}:role/${STACK_NAME}-*",
        "arn:aws:iam::${ACCOUNT_ID}:role/${STACK_NAME}"
      ]
    },
    {
      "Sid": "Artifacts",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": ${BUCKET_RESOURCE}
    }
  ]
}
EOF
)

aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name "${STACK_NAME}-deploy" --policy-document "$POLICY"

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo ""
echo "Done. Add to GitHub repo variables:"
echo "  AWS_DEPLOY_ROLE_ARN = ${ROLE_ARN}"
echo "  AWS_REGION          = ${AWS_REGION}"
if [ -n "$ARTIFACT_BUCKET" ]; then
  echo "  ARTIFACT_BUCKET     = ${ARTIFACT_BUCKET}"
else
  echo "  ARTIFACT_BUCKET     = <run scripts/create-deploy-bucket.sh>"
fi
echo "And secrets:"
echo "  DEPUTY_CLIENT_ID, DEPUTY_CLIENT_SECRET"
