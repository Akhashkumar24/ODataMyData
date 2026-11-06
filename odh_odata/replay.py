"""Replay a recorded run offline: serve every request from runs/<run>/responses, re-judge with the current code,
and report which outcomes changed. Use it to check a framework change against real service behaviour without
the tunnel or a token. The run must have been recorded with --capture all."""
from __future__ import annotations

import json
import re
import shlex
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from . import baseline, metadata
from .http import Client, Resp

_NONCE = re.compile(r"zz-odh-nonexistent-[0-9a-f]{8}")  # edm.fake_value is random per run


def _key(method, url, auth):
    return method, _NONCE.sub("zz-odh-nonexistent-~", url), auth


def load_capture(text):
    """One responses/NNNNN.txt -> (key, Resp)."""
    if text.split("\n", 1)[0].endswith("\r"):  # written by an older version in Windows text mode: every \n became \r\n
        text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    curl = lines[1]
    url = shlex.split(curl.split(" -H ")[0].replace("-X POST --data-binary @batch-body.txt", ""))[2]
    auth = "test" if "<test-token>" in curl else "token" if "$ODATA_TOKEN" in curl else "none"
    m = re.match(r"HTTP (\d+|None) \((\d+|None) ms\)", lines[3])
    status, ms = (int(m.group(1)) if m.group(1) != "None" else None), (int(m.group(2)) if m.group(2) != "None" else None)
    i, headers = 4, {}
    while i < len(lines) and lines[i]:
        k, _, v = lines[i].partition(": ")
        headers[k] = v
        i += 1
    body = "\n".join(lines[i + 1:]).lstrip("\n") if i < len(lines) else ""
    body = body.split("\n\n# request body (batch-body.txt)\n")[0]
    js = None
    if "json" in headers.get("Content-Type", ""):
        try:
            js = json.loads(body, parse_float=Decimal)
        except ValueError:
            pass
    method = "POST" if "-X POST" in curl else "GET"
    return _key(method, url, auth), Resp(url, status, ms, headers, body, js, None if status else "transport error (recorded)")


class ReplayClient(Client):
    """Client whose network is a folder of captured responses. Repeated identical requests replay in order."""

    def __init__(self, base_url, responses_dir):
        super().__init__(base_url, token="replay.token.x")  # a JWT-shaped token so the tampered-token test is generated
        self.store, self.missing = defaultdict(list), []
        for f in sorted(Path(responses_dir).glob("*.txt")):
            k, r = load_capture(f.read_bytes().decode("utf-8"))  # keep CRLF: batch bodies depend on it
            self.store[k].append(r)

    def _send(self, method, url, headers, auth, token, max_bytes, data=None):
        with self._lock:
            self.count += 1
        public = self._swap(url, self.public)
        hits = self.store.get(_key(method, public, "none" if not auth else "test" if token else "token"))
        if not hits:
            self.missing.append(f"{method} {public}")
            return Resp(public, None, 0, {}, error="not in the recording")
        return hits.pop(0) if len(hits) > 1 else hits[0]


def run(run_dir, cli_opts, log=print):
    """Returns (exit code, results)."""
    from .suite import Runner  # late: suite imports http, keep module import order simple
    run_dir = Path(run_dir)
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    meta = report["meta"]
    if (meta.get("options") or {}).get("capture") in ("failures", "none", False):
        raise SystemExit(f"{run_dir} was recorded without --capture all: passing responses are missing, it can't be replayed")
    inferred = (run_dir / "metadata.inferred.xml").exists()
    xml = (run_dir / ("metadata.inferred.xml" if inferred else "metadata.xml")).read_bytes()
    model = metadata.parse(xml)
    if inferred:
        model = metadata.as_inferred(model)
    base = re.match(r"https?://[^/]+", meta["service"]).group(0)
    root = meta["service"][len(base):].strip("/")
    opts = SimpleNamespace(**{**vars(cli_opts), **(meta.get("options") or {}), "capture": "none", "verbose": False})
    client = ReplayClient(base, run_dir / "responses")
    runner = Runner(client, model, root, opts, log=lambda *_: None, base_filters=meta.get("scope") or {})
    results = runner.run(meta.get("only"))
    new, fixed, _ = baseline.compare(results, baseline.findings(report["results"]))
    log(f"replayed {run_dir.name}: {len(results)} results, {client.count} requests, {len(client.missing)} not in the recording")
    for k, o in sorted(new.items()):
        log(f"  now {o:<4} {k}")
    for k, o in sorted(fixed.items()):
        log(f"  was {o:<4} {k}  -> no longer reported")
    gone = sorted({baseline.test_id(r) for r in report["results"]} - {baseline.test_id(r) for r in results})
    for k in gone[:10]:
        log(f"  no longer generated: {k}")
    if len(gone) > 10:
        log(f"  ... {len(gone) - 10} more tests no longer generated")
    for m in client.missing[:10]:
        log(f"  not recorded: {m}")
    changed = new or fixed or gone or client.missing
    if not changed:
        log("  same tests and outcomes as the recorded run")
    return (1 if changed else 0), results
