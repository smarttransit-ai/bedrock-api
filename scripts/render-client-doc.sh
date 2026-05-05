#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: render-client-doc.sh --owner OWNER --token-id TOKEN_ID \
                            --bearer BEARER_TOKEN --api-url API_URL \
                            [--budget BUDGET_USD] \
                            [--rps N] [--monthly-requests N] \
                            [--max-input-tokens N] [--max-output-tokens N] \
                            [--models CSV] \
                            [--lookup] [--region REGION] [--table-prefix PREFIX]

Renders docs/clients.md.tmpl for one token holder and writes the result
to stdout. Redirect to docs/clients-OWNER.md.

Required: --owner, --token-id, --bearer, --api-url

Limits (--budget, --rps, --monthly-requests, --max-input-tokens,
--max-output-tokens, --models) can be passed explicitly, or fetched from
DynamoDB by adding --lookup. Unset limits render as "unlimited" (or "all"
for models). Explicit flags always win over --lookup values.

--lookup runs aws dynamodb get-item on the tokens table; honors AWS_PROFILE
and AWS_REGION env vars. Requires aws and jq on PATH.

Example (manual):
  render-client-doc.sh \
    --owner alice --token-id bk_<32hex> \
    --bearer "$(cat token.txt)" \
    --api-url "$(cd terraform/main && terraform output -raw api_url)" \
    --budget 10.00 --rps 5 \
    > docs/clients-alice.md

Example (auto-fill from DynamoDB):
  AWS_PROFILE=admin render-client-doc.sh \
    --owner alice --token-id bk_<32hex> \
    --bearer "$(cat token.txt)" \
    --api-url "$(cd terraform/main && terraform output -raw api_url)" \
    --lookup \
    > docs/clients-alice.md
EOF
  exit 1
}

OWNER= TOKEN_ID= BEARER= API_URL=
BUDGET= RPS= MONTHLY_REQUESTS= MAX_INPUT_TOKENS= MAX_OUTPUT_TOKENS= ALLOWED_MODELS=
LOOKUP=0
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
TABLE_PREFIX=bedrock-api

while [[ $# -gt 0 ]]; do
  case $1 in
    --owner) OWNER=$2; shift 2 ;;
    --token-id) TOKEN_ID=$2; shift 2 ;;
    --bearer) BEARER=$2; shift 2 ;;
    --api-url) API_URL=$2; shift 2 ;;
    --budget) BUDGET=$2; shift 2 ;;
    --rps) RPS=$2; shift 2 ;;
    --monthly-requests) MONTHLY_REQUESTS=$2; shift 2 ;;
    --max-input-tokens) MAX_INPUT_TOKENS=$2; shift 2 ;;
    --max-output-tokens) MAX_OUTPUT_TOKENS=$2; shift 2 ;;
    --models) ALLOWED_MODELS=$2; shift 2 ;;
    --lookup) LOOKUP=1; shift ;;
    --region) REGION=$2; shift 2 ;;
    --table-prefix) TABLE_PREFIX=$2; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown flag: $1" >&2; usage ;;
  esac
done

[[ -z $OWNER || -z $TOKEN_ID || -z $BEARER || -z $API_URL ]] && usage

if [[ $LOOKUP -eq 1 ]]; then
  command -v aws >/dev/null || { echo "--lookup requires aws CLI on PATH" >&2; exit 1; }
  command -v jq >/dev/null || { echo "--lookup requires jq on PATH" >&2; exit 1; }

  ITEM_JSON=$(aws dynamodb get-item \
    --region "$REGION" \
    --table-name "${TABLE_PREFIX}-tokens" \
    --key "{\"token_id\":{\"S\":\"$TOKEN_ID\"}}" \
    --output json)

  if [[ -z $(echo "$ITEM_JSON" | jq -r '.Item // empty') ]]; then
    echo "--lookup: token_id not found in ${TABLE_PREFIX}-tokens: $TOKEN_ID" >&2
    exit 1
  fi

  # For each limit: only fall back to the looked-up value if the flag wasn't set.
  # jq returns "null" for missing attributes; leave the script default in that case.
  fetch() { echo "$ITEM_JSON" | jq -r "$1 // empty"; }

  [[ -z $BUDGET ]] && {
    micros=$(fetch '.Item.limit_monthly_usd_micros.N')
    [[ -n $micros ]] && BUDGET=$(awk -v m="$micros" 'BEGIN{printf "%.2f", m/1000000}')
  }
  [[ -z $RPS ]] && RPS=$(fetch '.Item.limit_rps.N')
  [[ -z $MONTHLY_REQUESTS ]] && MONTHLY_REQUESTS=$(fetch '.Item.limit_monthly_requests.N')
  [[ -z $MAX_INPUT_TOKENS ]] && MAX_INPUT_TOKENS=$(fetch '.Item.limit_max_input_tokens.N')
  [[ -z $MAX_OUTPUT_TOKENS ]] && MAX_OUTPUT_TOKENS=$(fetch '.Item.limit_max_output_tokens.N')
  [[ -z $ALLOWED_MODELS ]] && ALLOWED_MODELS=$(echo "$ITEM_JSON" | jq -r '.Item.allowed_models.SS // [] | join(",")')
fi

# Apply final defaults for anything still empty
[[ -z $RPS ]] && RPS=unlimited
[[ -z $MONTHLY_REQUESTS ]] && MONTHLY_REQUESTS=unlimited
[[ -z $MAX_INPUT_TOKENS ]] && MAX_INPUT_TOKENS=unlimited
[[ -z $MAX_OUTPUT_TOKENS ]] && MAX_OUTPUT_TOKENS=unlimited
[[ -z $ALLOWED_MODELS ]] && ALLOWED_MODELS=all

if [[ -z $BUDGET ]]; then
  echo "error: --budget required (or pass --lookup with a token that has limit_monthly_usd_micros set)" >&2
  exit 1
fi

TMPL="$(dirname "$0")/../docs/clients.md.tmpl"
[[ -f $TMPL ]] || { echo "missing template: $TMPL" >&2; exit 1; }

sed \
  -e "s|{{OWNER}}|$OWNER|g" \
  -e "s|{{TOKEN_ID}}|$TOKEN_ID|g" \
  -e "s|{{BEARER_TOKEN}}|$BEARER|g" \
  -e "s|{{API_URL}}|$API_URL|g" \
  -e "s|{{BUDGET_USD}}|$BUDGET|g" \
  -e "s|{{RPS}}|$RPS|g" \
  -e "s|{{MONTHLY_REQUESTS}}|$MONTHLY_REQUESTS|g" \
  -e "s|{{MAX_INPUT_TOKENS}}|$MAX_INPUT_TOKENS|g" \
  -e "s|{{MAX_OUTPUT_TOKENS}}|$MAX_OUTPUT_TOKENS|g" \
  -e "s|{{ALLOWED_MODELS}}|$ALLOWED_MODELS|g" \
  "$TMPL"
