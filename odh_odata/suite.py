"""Test generation and execution.

Three layers, each feeding the next:
  1. metadata-driven  - every set/property/navigation/operation gets structural tests ($select, $orderby, $expand, keys...)
  2. data-driven      - sampled rows supply real values for $filter (every operator/function valid for the type),
                        combinations, key lookups, nested expands and navigation filters
  3. crawl-driven     - follow navigation links entity -> related entity -> its related entities (depth N), checking
                        that everything reachable is addressable, type-correct and referentially consistent
Every returned payload is validated against the schema, and every $filter result is re-evaluated locally.
"""
from __future__ import annotations

import itertools
import json
import math
import threading
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from urllib.parse import quote

from . import edm
from .http import BudgetExceeded

PASS, INFO, WARN, FAIL, SKIP = "PASS", "INFO", "WARN", "FAIL", "SKIP"
RANK = {PASS: 0, INFO: 0, SKIP: 0, WARN: 1, FAIL: 2}
_MISSING = object()


def q(s):
    return quote(str(s), safe="'(),;=:@$*!/")


def qkey(s):
    return quote(str(s), safe="'(),;=:@$*!")


def pick(row, path):
    v = row
    for p in path.split("/"):
        if not isinstance(v, dict):
            return None
        v = v.get(p)
    return v


def _norm(v, ci):
    return v.casefold() if ci and isinstance(v, str) else v


def _cmp(op, a, b):
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if a is None or b is None:
        return False
    try:
        return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]
    except TypeError:
        return False


def compress(notes, cap=8):
    out = [(lvl, msg if n == 1 else f"{msg} (x{n})") for (lvl, msg), n in Counter(notes).items()]
    out.sort(key=lambda x: -RANK[x[0]])
    return out[:cap] + ([(INFO, f"... {len(out) - cap} more")] if len(out) > cap else [])


@dataclass
class Result:
    set: str
    category: str
    name: str
    url: str = ""
    status: int | None = None
    ms: int | None = None
    outcome: str = PASS
    notes: list = field(default_factory=list)
    rows: int | None = None
    id: int | None = None
    curl: str = ""
    response: str | None = None  # captured response file, relative to the run folder


class Expr:
    """A $filter expression: OData text plus a local evaluator fn(row, case_insensitive) -> bool."""

    def __init__(self, text, fn, props=()):
        self.text, self.fn, self.props = text, fn, set(props)

    def __and__(self, o):
        return Expr(f"{self.text} and {o.text}", lambda r, ci: self.fn(r, ci) and o.fn(r, ci), self.props | o.props)

    def __or__(self, o):
        return Expr(f"({self.text} or {o.text})", lambda r, ci: self.fn(r, ci) or o.fn(r, ci), self.props | o.props)

    def __invert__(self):
        return Expr(f"not ({self.text})", lambda r, ci: not self.fn(r, ci), self.props)


class Exprs:
    def __init__(self, v4):
        self.v4 = v4

    def cmp(self, path, op, raw, t, enum=None):
        lit = edm.literal(raw, t, self.v4, enum)
        if lit is None:
            return None
        target = edm.to_py(raw, t)

        def fn(r, ci):
            try:
                v = edm.to_py(pick(r, path), t)
            except (ValueError, TypeError, ArithmeticError):
                return False
            return _cmp(op, _norm(v, ci), _norm(target, ci))
        return Expr(f"{path} {op} {lit}", fn, [path.split("/")[0]])

    def in_(self, path, raws, t, enum=None):
        lits = [edm.literal(x, t, self.v4, enum) for x in raws]
        if None in lits:
            return None
        targets = [edm.to_py(x, t) for x in raws]

        def fn(r, ci):
            try:
                v = edm.to_py(pick(r, path), t)
            except (ValueError, TypeError, ArithmeticError):
                return False
            return _norm(v, ci) in [_norm(x, ci) for x in targets]
        return Expr(f"{path} in ({','.join(lits)})", fn, [path.split("/")[0]])

    def strfn(self, fname, path, arg):
        lit = edm.literal(arg, "Edm.String", self.v4)
        ops = {"contains": lambda v, a: a in v, "startswith": str.startswith, "endswith": str.endswith}
        text = f"substringof({lit},{path})" if (fname == "contains" and not self.v4) else f"{fname}({path},{lit})"

        def fn(r, ci):
            v = pick(r, path)
            return isinstance(v, str) and ops[fname](_norm(v, ci), _norm(arg, ci))
        return Expr(text, fn, [path.split("/")[0]])

    def xform(self, lhs, path, pyf, op, result, lit):
        """e.g. lhs='year(P)', pyf=lambda raw: ..., compares pyf(row[path]) <op> result."""
        def fn(r, ci):
            v = pick(r, path)
            if v is None:
                return False
            try:
                x = pyf(v)
            except (ValueError, TypeError, ArithmeticError):
                return False
            return _cmp(op, _norm(x, ci), _norm(result, ci))
        return Expr(f"{lhs} {op} {lit}", fn, [path.split("/")[0]])

    def any_(self, coll, inner, op, raw, t, enum=None):
        lit = edm.literal(raw, t, self.v4, enum)
        if lit is None or not self.v4:
            return None
        target = edm.to_py(raw, t)
        sub = f"o/{inner}" if inner else "o"

        def fn(r, ci):
            for it in pick(r, coll) or []:
                try:
                    v = edm.to_py(pick(it, inner) if inner else it, t)
                except (ValueError, TypeError, ArithmeticError):
                    continue
                if _cmp(op, _norm(v, ci), _norm(target, ci)):
                    return True
            return False
        return Expr(f"{coll}/any(o:{sub} {op} {lit})", fn, [coll])


@dataclass
class Ctx:
    name: str
    T: object
    caps: object
    scalars: dict  # prop -> (edm_type, enum_qn, Property)
    bindings: dict
    rows: list = field(default_factory=list)
    total: int | None = None
    expanded: dict = field(default_factory=dict)  # nav -> rows that had it expanded
    ne_gaps: list = field(default_factory=list)  # properties whose 'ne' drops null rows


class Runner:
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

    # ---------- plumbing ----------

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

    # ---------- validators ----------

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

    # ---------- orchestration ----------

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

    # ---------- service level ----------

    def t_service(self):
        svc = "(service)"
        expected = {s.name for s in self.m.sets.values()} | {i.name for i in self.m.imports
                                                            if i.kind == "function" and i.in_service_document}

        def v(r):
            rows = self.rows(r)
            if rows is None:
                return [(FAIL, "service document has no 'value' array")] if self.v4 else []
            got = {x.get("name") for x in rows if isinstance(x, dict)}
            notes = []
            if expected - got:
                notes.append((FAIL, f"in metadata but not in service document: {sorted(expected - got)}"))
            if got - expected:
                notes.append((WARN, f"in service document but not in metadata: {sorted(got - expected)}"))
            return notes
        if not self.m.inferred:  # an inferred model only knows the probed set, so these comparisons mean nothing
            self.check(svc, "service", "service document lists every entity set", "", validate=v)
        res = Result(svc, "metadata", "metadata consistency (types, keys, bindings, constraints)",
                     url=self.c.public + "/" + self.url("$metadata"))
        res.notes = [(FAIL, p) for p in self.m.problems][:40]
        res.outcome = FAIL if self.m.problems else PASS
        self._add(res)

        def ver(r):
            v = r.headers.get("OData-Version") or r.headers.get("DataServiceVersion")
            return [] if v else [(WARN, "no OData-Version header in response")]
        self.check(svc, "service", "$metadata returns OData-Version header", "$metadata", headers={"Accept": "application/xml"}, validate=ver)
        if self.o.negative:
            self.check(svc, "security", "$metadata without credentials", "$metadata", expect="denied_soft", auth=False,
                       headers={"Accept": "application/xml"})
            self.check(svc, "negative", "unknown entity set -> 404", "OdhNoSuchEntitySet", expect="notfound")
        first = next((s for s in self.m.sets.values() if not s.singleton), None)
        if first and self.v4 and self.thorough:
            def full(r):
                rows = self.rows(r) or []
                return [] if all("@odata.id" in x or "@odata.editLink" in x for x in rows) else [(WARN, "odata.metadata=full rows lack @odata.id")]
            self.check(svc, "format", f"odata.metadata=full ({first.name})", first.name, {"$top": 2},
                       headers={"Accept": "application/json;odata.metadata=full"}, validate=full, optional=True)

            def none(r):
                return [(WARN, "odata.metadata=none still returns @odata.context")] if r.json and "@odata.context" in r.json else []
            self.check(svc, "format", f"odata.metadata=none ({first.name})", first.name, {"$top": 2},
                       headers={"Accept": "application/json;odata.metadata=none"}, validate=none, optional=True)
            self.check(svc, "format", f"$format=json ({first.name})", first.name, {"$top": 1, "$format": "json"}, optional=True)
        self.t_operations()

    def t_operations(self):
        svc = "(service)"
        for imp in self.m.imports:
            if imp.kind == "action":
                self._add(Result(svc, "operation", f"action import {imp.name}", outcome=SKIP,
                                 notes=[(SKIP, "actions are never invoked (side effects)")]))
                continue
            ops = [o for o in self.m.operations if o.name == imp.operation]
            op = next((o for o in ops if all(p.nullable for p in o.params)), None)
            if op is None:
                req = sorted({p.name for o in ops for p in o.params if not p.nullable})
                self._add(Result(svc, "operation", f"function import {imp.name}", outcome=SKIP,
                                 notes=[(SKIP, f"requires parameters {req}; no safe values known")]))
                continue
            self.check(svc, "operation", f"function import {imp.name}()", f"{imp.name}()" if self.v4 else imp.name,
                       validate=self._op_validator(op))
        for op in self.m.operations:
            if not op.bound or not op.params:
                continue
            if op.kind == "action":
                self._add(Result(svc, "operation", f"bound action {op.name}", outcome=SKIP,
                                 notes=[(SKIP, "actions are never invoked (side effects)")]))
                continue
            bp, rest = op.params[0], op.params[1:]
            if any(not p.nullable for p in rest):
                self._add(Result(svc, "operation", f"bound function {op.name}", outcome=SKIP,
                                 notes=[(SKIP, f"requires parameters {[p.name for p in rest if not p.nullable]}")]))
                continue
            for s in self.m.sets.values():
                if bp.collection and self.m.is_derived(s.type, bp.type):
                    self.check(s.name, "operation", f"bound function {op.name}() on {s.name}", f"{s.name}/{op.name}()",
                               validate=self._op_validator(op))

    def _op_validator(self, op):
        def v(r):
            if op.return_type in self.m.types and op.return_collection:
                return self.rows_check(op.return_type)(r)
            return []
        return v

    # ---------- per entity set ----------

    def t_sample(self, S):
        n = self.o.sample
        params = {"$top": n} if S.caps.top else {}

        def uniq(rows, r):
            keys = [self.key_of(S.T, x) for x in rows]
            d = [k for k, c in Counter(keys).items() if c > 1]
            return [(FAIL, f"duplicate keys in one page: {d[:3]}")] if d and S.T.keys else []
        r, _ = self.check(S.name, "read", f"list $top={n}", S.name, params,
                          validate=self.rows_check(S.T.name, top=n if S.caps.top else None, extra=uniq))
        return (self.rows(r) or []) if r is not None and r.status == 200 else []

    def t_count(self, S):
        exp = "ok" if S.caps.countable else "error"

        def v(r):
            c, rows = self.count_of(r), self.rows(r) or []
            if c is None:
                return [(FAIL, "no count in response")]
            S.total = c
            return [] if c >= len(rows) else [(FAIL, f"count {c} < rows returned {len(rows)}")]
        self.check(S.name, "count", "$count=true", S.name, {**self.cparam, "$top": 1}, expect=exp, validate=v)

        def raw(r):
            t = r.text.strip().lstrip("﻿")
            if not t.isdigit():
                return [(FAIL, f"/$count body is not an integer: {t[:60]!r}")]
            if S.total is not None and int(t) != S.total:
                return [(WARN, f"/$count={t} differs from inline count {S.total} (live data?)")]
            S.total = int(t) if S.total is None else S.total
            return []
        self.check(S.name, "count", "/$count", f"{S.name}/$count", expect=exp, validate=raw, headers={"Accept": "text/plain"})
        if S.caps.countable and S.rows:
            p = next((p for p, (t, _, _) in S.scalars.items() if S.rows[0].get(p) is not None and p not in S.T.keys), None)
            if p:
                e = self.X.cmp(p, "eq", S.rows[0][p], *S.scalars[p][:2])

                def vf(r):
                    c = self.count_of(r)
                    if c is None:
                        return [(FAIL, "no count in response")]
                    out = [(FAIL, f"filtered count {c} > total {S.total}")] if S.total is not None and c > S.total else []
                    return out + ([(FAIL, "filtered count is 0 but a matching row exists")] if c == 0 else [])
                if e:
                    self.check(S.name, "count", f"$count with $filter={e.text}", S.name,
                               {"$filter": e.text, **self.cparam, "$top": 1}, validate=vf)

    def t_paging(self, S):
        if not S.T.keys or not (S.caps.top and S.caps.skip):
            return
        ob = ",".join(S.T.keys)
        self.check(S.name, "paging", "$top=0 returns empty", S.name, {"$top": 0},
                   validate=lambda r: [] if self.rows(r) == [] else [(FAIL, f"$top=0 returned {len(self.rows(r) or [])} rows")])
        self.check(S.name, "paging", "$top=1", S.name, {"$top": 1}, validate=self.rows_check(S.T.name, top=1))
        r, _ = self.check(S.name, "paging", f"$orderby={ob}&$top=6 (window base)", S.name, {"$orderby": ob, "$top": 6},
                          validate=self.rows_check(S.T.name, top=6))
        base = self.rows(r) if r is not None and r.status == 200 else None
        if base and len(base) >= 3:
            want = [self.key_of(S.T, x) for x in base[2:5]]

            def win(r):
                got = [self.key_of(S.T, x) for x in self.rows(r) or []]
                return [] if got == want else [(FAIL, f"$skip=2&$top=3 gave {got}, expected rows 3-5 of the same order {want}")]
            self.check(S.name, "paging", "$skip=2&$top=3 matches window", S.name, {"$orderby": ob, "$skip": 2, "$top": 3}, validate=win)
        skip = (S.total or 0) + 1_000_000
        self.check(S.name, "paging", "$skip past end returns empty", S.name, {"$orderby": ob, "$skip": skip, "$top": 1},
                   validate=lambda r: [] if self.rows(r) == [] else [(FAIL, "rows returned past the end")])
        self.t_server_paging(S)

    def t_server_paging(self, S):
        hdr = {"Prefer": "odata.maxpagesize=3"} if self.v4 else None
        seen, url, notes, pages = set(), S.name, [], 0
        while url and pages < 3:
            r, res = self.check(S.name, "paging", f"server-driven paging page {pages + 1}", url, headers=hdr,
                                validate=self.rows_check(S.T.name), max_bytes=5_000_000)
            if r is None or r.status != 200:
                return
            if r.truncated:
                res.notes.append((WARN, "no server-driven paging: unbounded GET returned > 5 MB"))
                res.outcome = WARN
                return
            rows = self.rows(r) or []
            keys = [self.key_of(S.T, x) for x in rows]
            dup = seen & set(keys)
            if dup:
                res.notes.append((FAIL, f"page {pages + 1} repeats keys from earlier pages: {list(dup)[:3]}"))
                res.outcome = FAIL
            seen |= set(keys)
            url = self.next_link(r)
            pages += 1
            if not url and pages == 1 and S.total and len(rows) < S.total:
                res.notes.append((FAIL, f"returned {len(rows)} of {S.total} rows without a nextLink"))
                res.outcome = FAIL
            elif not url and pages == 1 and len(rows) > 1000:
                res.notes.append((WARN, f"no server-driven paging: {len(rows)} rows in one response"))
                res.outcome = WARN if res.outcome != FAIL else FAIL

    def t_select(self, S):
        props = list(S.T.properties)
        if not props:
            return
        k = min(3, self.o.sample)
        for p in self._limit(props, key=lambda n: S.T.properties[n].type, S=S):
            self.check(S.name, "select", f"$select={p}", S.name, {"$select": p, "$top": k},
                       validate=self.rows_check(S.T.name, select=[p], top=k))
        multi = props[: max(2, len(props) // 2)]
        self.check(S.name, "select", f"$select {len(multi)} properties", S.name, {"$select": ",".join(multi), "$top": k},
                   validate=self.rows_check(S.T.name, select=multi, top=k))
        self.check(S.name, "select", "$select=*", S.name, {"$select": "*", "$top": k}, validate=self.rows_check(S.T.name, top=k))
        if S.T.keys:
            self.check(S.name, "select", "$select=<keys only>", S.name, {"$select": ",".join(S.T.keys), "$top": k},
                       validate=self.rows_check(S.T.name, select=list(S.T.keys), top=k))

    def _sortable(self, S):
        return [(p, t) for p, (t, enum, _) in S.scalars.items()
                if (t in edm.ORDERABLE or enum) and p not in S.caps.non_sortable]

    def t_orderby(self, S):
        if not S.caps.sortable:
            return
        n = self.o.sample
        sortable = self._sortable(S)
        for p, t in self._limit(sortable, key=lambda x: x[1], S=S, name=lambda x: x[0]):
            for desc in (False, True):
                spec = [(p, desc, t)]
                self.check(S.name, "orderby", f"$orderby={p} {'desc' if desc else 'asc'}", S.name,
                           {"$orderby": f"{p} desc" if desc else f"{p} asc", "$top": n},
                           validate=self.rows_check(S.T.name, top=n, extra=lambda rows, r, spec=spec: self.order_notes(rows, spec)))
        if len(sortable) >= 2:
            (a, ta), (b, tb) = sortable[0], sortable[1]
            spec = [(a, False, ta), (b, True, tb)]
            self.check(S.name, "orderby", f"$orderby={a} asc,{b} desc", S.name, {"$orderby": f"{a} asc,{b} desc", "$top": n},
                       validate=self.rows_check(S.T.name, top=n, extra=lambda rows, r: self.order_notes(rows, spec)))

    def t_expand(self, S):
        if not S.caps.expandable or not S.T.navs:
            return
        k = min(3, self.o.sample)
        for idx, nav in enumerate(S.T.navs.values()):
            if nav.name in S.caps.non_expandable:
                continue
            tT = self.m.types.get(nav.type)

            def base(rows, r, nav=nav):
                S.expanded[nav.name] = rows
                return self.nav_checks(S.T, nav, rows)
            self.check(S.name, "expand", f"$expand={nav.name}", S.name, {"$top": k, "$expand": nav.name},
                       validate=self.rows_check(S.T.name, expand=(nav.name,), top=k, extra=base))
            if not self.v4 or tT is None or (idx >= 2 and not self.thorough):
                continue
            tsc = self.scalars(tT)
            if nav.collection:
                self.check(S.name, "expand", f"$expand={nav.name}($top=2)", S.name, {"$top": k, "$expand": f"{nav.name}($top=2)"},
                           validate=self.rows_check(S.T.name, expand=(nav.name,), extra=lambda rows, r, nav=nav: [
                               (FAIL, f"nested $top=2 returned {len(x.get(nav.name) or [])} items")
                               for x in rows if len(x.get(nav.name) or []) > 2]))
                if tT.keys:
                    kk = tT.keys[0]
                    kt = tsc.get(kk, ("Edm.String",))[0]
                    self.check(S.name, "expand", f"$expand={nav.name}($orderby={kk} desc)", S.name,
                               {"$top": k, "$expand": f"{nav.name}($orderby={kk} desc)"},
                               validate=self.rows_check(S.T.name, expand=(nav.name,), extra=lambda rows, r, nav=nav, kk=kk, kt=kt: [
                                   n for x in rows for n in self.order_notes(x.get(nav.name) or [], [(kk, True, kt)], nav.name)]))

                def cnt(rows, r, nav=nav):
                    return [(FAIL, f"missing or wrong {nav.name}@odata.count") for x in rows
                            if not isinstance(x.get(f"{nav.name}@odata.count"), int)
                            or x[f"{nav.name}@odata.count"] < len(x.get(nav.name) or [])]
                self.check(S.name, "expand", f"$expand={nav.name}($count=true)", S.name, {"$top": k, "$expand": f"{nav.name}($count=true)"},
                           validate=self.rows_check(S.T.name, expand=(nav.name,), extra=cnt), optional=True)
                items = [it for x in S.expanded.get(nav.name, []) for it in (x.get(nav.name) or [])]
                fp = next(((p, it[p]) for it in items for p in tsc if it.get(p) is not None and p not in tT.keys), None)
                if fp:
                    e = self.X.cmp(fp[0], "eq", fp[1], *tsc[fp[0]][:2])
                    if e:
                        self.check(S.name, "expand", f"$expand={nav.name}($filter={e.text})", S.name,
                                   {"$top": k, "$expand": f"{nav.name}($filter={e.text})"},
                                   validate=self.rows_check(S.T.name, expand=(nav.name,), extra=lambda rows, r, nav=nav, e=e: [
                                       (FAIL, f"nested $filter item does not satisfy {e.text}")
                                       for x in rows for it in (x.get(nav.name) or []) if not e.fn(it, True)]))
            if tsc:
                sp = next(iter(tsc))
                self.check(S.name, "expand", f"$expand={nav.name}($select={sp})", S.name, {"$top": k, "$expand": f"{nav.name}($select={sp})"},
                           validate=self.rows_check(S.T.name, expand=(nav.name,), extra=lambda rows, r, nav=nav, sp=sp: [
                               n for x in rows for it in ((x.get(nav.name) or []) if nav.collection else [x.get(nav.name)])
                               if it for n in self.validate_struct(nav.type, it, nav.name, select=[sp])]))
            for nav2 in list(tT.navs.values())[:3]:
                self.check(S.name, "expand", f"$expand={nav.name}($expand={nav2.name})", S.name,
                           {"$top": k, "$expand": f"{nav.name}($expand={nav2.name})"},
                           validate=self.rows_check(S.T.name, expand=(nav.name,), extra=lambda rows, r, nav=nav, nav2=nav2, tT=tT: [
                               n for x in rows for it in ((x.get(nav.name) or []) if nav.collection else [x.get(nav.name)])
                               if it for n in self.nav_checks(tT, nav2, [it])]))
        if len(S.T.navs) > 1:
            all_navs = ",".join(n for n in S.T.navs if n not in S.caps.non_expandable)
            self.check(S.name, "expand", "$expand=<all navigations>", S.name, {"$top": k, "$expand": all_navs},
                       validate=self.rows_check(S.T.name, expand=tuple(S.T.navs), top=k))
        if self.v4 and self.thorough:
            self.check(S.name, "expand", "$expand=*", S.name, {"$top": k, "$expand": "*"},
                       validate=self.rows_check(S.T.name, expand=tuple(S.T.navs), top=k), optional=True)
            nav = next(iter(S.T.navs.values()))
            self.check(S.name, "expand", f"$expand={nav.name}/$ref", S.name, {"$top": k, "$expand": f"{nav.name}/$ref"}, optional=True)

    # ---------- filters ----------

    def run_filter(self, S, e, must_hit, optional=False, extra=None, exact=None, select=None, label=None):
        if e is None:
            return None
        params = {"$filter": e.text, "$top": self.o.sample}
        if S.caps.countable:
            params.update(self.cparam)
        params.update(extra or {})

        def v(rows, r):
            notes = []
            bad = [x for x in rows if not e.fn(x, False)]
            if bad:
                bad_ci = [x for x in bad if not e.fn(x, True)]
                if bad_ci:
                    show = {p: pick(bad_ci[0], p) for p in list(S.T.keys) + sorted(e.props)}
                    notes.append((FAIL, f"{len(bad_ci)}/{len(rows)} rows do not satisfy the filter, e.g. {show}"))
                else:
                    notes.append((WARN, f"{len(bad)} rows match only case-insensitively (database collation)"))
            if must_hit and not rows:
                notes.append((FAIL, "0 rows, but sampled data contains a matching row"))
            if exact is not None and [self.key_of(S.T, x) for x in rows] != exact:
                notes.append((FAIL, f"expected exactly {exact}, got {[self.key_of(S.T, x) for x in rows][:5]}"))
            c = self.count_of(r)
            if c is not None and c < len(rows):
                notes.append((FAIL, f"count {c} < rows returned {len(rows)}"))
            return notes
        r, _ = self.check(S.name, "filter", label or e.text, S.name, params, optional=optional,
                          validate=self.rows_check(S.T.name, select=select, expand=tuple((extra or {}).get("$expand", "").split(",")) if extra else (),
                                                   top=self.o.sample, extra=v))
        return self.count_of(r) if r is not None and r.status == 200 else None

    def t_filter(self, S):
        if not S.caps.filterable or not S.rows:
            return
        X, rows = self.X, S.rows
        props = [(p, v) for p, v in S.scalars.items() if p not in S.caps.non_filterable
                 and v[0] not in ("Edm.Stream", "Edm.Binary") and not v[0].startswith("Edm.Geo")]
        for p, (t, enum, prop) in self._limit(props, key=lambda x: x[1][0], S=S, name=lambda x: x[0]):
            vals = [x.get(p) for x in rows if x.get(p) is not None]
            if not vals:
                if prop.nullable:
                    self.run_filter(S, X.cmp(p, "eq", None, t, enum), True)
                continue
            v = Counter(vals).most_common(1)[0][0]
            c_eq = self.run_filter(S, X.cmp(p, "eq", v, t, enum), True)
            c_ne = self.run_filter(S, X.cmp(p, "ne", v, t, enum), False)
            if prop.nullable:
                c_null = self.run_filter(S, X.cmp(p, "eq", None, t, enum), False)
                self.run_filter(S, X.cmp(p, "ne", None, t, enum), True)
                gap = S.total - c_eq - c_ne if None not in (c_eq, c_ne, S.total) else 0
                if gap > 0 and c_null == gap:  # live data drifts mid-run: only a gap the null rows exactly explain counts
                    S.ne_gaps.append(f"{p} ({gap})")
            self._typed_filters(S, p, t, enum, v, vals)
        if S.ne_gaps:
            self._add(Result(S.name, "filter", "'ne' drops null rows (SQL null semantics)", outcome=WARN,
                             notes=[(WARN, "OData says 'null ne x' is true, but 'ne' omits null rows for: " + ", ".join(S.ne_gaps))]))
        self._structured_filters(S)
        self._nav_filters(S)

    def _typed_filters(self, S, p, t, enum, v, vals):
        X, run = self.X, self.run_filter
        if not enum and t in edm.RANGED:
            for op in ("gt", "lt"):
                run(S, X.cmp(p, op, v, t), False)
            for op in ("ge", "le"):
                run(S, X.cmp(p, op, v, t), True)
            srt = sorted(set(vals), key=lambda x: edm.to_py(x, t))
            lo, hi = X.cmp(p, "ge", srt[0], t), X.cmp(p, "le", srt[-1], t)
            if lo and hi:
                run(S, lo & hi, True)
        distinct = list(dict.fromkeys(vals))
        if self.v4 and len(distinct) >= 2:
            run(S, X.in_(p, distinct[:3], t, enum), True, optional=True)
        if t == "Edm.Boolean":
            run(S, X.cmp(p, "eq", not v, t), False)
            run(S, ~X.cmp(p, "eq", v, t), False)
        if t == "Edm.String" and isinstance(v, str) and v:
            n = len(v)
            k = max(1, min(3, n // 2))
            mid = v[n // 4: n // 4 + max(1, n // 2)]
            lit = lambda s: edm.literal(s, "Edm.String", self.v4)
            run(S, X.strfn("contains", p, mid), True)
            run(S, X.strfn("startswith", p, v[:k]), True)
            run(S, X.strfn("endswith", p, v[-k:]), True)
            run(S, ~X.strfn("contains", p, mid), False)
            run(S, X.xform(f"tolower({p})", p, str.lower, "eq", v.lower(), lit(v.lower())), True)
            if self.thorough:
                run(S, X.xform(f"toupper({p})", p, str.upper, "eq", v.upper(), lit(v.upper())), True)
                run(S, X.xform(f"length({p})", p, len, "eq", n, str(n)), True)
            if self.v4 and self.thorough:
                run(S, X.xform(f"indexof({p},{lit(mid)})", p, lambda s, m=mid: s.find(m), "ge", 0, "0"), True, optional=True)
                run(S, X.xform(f"trim({p})", p, str.strip, "eq", v.strip(), lit(v.strip())), True, optional=True)
                run(S, X.xform(f"concat({p},'~')", p, lambda s: s + "~", "eq", v + "~", lit(v + "~")), True, optional=True)
                if n > 1:
                    run(S, X.xform(f"substring({p},1)", p, lambda s: s[1:], "eq", v[1:], lit(v[1:])), True, optional=True)
        if self.v4 and t in ("Edm.DateTimeOffset", "Edm.Date"):
            d = edm.to_py(v, t)
            parts = ("year", "month", "day") + (("hour", "minute") if t == "Edm.DateTimeOffset" and self.thorough else ())
            for fn in parts:
                val = getattr(d, fn)
                run(S, X.xform(f"{fn}({p})", p, lambda s, fn=fn, t=t: getattr(edm.to_py(s, t), fn), "eq", val, str(val)), True)
            if t == "Edm.DateTimeOffset" and self.thorough:
                run(S, X.xform(f"date({p})", p, lambda s: edm.to_py(s, t).date(), "eq", d.date(), d.date().isoformat()), True, optional=True)
                limit = datetime.now(timezone.utc) + timedelta(minutes=5)  # tolerate clock skew
                run(S, Expr(f"{p} le now()", lambda r, ci: pick(r, p) is not None and edm.to_py(pick(r, p), t) <= limit, [p]),
                    False, optional=True)
        if self.v4 and self.thorough and t in edm.NUMERIC and not isinstance(v, bool):
            pv = edm.to_py(v, t)
            lit = edm.literal(v, t, True)
            run(S, X.xform(f"{p} add 1", p, lambda s, t=t: edm.to_py(s, t) + 1, "gt", pv, lit), True, optional=True)
            if t in edm.INT_RANGES:
                run(S, X.xform(f"{p} mod 2", p, lambda s, t=t: int(math.fmod(edm.to_py(s, t), 2)), "eq",
                               int(math.fmod(pv, 2)), str(int(math.fmod(pv, 2)))), True, optional=True)
            else:
                fl = math.floor(pv)
                run(S, X.xform(f"floor({p})", p, lambda s, t=t: math.floor(edm.to_py(s, t)), "eq", fl, str(fl)), True, optional=True)

    def _structured_filters(self, S):
        """Complex-property paths, and any() over collections of complex/primitive values."""
        for p in S.T.properties.values():
            t = self.m.type_defs.get(p.type, p.type)
            if p.name in S.caps.non_filterable:
                continue
            if t in self.m.types:
                sub = self.scalars(self.m.types[t])
                vals = [x for row in S.rows for x in ((row.get(p.name) or []) if p.collection else [row.get(p.name)]) if isinstance(x, dict)]
                hit = next(((sp, it[sp]) for it in vals for sp in sub if it.get(sp) is not None), None)
                if not hit:
                    continue
                sp, val = hit
                st, enum, _ = sub[sp]
                e = self.X.any_(p.name, sp, "eq", val, st, enum) if p.collection else self.X.cmp(f"{p.name}/{sp}", "eq", val, st, enum)
                self.run_filter(S, e, True, optional=p.collection)
            elif p.collection and (t.startswith("Edm.") or t in self.m.enums):
                val = next((x for row in S.rows for x in (row.get(p.name) or []) if x is not None), None)
                if val is not None:
                    self.run_filter(S, self.X.any_(p.name, None, "eq", val, t, t if t in self.m.enums else None), True, optional=True)

    def _nav_filters(self, S):
        """Filter by related data (Nav/Prop, Nav/any()) and verify locally with the same $expand."""
        for nav_name, erows in S.expanded.items():
            nav = S.T.navs[nav_name]
            tT = self.m.types.get(nav.type)
            if tT is None:
                continue
            tsc = self.scalars(tT)
            items = [it for x in erows for it in (((x.get(nav_name) or []) if nav.collection else [x.get(nav_name)])) if isinstance(it, dict)]
            done = 0
            for sp, (st, enum, _) in tsc.items():
                val = next((it[sp] for it in items if it.get(sp) is not None), None)
                if val is None or done >= 2:
                    continue
                e = self.X.any_(nav_name, sp, "eq", val, st, enum) if nav.collection else self.X.cmp(f"{nav_name}/{sp}", "eq", val, st, enum)
                if e is not None:
                    self.run_filter(S, e, True, extra={"$expand": nav_name})
                    done += 1

    def t_search(self, S):
        """$search only where SearchRestrictions declares it. Matching is server-defined, so results can't be
        re-evaluated locally: check the shape, the count bound, and that a word taken from real data finds something."""
        if not (self.v4 and S.caps.searchable):
            return
        word = next((w for p, (t, _, _) in S.scalars.items() if t == "Edm.String"
                     for x in S.rows if isinstance(x.get(p), str)
                     for w in x[p].split() if len(w) >= 3 and w.isalnum()), None)
        if not word:
            return

        def v(rows, r):
            c = self.count_of(r)
            notes = [(FAIL, f"search count {c} > total {S.total}")] if None not in (c, S.total) and c > S.total else []
            return notes + ([] if rows else [(WARN, f"$search={word} found nothing although '{word}' occurs in sampled data")])
        self.check(S.name, "search", f"$search={word}", S.name, {"$search": word, "$top": self.o.sample, **self.cparam},
                   validate=self.rows_check(S.T.name, top=self.o.sample, extra=v))

    def t_combos(self, S):
        if not S.caps.filterable or not S.rows:
            return
        X = self.X
        cand = [p for p, (t, _, _) in S.scalars.items() if p not in S.T.keys and p not in S.caps.non_filterable
                and t in ("Edm.String", "Edm.Boolean", *edm.INT_RANGES)]
        r0 = next((x for x in S.rows if sum(x.get(p) is not None for p in cand) >= 2), None)
        if r0 is not None:
            a, b = [p for p in cand if r0.get(p) is not None][:2]
            ea, eb = X.cmp(a, "eq", r0[a], *S.scalars[a][:2]), X.cmp(b, "eq", r0[b], *S.scalars[b][:2])
            if ea and eb:
                self.run_filter(S, ea & eb, True)
                self.run_filter(S, ea | eb, True)
                self.run_filter(S, ~ea, False)
                self.run_filter(S, (ea | eb) & ~X.cmp(a, "eq", edm.fake_value("Edm.String") if S.scalars[a][0] == "Edm.String" else r0[a], *S.scalars[a][:2]),
                                S.scalars[a][0] == "Edm.String")
                sortable = dict(self._sortable(S))
                if b in sortable and S.caps.sortable:
                    sel = list(dict.fromkeys(list(S.T.keys) + [a, b]))
                    spec = [(b, True, sortable[b])]
                    params = {"$orderby": f"{b} desc", "$select": ",".join(sel), "$top": 5}
                    e = ea

                    def combo(rows, r):
                        return self.order_notes(rows, spec) + ([(FAIL, f"{len(rows)} rows > $top=5")] if len(rows) > 5 else [])
                    self.check(S.name, "combo", f"$filter+$orderby+$select+$top+$count ({a}, {b})", S.name,
                               {"$filter": e.text, **params, **(self.cparam if S.caps.countable else {})},
                               validate=self.rows_check(S.T.name, select=sel, extra=lambda rows, r: combo(rows, r) + [
                                   (FAIL, "row does not satisfy filter") for x in rows if not e.fn(x, True)]))
        if S.T.keys:
            for row in S.rows[:2]:
                ek = None
                for k in S.T.keys:
                    st = S.scalars.get(k)
                    part = X.cmp(k, "eq", row.get(k), *st[:2]) if st and row.get(k) is not None else None
                    if part is None:
                        ek = None
                        break
                    ek = part if ek is None else ek & part
                if ek:
                    self.run_filter(S, ek, True, exact=[self.key_of(S.T, row)], label=f"key filter {ek.text}")

    # ---------- key access ----------

    def t_keys(self, S):
        if not S.T.keys or not S.rows:
            return
        if not self.key_works(S.name, S.T, S.rows[0]):
            return  # recorded once above; every other key-based request would fail the same way
        for row in S.rows[1: self.o.key_rows]:
            seg = self.key_seg(S.T, row)
            if seg:
                self.check(S.name, "key", f"get by key {seg}", S.name + seg, validate=self.entity_check(S.T, row))
        row = S.rows[0]
        seg = self.key_seg(S.T, row)
        if not seg:
            return
        k0 = S.T.keys[0]
        if self.v4 and self.thorough and len(S.T.keys) == 1:
            self.check(S.name, "key", f"named key {S.name}({k0}=...)", f"{S.name}({k0}={seg[1:-1]})",
                       validate=self.entity_check(S.T, row), optional=True)
            self.check(S.name, "key", "key-as-segment", f"{S.name}/{qkey(row[k0])}", validate=self.entity_check(S.T, row), optional=True)
        sel = [p for p in S.scalars if p not in S.T.keys][:2]
        if sel:
            self.check(S.name, "key", f"{seg}?$select={','.join(sel)}", S.name + seg, {"$select": ",".join(sel)},
                       validate=self.entity_check(S.T, select=sel))
        for nav in S.T.navs.values():
            self.check(S.name, "key", f"{seg}?$expand={nav.name}", S.name + seg, {"$expand": nav.name},
                       validate=lambda r, nav=nav: self.nav_checks(S.T, nav, [self.entity(r) or {}])
                       + self.entity_check(S.T, row, expand=(nav.name,))(r))
        for p in [p for p in S.scalars if p not in S.T.keys and row.get(p) is not None][:2]:
            t = S.scalars[p][0]

            def prop(r, p=p, t=t):
                j = r.json if isinstance(r.json, dict) else {}
                val = j.get("value", (j.get("d") or {}).get(p) if isinstance(j.get("d"), dict) else None)
                try:
                    return [] if edm.to_py(val, t) == edm.to_py(row[p], t) else [(WARN, f"value {val!r} differs from collection {row[p]!r}")]
                except (ValueError, TypeError, ArithmeticError):
                    return [(FAIL, f"invalid property payload {val!r}")]
            self.check(S.name, "key", f"property {seg}/{p}", f"{S.name}{seg}/{p}", validate=prop, optional=True)
            self.thorough and self.check(S.name, "key", f"raw value {seg}/{p}/$value", f"{S.name}{seg}/{p}/$value", headers={"Accept": "text/plain"},
                       optional=True, validate=lambda r, p=p: [] if r.text.strip() == str(row[p]).strip() or t != "Edm.String"
                       else [(WARN, f"$value {r.text[:40]!r} != {row[p]!r}")])
        for nav in S.T.navs.values():
            def navv(r, nav=nav):
                if nav.collection:
                    return self.rows_check(nav.type)(r)
                return self.validate_struct(nav.type, self.entity(r), nav.name) if r.text.strip() else []
            self.check(S.name, "key", f"navigation path {seg}/{nav.name}", f"{S.name}{seg}/{nav.name}", validate=navv, optional=True)
        fake = {}
        for k in S.T.keys:
            fv = edm.fake_value(S.scalars.get(k, ("Edm.String",))[0])
            if fv is None:
                fake = None
                break
            fake[k] = fv
        if fake:
            fseg = self.key_seg(S.T, fake)
            if fseg:
                self.check(S.name, "key", f"non-existent key {fseg} -> 404", S.name + fseg, expect="notfound")
        if self.o.negative:
            bad = f"({seg[1:-1]},{seg[1:-1]})" if len(S.T.keys) == 1 else f"({S.T.keys[0]}={seg[1:-1].split(',')[0].split('=', 1)[-1]})"
            self.check(S.name, "negative", f"malformed key {bad}", S.name + bad, expect="error")

    # ---------- crawl ----------

    def t_crawl(self, S):
        if self.o.depth <= 0 or not S.T.keys or not S.rows or not self.key_works(S.name, S.T, S.rows[0]):
            return
        frontier = []
        for row in S.rows[: self.o.roots]:
            seg = self.key_seg(S.T, row)
            if seg:
                with self._lock:
                    self.visited.add((S.name, seg))
                frontier.append((S.name, row, 0))
        while frontier:
            sname, row, d = frontier.pop(0)
            es = self.m.sets[sname]
            T = self.m.types[es.type]
            seg = self.key_seg(T, row)
            if not seg or d >= self.o.depth:
                continue
            for nav in T.navs.values():
                if nav.name in self.m.caps(sname).non_expandable:
                    continue
                r, _ = self.check(S.name, "crawl", f"d{d + 1} {sname}{seg}?$expand={nav.name}", sname + seg, {"$expand": nav.name},
                                  validate=lambda r, nav=nav, T=T: self.nav_checks(T, nav, [self.entity(r) or {}]))
                ent = self.entity(r) if r is not None and r.status == 200 else None
                if not ent:
                    continue
                val = ent.get(nav.name)
                items = [x for x in ((val or []) if nav.collection else [val]) if isinstance(x, dict)]
                tset = es.bindings.get(nav.name)
                if not tset or tset not in self.m.sets:
                    continue
                tT = self.m.types[self.m.sets[tset].type]
                for it in items[: self.o.width]:
                    cseg = self.key_seg(tT, it)
                    if not cseg:
                        continue
                    with self._lock:
                        if (tset, cseg) in self.visited:
                            continue
                        self.visited.add((tset, cseg))
                    origin = f"{sname}{seg}/{nav.name}"
                    if not self.key_works(tset, tT, it):  # target set has no key route: its finding is recorded once
                        continue
                    rr, res = self.check(S.name, "crawl", f"d{d + 1} resolve {tset}{cseg} (from {origin})", tset + cseg,
                                       validate=self.entity_check(tT, it, origin=f"$expand on {origin}"))
                    if rr is None or rr.status != 200:
                        if rr is not None and rr.status == 404:
                            res.notes.insert(0, (FAIL, f"dangling: reachable via {origin} but not addressable by key in {tset}"))
                        continue  # do not crawl further from an entity the service cannot address
                    frontier.append((tset, it, d + 1))
                pairs, kind = self.m.fk_pairs(T.name, nav)
                if nav.collection and pairs and self.m.caps(tset).filterable:
                    tsc = self.scalars(tT)
                    e = None
                    for sp, tp in pairs:
                        if row.get(sp) is None or tp not in tsc:
                            e = None
                            break
                        part = self.X.cmp(tp, "eq", row[sp], *tsc[tp][:2])
                        e = part if e is None else e & part
                    if e is None:
                        continue
                    want = {self.key_of(tT, x) for x in items}
                    lvl = FAIL if kind == "declared" else WARN

                    def rev(r, want=want, lvl=lvl, origin=f"{sname}{seg}/{nav.name}", kind=kind):
                        got = {self.key_of(tT, x) for x in self.rows(r) or []}
                        notes = []
                        if want - got:
                            notes.append((lvl, f"{len(want - got)} related rows from $expand on {origin} not found by the {kind} FK filter"))
                        if got - want and not self.next_link(r):
                            notes.append((WARN, f"FK filter finds {len(got - want)} rows that $expand on {origin} does not return"))
                        return notes
                    self.check(S.name, "crawl", f"d{d + 1} reverse {tset}?$filter={e.text}", tset,
                               {"$filter": e.text, "$top": max(100, len(items) * 2)}, validate=rev)

    # ---------- negative / security ----------

    def t_negative(self, S):
        n = S.name
        bad = [("unknown $select property", {"$select": "OdhNoSuchProperty"}),
               ("unknown $filter property", {"$filter": "OdhNoSuchProperty eq 1"}),
               ("malformed $filter", {"$filter": "((("}),
               ("unknown $orderby property", {"$orderby": "OdhNoSuchProperty"}),
               ("unknown $expand navigation", {"$expand": "OdhNoSuchNavigation"}),
               ("$top=-1", {"$top": "-1"}), ("$skip=-1", {"$skip": "-1"}), ("$top=abc", {"$top": "abc"})]
        num = next((p for p, (t, _, _) in S.scalars.items() if t in edm.NUMERIC), None)
        if num:
            bad.append((f"type mismatch {num} eq 'odh'", {"$filter": f"{num} eq 'odh'"}))
        dt = next((p for p, (t, _, _) in S.scalars.items() if t == "Edm.DateTimeOffset"), None)
        if dt:
            bad.append((f"invalid date literal {dt} gt 2024-13-45", {"$filter": f"{dt} gt 2024-13-45T99:00:00Z"}))
        for name, params in bad:
            self.check(n, "negative", name, n, params, expect="error")
        self.check(n, "negative", "unknown system query option $odhbogus", n, {"$odhbogus": "1", "$top": 1}, expect="error_soft")
        s = next((p for p, (t, _, _) in S.scalars.items() if t == "Edm.String" and p not in S.caps.non_filterable), None)
        if s:
            e = self.X.cmp(s, "eq", "odh' or 1 eq 1 or 'a' eq 'a", "Edm.String")
            self.run_filter(S, e, False, label=f"quote escaping / injection on {s}")
        self.check(n, "security", "no Authorization header", n, {"$top": 1}, expect="denied", auth=False)
        self.check(n, "security", "invalid bearer token", n, {"$top": 1}, expect="denied", token="odh.invalid.token")
        tok = self.c.token or ""
        if tok.count(".") == 2:
            tampered = tok[:-6] + ("AAAAAA" if not tok.endswith("AAAAAA") else "BBBBBB")
            self.check(n, "security", "tampered token signature", n, {"$top": 1}, expect="denied", token=tampered)
