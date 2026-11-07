import os

import app
import cli
from test_app import TRACK, FakeResp


def test_batch(tmp_path, monkeypatch):
    def fake_get(url, **kw):
        if url.endswith("/404/ab"):
            return FakeResp(404)
        return FakeResp(200, TRACK)

    monkeypatch.setattr(app.requests, "get", fake_get)
    inp = tmp_path / "urls.txt"
    inp.write_text(
        "# Kommentar\n"
        "https://www.iski.app/track/view/5797606/78eb5c4d04295af1\n"
        "\n"
        "https://www.iski.app/track/view/5797606/78eb5c4d04295af1\n"  # Duplikat
        "https://www.iski.app/track/view/5797607/aa\n"
        "https://www.iski.app/track/view/404/ab\n"
        "kaputt\n",
        encoding="utf-8",
    )
    out = tmp_path / "gpx"
    rc = cli.main([str(inp), "-o", str(out), "--delay", "0"])
    assert rc == 1
    files = sorted(os.listdir(out))
    assert files == [
        "iski_5797606_skiliftkarussell_winterberg_2025-11-30.gpx",
        "iski_5797607_skiliftkarussell_winterberg_2025-11-30.gpx",
    ]


def test_batch_all_ok_and_no_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(app.requests, "get", lambda url, **kw: FakeResp(200, TRACK))
    inp = tmp_path / "urls.txt"
    inp.write_text("https://www.iski.app/track/view/1/ab\n", encoding="utf-8")
    out = tmp_path / "gpx"
    assert cli.main([str(inp), "-o", str(out), "--delay", "0"]) == 0
    assert cli.main([str(inp), "-o", str(out), "--delay", "0"]) == 0
    assert len(os.listdir(out)) == 2  # zweiter Lauf -> _2


def test_empty_input(tmp_path):
    inp = tmp_path / "urls.txt"
    inp.write_text("# nur Kommentar\n", encoding="utf-8")
    assert cli.main([str(inp), "-o", str(tmp_path / "o")]) == 2
