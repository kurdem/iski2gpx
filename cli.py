"""Batch: Textdatei mit iSKI-Links -> GPX-Dateien.

    python cli.py urls.txt -o gpx/
    python cli.py urls.txt -o gpx/ --delay 2

Eine URL pro Zeile; Leerzeilen und Zeilen mit # werden ignoriert.
Exit-Code 0 = alles ok, 1 = mindestens ein Fehler, 2 = Aufruffehler.
"""
import argparse
import os
import sys
import time

import app as core


def read_urls(path: str) -> list[str]:
    with (sys.stdin if path == "-" else open(path, encoding="utf-8-sig")) as f:
        lines = [ln.strip() for ln in f]
    urls, seen = [], set()
    for ln in lines:
        if not ln or ln.startswith("#") or ln in seen:
            continue
        seen.add(ln)
        urls.append(ln)
    return urls


def unique_path(outdir: str, name: str) -> str:
    path = os.path.join(outdir, name)
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = f"{stem}_{n}{ext}"
        n += 1
    return path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="iSKI-Track-Links (Textdatei) -> GPX-Dateien")
    p.add_argument("input", help="Textdatei mit einer URL pro Zeile ('-' = stdin)")
    p.add_argument("-o", "--outdir", default="gpx", help="Zielverzeichnis (Default: gpx)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Pause zwischen Abrufen in s, schont die iSKI-API (Default: 1)")
    p.add_argument("--overwrite", action="store_true",
                   help="Vorhandene Dateien überschreiben statt _2, _3 … anzuhängen")
    a = p.parse_args(argv)

    try:
        urls = read_urls(a.input)
    except OSError as e:
        print(f"Eingabedatei nicht lesbar: {e}", file=sys.stderr)
        return 2
    if not urls:
        print("Keine URLs gefunden.", file=sys.stderr)
        return 2
    os.makedirs(a.outdir, exist_ok=True)

    ok, failed = 0, []
    for i, url in enumerate(urls, 1):
        if i > 1 and a.delay > 0:
            time.sleep(a.delay)
        prefix = f"[{i}/{len(urls)}]"
        try:
            track_id, sig = core.parse_url(url)
            t = core.fetch_track(track_id, sig)
            gpx = core.to_gpx(t)
        except core.ConvertError as e:
            failed.append((url, str(e)))
            print(f"{prefix} FEHLER {url}: {e}", file=sys.stderr)
            continue
        name = core.filename(t, track_id)
        path = os.path.join(a.outdir, name) if a.overwrite else unique_path(a.outdir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(gpx)
        ok += 1
        print(f"{prefix} OK {path} ({gpx.count('<trkpt')} Punkte)")

    print(f"\nFertig: {ok} OK, {len(failed)} Fehler -> {os.path.abspath(a.outdir)}")
    if failed:
        print("Fehlgeschlagen:", file=sys.stderr)
        for url, err in failed:
            print(f"  {url}  ({err})", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
