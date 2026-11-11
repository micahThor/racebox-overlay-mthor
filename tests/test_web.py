"""The club web app's sign-in and file rules (no AWS needed)."""
import json
import time
import urllib.parse

import pytest

from rbo import web


def make_app(tmp_path, **over):
    cfg = {"data": str(tmp_path), "auth": "cognito", "public_url": "https://example.cloudfront.net",
           "cognito_domain": "rbo-test.auth.us-east-1.amazoncognito.com", "cognito_client_id": "client123",
           "cognito_pool_id": "us-east-1_abc", "region": "us-east-1", "idle_minutes": 0, "max_gb": 20}
    cfg.update(over)
    return web.App(cfg)


def cookies_from(set_cookies):
    return "; ".join(c.split(";")[0] for c in set_cookies)


def test_signed_cookie_round_trip(tmp_path):
    app = make_app(tmp_path)
    tok = app._sign({"sub": "a", "exp": time.time() + 60})
    assert app._unsign(tok)["sub"] == "a"
    raw, _, sig = tok.partition(".")
    assert app._unsign(raw + "." + sig[:-1] + ("0" if sig[-1] != "0" else "1")) is None   # tampered
    assert app._unsign(app._sign({"sub": "a", "exp": time.time() - 1})) is None             # expired
    assert make_app(tmp_path / "other")._unsign(tok) is None                                # another server's key


def test_not_signed_in(tmp_path):
    app = make_app(tmp_path)
    assert app.person_for("") is None
    assert app.person_for("rbo_session=junk") is None


def test_login_flow(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    where, set_cookies = app.login_redirect()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(where).query)
    assert where.startswith("https://rbo-test.auth.us-east-1.amazoncognito.com/oauth2/authorize?")
    assert q["redirect_uri"] == ["https://example.cloudfront.net/auth/callback"]
    assert q["code_challenge_method"] == ["S256"]
    assert "Secure" in set_cookies[0] and "HttpOnly" in set_cookies[0]

    sent = {}

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent.update(urllib.parse.parse_qs(req.data.decode()))
        return Resp(json.dumps({"id_token": "tok"}).encode())

    monkeypatch.setattr(web.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(app, "_verify", lambda t: {"sub": "user-1", "email": "rider@example.com"})

    with pytest.raises(web.AuthError):                       # wrong state
        app.finish_login({"code": ["c"], "state": ["nope"]}, cookies_from(set_cookies))
    where2, set2 = app.finish_login({"code": ["c"], "state": q["state"]}, cookies_from(set_cookies))
    assert where2 == "/"
    assert sent["code_verifier"][0] and sent["client_id"] == ["client123"]
    p = app.person_for(cookies_from(set2))
    assert p and p.email == "rider@example.com" and p.files.is_dir()


def test_real_token_check(tmp_path, monkeypatch):
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app = make_app(tmp_path)
    iss = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc"

    class Keys:
        def get_signing_key_from_jwt(self, t):
            return type("K", (), {"key": key.public_key()})()
    app._jwks = Keys()
    good = jwt.encode({"sub": "s", "aud": "client123", "iss": iss, "exp": time.time() + 60}, key, "RS256")
    assert app._verify(good)["sub"] == "s"
    for bad in ({"aud": "someone-else"}, {"iss": "https://evil"}, {"exp": time.time() - 60}):
        claims = {"sub": "s", "aud": "client123", "iss": iss, "exp": time.time() + 60, **bad}
        with pytest.raises(web.AuthError):
            app._verify(jwt.encode(claims, key, "RS256"))


def test_people_only_reach_their_own_files(tmp_path):
    app = make_app(tmp_path, auth="off")
    p = app.person_for("")
    (p.files / "a.mp4").write_bytes(b"x")
    assert p.path("a.mp4") == (p.files / "a.mp4").resolve()
    for bad in ("../../.cookie-key", "/etc/passwd", str(tmp_path / "users" / "someone" / "files" / "x.mp4")):
        with pytest.raises(ValueError):
            p.path(bad)


def test_clean_names():
    assert web._clean_name("../../my ride (1).mp4") == "my ride (1).mp4"
    assert web._clean_name("a;rm -rf.csv") == "a_rm -rf.csv"
    with pytest.raises(ValueError):
        web._clean_name(".hidden")


def test_polly_voice(monkeypatch):
    import numpy as np
    from rbo import crewchief
    asked = {}

    class Polly:
        def synthesize_speech(self, **kw):
            asked.update(kw)
            t = np.arange(16000) / 16000
            pcm = (np.sin(2 * np.pi * 300 * t) * 12000).astype("<i2").tobytes()
            return {"AudioStream": type("S", (), {"read": lambda self: pcm})()}

    monkeypatch.setattr(crewchief, "TTS", "polly")
    monkeypatch.setattr(crewchief, "_polly", Polly())
    assert "Brian" in crewchief.list_voices() and crewchief.default_voice() == "Brian"
    a = crewchief.synthesize("Box <now> & push", voice="Daniel", rate=190)
    assert asked["VoiceId"] == "Brian" and asked["Engine"] == "neural"
    assert "&lt;now&gt; &amp; push" in asked["Text"] and 'rate="109%"' in asked["Text"]
    assert abs(len(a) - crewchief.RATE) < crewchief.RATE * 0.1      # about 1 s at 48 kHz
