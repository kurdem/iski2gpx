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
