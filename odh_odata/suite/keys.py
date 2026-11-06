"""Key access and the navigation crawl."""
from __future__ import annotations

from .. import edm
from .core import qkey, WARN, FAIL


class KeyTests:
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
