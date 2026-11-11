"""The editor as a web app for a club: `racebox-overlay serve`.

Same editor and renderer as `racebox-overlay gui`, with what a shared server needs:

* sign-in through Amazon Cognito (the people you add can sign in; nobody else);
* everyone has their own folder of videos, RaceBox files and saved layouts,
  and their own open video;
* files are uploaded from the browser in pieces (a big video can carry on
  after a dropped connection) instead of chosen from this computer;
* renders take turns, and a finished video goes to S3 to download;
* the server can stop itself when nobody has used it for a while, to save money.

Settings come from environment variables, or from a JSON parameter in AWS SSM
(RBO_CONFIG_PARAM, which is how the AWS setup in infra/ passes them in):

    RBO_DATA          folder for everyone's files (default ./rbo-data)
    RBO_PUBLIC_URL    the address people use, e.g. https://d1234.cloudfront.net
    RBO_AUTH          "cognito" (default when RBO_COGNITO_* are set) or "off" (testing only)
    RBO_COGNITO_DOMAIN, RBO_COGNITO_CLIENT_ID, RBO_COGNITO_POOL_ID, RBO_REGION
    RBO_BUCKET        S3 bucket for rendered videos (optional: without it they download from here)
    RBO_IDLE_MINUTES  stop this EC2 instance after this long unused (0 = never)
    RBO_MAX_GB        largest upload, in GB (default 20)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import gui, project
from .themes import THEMES

CHUNK = 8 * 1024 * 1024          # upload piece size (CloudFront is happy with these)
SESSION_DAYS = 14
EXT = {"video": {".mp4", ".mov", ".m4v", ".insv"}, "ghost_video": {".mp4", ".mov", ".m4v", ".insv"},
       "session": {".csv", ".vbo"}, "ghost_session": {".csv", ".vbo"},
       "logo": {".png", ".jpg", ".jpeg", ".webp"}}


# ---------------------------------------------------------------- settings

def load_config() -> dict:
    cfg = {}
    param = os.environ.get("RBO_CONFIG_PARAM")
    if param:                                   # the AWS setup keeps them in SSM
        import boto3
        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION") or os.environ.get("RBO_REGION"))
        cfg.update(json.loads(ssm.get_parameter(Name=param)["Parameter"]["Value"]))
    env = {k[4:].lower(): v for k, v in os.environ.items() if k.startswith("RBO_") and k != "RBO_CONFIG_PARAM"}
    cfg.update(env)
    cfg.setdefault("data", "rbo-data")
    cfg.setdefault("auth", "cognito" if cfg.get("cognito_client_id") else "off")
    cfg["idle_minutes"] = float(cfg.get("idle_minutes") or 0)
    cfg["max_gb"] = float(cfg.get("max_gb") or 20)
    cfg["public_url"] = str(cfg.get("public_url") or "").rstrip("/")
    return cfg


# ---------------------------------------------------------------- people

class Person:
    """Someone signed in: their folder, and the video they have open."""

    def __init__(self, root: Path, sub: str, email: str):
        self.sub, self.email = sub, email
        self.root = root / "users" / re.sub(r"[^A-Za-z0-9_-]", "_", sub)
        self.files = self.root / "files"
        self.layouts = self.root / "layouts"
        self.outputs = self.root / "outputs"
        for d in (self.files, self.layouts, self.outputs):
            d.mkdir(parents=True, exist_ok=True)
        self.studio: gui.Studio | None = None
        self.lock = threading.Lock()

    def path(self, name: str) -> Path:
        """A file of theirs, from its name or a full path inside their folder."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("no file chosen")
        p = Path(name)
        p = (p if p.is_absolute() else self.files / p).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError("that file isn't one of yours; upload it with Choose…")
        return p


def _clean_name(name: str) -> str:
    name = Path(str(name)).name.strip().replace("\x00", "")
    name = re.sub(r"[^\w.\- ()+]", "_", name)
    if not name or name.startswith("."):
        raise ValueError("that file name won't do; rename the file and try again")
    return name[:120]


# ---------------------------------------------------------------- the server

class WebHandler(gui.Handler):
    app: "App"                      # set by serve()
    person: Person | None = None

    # -- gui.Handler hooks: one open video per person, their own files only

    @property
    def studio(self):
        return self.person.studio if self.person else None

    def set_studio(self, st):
        old = self.person.studio
        self.person.studio = st
        st.after_render = self.app.after_render_for(self.person)
        if old is not None and old is not st:
            old._huds.clear()

    def recent_list(self):
        out = []
        for f in sorted(self.person.files.iterdir(), key=lambda f: -f.stat().st_mtime):
            if f.suffix.lower() not in EXT["video"] or not f.is_file():
                continue
            proj = project.load(f) if project.path_for(f).exists() else {}
            sess = proj.get("session")
            gb = f.stat().st_size / 1e9
            when = time.strftime("%b %d, %Y", time.localtime(f.stat().st_mtime))
            out.append({"video": f.name, "name": f.name,
                        "folder": f"{gb:.1f} GB · uploaded {when}" if gb >= 0.1 else f"uploaded {when}",
                        "session": Path(sess).name if sess and Path(sess).exists() else None})
        return out[:30]

    def open_studio(self, video_path, session_path):
        if self.studio and self.studio.render_state.get("state") == "running":
            raise ValueError("wait for your render to finish (or cancel it) before opening another video")
        v = self.person.path(video_path)
        s = str(self.person.path(session_path)) if session_path else None
        if s is None:
            saved = project.load(v).get("session") if project.path_for(v).exists() else None
            if saved:
                s = str(self.person.path(saved))
        return gui.open_files(str(v), s)

    def peek(self, video_path):
        v = self.person.path(video_path)
        sess = project.load(v).get("session") if project.path_for(v).exists() else None
        return {"session": Path(sess).name if sess and Path(sess).exists() else None}

    def check_path(self, path):
        return str(self.person.path(path))

    def check_project(self, changes):
        """Paths in the settings must be the person's own uploaded files."""
        ch = dict(changes)
        if isinstance(ch.get("theme"), str) and ch["theme"].lower() not in THEMES:
            raise ValueError("unknown theme")
        if ch.get("ghost_source"):
            g = dict(ch["ghost_source"])
            for k in ("session", "video"):
                if g.get(k):
                    g[k] = str(self.person.path(g[k]))
            ch["ghost_source"] = g
        if ch.get("info"):
            info = dict(ch["info"])
            for k in ("event_logo", "track_logo"):
                if info.get(k):
                    info[k] = str(self.person.path(info[k]))
            ch["info"] = info
        for k in ("race_source", "heat_source", "qual_source"):
            v = ch.get(k)
            if v and not re.match(r"^\s*(https?://|\d+\s*$)", str(v)):
                ch[k] = str(self.person.path(v))
        return ch

    # -- requests

    def _begin(self) -> bool:
        """Who's asking; False (and an answer sent) if they need to sign in."""
        project.preset_dir_var.set(None)
        self.person = self.app.person_for(self.headers.get("Cookie", ""))
        if self.person is None:
            path = urlparse(self.path).path
            if path.startswith(("/api/", "/video", "/ghost-video")):
                self._json({"error": "sign in again"}, HTTPStatus.UNAUTHORIZED)
            else:
                self._redirect("/login")
            return False
        project.preset_dir_var.set(self.person.layouts)
        self.app.touch()
        self.page_config = json.dumps({"email": self.person.email})
        return True

    def _redirect(self, where: str, cookies: list[str] = ()):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", where)
        self.send_header("Cache-Control", "no-store")
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/healthz":
                self._text("ok")
            elif u.path == "/login":
                self._redirect(*self.app.login_redirect())
            elif u.path == "/auth/callback":
                self._redirect(*self.app.finish_login(q, self.headers.get("Cookie", "")))
            elif u.path == "/logout":
                self._redirect(*self.app.logout_redirect())
            elif not self._begin():
                return
            elif u.path == "/api/download":
                self._download()
            else:
                super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except AuthError as e:
            self._text(f"Couldn't sign you in: {e}\n\nGo back to the editor's address to try again.",
                       HTTPStatus.FORBIDDEN)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if not self._begin():
                return
            if u.path == "/api/pick":
                self._json({"error": "use Choose… to upload the file"})
            elif u.path == "/api/upload/start":
                self._json(self._upload_start(self._body()))
            elif u.path == "/api/files/delete":
                self._json(self._delete(self._body()))
            else:
                with self.person.lock:          # one change at a time per person
                    super().do_POST()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    def do_PUT(self):
        u = urlparse(self.path)
        try:
            if not self._begin():
                return
            if u.path == "/api/upload":
                q = parse_qs(u.query)
                self._json(self._upload_piece(q.get("name", [""])[0], int(q.get("offset", ["0"])[0])))
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    def _text(self, text: str, status=HTTPStatus.OK):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- uploads: in pieces, resumable

    def _upload_start(self, b: dict) -> dict:
        kind = str(b.get("kind") or "video")
        name = _clean_name(b.get("name", ""))
        size = int(b.get("size") or 0)
        if Path(name).suffix.lower() not in EXT.get(kind, set()):
            want = ", ".join(sorted(EXT.get(kind, ())))
            raise ValueError(f"{name} isn't the right kind of file here (use {want})")
        if size <= 0:
            raise ValueError(f"{name} is empty")
        if size > self.app.cfg["max_gb"] * 1e9:
            raise ValueError(f"{name} is bigger than the {self.app.cfg['max_gb']:.0f} GB limit")
        free = shutil.disk_usage(self.person.files).free
        dest = self.person.files / name
        if dest.exists() and dest.stat().st_size == size:     # already here: no need to send it again
            return {"name": name, "offset": size, "done": True, "chunk": CHUNK}
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if size - have > free - 2e9:
            raise ValueError("the server is nearly out of space; delete some old videos first")
        meta = part.with_name(part.name + ".json")
        meta.write_text(json.dumps({"size": size, "kind": kind}))
        if not part.exists():
            part.touch()
        return {"name": name, "offset": have, "done": False, "chunk": CHUNK}

    def _upload_piece(self, name: str, offset: int) -> dict:
        name = _clean_name(name)
        dest = self.person.files / name
        part = dest.with_name(dest.name + ".part")
        meta = part.with_name(part.name + ".json")
        if not part.exists() or not meta.exists():
            raise ValueError("that upload wasn't started; choose the file again")
        size = json.loads(meta.read_text())["size"]
        n = int(self.headers.get("Content-Length") or 0)
        have = part.stat().st_size
        if n > CHUNK * 2:
            raise ValueError("upload piece too big")
        data = self.rfile.read(n) if n else b""
        if offset == have and data:                   # anything else: say where it got to
            with open(part, "ab") as f:
                f.write(data[: size - have])
            have = part.stat().st_size
        done = have >= size
        if done:
            os.replace(part, dest)
            meta.unlink(missing_ok=True)
            print(f"uploaded {name} ({size / 1e6:.0f} MB) for {self.person.email}", flush=True)
        return {"name": name, "offset": have, "done": done, "chunk": CHUNK}

    def _delete(self, b: dict) -> dict:
        v = self.person.path(b.get("video", ""))
        if self.studio and Path(self.studio.video_path) == v:
            if self.studio.render_state.get("state") == "running":
                raise ValueError("that video is rendering; cancel it first")
            self.person.studio = None
        proj = project.load(v) if project.path_for(v).exists() else {}
        for f in [v] + list(v.parent.glob(v.name + ".*")) + list(self.person.outputs.glob(v.stem + "_overlay*")):
            if f.is_file() and f.resolve().is_relative_to(self.person.root.resolve()):
                f.unlink()
        sess = proj.get("session")            # its RaceBox file too, if no other video uses it
        if sess and Path(sess).exists():
            others = [project.load(o).get("session") for o in self.person.files.glob("*.overlay.json")]
            if sess not in others:
                Path(sess).unlink(missing_ok=True)
        self.app.delete_outputs(self.person, v.stem)
        return {"ok": True}

    # -- downloads

    def _download(self):
        st = self.studio
        rs = st.render_state if st else {}
        if rs.get("state") != "done":
            self.send_error(404, "nothing rendered yet")
            return
        if rs.get("s3_key"):
            self._redirect(self.app.download_url(rs["s3_key"], rs["name"]))
            return
        self._send_video(rs["out"], attachment=rs["name"])


class AuthError(Exception):
    pass


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = Path(cfg["data"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        key = self.root / ".cookie-key"
        if not key.exists():
            key.write_bytes(secrets.token_bytes(32))
            key.chmod(0o600)
        self.key = key.read_bytes()
        self.people: dict[str, Person] = {}
        self.lock = threading.Lock()
        self.last_used = time.time()
        self._jwks = None
        self._s3 = None

    # -- signing in (Cognito's sign-in page, then our own cookie)

    def _sign(self, data: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()[:43]
        return f"{raw}.{sig}"

    def _unsign(self, token: str) -> dict | None:
        raw, _, sig = token.partition(".")
        good = hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()[:43]
        if not sig or not hmac.compare_digest(sig, good):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except ValueError:
            return None
        return data if data.get("exp", 0) > time.time() else None

    @staticmethod
    def _cookie(header: str, name: str) -> str:
        for part in header.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _set_cookie(self, name: str, value: str, max_age: int) -> str:
        secure = "; Secure" if self.cfg["public_url"].startswith("https://") else ""
        return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

    def person_for(self, cookie_header: str) -> Person | None:
        if self.cfg["auth"] == "off":
            who = {"sub": "local", "email": "you (sign-in is off)"}
        else:
            who = self._unsign(self._cookie(cookie_header, "rbo_session"))
            if not who:
                return None
        with self.lock:
            p = self.people.get(who["sub"])
            if p is None:
                p = self.people[who["sub"]] = Person(self.root, who["sub"], who.get("email", ""))
        return p

    def _cognito(self, path: str) -> str:
        dom = self.cfg["cognito_domain"]
        dom = dom if dom.startswith("https://") else f"https://{dom}"
        return dom.rstrip("/") + path

    def _redirect_uri(self) -> str:
        return self.cfg["public_url"] + "/auth/callback"

    def login_redirect(self):
        if self.cfg["auth"] == "off":
            return ("/", [])
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        q = urllib.parse.urlencode({
            "client_id": self.cfg["cognito_client_id"], "response_type": "code",
            "scope": "openid email", "redirect_uri": self._redirect_uri(), "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        pending = self._sign({"v": verifier, "s": state, "exp": time.time() + 900})
        return (self._cognito("/oauth2/authorize?" + q), [self._set_cookie("rbo_login", pending, 900)])

    def finish_login(self, q: dict, cookie_header: str):
        if self.cfg["auth"] == "off":
            return ("/", [])
        if q.get("error"):
            raise AuthError(q.get("error_description", q["error"])[0])
        pending = self._unsign(self._cookie(cookie_header, "rbo_login"))
        if not pending or q.get("state", [""])[0] != pending["s"]:
            raise AuthError("the sign-in took too long or came from somewhere else")
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code", "client_id": self.cfg["cognito_client_id"],
            "code": q.get("code", [""])[0], "redirect_uri": self._redirect_uri(),
            "code_verifier": pending["v"]}).encode()
        req = urllib.request.Request(self._cognito("/oauth2/token"), data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                tokens = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise AuthError(f"Cognito said {e.code}: {e.read()[:200]!r}") from None
        claims = self._verify(tokens["id_token"])
        who = {"sub": claims["sub"], "email": claims.get("email", ""),
               "exp": time.time() + SESSION_DAYS * 86400}
        print(f"signed in: {who['email']}", flush=True)
        return ("/", [self._set_cookie("rbo_session", self._sign(who), SESSION_DAYS * 86400),
                      self._set_cookie("rbo_login", "", 0)])

    def _verify(self, id_token: str) -> dict:
        import jwt                                   # PyJWT (pip install "pyjwt[crypto]")
        region, pool = self.cfg["region"], self.cfg["cognito_pool_id"]
        issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool}"
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(issuer + "/.well-known/jwks.json")
        key = self._jwks.get_signing_key_from_jwt(id_token).key
        try:
            return jwt.decode(id_token, key, algorithms=["RS256"], audience=self.cfg["cognito_client_id"],
                              issuer=issuer, options={"require": ["exp", "sub", "aud", "iss"]})
        except jwt.PyJWTError as e:
            raise AuthError(f"the sign-in token didn't check out ({e})") from None

    def logout_redirect(self):
        gone = [self._set_cookie("rbo_session", "", 0)]
        if self.cfg["auth"] == "off":
            return ("/", gone)
        q = urllib.parse.urlencode({"client_id": self.cfg["cognito_client_id"],
                                    "logout_uri": self.cfg["public_url"] + "/"})
        return (self._cognito("/logout?" + q), gone)

    # -- rendered videos: kept in S3 when there's a bucket

    def s3(self):
        if self._s3 is None:
            import boto3
            from botocore.config import Config
            self._s3 = boto3.client("s3", region_name=self.cfg.get("region"),
                                    config=Config(signature_version="s3v4"))
        return self._s3

    def after_render_for(self, person: Person):
        def done(out: str) -> dict:
            src = Path(out)
            final = person.outputs / src.name             # out of the uploads, into their outputs
            if src.resolve() != final.resolve():
                shutil.move(str(src), final)
            info = {"out": str(final), "name": final.name}
            if self.cfg.get("bucket"):
                key = f"outputs/{person.sub}/{final.name}"
                self.s3().upload_file(str(final), self.cfg["bucket"], key,
                                      ExtraArgs={"ContentType": "video/mp4"})
                final.unlink()                            # S3 keeps it; no need to fill the disk
                info["s3_key"] = key
            print(f"rendered {final.name} for {person.email}", flush=True)
            return info
        return done

    def download_url(self, key: str, name: str) -> str:
        return self.s3().generate_presigned_url(
            "get_object", ExpiresIn=3600,
            Params={"Bucket": self.cfg["bucket"], "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{name}"'})

    def delete_outputs(self, person: Person, stem: str) -> None:
        if not self.cfg.get("bucket"):
            return
        try:
            r = self.s3().list_objects_v2(Bucket=self.cfg["bucket"], Prefix=f"outputs/{person.sub}/{stem}_overlay")
            for o in r.get("Contents", []):
                self.s3().delete_object(Bucket=self.cfg["bucket"], Key=o["Key"])
        except Exception as e:  # noqa: BLE001 - the bucket's lifecycle rule clears them anyway
            print(f"couldn't delete old renders of {stem}: {e}", flush=True)

    # -- stopping when nobody's using it

    def touch(self) -> None:
        self.last_used = time.time()

    def busy(self) -> bool:
        for p in list(self.people.values()):
            st = p.studio
            if st and (st.render_state.get("state") == "running" or st.autosync_state.get("state") == "running"):
                return True
        return False

    def idle_watch(self) -> None:
        mins = self.cfg["idle_minutes"]
        if not mins:
            return

        def loop():
            while True:
                time.sleep(60)
                if self.busy():
                    self.touch()
                elif time.time() - self.last_used > mins * 60:
                    print(f"nobody has used the editor for {mins:.0f} min: stopping this server "
                          "(opening the editor's address starts it again)", flush=True)
                    try:
                        stop_this_instance(self.cfg.get("region"))
                    except Exception as e:  # noqa: BLE001
                        print(f"couldn't stop the server: {e}", flush=True)
                    self.touch()
        threading.Thread(target=loop, daemon=True).start()


def stop_this_instance(region: str | None) -> None:
    """Stop the EC2 instance this runs on (its data volume stays)."""
    import boto3
    tok = urllib.request.urlopen(urllib.request.Request(
        "http://169.254.169.254/latest/api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=2).read().decode()
    iid = urllib.request.urlopen(urllib.request.Request(
        "http://169.254.169.254/latest/meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": tok}), timeout=2).read().decode()
    boto3.client("ec2", region_name=region).stop_instances(InstanceIds=[iid])


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    cfg = load_config()
    if cfg["auth"] != "off":
        missing = [k for k in ("cognito_domain", "cognito_client_id", "cognito_pool_id", "region", "public_url")
                   if not cfg.get(k)]
        if missing:
            sys.exit("sign-in needs these settings: " + ", ".join("RBO_" + k.upper() for k in missing)
                     + " (or RBO_AUTH=off to test without signing in)")
    elif host not in ("127.0.0.1", "localhost"):
        print("WARNING: sign-in is off (RBO_AUTH=off): anyone who can reach this server can use it", flush=True)
    app = App(cfg)
    gui.RENDER_SLOT = threading.Semaphore(1)
    WebHandler.app = app
    app.idle_watch()
    httpd = ThreadingHTTPServer((host, port), WebHandler)
    httpd.daemon_threads = True
    print(f"club editor on http://{host}:{port}/ (public address {cfg['public_url'] or 'not set'}), "
          f"files in {app.root}, sign-in {cfg['auth']}, renders to "
          f"{'s3://' + cfg['bucket'] if cfg.get('bucket') else 'this server'}"
          + (f", stops after {cfg['idle_minutes']:.0f} min unused" if cfg["idle_minutes"] else ""), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
