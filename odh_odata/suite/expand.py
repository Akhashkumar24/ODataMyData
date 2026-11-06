"""$expand and its nested query options."""
from __future__ import annotations

from .core import FAIL


class ExpandTests:
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
                return self.nav_checks(S.T, nav, rows, require=False)
            self.check(S.name, "expand", f"$expand={nav.name}", S.name, {"$top": k, "$expand": nav.name},
                       validate=self.rows_check(S.T.name, expand=(nav.name,), top=k, extra=base))
            if not self.v4 or tT is None or (idx >= 2 and not self.thorough):
                continue
            tsc = self.scalars(tT)
            nc = S.caps.nav(nav.name)  # NavigationRestrictions for queries through this navigation
            if nav.collection and nc.top:
                self.check(S.name, "expand", f"$expand={nav.name}($top=2)", S.name, {"$top": k, "$expand": f"{nav.name}($top=2)"},
                           validate=self.rows_check(S.T.name, expand=(nav.name,), extra=lambda rows, r, nav=nav: [
                               (FAIL, f"nested $top=2 returned {len(x.get(nav.name) or [])} items")
                               for x in rows if len(x.get(nav.name) or []) > 2]))
            if nav.collection:
                if tT.keys and nc.sortable and tT.keys[0] not in nc.non_sortable:
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
                if nc.countable:
                    self.check(S.name, "expand", f"$expand={nav.name}($count=true)", S.name, {"$top": k, "$expand": f"{nav.name}($count=true)"},
                               validate=self.rows_check(S.T.name, expand=(nav.name,), extra=cnt), optional=True)
                items = [it for x in S.expanded.get(nav.name, []) for it in (x.get(nav.name) or [])]
                fp = nc.filterable and next(((p, it[p]) for it in items for p in tsc
                                             if it.get(p) is not None and p not in tT.keys and p not in nc.non_filterable), None)
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
            two_levels = S.caps.max_levels is None or S.caps.max_levels >= 2
            for nav2 in list(tT.navs.values())[:3] if two_levels and nc.expandable else []:
                self.check(S.name, "expand", f"$expand={nav.name}($expand={nav2.name})", S.name,
                           {"$top": k, "$expand": f"{nav.name}($expand={nav2.name})"},
                           validate=self.rows_check(S.T.name, expand=(nav.name,), extra=lambda rows, r, nav=nav, nav2=nav2, tT=tT: [
                               n for x in rows for it in ((x.get(nav.name) or []) if nav.collection else [x.get(nav.name)])
                               if it for n in self.nav_checks(tT, nav2, [it])]))
        expandable = tuple(n for n in S.T.navs if n not in S.caps.non_expandable)
        if len(expandable) > 1:
            self.check(S.name, "expand", "$expand=<all navigations>", S.name, {"$top": k, "$expand": ",".join(expandable)},
                       validate=self.rows_check(S.T.name, expand=expandable, top=k))
        if self.v4 and self.thorough:
            self.check(S.name, "expand", "$expand=*", S.name, {"$top": k, "$expand": "*"},
                       validate=self.rows_check(S.T.name, expand=expandable, top=k), optional=True)
            nav = next(iter(S.T.navs.values()))
            self.check(S.name, "expand", f"$expand={nav.name}/$ref", S.name, {"$top": k, "$expand": f"{nav.name}/$ref"}, optional=True)
