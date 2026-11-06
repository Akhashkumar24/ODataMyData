"""$batch (OData 4.0 multipart/mixed). Opt-in with --batch: it is a POST, but every request inside is a GET.
The answers inside the batch must match the same GETs sent on their own, and one bad request inside must
not fail the whole batch."""
from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal

from .core import FAIL, WARN, q


def batch_body(boundary, paths):
    parts = "".join(f"--{boundary}\r\nContent-Type: application/http\r\nContent-Transfer-Encoding: binary\r\n\r\n"
                    f"GET {p} HTTP/1.1\r\nAccept: application/json\r\n\r\n\r\n" for p in paths)
    return parts + f"--{boundary}--\r\n"


def parse_batch(text, content_type):
    """[(status, json_or_None)] for each part of a multipart/mixed batch response, or None if it isn't one."""
    m = re.search(r'boundary="?([^";\s]+)"?', content_type or "")
    if not m:
        return None
    out = []
    for part in (text or "").replace("\r\n", "\n").split("--" + m.group(1))[1:]:
        if part.startswith("--"):
            break
        http = part.partition("\n\n")[2]  # skip the MIME part headers
        st = re.match(r"\s*HTTP/1\.[01] (\d{3})", http)
        if not st:
            continue
        body = http.partition("\n\n")[2].strip()
        try:
            js = json.loads(body, parse_float=Decimal) if body else None
        except ValueError:
            js = None
        out.append((int(st.group(1)), js))
    return out


class BatchTests:
    def t_batch(self, names):
        if not (getattr(self.o, "batch", False) and self.v4 and names) or not self.caps(names[0]).batch:
            return  # BatchSupported is a container-level term; caps() merges container annotations into every set
        svc, direct, paths = "(service)", [], []
        for n in names:
            T = self.m.types.get(self.m.sets[n].type)
            if self.m.sets[n].singleton or not T or not T.keys or len(direct) >= 3:
                continue
            params = self.unsupported(n, self.scope(n, {"$orderby": ",".join(T.keys), "$top": 2}))
            r, _ = self.check(svc, "batch", f"direct GET {n} (batch reference)", n, params)
            if r is not None and r.status == 200:
                direct.append((n, T, [self.key_of(T, x) for x in self.rows(r) or []]))
                paths.append(n + "?" + "&".join(f"{k}={q(v)}" for k, v in params.items()))
        if not direct:
            return
        paths.append("OdhNoSuchEntitySet")
        boundary = f"batch_{uuid.uuid4().hex[:12]}"

        def v(r):
            parts = parse_batch(r.text, r.headers.get("Content-Type"))
            if parts is None:
                return [(FAIL, f"response is not multipart/mixed: {r.headers.get('Content-Type')}")]
            if len(parts) != len(paths):
                return [(FAIL, f"{len(parts)} responses for {len(paths)} requests")]
            notes = []
            for (n, T, want), (st, js) in zip(direct, parts):
                if st != 200:
                    notes.append((FAIL, f"{n} inside the batch returned {st}, alone it returned 200"))
                    continue
                got = [self.key_of(T, x) for x in (js or {}).get("value") or []]
                if got != want:
                    notes.append((WARN, f"{n} inside the batch gave keys {got}, alone {want} (live data?)"))
            st = parts[-1][0]
            if 200 <= st < 300 or st >= 500:
                notes.append((FAIL, f"unknown entity set inside the batch returned {st}, expected 404"))
            return notes
        self.check(svc, "batch", f"$batch with {len(paths)} GETs (one invalid)", "$batch", data=batch_body(boundary, paths),
                   headers={"Content-Type": f"multipart/mixed; boundary={boundary}", "Accept": "multipart/mixed"}, validate=v)
