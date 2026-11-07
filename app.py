"""iSKI track -> GPX converter (web UI)."""
import datetime as dt
import os
import re
from xml.sax.saxutils import escape, quoteattr

import requests
from flask import Flask, Response, render_template_string, request

app = Flask(__name__)

API_BASE = os.environ.get("ISKI_BASE", "https://www.iski.app")
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20"))
URL_RE = re.compile(r"/track/view/(\d+)(?:/([0-9a-fA-F]+))?")

PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>iSKI → GPX</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:640px;margin:3rem auto;padding:0 1rem;color:#222}
 input[type=text]{width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box}
 button{margin-top:.8rem;padding:.6rem 1.2rem;font-size:1rem;cursor:pointer}
 .err{background:#fde8e8;border:1px solid #f5b5b5;padding:.6rem;margin-top:1rem;border-radius:4px}
 small{color:#666}
</style></head><body>
<h1>iSKI → GPX</h1>
<form method="post" action="convert">
 <label for="url">iSKI Track-Link</label>
 <input type="text" id="url" name="url" required value="{{ url or '' }}"
  placeholder="https://www.iski.app/track/view/5797606/78eb5c4d04295af1">
 <button type="submit">GPX herunterladen</button>
</form>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<p><small>Hinweis: Die öffentliche API liefert die vereinfachte Geometrie
(ca. 1 Punkt / 10 s) inkl. Höhe und Zeitstempel.</small></p>
</body></html>"""


class ConvertError(Exception):
    pass


def parse_url(url: str):
    m = URL_RE.search(url.strip())
    if not m:
        raise ConvertError("Ungültiger Link. Erwartet: …/track/view/<id>/<signatur>")
    return m.group(1), m.group(2)


def fetch_track(track_id: str, sig: str | None) -> dict:
    api = f"{API_BASE}/api/track/view/{track_id}" + (f"/{sig}" if sig else "")
    try:
        r = requests.get(
            api,
            headers={"Accept": "application/json", "User-Agent": "iski2gpx/1.0"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise ConvertError(f"Verbindungsfehler zu iSKI: {e}") from e
    if r.status_code in (401, 403, 404):
        raise ConvertError(f"Track nicht abrufbar (HTTP {r.status_code}). Ist der Link öffentlich geteilt?")
    if not r.ok:
        raise ConvertError(f"iSKI-API antwortet mit HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise ConvertError("Antwort ist kein JSON – API evtl. geändert.") from e
    if "track" not in data:
        raise ConvertError("Keine Trackdaten in der Antwort.")
    return data["track"]


def to_gpx(t: dict) -> str:
    geom = t.get("geom_wgs_simplified") or {}
    pts = geom.get("coordinates") or []
    if not pts:
        raise ConvertError("Track enthält keine Koordinaten.")
    name = f"{t.get('location', 'iSKI')} {str(t.get('started_at_utc', ''))[:10]}".strip()
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="iski2gpx" xmlns="http://www.topografix.com/GPX/1/1">',
        f"<metadata><name>{escape(name)}</name>"
        + (f"<time>{escape(t['started_at_utc'])}</time>" if t.get("started_at_utc") else "")
        + "</metadata>",
        f"<trk><name>{escape(name)}</name><type>{escape(str(t.get('type', '')))}</type><trkseg>",
    ]
    for p in pts:
        lon, lat = p[0], p[1]
        line = f"<trkpt lat={quoteattr(str(lat))} lon={quoteattr(str(lon))}>"
        if len(p) > 2 and p[2] is not None:
            line += f"<ele>{p[2]}</ele>"
        if len(p) > 3 and p[3] is not None:
            ts = dt.datetime.fromtimestamp(float(p[3]), dt.timezone.utc)
            line += f"<time>{ts.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z</time>"
        out.append(line + "</trkpt>")
    out += ["</trkseg></trk>", "</gpx>", ""]
    return "\n".join(out)


def filename(t: dict, track_id: str) -> str:
    loc = re.sub(r"[^A-Za-z0-9]+", "_", str(t.get("location", "track"))).strip("_").lower()
    day = str(t.get("started_at_utc", ""))[:10]
    return f"iski_{track_id}_{loc}_{day}.gpx".replace("__", "_")


@app.get("/")
def index():
    return render_template_string(PAGE, url=request.args.get("url"), error=None)


@app.route("/convert", methods=["GET", "POST"])
def convert():
    url = request.values.get("url", "")
    try:
        track_id, sig = parse_url(url)
        t = fetch_track(track_id, sig)
        gpx = to_gpx(t)
    except ConvertError as e:
        return render_template_string(PAGE, url=url, error=str(e)), 400
    return Response(
        gpx,
        mimetype="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename(t, track_id)}"'},
    )


@app.get("/health")
def health():
    return "ok"
