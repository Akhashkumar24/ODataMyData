"""Per-set reads: sample, count, paging, $select, $orderby, $search."""
from __future__ import annotations

from collections import Counter
from .. import edm
from .core import WARN, FAIL


class ReadTests:
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
        seen, url, pages = set(), S.name, 0
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
                       validate=self.rows_check(S.T.name, select=[p], top=k), prop=p)
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
                           validate=self.rows_check(S.T.name, top=n, extra=lambda rows, r, spec=spec: self.order_notes(rows, spec)),
                           prop=p)
        if len(sortable) >= 2:
            (a, ta), (b, tb) = sortable[0], sortable[1]
            spec = [(a, False, ta), (b, True, tb)]
            self.check(S.name, "orderby", f"$orderby={a} asc,{b} desc", S.name, {"$orderby": f"{a} asc,{b} desc", "$top": n},
                       validate=self.rows_check(S.T.name, top=n, extra=lambda rows, r: self.order_notes(rows, spec)))

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
