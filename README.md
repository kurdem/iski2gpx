# iski2gpx

Web-UI: öffentlichen iSKI-Track-Link einfügen → GPX-Download.

## Lokal (Docker)

```bash
docker compose up -d --build
```
http://localhost:8080

Direktaufruf:
```bash
curl -OJ "http://localhost:8080/convert?url=https://www.iski.app/track/view/5797606/78eb5c4d04295af1"
```

## Azure (Container Apps, Consumption, scale-to-zero)

Architektur: GitHub Actions → Tests → Image nach `ghcr.io/kurdem/iski2gpx` → OIDC-Login → `az containerapp update`.

| Ressource | Name |
|---|---|
| Region | germanywestcentral |
| Resource Group | rg-iski2gpx |
| Container Apps Environment | cae-iski2gpx (Consumption, ohne Log Analytics) |
| Container App | iski2gpx (0.25 vCPU / 0.5 GiB, 0–1 Replicas) |
| Entra App (OIDC) | gh-iski2gpx-deploy (Contributor nur auf rg-iski2gpx) |

### Einmalige Einrichtung

1. Azure Cloud Shell (Bash) öffnen oder lokal `az login`:
   ```bash
   git clone https://github.com/kurdem/iski2gpx && cd iski2gpx
   bash infra/setup-azure.sh
   # optional mit Budget-Alarm (Mail bei 80 %/100 % Ist und 100 % Prognose):
   BUDGET_EMAIL=ich@example.com BUDGET_AMOUNT=5 bash infra/setup-azure.sh
   ```
2. Ausgegebene Werte als GitHub Actions Secrets eintragen (falls nicht automatisch per `gh` gesetzt):
   `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
3. Actions → **build-and-deploy** → *Run workflow*.
4. Erster Lauf: Package auf **Public** stellen
   (https://github.com/users/kurdem/packages/container/iski2gpx/settings → Change visibility),
   Workflow erneut starten.

Danach deployt jeder Push auf `main` automatisch.

### Kill-Switch (optional)

```bash
BUDGET_EMAIL=ich@example.com BUDGET_AMOUNT=5 KILL_SWITCH=1 bash infra/setup-azure.sh
# optional abweichende Schwelle in % der Ist-Kosten: KILL_THRESHOLD=120
```

Ablauf: Budget (Ist-Kosten ≥ `KILL_THRESHOLD` %) → Action Group `ag-iski2gpx-killswitch`
→ Logic App `la-iski2gpx-killswitch` (Managed Identity, Contributor nur auf der Container App)
→ `POST …/containerApps/iski2gpx/stop`.

- Kostendaten kommen mit 8–24 h Verzögerung → kein Echtzeit-Limit.
- Die App bleibt gestoppt, auch im Folgemonat, bis sie manuell gestartet wird.
- Rechenkosten sind ohnehin durch max. 1 Replica à 0.25 vCPU/0.5 GiB begrenzt;
  der Kill-Switch zielt auf Request-/Traffic-Kosten.

```bash
# Status
az containerapp show -n iski2gpx -g rg-iski2gpx --query properties.runningStatus -o tsv
# Wieder starten
az rest --method post --url "https://management.azure.com$(az containerapp show -n iski2gpx -g rg-iski2gpx --query id -o tsv)/start?api-version=2024-03-01"
```

Test: Portal → Monitor → Action groups → `ag-iski2gpx-killswitch` → *Test* (stoppt die App wirklich).

### Konfiguration (Env-Variablen)

| Variable | Default | Bedeutung |
|---|---|---|
| `RATE_LIMIT` | `10 per minute` | Limit pro Client-IP für `/convert` |
| `TRUSTED_PROXIES` | `0` | Anzahl Proxies vor der App (Azure: `1`) für korrekte Client-IP |
| `HTTP_TIMEOUT` | `20` | Timeout zur iSKI-API in s |

Das Rate Limit zählt im Speicher → 1 gunicorn-Worker und max. 1 Replica.

### Logs

Ohne Log Analytics (keine Kosten). Live-Logs:
```bash
az containerapp logs show -n iski2gpx -g rg-iski2gpx --follow
```

### Kosten

Consumption-Free-Grant pro Subscription/Monat: 180.000 vCPU-s, 360.000 GiB-s, 2 Mio. Requests.
Bei Scale-to-zero und geringem Traffic: ~0 €. Empfehlung: Budget-Alert auf der Subscription.

### Rückbau

```bash
az group delete -n rg-iski2gpx --yes
az ad app delete --id $(az ad app list --display-name gh-iski2gpx-deploy --query "[0].appId" -o tsv)
# Kill-Switch (Logic App + Action Group) liegt in der Resource Group und wird mit gelöscht.
```

## Funktionsweise

Link `…/track/view/<id>/<signatur>` → `GET https://www.iski.app/api/track/view/<id>/<signatur>`
→ `track.geom_wgs_simplified` (`[lon, lat, ele, unix_ts]`) → GPX 1.1 (trkpt mit ele + time UTC).
Der Ziel-Host ist fest; aus der Eingabe werden nur ID und Signatur übernommen.

## Einschränkungen

- Nur öffentlich geteilte Tracks (mit Signatur im Link).
- Nur vereinfachte Geometrie (~1 Punkt / 10 s), keine Rohdaten.
- Inoffizielle API – kann sich ohne Ankündigung ändern.
- Für öffentlichen Betrieb in DE: Impressum/Datenschutz ergänzen; iSKI-AGB prüfen.
