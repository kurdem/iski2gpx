"""iSKI track -> GPX converter (web UI)."""
import datetime as dt
import io
import os
import re
import secrets
import threading
import time
import zipfile
from xml.sax.saxutils import escape, quoteattr

import requests
from flask import Flask, Response, abort, render_template_string, request
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)

API_BASE = os.environ.get("ISKI_BASE", "https://www.iski.app")
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20"))
RATE_LIMIT = os.environ.get("RATE_LIMIT", "10 per minute")
# Batch-Upload: eigenes Limit, da ein Upload viele iSKI-Abrufe auslöst
BATCH_RATE_LIMIT = os.environ.get("BATCH_RATE_LIMIT", "3 per 10 minutes")
MAX_BATCH = int(os.environ.get("MAX_BATCH", "50"))
BATCH_DELAY = float(os.environ.get("BATCH_DELAY", "0.3"))
# Gesamtzeit pro Batch (Azure-Ingress-Timeout: 240 s); Rest wird als übersprungen markiert
BATCH_DEADLINE = float(os.environ.get("BATCH_DEADLINE", "180"))
RESULT_TTL = int(os.environ.get("RESULT_TTL", "1800"))       # s, Aufbewahrung Batch-Ergebnisse
MAX_STORED_BATCHES = int(os.environ.get("MAX_STORED_BATCHES", "20"))
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", str(64 * 1024)))

# Anzahl vertrauenswürdiger Reverse Proxies vor der App (Azure Container Apps: 1).
# 0 = X-Forwarded-For ignorieren (lokaler Betrieb ohne Proxy).
TRUSTED_PROXIES = int(os.environ.get("TRUSTED_PROXIES", "0"))
if TRUSTED_PROXIES > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXIES, x_proto=TRUSTED_PROXIES)

# In-Memory-Zähler/-Speicher: gilt pro Prozess -> 1 gunicorn-Worker und max. 1 Replica.
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")
URL_RE = re.compile(r"/track/view/(\d+)(?:/([0-9a-fA-F]+))?")

PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>iSKI → GPX</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:720px;margin:2.5rem auto;padding:0 1rem;color:#222}
 h2{margin-top:2rem;font-size:1.15rem}
 input[type=text]{width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box}
 input[type=file]{margin-top:.3rem}
 button,.btn{display:inline-block;margin-top:.8rem;padding:.6rem 1.2rem;font-size:1rem;cursor:pointer;
   border:1px solid #888;border-radius:4px;background:#f4f4f4;color:#222;text-decoration:none}
 .btn.primary,button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
 .err{background:#fde8e8;border:1px solid #f5b5b5;padding:.6rem;margin-top:1rem;border-radius:4px}
 .ok{background:#e8f6ea;border:1px solid #b5e0bb;padding:.6rem;margin-top:1rem;border-radius:4px}
 table{border-collapse:collapse;width:100%;margin-top:1rem;font-size:.92rem}
 td,th{border-bottom:1px solid #ddd;padding:.4rem;text-align:left;vertical-align:top}
 td.url{word-break:break-all;color:#555}
 small{color:#666}
</style></head><body>
<h1>iSKI → GPX</h1>

{% if batch %}
  <div class="{{ 'ok' if not batch.errors else 'err' }}">
    {{ batch.files|length }} von {{ batch.total }} Tracks konvertiert{% if batch.errors %},
    {{ batch.errors|length }} fehlgeschlagen{% endif %}.
  </div>
  {% if batch.files %}
    <a class="btn primary" href="batch/{{ token }}/zip">Alle als ZIP herunterladen</a>
  {% endif %}
  <table>
    <tr><th>#</th><th>Ergebnis</th><th>URL</th></tr>
    {% for r in batch.rows %}
    <tr>
      <td>{{ loop.index }}</td>
      <td>{% if r.ok %}<a href="batch/{{ token }}/{{ r.idx }}">{{ r.name }}</a>
          <small>({{ r.points }} Punkte)</small>
          {% else %}<span style="color:#b00">Fehler:</span> {{ r.error }}{% endif %}</td>
      <td class="url">{{ r.url }}</td>
    </tr>
    {% endfor %}
  </table>
  <p><small>Downloads sind {{ ttl_min }} Minuten verfügbar.</small></p>
  <a class="btn" href="./">Neue Konvertierung</a>
{% else %}
  <h2>Einzelner Track</h2>
  <form method="post" action="convert">
   <label for="url">iSKI Track-Link</label>
   <input type="text" id="url" name="url" required value="{{ url or '' }}"
    placeholder="https://www.iski.app/track/view/5797606/78eb5c4d04295af1">
   <button class="primary" type="submit">GPX herunterladen</button>
  </form>

  <h2>Mehrere Tracks (TXT-Datei)</h2>
  <form method="post" action="batch" enctype="multipart/form-data">
   <label for="file">Textdatei mit einer URL pro Zeile (max. {{ max_batch }}, # = Kommentar)</label><br>
   <input type="file" id="file" name="file" accept=".txt,text/plain" required>
   <br><button class="primary" type="submit">Hochladen &amp; konvertieren</button>
   <br><small>Die Verarbeitung dauert ca. 1 s pro Track – Seite bitte offen lassen.</small>
  </form>
{% endif %}

{% if error %}<div class="err">{{ error }}</div>{% endif %}
<p><small>Die öffentliche iSKI-API liefert die vereinfachte Geometrie (ca. 1 Punkt / 10 s)
inkl. Höhe und Zeitstempel.<br>
Kein offizieller Dienst von iSKI / Intermaps. Einzel-Downloads werden nicht gespeichert,
Batch-Ergebnisse nur {{ ttl_min }} Minuten im Arbeitsspeicher.</small></p>
</body></html>"""


class ConvertError(Exception):
    pass


# ---------------------------------------------------------------- Kernlogik

def parse_url(url: str):
    m = URL_RE.search(url.strip())
    if not m:
        raise ConvertError("Ungültiger Link. Erwartet: …/track/view/<id>/<signatur>")
    return m.group(1), m.group(2)


def parse_url_list(text: str) -> list[str]:
    """Eine URL pro Zeile; Leerzeilen, #-Kommentare und Duplikate werden ignoriert."""
    urls, seen = [], set()
    for ln in text.splitlines():
        ln = ln.strip().lstrip("﻿")
        if not ln or ln.startswith("#") or ln in seen:
            continue
        seen.add(ln)
        urls.append(ln)
    return urls


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


def convert_url(url: str) -> tuple[str, str]:
    """URL -> (Dateiname, GPX-Text). Wirft ConvertError."""
    track_id, sig = parse_url(url)
    t = fetch_track(track_id, sig)
    return filename(t, track_id), to_gpx(t)


def dedupe_name(name: str, used: set) -> str:
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while candidate in used:
        candidate = f"{stem}_{n}{ext}"
        n += 1
    used.add(candidate)
    return candidate


# ---------------------------------------------------- Batch-Ergebnisspeicher

_store: dict[str, dict] = {}
_store_lock = threading.Lock()


def _store_put(batch: dict) -> str:
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _store_lock:
        for k in [k for k, v in _store.items() if now - v["created"] > RESULT_TTL]:
            del _store[k]
        while len(_store) >= MAX_STORED_BATCHES:
            del _store[min(_store, key=lambda k: _store[k]["created"])]
        batch["created"] = now
        _store[token] = batch
    return token


def _store_get(token: str) -> dict:
    with _store_lock:
        b = _store.get(token)
        if not b or time.time() - b["created"] > RESULT_TTL:
            _store.pop(token, None)
            abort(404, "Ergebnis abgelaufen oder unbekannt.")
        return b


# ------------------------------------------------------------------- Routen

def render(**kw):
    kw.setdefault("error", None)
    kw.setdefault("batch", None)
    return render_template_string(PAGE, max_batch=MAX_BATCH, ttl_min=RESULT_TTL // 60, **kw)


@app.get("/")
def index():
    return render(url=request.args.get("url"))


@app.errorhandler(RateLimitExceeded)
def rate_limited(e):
    msg = f"Zu viele Anfragen (Limit: {e.description} pro IP). Bitte später erneut versuchen."
    return render(url=request.values.get("url"), error=msg), 429


@app.errorhandler(413)
def too_large(_e):
    kb = app.config["MAX_CONTENT_LENGTH"] // 1024
    return render(error=f"Datei zu groß (max. {kb} KB)."), 413


@app.route("/convert", methods=["GET", "POST"])
@limiter.limit(lambda: RATE_LIMIT)
def convert():
    url = request.values.get("url", "")
    try:
        name, gpx = convert_url(url)
    except ConvertError as e:
        return render(url=url, error=str(e)), 400
    return Response(
        gpx,
        mimetype="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/batch")
@limiter.limit(lambda: BATCH_RATE_LIMIT)
def batch():
    f = request.files.get("file")
    if not f or not f.filename:
        return render(error="Keine Datei ausgewählt."), 400
    raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    urls = parse_url_list(text)
    if not urls:
        return render(error="Keine URLs in der Datei gefunden."), 400
    if len(urls) > MAX_BATCH:
        return render(error=f"Zu viele URLs ({len(urls)}). Maximal {MAX_BATCH} pro Datei."), 400

    files, errors, rows, used = [], [], [], set()
    deadline = time.monotonic() + BATCH_DEADLINE
    for i, url in enumerate(urls):
        if time.monotonic() > deadline:
            msg = "Übersprungen (Zeitlimit) – bitte erneut hochladen"
            errors.append((url, msg))
            rows.append({"ok": False, "url": url, "error": msg})
            continue
        if i and BATCH_DELAY > 0:
            time.sleep(BATCH_DELAY)
        try:
            name, gpx = convert_url(url)
        except ConvertError as e:
            errors.append((url, str(e)))
            rows.append({"ok": False, "url": url, "error": str(e)})
            continue
        name = dedupe_name(name, used)
        rows.append({"ok": True, "url": url, "name": name, "idx": len(files),
                     "points": gpx.count("<trkpt")})
        files.append((name, gpx))

    data = {"files": files, "errors": errors, "rows": rows, "total": len(urls)}
    token = _store_put(data)
    return render(batch=data, token=token)


@app.get("/batch/<token>/<int:idx>")
def batch_file(token, idx):
    b = _store_get(token)
    if idx < 0 or idx >= len(b["files"]):
        abort(404)
    name, gpx = b["files"][idx]
    return Response(gpx, mimetype="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/batch/<token>/zip")
def batch_zip(token):
    b = _store_get(token)
    if not b["files"]:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, gpx in b["files"]:
            z.writestr(name, gpx)
        if b["errors"]:
            z.writestr("fehler.txt", "".join(f"{u}\t{e}\n" for u, e in b["errors"]))
    stamp = dt.datetime.fromtimestamp(b["created"]).strftime("%Y%m%d_%H%M%S")
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="iski_gpx_{stamp}.zip"'})


@app.get("/health")
def health():
    return "ok"
