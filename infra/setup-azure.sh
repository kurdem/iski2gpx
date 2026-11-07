#!/usr/bin/env bash
# Einmaliges Azure-Setup für iski2gpx (idempotent, mehrfach ausführbar).
# Ausführen in Azure Cloud Shell (Bash) oder lokal mit angemeldeter az CLI:
#   az login && az account set -s <SUBSCRIPTION>
#   bash infra/setup-azure.sh
set -euo pipefail

LOCATION="${LOCATION:-germanywestcentral}"
RG="${RG:-rg-iski2gpx}"
ENV_NAME="${ENV_NAME:-cae-iski2gpx}"
APP="${APP:-iski2gpx}"
GH_REPO="${GH_REPO:-kurdem/iski2gpx}"
GH_BRANCH="${GH_BRANCH:-main}"
SP_NAME="${SP_NAME:-gh-iski2gpx-deploy}"
# Optional: Budget-Alarm auf der Resource Group (leer = überspringen)
#   BUDGET_EMAIL=ich@example.com BUDGET_AMOUNT=5 bash infra/setup-azure.sh
BUDGET_EMAIL="${BUDGET_EMAIL:-}"
BUDGET_AMOUNT="${BUDGET_AMOUNT:-5}"
BUDGET_NAME="${BUDGET_NAME:-budget-iski2gpx}"
# Optional: Kill-Switch – stoppt die Container App, wenn die Ist-Kosten
# KILL_THRESHOLD % des Budgets überschreiten (benötigt BUDGET_EMAIL).
#   BUDGET_EMAIL=ich@example.com KILL_SWITCH=1 bash infra/setup-azure.sh
KILL_SWITCH="${KILL_SWITCH:-0}"
KILL_THRESHOLD="${KILL_THRESHOLD:-100}"
KILL_LA_NAME="${KILL_LA_NAME:-la-iski2gpx-killswitch}"
KILL_AG_NAME="${KILL_AG_NAME:-ag-iski2gpx-killswitch}"
ACA_API="2024-03-01"
# Platzhalter bis zum ersten GitHub-Deploy (lauscht auf 80, Ingress zeigt schon auf 8080)
PLACEHOLDER_IMAGE="mcr.microsoft.com/k8se/quickstart:latest"

if [ "$KILL_SWITCH" = 1 ] && [ -z "$BUDGET_EMAIL" ]; then
  echo "FEHLER: KILL_SWITCH=1 benötigt BUDGET_EMAIL." >&2; exit 1
fi

SUB_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
echo ">> Subscription: $(az account show --query name -o tsv) ($SUB_ID)"

echo ">> CLI-Extension + Resource Provider"
az extension add --name containerapp --upgrade --only-show-errors
az provider register --namespace Microsoft.App --wait

echo ">> Resource Group $RG ($LOCATION)"
az group create -n "$RG" -l "$LOCATION" -o none

if ! az containerapp env show -n "$ENV_NAME" -g "$RG" -o none 2>/dev/null; then
  echo ">> Container Apps Environment $ENV_NAME (Consumption, ohne Log Analytics)"
  az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOCATION" \
    --logs-destination none -o none
fi

if ! az containerapp show -n "$APP" -g "$RG" -o none 2>/dev/null; then
  echo ">> Container App $APP"
  az containerapp create -n "$APP" -g "$RG" --environment "$ENV_NAME" \
    --image "$PLACEHOLDER_IMAGE" \
    --ingress external --target-port 8080 \
    --cpu 0.25 --memory 0.5Gi \
    --min-replicas 0 --max-replicas 1 \
    --env-vars TRUSTED_PROXIES=1 "RATE_LIMIT=10 per minute" \
    -o none
fi

echo ">> Entra App + Service Principal für GitHub OIDC ($SP_NAME)"
APP_ID=$(az ad app list --display-name "$SP_NAME" --query "[0].appId" -o tsv)
if [ -z "$APP_ID" ]; then
  APP_ID=$(az ad app create --display-name "$SP_NAME" --query appId -o tsv)
fi
SP_OID=$(az ad sp list --filter "appId eq '$APP_ID'" --query "[0].id" -o tsv)
if [ -z "$SP_OID" ]; then
  SP_OID=$(az ad sp create --id "$APP_ID" --query id -o tsv)
fi

FC_NAME="github-${GH_BRANCH}"
if ! az ad app federated-credential list --id "$APP_ID" --query "[?name=='$FC_NAME'].name" -o tsv | grep -q .; then
  az ad app federated-credential create --id "$APP_ID" -o none --parameters "{
    \"name\": \"$FC_NAME\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"repo:${GH_REPO}:ref:refs/heads/${GH_BRANCH}\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }"
fi

echo ">> Rolle 'Contributor' auf $RG (nur diese Resource Group)"
RG_ID=$(az group show -n "$RG" --query id -o tsv)
for i in 1 2 3 4 5 6; do
  if az role assignment create --assignee-object-id "$SP_OID" \
       --assignee-principal-type ServicePrincipal \
       --role Contributor --scope "$RG_ID" -o none 2>/dev/null; then
    break
  fi
  # Neue Service Principals brauchen teils etwas bis zur Replikation
  if az role assignment list --assignee "$SP_OID" --scope "$RG_ID" --query "[0]" -o tsv | grep -q .; then
    break
  fi
  echo "   warte auf Replikation ($i/6) ..."; sleep 15
done

AG_ID=""
if [ "$KILL_SWITCH" = 1 ]; then
  echo ">> Kill-Switch: Logic App $KILL_LA_NAME + Action Group $KILL_AG_NAME"
  az provider register --namespace Microsoft.Logic --wait
  az provider register --namespace Microsoft.Insights --wait
  ACA_ID=$(az containerapp show -n "$APP" -g "$RG" --query id -o tsv)
  LA_ID="${RG_ID}/providers/Microsoft.Logic/workflows/${KILL_LA_NAME}"

  # Logic App (Consumption): HTTP-Trigger -> POST <ContainerApp>/stop per Managed Identity
  LA_BODY=$(cat <<JSON
{
  "location": "${LOCATION}",
  "identity": { "type": "SystemAssigned" },
  "properties": {
    "state": "Enabled",
    "definition": {
      "\$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
      "contentVersion": "1.0.0.0",
      "triggers": {
        "budget_alert": { "type": "Request", "kind": "Http", "inputs": { "schema": {} } }
      },
      "actions": {
        "Stop_Container_App": {
          "type": "Http",
          "runAfter": {},
          "inputs": {
            "method": "POST",
            "uri": "https://management.azure.com${ACA_ID}/stop?api-version=${ACA_API}",
            "authentication": { "type": "ManagedServiceIdentity", "audience": "https://management.azure.com/" }
          }
        }
      },
      "outputs": {}
    }
  }
}
JSON
)
  LA_PRINCIPAL=$(az rest --method put \
    --url "https://management.azure.com${LA_ID}?api-version=2019-05-01" \
    --body "$LA_BODY" --query identity.principalId -o tsv)

  echo "   Rolle 'Contributor' für Logic App nur auf Container App $APP"
  for i in 1 2 3 4 5 6; do
    if az role assignment create --assignee-object-id "$LA_PRINCIPAL" \
         --assignee-principal-type ServicePrincipal \
         --role Contributor --scope "$ACA_ID" -o none 2>/dev/null; then
      break
    fi
    if az role assignment list --assignee "$LA_PRINCIPAL" --scope "$ACA_ID" --query "[0]" -o tsv | grep -q .; then
      break
    fi
    echo "   warte auf Replikation ($i/6) ..."; sleep 15
  done

  CALLBACK_URL=$(az rest --method post \
    --url "https://management.azure.com${LA_ID}/triggers/budget_alert/listCallbackUrl?api-version=2019-05-01" \
    --query value -o tsv)

  AG_ID="${RG_ID}/providers/Microsoft.Insights/actionGroups/${KILL_AG_NAME}"
  az rest --method put -o none \
    --url "https://management.azure.com${AG_ID}?api-version=2023-01-01" \
    --body "{
      \"location\": \"Global\",
      \"properties\": {
        \"groupShortName\": \"iskikill\",
        \"enabled\": true,
        \"logicAppReceivers\": [
          { \"name\": \"stop-iski2gpx\", \"resourceId\": \"${LA_ID}\", \"callbackUrl\": \"${CALLBACK_URL}\", \"useCommonAlertSchema\": true }
        ]
      }
    }"
fi

if [ -n "$BUDGET_EMAIL" ]; then
  echo ">> Budget $BUDGET_NAME: $BUDGET_AMOUNT/Monat auf $RG, Alarm an $BUDGET_EMAIL"
  [ -n "$AG_ID" ] && echo "   Kill-Switch bei ${KILL_THRESHOLD} % Ist-Kosten"
  # Startdatum muss der 1. des aktuellen Monats sein; PUT ist idempotent.
  START_DATE=$(date -u +%Y-%m-01T00:00:00Z)
  BUDGET_BODY=$(python3 - "$BUDGET_AMOUNT" "$START_DATE" "$BUDGET_EMAIL" "$AG_ID" "$KILL_THRESHOLD" <<'PY'
import json, sys
amount, start, email, ag_id, kill_th = sys.argv[1:6]
amount = float(amount); kill_th = float(kill_th)
notes = {}
def add(kind, th, groups=None):
    key = f"{kind.lower()}{int(th)}"
    n = notes.setdefault(key, {"enabled": True, "operator": "GreaterThan", "threshold": th,
                               "thresholdType": kind, "contactEmails": [email]})
    if groups:
        n["contactGroups"] = groups
add("Actual", 80)
add("Actual", 100)
add("Forecasted", 100)
if ag_id:
    add("Actual", kill_th, [ag_id])
print(json.dumps({"properties": {"category": "Cost", "amount": amount, "timeGrain": "Monthly",
                                 "timePeriod": {"startDate": start}, "notifications": notes}}))
PY
)
  az rest --method put -o none \
    --url "https://management.azure.com${RG_ID}/providers/Microsoft.Consumption/budgets/${BUDGET_NAME}?api-version=2023-05-01" \
    --body "$BUDGET_BODY"
else
  echo ">> Budget übersprungen (BUDGET_EMAIL nicht gesetzt)"
fi

FQDN=$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  echo ">> GitHub Secrets per gh setzen"
  gh secret set AZURE_CLIENT_ID       -R "$GH_REPO" -b "$APP_ID"
  gh secret set AZURE_TENANT_ID       -R "$GH_REPO" -b "$TENANT_ID"
  gh secret set AZURE_SUBSCRIPTION_ID -R "$GH_REPO" -b "$SUB_ID"
  SECRETS_DONE=1
else
  SECRETS_DONE=0
fi

cat <<EOF

================ Fertig ================
App-URL (nach erstem Deploy): https://$FQDN

GitHub Secrets (Repo -> Settings -> Secrets and variables -> Actions):
  AZURE_CLIENT_ID       = $APP_ID
  AZURE_TENANT_ID       = $TENANT_ID
  AZURE_SUBSCRIPTION_ID = $SUB_ID
$( [ "$SECRETS_DONE" = 1 ] && echo "  (bereits per gh gesetzt)" || echo "  -> bitte manuell eintragen" )

Nächste Schritte:
  1. Actions -> build-and-deploy -> Run workflow
  2. Beim ersten Lauf schlägt 'deploy' ggf. fehl, weil das Image privat ist:
     https://github.com/users/kurdem/packages/container/iski2gpx/settings
     -> Change visibility -> Public, dann Workflow erneut starten.
$( [ -n "$AG_ID" ] && cat <<KS

Kill-Switch aktiv (${KILL_THRESHOLD} % von ${BUDGET_AMOUNT}/Monat, Verzögerung 8-24 h).
  Test (stoppt die App!):  Portal -> Monitor -> Action groups -> ${KILL_AG_NAME} -> Test
  Status:                  az containerapp show -n ${APP} -g ${RG} --query properties.runningStatus -o tsv
  Wieder starten:          az rest --method post --url "https://management.azure.com${RG_ID}/providers/Microsoft.App/containerApps/${APP}/start?api-version=${ACA_API}"
KS
)
========================================
EOF
