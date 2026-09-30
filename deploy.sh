#!/usr/bin/env bash
# One-shot deploy: creates the secret (prompts once), builds, deploys, prints the connector URL.
# Needs: AWS CLI v2 (logged in), AWS SAM CLI, python3, openssl.
set -euo pipefail
cd "$(dirname "$0")"
SECRET="${SECRET_NAME:-vivosun-tent}"
STACK="vivosun-tent-mcp"

if ! aws secretsmanager describe-secret --secret-id "$SECRET" >/dev/null 2>&1; then
  read -r -p "Vivosun app email: " VS_EMAIL
  read -r -s -p "Vivosun app password: " VS_PW; echo
  VS_TOKEN="$(openssl rand -hex 24)"
  TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
  VS_EMAIL="$VS_EMAIL" VS_PW="$VS_PW" VS_TOKEN="$VS_TOKEN" python3 -c \
    'import json,os;print(json.dumps({"email":os.environ["VS_EMAIL"],"password":os.environ["VS_PW"],"path_token":os.environ["VS_TOKEN"]}))' > "$TMP"
  aws secretsmanager create-secret --name "$SECRET" --secret-string "file://$TMP" >/dev/null
  unset VS_PW
  echo "created secret $SECRET"
fi

./build.sh
sam deploy --template-file template.yaml --stack-name "$STACK" --resolve-s3 \
  --capabilities CAPABILITY_IAM --parameter-overrides "SecretName=$SECRET" \
  --no-confirm-changeset --no-fail-on-empty-changeset

URL="$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue" --output text)"
TOKEN="$(aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text | python3 -c 'import json,sys;print(json.load(sys.stdin)["path_token"])')"
echo
echo "Connector URL (keep private — it is the password):"
echo "${URL}mcp/${TOKEN}"
