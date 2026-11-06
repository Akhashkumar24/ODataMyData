"""Invalid requests and credential checks."""
from __future__ import annotations

from .. import edm


class NegativeTests:
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
