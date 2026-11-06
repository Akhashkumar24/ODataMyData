"""Result model, outcome levels, and $filter expressions with local evaluators."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import quote
from .. import edm

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
    baseline: str | None = None  # "new" when --baseline is used and this finding is not in it


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
    unsupported_fns: set = field(default_factory=set)  # filter functions skipped because FilterFunctions omits them
