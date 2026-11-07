import importlib
import xml.dom.minidom

import pytest

TRACK = {
    "track": {
        "location": "Skiliftkarussell Winterberg",
        "type": "skiing",
        "started_at_utc": "2025-11-30T07:44:53Z",
        "geom_wgs_simplified": {
            "type": "LineString",
            "coordinates": [
                [8.50737638, 51.18957653, 735, 1764488693.293],
                [8.50741624, 51.18966691, 735, 1764488723],
                [8.50736806, 51.18976122, 736, 1764488732],
            ],
        },
    }
}


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT", "3 per minute")
    import app as a
    a = importlib.reload(a)
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return FakeResp(200, TRACK)

    monkeypatch.setattr(a.requests, "get", fake_get)
    a.calls = calls
    return a


URL = "https://www.iski.app/track/view/5797606/78eb5c4d04295af1"


def test_index(mod):
    assert mod.app.test_client().get("/").status_code == 200


def test_convert_ok(mod):
    r = mod.app.test_client().post("/convert", data={"url": URL})
    assert r.status_code == 200
    assert r.mimetype == "application/gpx+xml"
    assert "iski_5797606_skiliftkarussell_winterberg_2025-11-30.gpx" in r.headers["Content-Disposition"]
    doc = xml.dom.minidom.parseString(r.data)
    pts = doc.getElementsByTagName("trkpt")
    assert len(pts) == 3
    assert pts[0].getAttribute("lat") == "51.18957653"
    assert pts[0].getElementsByTagName("time")[0].firstChild.data == "2025-11-30T07:44:53.293Z"
    assert mod.calls == ["https://www.iski.app/api/track/view/5797606/78eb5c4d04295af1"]


def test_host_is_fixed(mod):
    # Fremder Host in der Eingabe darf nicht abgefragt werden (kein SSRF)
    mod.app.test_client().post("/convert", data={"url": "https://evil.example/track/view/1/ab"})
    assert mod.calls == ["https://www.iski.app/api/track/view/1/ab"]


def test_invalid_url(mod):
    r = mod.app.test_client().post("/convert", data={"url": "foo"})
    assert r.status_code == 400


def test_upstream_404(mod, monkeypatch):
    monkeypatch.setattr(mod.requests, "get", lambda url, **kw: FakeResp(404))
    r = mod.app.test_client().post("/convert", data={"url": URL})
    assert r.status_code == 400
    assert b"404" in r.data


def test_rate_limit(mod):
    c = mod.app.test_client()
    codes = [c.post("/convert", data={"url": URL}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_rate_limit_per_client_ip_behind_proxy(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT", "1 per minute")
    monkeypatch.setenv("TRUSTED_PROXIES", "1")
    import app as a
    a = importlib.reload(a)
    monkeypatch.setattr(a.requests, "get", lambda url, **kw: FakeResp(200, TRACK))
    c = a.app.test_client()
    h1 = {"X-Forwarded-For": "203.0.113.1"}
    h2 = {"X-Forwarded-For": "203.0.113.2"}
    assert c.post("/convert", data={"url": URL}, headers=h1).status_code == 200
    assert c.post("/convert", data={"url": URL}, headers=h1).status_code == 429
    assert c.post("/convert", data={"url": URL}, headers=h2).status_code == 200


# ------------------------------------------------------------------ Batch

import io as _io
import zipfile as _zip


def _upload(client, text, name="urls.txt"):
    return client.post("/batch", data={"file": (_io.BytesIO(text.encode()), name)},
                       content_type="multipart/form-data")


@pytest.fixture
def bmod(monkeypatch):
    monkeypatch.setenv("BATCH_DELAY", "0")
    monkeypatch.setenv("BATCH_RATE_LIMIT", "100 per minute")
    monkeypatch.setenv("MAX_BATCH", "5")
    import app as a
    a = importlib.reload(a)

    def fake_get(url, **kw):
        return FakeResp(404) if "/404/" in url else FakeResp(200, TRACK)

    monkeypatch.setattr(a.requests, "get", fake_get)
    return a


def _token(html: bytes) -> str:
    import re
    return re.search(rb'href="batch/([A-Za-z0-9_-]+)/zip"', html).group(1).decode()


def test_batch_upload_zip_and_single(bmod):
    c = bmod.app.test_client()
    r = _upload(c, "# x\nhttps://www.iski.app/track/view/1/ab\n\nhttps://www.iski.app/track/view/2/ab\n"
                   "https://www.iski.app/track/view/404/ab\nkaputt\n")
    assert r.status_code == 200
    assert b"2 von 4 Tracks konvertiert" in r.data
    tok = _token(r.data)

    z = c.get(f"/batch/{tok}/zip")
    assert z.status_code == 200 and z.mimetype == "application/zip"
    names = sorted(_zip.ZipFile(_io.BytesIO(z.data)).namelist())
    assert names == ["fehler.txt",
                     "iski_1_skiliftkarussell_winterberg_2025-11-30.gpx",
                     "iski_2_skiliftkarussell_winterberg_2025-11-30.gpx"]

    one = c.get(f"/batch/{tok}/1")
    assert one.status_code == 200
    assert "iski_2_" in one.headers["Content-Disposition"]
    xml.dom.minidom.parseString(one.data)
    assert c.get(f"/batch/{tok}/9").status_code == 404
    assert c.get("/batch/unbekannt/zip").status_code == 404


def test_batch_duplicate_names(bmod):
    c = bmod.app.test_client()
    r = _upload(c, "https://www.iski.app/track/view/1/ab\nhttps://www.iski.app/track/view/1/cd\n")
    names = _zip.ZipFile(_io.BytesIO(c.get(f"/batch/{_token(r.data)}/zip").data)).namelist()
    assert sorted(names) == ["iski_1_skiliftkarussell_winterberg_2025-11-30.gpx",
                             "iski_1_skiliftkarussell_winterberg_2025-11-30_2.gpx"]


def test_batch_limits(bmod):
    c = bmod.app.test_client()
    many = "\n".join(f"https://www.iski.app/track/view/{i}/ab" for i in range(6))
    assert _upload(c, many).status_code == 400
    assert _upload(c, "# nur Kommentar\n").status_code == 400
    assert c.post("/batch", data={}, content_type="multipart/form-data").status_code == 400
    big = "x" * (70 * 1024)
    assert _upload(c, big).status_code == 413


def test_batch_expired(bmod, monkeypatch):
    c = bmod.app.test_client()
    tok = _token(_upload(c, "https://www.iski.app/track/view/1/ab\n").data)
    monkeypatch.setattr(bmod, "RESULT_TTL", -1)
    assert c.get(f"/batch/{tok}/zip").status_code == 404


def test_batch_deadline(bmod, monkeypatch):
    monkeypatch.setattr(bmod, "BATCH_DEADLINE", -1)
    c = bmod.app.test_client()
    r = _upload(c, "https://www.iski.app/track/view/1/ab\n")
    assert "Zeitlimit".encode() in r.data
    assert b"0 von 1" in r.data
