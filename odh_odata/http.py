"""HTTP plumbing: client (optionally through an SSM tunnel with the real SNI/Host), tokens, service discovery."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal

import requests
import urllib3
from requests.adapters import HTTPAdapter

from . import metadata


class BudgetExceeded(Exception):
    """Stop issuing requests (budget used up or token expired); the message says which."""


class TokenExpired(BudgetExceeded):
    pass


@dataclass
class Resp:
    url: str
    status: int | None
    ms: int | None
    headers: dict
    text: str = ""
    json: object = None
    error: str | None = None
    truncated: bool = False


class _SNIAdapter(HTTPAdapter):
    """Send the real host name in TLS SNI even though the socket goes to the tunnel (localhost)."""

    def __init__(self, sni, verify):
        self._sni, self._verify = sni, verify
        super().__init__(pool_maxsize=32)

    def init_poolmanager(self, *a, **kw):
        kw["server_hostname"] = self._sni
        if self._verify:
            kw["assert_hostname"] = self._sni
        super().init_poolmanager(*a, **kw)


class Client:
    def __init__(self, base_url, token=None, connect=None, verify=False, timeout=60, max_requests=5000, headers=None):
        u = urllib.parse.urlsplit(base_url)
        if not u.scheme or not u.hostname:
            raise ValueError(f"base URL must look like https://host, got {base_url!r}")
        self.host = u.hostname
        self.public = f"{u.scheme}://{u.netloc}"
        self.wire = f"{u.scheme}://{connect}" if connect else self.public
        self.s = requests.Session()
        self.s.verify = verify
        if not verify:
            urllib3.disable_warnings()
        if connect:
            self.s.headers["Host"] = u.netloc
            self.s.mount(self.wire, _SNIAdapter(self.host, verify))
        else:
            self.s.mount(self.wire, HTTPAdapter(pool_maxsize=32))
        self.token, self.timeout, self.max_requests = token, timeout, max_requests
        self.connect = connect
        self.extra = dict(headers or {})  # user headers sent on every request
        self.exp = token_claims(token).get("exp") if token else None
        self.count = 0
        self.stop_on_401 = False  # armed by the CLI once discovery is done; discovery/diagnosis may see 401s legitimately
        self.rejected = False
        self._lock = threading.Lock()

    def _swap(self, url, base):
        if url.startswith(("http://", "https://")):
            p = urllib.parse.urlsplit(url)
            return base + p.path + (f"?{p.query}" if p.query else "")
        return base + "/" + url.lstrip("/")

    def get(self, url, headers=None, auth=True, token=None, max_bytes=20_000_000):
        """GET a path (relative to host) or absolute URL. token overrides the bearer; auth=False sends none."""
        with self._lock:
            self.count += 1
            if self.count > self.max_requests:
                raise BudgetExceeded("request budget exhausted (--max-requests)")
        if auth and not token and self.exp and time.time() > self.exp - 15:
            raise TokenExpired("bearer token expired during the run - remaining tests skipped")
        if auth and not token and self.rejected:
            raise TokenExpired("bearer token rejected with 401 (revoked or wrong audience) - remaining tests skipped")
        h = {"Accept": "application/json", **self.extra}
        if auth and (token or self.token):
            h["Authorization"] = f"Bearer {token or self.token}"
        h.update(headers or {})
        public = self._swap(url, self.public)
        t0 = time.perf_counter()
        try:
            r = self.s.get(self._swap(url, self.wire), headers=h, timeout=self.timeout, stream=True, allow_redirects=False)
            chunks, size, truncated = [], 0, False
            for c in r.iter_content(65536):
                size += len(c)
                if size > max_bytes:
                    truncated = True
                    break
                chunks.append(c)
            r.close()
            if r.status_code == 401 and auth and not token and self.stop_on_401:
                self.rejected = True  # 401 is about the credential, not the resource: every later request would repeat it
            ms = int((time.perf_counter() - t0) * 1000)
            text = b"".join(chunks).decode(r.encoding or "utf-8", "replace")
            js = None
            if not truncated and "json" in r.headers.get("Content-Type", ""):
                try:
                    js = json.loads(text, parse_float=Decimal)  # keep exact decimal digits for literals
                except ValueError:
                    pass
            return Resp(public, r.status_code, ms, dict(r.headers), text, js, None, truncated)
        except requests.RequestException as e:
            return Resp(public, None, int((time.perf_counter() - t0) * 1000), {}, error=str(e)[:300])


# ---------- tokens / env ----------

def load_dotenv(path=".env"):
    """KEY=VALUE lines into os.environ (existing environment wins)."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def clean_token(t):
    t = (t or "").strip()
    return t[7:].strip() if t.lower().startswith("bearer ") else t


def token_claims(t):
    try:
        part = t.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


# ---------- SSM tunnel ----------

class SsmTunnel:
    """aws ssm start-session port forwarding: localhost:<local_port> -> <remote_host>:<remote_port> via an instance."""

    def __init__(self, target, remote_host, profile=None, region=None, local_port=8443, remote_port=443, log=print):
        self.target, self.remote_host, self.profile, self.region = target, remote_host, profile, region
        self.local_port, self.remote_port, self.log = local_port, remote_port, log
        self.proc, self.output = None, []

    def _listening(self):
        try:
            socket.create_connection(("127.0.0.1", self.local_port), timeout=1).close()
            return True
        except OSError:
            return False

    def __enter__(self):
        if self._listening():
            self.log(f"tunnel: localhost:{self.local_port} already listening, reusing it")
            return self
        aws = shutil.which("aws")
        if not aws:
            raise RuntimeError("AWS CLI not found on PATH (needed for the SSM tunnel)")
        cmd = [aws, "ssm", "start-session", "--target", self.target,
               "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
               "--parameters", f"host={self.remote_host},portNumber={self.remote_port},localPortNumber={self.local_port}"]
        if self.profile:
            cmd += ["--profile", self.profile]
        if self.region:
            cmd += ["--region", self.region]
        self.log(f"tunnel: starting SSM port forward via {self.target} -> {self.remote_host}:{self.remote_port}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        threading.Thread(target=lambda: self.output.extend(self.proc.stdout), daemon=True).start()
        deadline = time.time() + 60
        while time.time() < deadline:
            if any("Waiting for connections" in line for line in self.output):
                self.log(f"tunnel: ready on localhost:{self.local_port}")
                return self
            if self.proc.poll() is not None:
                time.sleep(0.2)
                raise RuntimeError("SSM session failed:\n" + "".join(self.output).strip())
            time.sleep(0.2)
        self.__exit__()
        raise RuntimeError("SSM tunnel did not become ready within 60s:\n" + "".join(self.output).strip())

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":  # aws.exe spawns session-manager-plugin.exe; kill the whole tree
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
            else:
                self.proc.terminate()
            self.log("tunnel: closed")


# ---------- discovery ----------

PREFIXES = ["api/v1.0/odata", "api/v1/odata", "api/v2.0/odata", "api/v2/odata", "api/odata", "odata",
            "api/v1.0", "api/v1", "api/v2.0", "api", ""]


def _variants(seg):
    out = [seg]
    if seg.endswith("ies"):
        out.append(seg[:-3] + "y")
    elif seg.endswith("s"):
        out.append(seg[:-1])
    else:
        out += [seg + "s"] + ([seg[:-1] + "ies"] if seg.endswith("y") else [])
    return out


def candidates(hint):
    """(service_root, remaining_segments) guesses for a URL path or a bare name, most specific first."""
    segs = [s for s in urllib.parse.unquote(hint).strip("/").split("/") if s]
    out, seen = [], set()

    def add(root, rest):
        k = (root.lower(), tuple(rest))
        if k not in seen:
            seen.add(k)
            out.append((root, list(rest)))

    if len(segs) == 1:  # bare service name ("AircraftSensor") or entity set name
        for p in PREFIXES:
            for v in _variants(segs[0]):
                add(f"{p}/{v}".strip("/"), [])
        for p in PREFIXES:
            add(p, segs)
        return out
    for i in range(len(segs), -1, -1):
        pre, rest = segs[:i], segs[i:]
        if pre:
            for v in _variants(pre[-1])[1:]:  # "odata/Vehicles" may mean service "Vehicle" + entity set "Vehicles"
                add("/".join(pre[:-1] + [v]), [pre[-1]] + rest)
        add("/".join(pre), rest)
        if pre:
            for v in _variants(pre[-1])[1:]:
                add("/".join(pre[:-1] + [v]), rest)
    return out


def _fetch_metadata(client, root, tried):
    r = client.get(f"{root}/$metadata" if root else "$metadata", headers={"Accept": "application/xml"})
    tried.append(f"/{root}/$metadata -> {r.status or r.error}")
    return r.text if r.status == 200 and "Edmx" in r.text[:4000] else None


def probe(client, hint, query, tried, infer=False):
    """GET the user's exact URL. Its @odata.context names the metadata document and entity set precisely;
    without one, a JSON collection still lets us infer a schema. Returns (root, xml, model, set) or None."""
    path = hint.strip("/")
    if not path:
        return None
    r = client.get(path + (f"?{query}" if query else ""))
    tried.append(f"/{path}{'?' + query if query else ''} -> {r.status or r.error}")
    j = r.json if r.status == 200 else None
    if not isinstance(j, dict):
        return None
    ctx = j.get("@odata.context")
    rows = j["value"] if isinstance(j.get("value"), list) else None
    root, set_name = path.rsplit("/", 1) if "/" in path else ("", path)
    if isinstance(ctx, str) and "$metadata" in ctx and not infer:
        meta_url = urllib.parse.urljoin(client.public + "/" + path, ctx.split("#")[0])
        root = urllib.parse.urlsplit(meta_url).path.strip("/").removesuffix("$metadata").strip("/")
        frag = ctx.split("#", 1)[1] if "#" in ctx else ""
        set_name = re.split(r"[/(]", frag)[0] or None
        xml = _fetch_metadata(client, root, tried)
        if xml:
            model = metadata.parse(xml)
            return root, xml, model, set_name if set_name in model.sets else None
        if not set_name:  # a service document, not entity data: nothing to infer from
            return None
    if rows and all(isinstance(x, dict) for x in rows):  # no usable $metadata: learn the schema from the data
        xml = metadata.infer_xml(set_name, rows)
        model = metadata.infer_model(set_name, rows)
        tried.append(f"no $metadata: schema inferred from {len(rows)} rows of {set_name}")
        return root, xml, model, set_name
    return None


def discover(client, hint, log=print, query="", infer=False):
    """Find the service root for hint. Returns (root, xml, model, entity_set_or_None, tried)."""
    tried, cache, fallback = [], {}, None
    hit = probe(client, hint, query, tried, infer)
    if infer and not hit:
        raise LookupError("--no-metadata: the target URL did not return a JSON collection to infer a schema from")
    if hit:
        return (*hit, tried)
    for root, rest in candidates(hint):
        if root.lower() not in cache:
            cache[root.lower()] = _fetch_metadata(client, root, tried)
        xml = cache[root.lower()]
        if not xml:
            continue
        model = metadata.parse(xml)
        names = {n.lower(): n for n in model.sets}
        head = rest[0].split("(")[0].lower() if rest else None
        if not rest:
            return root, xml, model, None, tried
        if head in names and len(rest) == 1:
            return root, xml, model, names[head], tried
        # Never fall back to a different service: testing the wrong API silently is worse than stopping.
        fallback = fallback or f"/{root} exists ({', '.join(model.sets)}) but has no '{'/'.join(rest)}'"
    msg = f"No OData service matches '{hint}'." + (f"\nNearest service found: {fallback}" if fallback else "")
    raise LookupError(msg + "\nTried:\n  " + "\n  ".join(tried))
