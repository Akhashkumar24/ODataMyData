"""CLI: python -m odh_odata <url | path | service-name> [options]   (see README.md)"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from . import metadata, report
from .http import Client, SsmTunnel, clean_token, discover, load_dotenv, token_claims
from .suite import FAIL, Runner


def env(name, default=None):
    return os.environ.get(name, default)


def build_parser():
    ap = argparse.ArgumentParser(prog="python -m odh_odata", description="Metadata-driven OData API test framework")
    ap.add_argument("target", nargs="?", default=env("ODATA_URL"),
                    help="full URL (https://host/api/v1.0/odata/Vehicle/Vehicles), a path, or a service name (AircraftSensor)")
    ap.add_argument("--base", default=env("ODATA_BASE_URL"), help="https://host, needed when target is a path or name")
    ap.add_argument("--token", default=env("ODATA_TOKEN"), help="bearer token (default: ODATA_TOKEN from env or .env)")
    ap.add_argument("-H", "--header", action="append", default=[], metavar="NAME: VALUE",
                    help="extra header for every request (repeatable), e.g. -H 'X-Correlation-ID: abc'")
    ap.add_argument("--only", nargs="*", help="entity sets to test (default: all, or the one named in the URL)")
    g = ap.add_argument_group("network")
    g.add_argument("--connect", default=env("ODATA_CONNECT"), help="host:port carrying the traffic, e.g. localhost:8443")
    g.add_argument("--ssm-target", default=env("ODATA_SSM_TARGET"), help="EC2 instance id: open an SSM tunnel automatically")
    g.add_argument("--ssm-profile", default=env("ODATA_SSM_PROFILE"))
    g.add_argument("--ssm-region", default=env("ODATA_SSM_REGION"))
    g.add_argument("--local-port", type=int, default=int(env("ODATA_LOCAL_PORT", "8443")))
    g.add_argument("--verify", action="store_true", help="verify TLS certificates")
    g.add_argument("--timeout", type=int, default=60)
    t = ap.add_argument_group("test generation")
    t.add_argument("--sample", type=int, default=20, help="rows sampled per set to drive data tests")
    t.add_argument("--depth", type=int, default=2, help="crawl depth through navigation links (0 = off)")
    t.add_argument("--width", type=int, default=3, help="related entities followed per navigation while crawling")
    t.add_argument("--roots", type=int, default=2, help="sampled rows per set used as crawl roots")
    t.add_argument("--key-rows", type=int, default=3, help="sampled rows fetched by key")
    t.add_argument("--thorough", action="store_true",
                   help="exhaustive: every property, every function, every optional URL form (default: focused on high-value tests)")
    t.add_argument("--no-negative", dest="negative", action="store_false", help="skip negative and security tests")
    t.add_argument("--workers", type=int, default=4, help="entity sets tested in parallel")
    t.add_argument("--max-requests", type=int, default=5000, help="hard request budget")
    t.add_argument("--slow-ms", type=int, default=5000, help="WARN above this response time")
    o = ap.add_argument_group("output")
    o.add_argument("--out", default="runs", help="output folder")
    o.add_argument("--plan", action="store_true", help="only parse metadata and print the model (no tests)")
    o.add_argument("--metadata-file", help="offline: parse this $metadata file (implies --plan)")
    o.add_argument("--no-capture", dest="capture", action="store_false", help="do not save each response under responses/")
    o.add_argument("--no-metadata", action="store_true", help="ignore $metadata and infer the schema from the target's data")
    o.add_argument("-v", "--verbose", action="store_true", help="print every test, not only WARN/FAIL")
    return ap


def print_plan(model, root=None):
    s = model.summary()
    print(f"\nOData {s['version']} service{(' /' + root) if root else ''} - {len(s['entity_sets'])} entity sets, "
          f"{len(s['operations'])} operations, {len(s['problems'])} metadata problems")
    for name, e in s["entity_sets"].items():
        print(f"\n  {name}{' (singleton)' if e['singleton'] else ''}: {e['type']}  key={e['keys']}")
        print(f"    properties ({len(e['properties'])}): " + ", ".join(f"{k}:{v.replace('Edm.', '')}" for k, v in e["properties"].items()))
        for n, x in e["navigation"].items():
            fk = f"  FK {x['fk_kind']}: {', '.join(x['fk'])}" if x["fk"] else "  FK unknown"
            print(f"    -> {n} ({'many' if x['collection'] else 'one'} {x['type']}) => {x['target_set'] or 'unbound'}{fk}")
        restricted = {k: v for k, v in e["capabilities"].items() if v not in (True, [], False) or k in ("countable", "filterable") and v is False}
        if restricted:
            print(f"    capabilities: {restricted}")
    for op in s["operations"]:
        print(f"  {op['kind']} {op['name']}({', '.join(op['params'])}) -> {op['returns']}{' [bound]' if op['bound'] else ''}")
    for p in s["problems"]:
        print(f"  PROBLEM: {p}")


def diagnose(client, token, hint):
    """Tell 'route does not exist' apart from 'not authorised' by asking the target without a token."""
    c = token_claims(token)
    anon = client.get(hint, auth=False)
    authed = client.get(hint)
    print("\ndiagnosis:")
    print(f"  {hint}: with token -> {authed.status}, without token -> {anon.status}"
          f"  (server: {authed.headers.get('Server', '?')})")
    if anon.status in (401, 403):
        print("  the route exists (it demands auth) but your token is refused: check the app roles below.")
        print(f"  token audience: {c.get('aud')}  roles: {len(c.get('roles') or [])}")
    elif anon.status == 404 and authed.status == 404:
        print("  404 with and without a token: the app has no such route. The API is not deployed on this host,")
        print("  or the path is different (check version segment, 'odata/' prefix, service name).")
    elif authed.status and authed.status >= 500:
        print("  the server errors on this URL; capture the response and raise it with the API team.")


def main(argv=None):
    load_dotenv()
    a = build_parser().parse_args(argv)
    if a.metadata_file:
        print_plan(metadata.parse(Path(a.metadata_file).read_bytes()))
        return 0
    if not a.target:
        sys.exit("give a target: full URL, path or service name (or set ODATA_URL)")
    if a.target.startswith(("http://", "https://")):
        u = urllib.parse.urlsplit(a.target)
        base, hint, query = f"{u.scheme}://{u.netloc}", u.path, u.query
    else:
        if not a.base:
            sys.exit("target is not a full URL: pass --base https://host (or set ODATA_BASE_URL)")
        hint, _, query = a.target.partition("?")
        base = a.base.rstrip("/")
    token = clean_token(a.token)
    if not token:
        sys.exit("no bearer token: set ODATA_TOKEN in the environment or .env, or pass --token")
    exp = token_claims(token).get("exp")
    if exp and exp < time.time():
        sys.exit(f"bearer token expired at {datetime.fromtimestamp(exp)} - get a fresh one")
    if exp and exp - time.time() < 600:
        print(f"WARNING: token expires in {int((exp - time.time()) / 60)} min")

    host = urllib.parse.urlsplit(base).hostname
    tunnel = (SsmTunnel(a.ssm_target, host, a.ssm_profile, a.ssm_region, a.local_port) if a.ssm_target
              else contextlib.nullcontext())
    connect = a.connect or (f"localhost:{a.local_port}" if a.ssm_target else None)
    with tunnel:
        hdrs = dict((k.strip(), v.strip()) for k, _, v in (h.partition(":") for h in a.header) if k.strip())
        client = Client(base, token, connect, a.verify, a.timeout, a.max_requests, hdrs)
        print(f"target {base}{'/' + hint.strip('/') if hint.strip('/') else ''}" + (f"  (via {connect})" if connect else ""))
        try:
            root, xml, model, set_hint, tried = discover(client, hint, query=query, infer=a.no_metadata)
        except LookupError as e:
            print(e)
            diagnose(client, token, hint)
            return 2
        if a.verbose:
            print("discovery:\n  " + "\n  ".join(tried))
        src = "schema INFERRED from data (no $metadata)" if model.inferred else f"$metadata: OData {model.version}"
        print(f"service root: {base}/{root}   ({src}, {len(model.sets)} entity sets)")
        print_plan(model, root)
        if a.plan:
            return 0
        only = a.only or ([set_hint] if set_hint else None)
        user_filter = urllib.parse.parse_qs(query).get("$filter", [None])[0]
        scope = {s: user_filter for s in (only or [])} if user_filter else {}
        if user_filter:
            print(f"scope: every query on {only} is AND-ed with the URL's $filter: {user_filter}")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", root).strip("-") or "root"
        out = Path(a.out) / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}"
        out.mkdir(parents=True, exist_ok=True)
        (out / ("metadata.inferred.xml" if model.inferred else "metadata.xml")).write_text(xml, encoding="utf-8")
        cap = out / "responses" if a.capture else None
        if cap:
            cap.mkdir()
        print(f"\nrunning tests on {only or 'all entity sets'} -> {out}\n")
        t0 = time.time()
        client.stop_on_401 = True
        runner = Runner(client, model, root, a, base_filters=scope, capture_dir=cap)
        results = runner.run(only)
        meta = {"service": f"{base}/{root}", "inferred": model.inferred, "scope": scope, "started": datetime.now().isoformat(timespec="seconds"),
                "duration_s": round(time.time() - t0, 1), "requests": client.count, "only": only,
                "options": {k: v for k, v in vars(a).items() if k != "token"}}
        report.console(results)
        j, h = report.write(out, results, model, meta)
        print(f"\n{client.count} requests in {meta['duration_s']} s\nreport: {h}\n        {j}")
        return 1 if any(r.outcome == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
