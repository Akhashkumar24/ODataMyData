"""Service-level checks: service document, $metadata, formats, functions."""
from __future__ import annotations

from .core import PASS, SKIP, Result, WARN, FAIL


class ServiceTests:
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
