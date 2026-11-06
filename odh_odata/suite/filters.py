"""$filter: every operator/function valid for the type, structured and navigation paths, combinations."""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from .. import edm
from .core import pick, Result, Expr, WARN, FAIL


class FilterTests:
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
