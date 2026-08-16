#!/usr/bin/env bash
#
# Register the Azure DevOps Service Hook that feeds the self-healing Function.
#
# Creates ONE project-wide subscription: every *failed* build POSTs to the
# Function's /hook endpoint. The Function then filters to MONITORED_PIPELINES, so
# you manage the monitored *set* as an app setting — not as N subscriptions and
# not by editing pipeline YAML.
#
# This is a one-time admin action; it uses a PAT for setup only (the runtime
# healer uses managed identity). Required env vars:
#   ADO_ORG_URL      e.g. https://dev.azure.com/contoso
#   ADO_PROJECT_ID   project GUID (az devops project show --query id -o tsv)
#   HEALER_HOOK_URL  Function URL incl. key, e.g.
#                    https://<app>.azurewebsites.net/api/hook?code=<function-key>
#   AZDO_PAT         admin PAT with "Service Hooks: read & write" (setup only)
#
set -euo pipefail
: "${ADO_ORG_URL:?set ADO_ORG_URL}"
: "${ADO_PROJECT_ID:?set ADO_PROJECT_ID}"
: "${HEALER_HOOK_URL:?set HEALER_HOOK_URL}"
: "${AZDO_PAT:?set AZDO_PAT}"

curl -sf -u ":${AZDO_PAT}" -H "Content-Type: application/json" \
  "${ADO_ORG_URL%/}/_apis/hooks/subscriptions?api-version=7.1" -d @- <<JSON
{
  "publisherId": "tfs",
  "eventType": "build.complete",
  "resourceVersion": "1.0",
  "consumerId": "webHooks",
  "consumerActionId": "httpRequest",
  "publisherInputs": {
    "buildStatus": "Failed",
    "projectId": "${ADO_PROJECT_ID}"
  },
  "consumerInputs": {
    "url": "${HEALER_HOOK_URL}"
  }
}
JSON

echo
echo "Subscription created. All failed builds in project ${ADO_PROJECT_ID} now POST"
echo "to the healer; set MONITORED_PIPELINES on the Function to choose which heal."
echo
echo "To scope natively to ONE pipeline instead of the allowlist, add"
echo '  "definitionName": "<pipeline name>"  to publisherInputs and run once per pipeline.'
