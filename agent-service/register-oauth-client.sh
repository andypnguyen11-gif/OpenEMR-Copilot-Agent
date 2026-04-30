#!/bin/sh
#
# One-shot OAuth2 confidential client registration against the deployed
# OpenEMR. POSTs to /oauth2/default/registration with a confidential
# client_credentials body and saves the response (which contains
# client_id + client_secret + registration_access_token) to
# /tmp/oauth-registration.json.
#
# Run from anywhere:
#   sh agent-service/register-oauth-client.sh
#
# Requires (in OpenEMR globals → Connectors, all checked + saved):
#   - Enable OpenEMR Standard FHIR REST API
#   - Enable OpenEMR FHIR System Scopes ← without this, OpenEMR strips
#                                         the system/* scopes silently
#   - Enable OpenEMR Standard REST API
#
# This is a one-shot. Re-running creates a separate client registration;
# the original credentials still work and have to be revoked separately
# via the API Clients admin page if you want to clean them up.
set -eu

OPENEMR_HOST="openemr-production-6c31.up.railway.app"
OUTPUT_FILE="/tmp/oauth-registration.json"
PAYLOAD_FILE="/tmp/oauth-register-payload.json"

cat > "$PAYLOAD_FILE" <<JSON
{
  "application_type": "private",
  "client_name": "Clinical Co-Pilot Agent Service",
  "redirect_uris": ["https://${OPENEMR_HOST}/"],
  "token_endpoint_auth_method": "client_secret_post",
  "grant_types": ["client_credentials"],
  "scope": "system/Patient.read system/Condition.read system/MedicationRequest.read system/MedicationStatement.read system/AllergyIntolerance.read system/Observation.read system/Encounter.read system/DocumentReference.read"
}
JSON

echo "POSTing OAuth2 client registration to https://${OPENEMR_HOST}/oauth2/default/registration ..."
echo
curl -sS -X POST "https://${OPENEMR_HOST}/oauth2/default/registration" \
    -H "Content-Type: application/json" \
    --data-binary "@${PAYLOAD_FILE}" \
    | tee "$OUTPUT_FILE"
echo
echo
echo "Response saved to $OUTPUT_FILE"
echo "Copy client_id and client_secret from the JSON above — they're the values"
echo "to set as OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET on the agent-service in Railway."
