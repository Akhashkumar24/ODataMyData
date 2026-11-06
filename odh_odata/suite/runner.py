"""Runner: request plumbing, judging, validators and orchestration. The test families are mixins."""
from __future__ import annotations

import itertools
import json
import threading
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from difflib import get_close_matches
from .. import edm
from ..http import BudgetExceeded
from .core import PASS, SKIP, RANK, _MISSING, q, qkey, pick, _norm, compress, Result, Exprs, Ctx, INFO, WARN, FAIL
from .service import ServiceTests
from .read import ReadTests
from .expand import ExpandTests
from .filters import FilterTests
from .keys import KeyTests
from .negative import NegativeTests


class Runner(ServiceTests, ReadTests, ExpandTests, FilterTests, KeyTests, NegativeTests):
    def __init__(self, client, model, root, opts, log=print, base_filters=None, capture_dir=None):
        self.c, self.m, self.root, self.o, self._log = client, model, root.strip("/"), opts, log
        self.base_filters = {k: v for k, v in (base_filters or {}).items() if v}  # set -> user $filter scope
        self.capture_dir = capture_dir
        self._ids = itertools.count(1)
        self.run_id = f"odh-odata-{datetime.now():%Y%m%d%H%M%S}"
        self.key_ok = {}  # entity set -> does 'get by key' work (probed once)
        self.v4 = model.v4
        self.X = Exprs(self.v4)
        self.results, self.visited = [], set()
        self._lock = threading.Lock()
        self.cparam = {"$count": "true"} if self.v4 else {"$inlinecount": "allpages"}

    @property
    def thorough(self):
        return getattr(self.o, "thorough", False)

    def log(self, msg):
        with self._lock:
            self._log(msg)

    def scope(self, path, params):
        """AND the user-supplied $filter for a set into every collection request on that set."""
        if not self.base_filters or path.startswith(("http://", "https://")):
            return params
        head = path.split("?")[0].lstrip("/")
        seg = head.split("/")[0]
        f = self.base_filters.get(seg)
        if not f or "(" in seg or head not in (seg, f"{seg}/$count"):
            return params
        params = dict(params or {})
        params["$filter"] = f"({f}) and ({params['$filter']})" if params.get("$filter") else f
        return params

    def curl(self, url, headers, auth, token):
        def sq(s):
            return "'" + s.replace("'", "'\\''") + "'"
        parts = ["curl", "-sk", sq(url)]
        if self.c.connect:
            parts += ["--connect-to", f"{self.c.host}:443:{self.c.connect}"]
        for k, v in headers.items():
            parts += ["-H", sq(f"{k}: {v}")]
        if auth:
            parts += ["-H", '"Authorization: Bearer $ODATA_TOKEN"' if not token else sq("Authorization: Bearer <test-token>")]
        return " ".join(parts)

    def capture(self, res, r):
        if not self.capture_dir:
            return
        body = r.text or ""
        if isinstance(r.json, (dict, list)) and len(body) < 2_000_000:
            body = json.dumps(r.json, indent=2, default=str)
        name = f"{res.id:05d}.txt"
        head = "\n".join(f"{k}: {v}" for k, v in r.headers.items() if k.lower() != "set-cookie")
        trunc = "  [body truncated]" if r.truncated else ""
        (self.capture_dir / name).write_text(
            f"# {res.set} | {res.category} | {res.name}\n{res.curl}\n\nHTTP {r.status} ({r.ms} ms){trunc}\n{head}\n\n{body[:262144]}",
            encoding="utf-8")
        res.response = f"responses/{name}"

    def url(self, path, params=None):
        p = path.lstrip("/")
        u = path if path.startswith(("http://", "https://")) else (f"{self.root}/{p}" if self.root and p else self.root or p)
        if params:
            u += ("&" if "?" in u else "?") + "&".join(f"{k}={q(v)}" for k, v in params.items())
        return u

    def rows(self, r):
        j = r.json if r is not None else None
        if isinstance(j, dict):
            if isinstance(j.get("value"), list):
                return j["value"]
            d = j.get("d")
            if isinstance(d, dict) and isinstance(d.get("results"), list):
                return d["results"]
            if isinstance(d, list):
                return d
        return None

    def entity(self, r):
        j = r.json if r is not None else None
        if not isinstance(j, dict):
            return None
        if "d" in j and isinstance(j["d"], dict) and len(j) == 1:
            return j["d"]
        ctx = j.get("@odata.context", "")
        if isinstance(j.get("value"), list) and "$entity" not in ctx:
            return None
        return j

    def count_of(self, r):
        j = r.json if r is not None else None
        if isinstance(j, dict):
            c = j.get("@odata.count", j.get("d", {}).get("__count") if isinstance(j.get("d"), dict) else None)
            try:
                return int(c) if c is not None else None
            except (TypeError, ValueError):
                return None
        return None

    def next_link(self, r):
        j = r.json if r is not None else None
        if isinstance(j, dict):
            d = j.get("d")
            return j.get("@odata.nextLink") or (d.get("__next") if isinstance(d, dict) else None)
        return None

    def scalars(self, T):
        out = {}
        for p in T.properties.values():
            st = self.m.scalar_type(p)
            if st:
                out[p.name] = (st[0], st[1], p)
        return out

    def key_of(self, T, row):
        return tuple(str(row.get(k)) for k in T.keys)

    def key_seg(self, T, row):
        if not T.keys:
            return None
        parts = []
        for k in T.keys:
            p = T.properties.get(k)
            if p is None or row.get(k) is None:
                return None
            st = self.m.scalar_type(p)
            lit = edm.literal(row[k], st[0] if st else p.type, self.v4, st[1] if st else None)
            if lit is None:
                return None
            parts.append(lit if len(T.keys) == 1 else f"{k}={lit}")
        return "(" + qkey(",".join(parts)) + ")"

    def _add(self, res):
        with self._lock:
            self.results.append(res)
            if self.o.verbose or res.outcome in (FAIL, WARN):
                first = next((m for lvl, m in res.notes if RANK[lvl] == RANK[res.outcome]), "")
                self._log(f"  [{res.outcome}] {res.set:<20} {res.category:<8} {res.name[:70]:<70} "
                          f"{res.status} {res.ms}ms  {first[:160]}")

    def check(self, set_, cat, name, path, params=None, expect="ok", validate=None, headers=None,
              auth=True, token=None, optional=False, max_bytes=20_000_000):
        """One GET judged against `expect` (ok|error|notfound|denied|denied_soft|error_soft); validate(r) -> notes."""
        res = Result(set_, cat, name, id=next(self._ids))
        if "x-correlation-id" not in {k.lower() for k in self.c.extra}:  # lets the API team find this request in server logs
            headers = {**(headers or {}), "X-Correlation-ID": f"{self.run_id}-{res.id:05d}"}
        params = self.scope(path, params)
        try:
            r = self.c.get(self.url(path, params), headers=headers, auth=auth, token=token, max_bytes=max_bytes)
        except BudgetExceeded as e:
            res.outcome, res.notes = SKIP, [(SKIP, str(e) or "request budget exhausted")]
            self._add(res)
            return None, res
        res.url, res.status, res.ms = r.url, r.status, r.ms
        res.curl = self.curl(r.url, {"Accept": "application/json", **self.c.extra, **(headers or {})}, auth, token)
        self.capture(res, r)
        st, notes, snip = r.status, [], (r.text or "")[:200].replace("\n", " ")
        if r.error:
            notes.append((FAIL, f"transport error: {r.error}"))
        elif expect == "ok":
            if st == 200:
                try:
                    notes += (validate(r) or []) if validate else []
                except Exception as e:  # a validator bug must not kill the run
                    notes.append((FAIL, f"validator crashed: {type(e).__name__}: {e}"))
            elif optional and st in (400, 403, 404, 405, 406, 501):
                notes.append((WARN, f"not supported by server ({st}) {snip}".strip()))
            else:
                notes.append((FAIL, f"expected 200, got {st} {snip}".strip()))
        elif expect in ("error", "error_soft"):
            if st and 200 <= st < 300:
                notes.append((FAIL if expect == "error" else WARN, f"invalid request accepted with {st}"))
            elif st and st >= 500:
                notes.append((FAIL, f"server error {st} on invalid input (should be 4xx) {snip}".strip()))
            elif st != 400:
                notes.append((INFO, f"rejected with {st}"))
        elif expect == "notfound":
            if st == 404:
                pass
            elif st and st >= 500:
                notes.append((FAIL, f"server error {st} for a missing key {snip}".strip()))
            elif st and 200 <= st < 300:
                notes.append((FAIL, f"non-existent key returned {st} (expected 404)"))
            else:
                notes.append((WARN, f"expected 404, got {st}"))
        elif expect in ("denied", "denied_soft"):
            if st in (401, 403):
                pass
            elif st and 200 <= st < 300:
                notes.append((FAIL if expect == "denied" else WARN,
                              f"SECURITY: {st} returned without valid credentials" if expect == "denied"
                              else f"accessible without credentials ({st})"))
            elif st == 404:
                notes.append((WARN, "returns 404 instead of 401 when unauthenticated (hides resource; non-standard)"))
            elif st and st >= 500:
                notes.append((FAIL, f"server error {st} on unauthenticated request"))
            else:
                notes.append((WARN, f"unexpected {st} for unauthenticated request"))
        if r.ms and r.ms > self.o.slow_ms:
            notes.append((WARN, f"slow response: {r.ms} ms (> {self.o.slow_ms})"))
        res.notes = compress(notes)
        res.outcome = max((lvl for lvl, _ in res.notes), key=lambda x: RANK[x], default=PASS)
        res.outcome = PASS if RANK[res.outcome] == 0 else res.outcome
        rows = self.rows(r)
        res.rows = len(rows) if rows is not None else None
        self._add(res)
        return r, res

    def soften(self, notes):
        """Inferred schemas are guesses from a sample: a type mismatch is a WARN, not a service defect."""
        if not self.m.inferred:
            return notes
        return [(WARN, m + " [inferred schema]") if lvl == FAIL else (lvl, m) for lvl, m in notes]

    def validate_struct(self, tqn, obj, where, select=None, expand=(), depth=0):
        T = self.m.types.get(tqn)
        if not isinstance(obj, dict):
            return [(FAIL, f"{where}: expected an object of {tqn}, got {type(obj).__name__}")]
        if T is None:
            return []
        ot = obj.get("@odata.type")
        if isinstance(ot, str) and self.m.qn(ot.lstrip("#")) in self.m.types:
            T = self.m.types[self.m.qn(ot.lstrip("#"))]
        notes = []
        for k in obj:
            if k.startswith("@") or "@" in k:
                continue
            if k not in T.properties and k not in T.navs and not T.open:
                notes.append((FAIL, f"{where}: undeclared property '{k}' (not in metadata)"))
            elif select is not None and k in T.properties and k not in select and k not in T.keys:
                notes.append((WARN, f"{where}: '{k}' returned although not in $select"))
            elif k in T.navs and k not in expand:
                notes.append((WARN, f"{where}: navigation '{k}' inlined without $expand"))
        props = T.properties if select is None else {k: T.properties[k] for k in select if k in T.properties}
        for name, p in props.items():
            if name not in obj:
                notes.append((FAIL if select is not None else WARN, f"{where}: property '{name}' missing"))
                continue
            notes += self.validate_value(p, obj[name], f"{where}.{name}", depth)
        for k in T.keys:
            if k in obj and obj[k] is None:
                notes.append((FAIL, f"{where}: key '{k}' is null"))
        return notes

    def validate_value(self, p, v, where, depth=0):
        if v is None:
            return [] if p.nullable or p.collection else [(FAIL, f"{where}: null but Nullable=false")]
        if p.collection and not isinstance(v, list):
            return [(FAIL, f"{where}: expected an array")]
        out = []
        t = self.m.type_defs.get(p.type, p.type)
        for it in (v if p.collection else [v])[:50]:
            if it is None:
                if not p.nullable:
                    out.append((FAIL, f"{where}: null item but Nullable=false"))
            elif t.startswith("Edm."):
                err = edm.check(it, t, p.max_length)
                if err:
                    out.append((FAIL, f"{where}: {err}"))
            elif t in self.m.enums:
                e = self.m.enums[t]
                names = [x.strip() for x in str(it).split(",")] if e.flags else [str(it)]
                if not all(n in e.members or n.lstrip("-").isdigit() for n in names):
                    out.append((FAIL, f"{where}: {it!r} is not a member of {t}"))
            elif t in self.m.types and depth < 6:
                out += self.validate_struct(t, it, where, depth=depth + 1)
        return out

    def rows_check(self, tqn, select=None, expand=(), top=None, extra=None):
        def v(r):
            rows = self.rows(r)
            if rows is None:
                return [(FAIL, "response is not an OData collection (no 'value' array)")]
            notes = []
            if top is not None and len(rows) > top:
                notes.append((FAIL, f"returned {len(rows)} rows for $top={top}"))
            for row in rows:
                notes += self.soften(self.validate_struct(tqn, row, "row", select, expand))
            if extra:
                notes += extra(rows, r) or []
            return notes
        return v

    def order_notes(self, rows, specs, where="rows"):
        """specs = [(path, desc, edm_type)]. FAIL on real mis-ordering, WARN when only collation/null placement explains it."""
        def violations(ci=False, nulls_last=False):
            bad = 0
            for a, b in zip(rows, rows[1:]):
                for path, desc, t in specs:
                    try:
                        x, y = edm.to_py(pick(a, path), t), edm.to_py(pick(b, path), t)
                    except (ValueError, TypeError, ArithmeticError):
                        break
                    if x is None or y is None:
                        c = 0 if x is y else ((1 if x is None else -1) if nulls_last else (-1 if x is None else 1))
                    else:
                        x, y = _norm(x, ci), _norm(y, ci)
                        c = (x > y) - (x < y)
                    c = -c if desc else c
                    if c < 0:
                        break
                    if c > 0:
                        bad += 1
                        break
            return bad
        if not violations():
            return []
        label = ", ".join(f"{p} {'desc' if d else 'asc'}" for p, d, _ in specs)
        if not violations(nulls_last=True):
            return [(WARN, f"{where}: nulls sorted last for '{label}' (OData sorts null as the lowest value)")]
        strings = any(t == "Edm.String" for _, _, t in specs)
        if strings and not violations(ci=True):
            return [(WARN, f"{where}: order is case-insensitive for '{label}' (database collation)")]
        if strings:
            return [(WARN, f"{where}: order differs from ordinal string order for '{label}' (collation-specific)")]
        return [(FAIL, f"{where}: not sorted by '{label}' ({violations()} out-of-order pairs)")]

    def entity_check(self, T, expected=None, origin="the collection", select=None, expand=()):
        def v(r):
            ent = self.entity(r)
            if not isinstance(ent, dict):
                return [(FAIL, "response is not a single entity object")]
            notes = self.soften(self.validate_struct(T.name, ent, "entity", select, expand))
            if expected is not None:
                for k in T.keys:
                    if str(ent.get(k)) != str(expected.get(k)):
                        notes.append((FAIL, f"key {k}={ent.get(k)!r}, expected {expected.get(k)!r}"))
                diffs = []
                for p, (t, _, _) in self.scalars(T).items():
                    if p in ent and p in expected:
                        try:
                            if edm.to_py(ent[p], t) != edm.to_py(expected[p], t):
                                diffs.append(p)
                        except (ValueError, TypeError, ArithmeticError):
                            pass
                if diffs:
                    notes.append((WARN, f"{len(diffs)} field(s) differ from {origin}: {diffs[:6]} (live data or inconsistent projection)"))
            return notes
        return v

    def nav_checks(self, S_T, nav, rows):
        """Shape, schema and referential integrity of an expanded navigation on each row."""
        notes = []
        pairs, kind = self.m.fk_pairs(S_T.name, nav)
        lvl = FAIL if kind == "declared" else WARN
        for row in rows:
            val = row.get(nav.name, _MISSING)
            if val is _MISSING:
                notes.append((FAIL, f"expanded '{nav.name}' missing from row"))
                continue
            if nav.collection != isinstance(val, list) and val is not None:
                notes.append((FAIL, f"'{nav.name}' should be {'an array' if nav.collection else 'an object'}"))
                continue
            items = (val or []) if nav.collection else ([] if val is None else [val])
            if not nav.collection and val is None and not nav.nullable:
                notes.append((FAIL, f"'{nav.name}' is null but Nullable=false"))
            for it in items:
                notes += self.validate_struct(nav.type, it, nav.name)
                for sp, tp in pairs:
                    a, b = row.get(sp), it.get(tp) if isinstance(it, dict) else None
                    if a is not None and b is not None and str(a) != str(b):
                        same_ci = str(a).casefold() == str(b).casefold()
                        notes.append((WARN if same_ci else lvl, f"referential mismatch ({kind}): row.{sp} vs {nav.name}.{tp}"
                                      + (" differ only by case" if same_ci else "")))
            if pairs and not nav.collection and val is None and all(row.get(sp) is not None for sp, _ in pairs):
                notes.append((lvl, f"dangling reference ({kind}): {'/'.join(sp for sp, _ in pairs)} is set but '{nav.name}' expands to null"))
        return notes

    def run(self, only=None):
        self.t_service()
        names = []
        for n in only or list(self.m.sets):
            if n in self.m.sets:
                names.append(n)
            else:
                close = get_close_matches(n, list(self.m.sets), 3)
                self._add(Result(n, "service", "entity set exists", outcome=FAIL,
                                 notes=[(FAIL, f"'{n}' not in metadata; did you mean {close}?")]))
        with ThreadPoolExecutor(max(1, self.o.workers)) as ex:
            list(ex.map(self._safe_set, names))
        return self.results

    def _safe_set(self, name):
        try:
            self.test_set(name)
        except Exception as e:
            self._add(Result(name, "internal", "framework error", outcome=FAIL,
                             notes=[(FAIL, f"{type(e).__name__}: {e}"), (INFO, traceback.format_exc()[-800:])]))
        mine = [r for r in self.results if r.set == name]
        c = Counter(r.outcome for r in mine)
        self.log(f"== {name}: {len(mine)} tests  PASS {c[PASS]}  WARN {c[WARN]}  FAIL {c[FAIL]}  SKIP {c[SKIP]}")

    def test_set(self, name):
        es = self.m.sets[name]
        T = self.m.types.get(es.type)
        if T is None:
            self._add(Result(name, "metadata", "entity type resolvable", outcome=FAIL, notes=[(FAIL, f"unknown type {es.type}")]))
            return
        S = Ctx(name, T, self.m.caps(name), self.scalars(T), es.bindings)
        if es.singleton:
            self.check(name, "read", "singleton", name, validate=self.entity_check(T))
            return
        self.log(f"-- {name} ({T.name}): keys={T.keys} props={len(T.properties)} navs={list(T.navs)}")
        if S.caps.requires_filter and name not in self.base_filters:
            req = sorted(S.caps.required_filter)
            self.check(name, "read", "unfiltered list is rejected (RequiresFilter)", name, {"$top": 1}, expect="error_soft")
            self._add(Result(name, "read", "data-driven tests", outcome=SKIP, notes=[(SKIP, "metadata declares RequiresFilter"
                             + (f" on {req}" if req else "") + ": put a $filter in the target URL to scope this set")]))
            return
        S.rows = self.t_sample(S)
        if not S.rows:  # list failed (already recorded) or empty: filters, keys and crawling need real rows
            self._add(Result(name, "read", "data-driven tests", outcome=SKIP,
                             notes=[(SKIP, "no sample rows: filter, key and crawl tests need real data")]))
            return
        steps = [self.t_count, self.t_paging, self.t_select, self.t_orderby, self.t_expand,
                 self.t_filter, self.t_search, self.t_combos, self.t_keys, self.t_crawl]
        if self.o.negative:
            steps.append(self.t_negative)
        for step in steps:
            step(S)

    def _limit(self, items, key=lambda x: x, S=None, name=lambda x: x):
        """Focused mode (default): per Edm type keep the 2 most informative properties (most distinct sampled values).
        Constant or all-null columns make weak tests and are dropped. --thorough keeps everything."""
        if self.thorough:
            return list(items)

        def distinct(it):
            return len({str(r.get(name(it))) for r in (S.rows if S else []) if r.get(name(it)) is not None})
        seen, out = Counter(), []
        for it in sorted(items, key=lambda it: -distinct(it)):
            if S is not None and S.rows and distinct(it) < 2:
                continue
            if seen[key(it)] < 2:
                out.append(it)
                seen[key(it)] += 1
        return out

    def key_works(self, sname, T, row):
        """Probe 'get by key' once per set. If the route is not served that is one finding; every key-based
        variant and every crawl resolve would fail identically, so callers skip them."""
        with self._lock:
            if sname in self.key_ok:
                return self.key_ok[sname]
        seg = self.key_seg(T, row)
        r = self.check(sname, "key", f"get by key {seg}", sname + seg, validate=self.entity_check(T, row))[0] if seg else None
        ok = r is not None and r.status == 200
        with self._lock:
            self.key_ok[sname] = ok
        return ok
