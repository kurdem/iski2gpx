# iski2gpx

Web-UI: iSKI-Track-Link einfügen → GPX-Download.

## Start

```bash
docker compose up -d --build
```

Aufruf: http://<host>:8080

## Direktaufruf (z. B. Skript / Bookmark)

```bash
curl -OJ "http://<host>:8080/convert?url=https://www.iski.app/track/view/5797606/78eb5c4d04295af1"
```

## Funktionsweise

Link `…/track/view/<id>/<signatur>` → `GET https://www.iski.app/api/track/view/<id>/<signatur>`
→ `track.geom_wgs_simplified` (`[lon, lat, ele, unix_ts]`) → GPX 1.1 (trkpt mit ele + time UTC).

## Einschränkungen

- Nur öffentlich geteilte Tracks (mit Signatur im Link).
- Die API liefert nur die vereinfachte Geometrie (~1 Punkt / 10 s), keine Rohdaten.
- Inoffizielle API – kann sich ohne Ankündigung ändern.
