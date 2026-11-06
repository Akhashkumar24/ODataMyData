"""CSDL / EDMX metadata parser for OData v2, v3 and v4.

parse(xml) -> Model: flattened entity/complex types (inheritance resolved), enums, type definitions,
entity sets and singletons with navigation bindings, referential constraints (declared or inferred),
functions/actions/imports, capability annotations, and a list of consistency problems in the document.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from decimal import Decimal
from xml.sax.saxutils import quoteattr

CAP = "Org.OData.Capabilities.V1."
_LEAF = ("Bool", "String", "Int", "Float", "Decimal", "EnumMember", "Path", "PropertyPath",
         "NavigationPropertyPath", "AnnotationPath", "Date", "DateTimeOffset", "Guid", "Duration", "TimeOfDay")


def _l(tag):
    return tag.rsplit("}", 1)[-1]


def _kids(el, *names):
    return [c for c in el if _l(c.tag) in names]


def _attr(el, name, default=None):
    """Attribute lookup ignoring XML namespace (v2 puts some on m:/sap: namespaces)."""
    if name in el.attrib:
        return el.attrib[name]
    for k, v in el.attrib.items():
        if _l(k) == name:
            return v
    return default


def _bool(v, default):
    return default if v is None else v.strip().lower() == "true"


def split_collection(t):
    if t and t.startswith("Collection(") and t.endswith(")"):
        return t[11:-1], True
    return t, False


@dataclass
class Property:
    name: str
    type: str
    nullable: bool = True
    collection: bool = False
    max_length: str | None = None
    precision: str | None = None
    scale: str | None = None


@dataclass
class NavProperty:
    name: str
    type: str
    collection: bool
    nullable: bool = True
    partner: str | None = None
    constraints: dict = field(default_factory=dict)  # dependent prop (this type) -> principal prop (target type)
    contains_target: bool = False


@dataclass
class StructuredType:
    name: str
    kind: str  # "entity" | "complex"
    keys: list
    properties: dict
    navs: dict
    base: str | None = None
    abstract: bool = False
    open: bool = False
    has_stream: bool = False

    @property
    def short(self):
        return self.name.rsplit(".", 1)[-1]


@dataclass
class EnumType:
    name: str
    members: dict
    flags: bool = False
    underlying: str = "Edm.Int32"


@dataclass
class Parameter:
    name: str
    type: str
    collection: bool = False
    nullable: bool = True


@dataclass
class Operation:
    name: str  # qualified
    kind: str  # "function" | "action"
    bound: bool = False
    params: list = field(default_factory=list)
    return_type: str | None = None
    return_collection: bool = False
    composable: bool = False


@dataclass
class Import:
    name: str
    kind: str
    operation: str
    entity_set: str | None = None
    in_service_document: bool = True


@dataclass
class EntitySet:
    name: str
    type: str
    bindings: dict = field(default_factory=dict)  # nav path -> target entity set
    singleton: bool = False


@dataclass
class Caps:
    """Capability vocabulary restrictions (defaults = everything supported)."""
    countable: bool = True
    filterable: bool = True
    sortable: bool = True
    expandable: bool = True
    top: bool = True
    skip: bool = True
    searchable: bool = False
    requires_filter: bool = False
    non_filterable: set = field(default_factory=set)
    non_sortable: set = field(default_factory=set)
    non_expandable: set = field(default_factory=set)
    required_filter: set = field(default_factory=set)


@dataclass
class Model:
    version: str
    alias: dict
    types: dict
    enums: dict
    type_defs: dict
    sets: dict
    operations: list
    imports: list
    container: str | None
    set_annotations: dict
    problems: list = field(default_factory=list)
    inferred: bool = False  # True when built from sample data because $metadata was unavailable

    @property
    def v4(self):
        return self.version.startswith("4")

    def qn(self, name):
        """Normalise a (possibly aliased) qualified name."""
        if not name:
            return name
        t, coll = split_collection(name)
        if "." in t:
            pre, last = t.rsplit(".", 1)
            t = f"{self.alias.get(pre, pre)}.{last}"
        return f"Collection({t})" if coll else t

    def is_derived(self, a, b):
        seen = set()
        while a and a not in seen:
            if a == b:
                return True
            seen.add(a)
            a = self.types[a].base if a in self.types else None
        return False

    def scalar_type(self, prop):
        """(edm_type, enum_qn) for a single-valued primitive/enum property, else None."""
        if prop.collection:
            return None
        t = self.type_defs.get(prop.type, prop.type)
        if t in self.enums:
            return t, t
        return (t, None) if t.startswith("Edm.") else None

    def caps(self, set_name):
        a = self.set_annotations.get(set_name, {})
        c = Caps()

        def rec(term):
            v = a.get(CAP + term)
            return v if isinstance(v, dict) else {}

        def paths(v):
            return {str(x) for x in v} if isinstance(v, list) else set()

        c.countable = rec("CountRestrictions").get("Countable", True) is not False
        f = rec("FilterRestrictions")
        c.filterable = f.get("Filterable", True) is not False
        c.requires_filter = f.get("RequiresFilter") is True
        c.non_filterable = paths(f.get("NonFilterableProperties"))
        c.required_filter = paths(f.get("RequiredProperties"))
        s = rec("SortRestrictions")
        c.sortable = s.get("Sortable", True) is not False
        c.non_sortable = paths(s.get("NonSortableProperties"))
        e = rec("ExpandRestrictions")
        c.expandable = e.get("Expandable", True) is not False
        c.non_expandable = paths(e.get("NonExpandableProperties"))
        c.top = a.get(CAP + "TopSupported", True) is not False
        c.skip = a.get(CAP + "SkipSupported", True) is not False
        c.searchable = rec("SearchRestrictions").get("Searchable") is True
        return c

    def fk_pairs(self, src_qn, nav):
        """[(src_prop, tgt_prop)] meaning row[src_prop] == related[tgt_prop], plus 'declared'|'inferred'|None."""
        src, tgt = self.types.get(src_qn), self.types.get(nav.type)
        if not src or not tgt:
            return [], None
        if nav.constraints:
            return list(nav.constraints.items()), "declared"
        partner = tgt.navs.get(nav.partner) if nav.partner else None
        back = [partner] if partner else [n for n in tgt.navs.values()
                                          if self.is_derived(src_qn, n.type) and not n.collection and n.constraints]
        if len(back) == 1 and back[0].constraints:
            return [(prin, dep) for dep, prin in back[0].constraints.items()], "declared"
        # Inference from naming conventions, only when every key part maps unambiguously.
        if not nav.collection:  # FK lives on the source: <Nav><Key>, <Nav>_<Key>, or a key already prefixed by the target type
            pairs = []
            for k in tgt.keys:
                cands = [nav.name + k, f"{nav.name}_{k}"] + ([k] if k.startswith(tgt.short) else [])
                if len(tgt.keys) == 1:
                    cands.append(nav.name + "Id")
                hit = next((c for c in cands if c in src.properties), None)
                if not hit:
                    return [], None
                pairs.append((hit, k))
            return pairs, "inferred"
        pairs = []  # FK lives on the target: <SrcType><Key> or a key already prefixed by the source type
        for k in src.keys:
            cands = [src.short + k, f"{src.short}_{k}"] + ([k] if k.startswith(src.short) else [])
            hit = next((c for c in cands if c in tgt.properties), None)
            if not hit:
                return [], None
            pairs.append((k, hit))
        return pairs, "inferred"

    def summary(self):
        """JSON-friendly description of the model, used for --plan and the report."""
        out = {"version": self.version, "container": self.container, "entity_sets": {}, "operations": [],
               "imports": [vars(i) for i in self.imports], "problems": self.problems}
        for s in self.sets.values():
            T = self.types.get(s.type)
            if not T:
                continue
            navs = {}
            for n in T.navs.values():
                pairs, kind = self.fk_pairs(T.name, n)
                navs[n.name] = {"type": n.type, "collection": n.collection, "target_set": s.bindings.get(n.name),
                                "fk": [f"{a}->{b}" for a, b in pairs], "fk_kind": kind}
            caps = self.caps(s.name)
            out["entity_sets"][s.name] = {
                "type": s.type, "singleton": s.singleton, "keys": T.keys,
                "properties": {p.name: (f"Collection({p.type})" if p.collection else p.type) + ("" if p.nullable else " NOT NULL")
                               for p in T.properties.values()},
                "navigation": navs,
                "capabilities": {k: (sorted(v) if isinstance(v, set) else v) for k, v in vars(caps).items()},
            }
        for o in self.operations:
            out["operations"].append({"name": o.name, "kind": o.kind, "bound": o.bound,
                                      "params": [f"{p.name}:{p.type}" for p in o.params],
                                      "returns": o.return_type and (f"Collection({o.return_type})" if o.return_collection else o.return_type)})
        return out


class _Parser:
    def __init__(self, root):
        self.root = root
        self.alias = {}
        for el in root.iter():
            if _l(el.tag) in ("Include", "Schema") and el.get("Alias"):
                self.alias[el.get("Alias")] = el.get("Namespace")
        self.schemas = [s for s in root.iter() if _l(s.tag) == "Schema"]

    def qn(self, name):
        return Model.qn(self, name)  # same rules, uses self.alias

    def _annos(self, el):
        return {self.qn(a.get("Term")): self._aval(a) for a in _kids(el, "Annotation") if a.get("Term")}

    def _aval(self, el):
        for k in _LEAF:
            if el.get(k) is not None:
                return self._conv(k, el.get(k))
        kids = [c for c in el if _l(c.tag) != "Annotation"]
        return self._expr(kids[0]) if kids else True  # bare Bool-typed term defaults to true

    def _expr(self, e):
        t = _l(e.tag)
        if t == "Record":
            return {pv.get("Property"): self._aval(pv) for pv in _kids(e, "PropertyValue")}
        if t == "Collection":
            return [self._expr(c) for c in e]
        return self._conv(t, e.text or "") if t in _LEAF else e.text

    @staticmethod
    def _conv(kind, v):
        v = v.strip()
        if kind == "Bool":
            return v.lower() == "true"
        if kind == "Int":
            try:
                return int(v)
            except ValueError:
                return v
        return v

    def _prop(self, p):
        t, coll = split_collection(p.get("Type"))
        return Property(p.get("Name"), self.qn(t), _bool(p.get("Nullable"), True), coll,
                        p.get("MaxLength"), p.get("Precision"), p.get("Scale"))

    def parse(self):
        root = self.root
        version = root.get("Version", "4.0")
        ds = next((e for e in root.iter() if _l(e.tag) == "DataServices"), root)
        dsv = _attr(ds, "DataServiceVersion")
        if not version.startswith("4"):
            version = dsv or "2.0"

        raw, enums, tdefs, assocs, v2navs, ops = {}, {}, {}, {}, {}, []
        containers = []
        for s in self.schemas:
            ns = s.get("Namespace")
            for el in s:
                tag, name = _l(el.tag), el.get("Name")
                if tag in ("EntityType", "ComplexType"):
                    qn = f"{ns}.{name}"
                    keys = [pr.get("Name") for k in _kids(el, "Key") for pr in _kids(k, "PropertyRef")]
                    props = {p.get("Name"): self._prop(p) for p in _kids(el, "Property")}
                    navs = {}
                    for n in _kids(el, "NavigationProperty"):
                        if n.get("Type"):
                            t, coll = split_collection(n.get("Type"))
                            navs[n.get("Name")] = NavProperty(
                                n.get("Name"), self.qn(t), coll, _bool(n.get("Nullable"), True), n.get("Partner"),
                                {rc.get("Property"): rc.get("ReferencedProperty") for rc in _kids(n, "ReferentialConstraint")},
                                _bool(n.get("ContainsTarget"), False))
                        else:  # v2: resolved through Association below
                            v2navs[(qn, n.get("Name"))] = (self.qn(n.get("Relationship")), n.get("FromRole"), n.get("ToRole"))
                    raw[qn] = StructuredType(qn, "entity" if tag == "EntityType" else "complex", keys, props, navs,
                                             self.qn(el.get("BaseType")), _bool(el.get("Abstract"), False),
                                             _bool(el.get("OpenType"), False), _bool(_attr(el, "HasStream"), False))
                elif tag == "EnumType":
                    members, nxt = {}, 0
                    for m in _kids(el, "Member"):
                        val = int(m.get("Value")) if m.get("Value") is not None else nxt
                        members[m.get("Name")] = val
                        nxt = val + 1
                    enums[f"{ns}.{name}"] = EnumType(f"{ns}.{name}", members, _bool(el.get("IsFlags"), False),
                                                     el.get("UnderlyingType", "Edm.Int32"))
                elif tag == "TypeDefinition":
                    tdefs[f"{ns}.{name}"] = self.qn(el.get("UnderlyingType"))
                elif tag == "Association":
                    ends = {e.get("Role"): (self.qn(e.get("Type")), e.get("Multiplicity")) for e in _kids(el, "End")}
                    rc = next(iter(_kids(el, "ReferentialConstraint")), None)
                    cons = None
                    if rc is not None:
                        pr, de = _kids(rc, "Principal")[0], _kids(rc, "Dependent")[0]
                        cons = (pr.get("Role"), [x.get("Name") for x in _kids(pr, "PropertyRef")],
                                de.get("Role"), [x.get("Name") for x in _kids(de, "PropertyRef")])
                    assocs[f"{ns}.{name}"] = (ends, cons)
                elif tag in ("Function", "Action"):
                    params = []
                    for p in _kids(el, "Parameter"):
                        t, coll = split_collection(p.get("Type"))
                        params.append(Parameter(p.get("Name"), self.qn(t), coll, _bool(p.get("Nullable"), True)))
                    rt = next(iter(_kids(el, "ReturnType")), None)
                    rtt, rcoll = split_collection(rt.get("Type")) if rt is not None else (None, False)
                    ops.append(Operation(f"{ns}.{name}", tag.lower(), _bool(el.get("IsBound"), False), params,
                                         self.qn(rtt), rcoll, _bool(el.get("IsComposable"), False)))
                elif tag == "EntityContainer":
                    containers.append((ns, el))

        for (tqn, nname), (rel, frm, to) in v2navs.items():  # v2 navigation via associations
            if rel not in assocs or tqn not in raw:
                continue
            ends, cons = assocs[rel]
            ttype, mult = ends.get(to, (None, None))
            constraints = {}
            if cons and cons[2] == frm:  # this type is the dependent end
                constraints = dict(zip(cons[3], cons[1]))
            raw[tqn].navs[nname] = NavProperty(nname, ttype, mult == "*", mult != "1", None, constraints)
            raw[tqn].navs[nname].v2rel = (rel, frm, to)

        types = _flatten(raw)
        sets, imports, set_annos, cname = {}, [], {}, None
        for ns, c in containers:
            if cname and not _bool(_attr(c, "IsDefaultEntityContainer"), False):
                continue  # v2 may declare several containers; prefer the default one
            cname = f"{ns}.{c.get('Name')}"
            assoc_sets = []
            for el in c:
                tag, name = _l(el.tag), el.get("Name")
                if tag in ("EntitySet", "Singleton"):
                    es = EntitySet(name, self.qn(el.get("EntityType") or el.get("Type")), singleton=tag == "Singleton")
                    for b in _kids(el, "NavigationPropertyBinding"):
                        es.bindings[b.get("Path").rsplit("/", 1)[-1]] = b.get("Target").rsplit("/", 1)[-1]
                    sets[name] = es
                    if self._annos(el):
                        set_annos.setdefault(name, {}).update(self._annos(el))
                elif tag == "AssociationSet":
                    assoc_sets.append((self.qn(el.get("Association")), {e.get("Role"): e.get("EntitySet") for e in _kids(el, "End")}))
                elif tag in ("FunctionImport", "ActionImport"):
                    if el.get("Function") or el.get("Action"):
                        imports.append(Import(name, "function" if tag == "FunctionImport" else "action",
                                              self.qn(el.get("Function") or el.get("Action")),
                                              (el.get("EntitySet") or "").rsplit("/", 1)[-1] or None,
                                              _bool(el.get("IncludeInServiceDocument"), True)))
                    else:  # v2 FunctionImport carries its own signature
                        rtt, rcoll = split_collection(el.get("ReturnType"))
                        kind = "action" if (_attr(el, "HttpMethod") or "GET").upper() == "POST" else "function"
                        op = Operation(f"{ns}.{name}", kind, False,
                                       [Parameter(p.get("Name"), self.qn(p.get("Type")), False, _bool(p.get("Nullable"), True))
                                        for p in _kids(el, "Parameter")], self.qn(rtt), rcoll)
                        ops.append(op)
                        imports.append(Import(name, kind, op.name, el.get("EntitySet")))
            for es in sets.values():  # v2 bindings from AssociationSets
                T = types.get(es.type)
                for n in (T.navs.values() if T else []):
                    rel = getattr(n, "v2rel", None)
                    for aname, roles in assoc_sets:
                        if rel and aname == rel[0] and roles.get(rel[1]) == es.name:
                            es.bindings[n.name] = roles.get(rel[2])

        cshort = cname.rsplit(".", 1)[-1] if cname else None
        for s in self.schemas:
            for a in _kids(s, "Annotations"):
                tgt = a.get("Target") or ""
                if "/" not in tgt:
                    continue
                cont, rest = tgt.split("/", 1)
                if "/" not in rest and self.qn(cont) in (cname, cshort):
                    set_annos.setdefault(rest, {}).update(self._annos(a))

        m = Model(version, dict(self.alias), types, enums, tdefs, sets, ops, imports, cname, set_annos)
        _lint(m)
        return m


def _flatten(raw):
    done = {}

    def get(qn, stack=()):
        if qn in done:
            return done[qn]
        t = raw[qn]
        if t.base in raw and t.base not in stack:
            b = get(t.base, stack + (qn,))
            t = replace(t, keys=t.keys or list(b.keys), properties={**b.properties, **t.properties},
                        navs={**b.navs, **t.navs}, open=t.open or b.open, has_stream=t.has_stream or b.has_stream)
        done[qn] = t
        return t

    for qn in raw:
        get(qn)
    return done


def _lint(m):
    P = m.problems

    def known(t):
        return t.startswith("Edm.") or t in m.types or t in m.enums or t in m.type_defs

    for qn, t in m.types.items():
        if t.base and t.base not in m.types:
            P.append(f"{qn}: unknown BaseType {t.base}")
        if t.kind == "entity" and not t.abstract and not t.keys:
            P.append(f"EntityType {qn} has no key")
        for k in t.keys:
            p = t.properties.get(k)
            if p is None:
                P.append(f"{qn}: key '{k}' is not a declared property")
            elif p.nullable:
                P.append(f"{qn}: key property '{k}' is Nullable")
        for p in t.properties.values():
            if not known(p.type):
                P.append(f"{qn}.{p.name}: unknown type {p.type}")
        for n in t.navs.values():
            tgt = m.types.get(n.type)
            if tgt is None:
                P.append(f"{qn}.{n.name}: unknown navigation target type {n.type}")
                continue
            for dep, prin in n.constraints.items():
                if dep not in t.properties:
                    P.append(f"{qn}.{n.name}: constraint property '{dep}' not on {qn}")
                if prin not in tgt.properties:
                    P.append(f"{qn}.{n.name}: referenced property '{prin}' not on {n.type}")
            if n.partner and n.partner not in tgt.navs:
                P.append(f"{qn}.{n.name}: partner '{n.partner}' not found on {n.type}")
    for s in m.sets.values():
        T = m.types.get(s.type)
        if T is None:
            P.append(f"EntitySet {s.name}: unknown entity type {s.type}")
            continue
        for path, target in s.bindings.items():
            nav = T.navs.get(path) or next((t.navs[path] for t in m.types.values()
                                            if path in t.navs and m.is_derived(t.name, T.name)), None)
            if nav is None:
                P.append(f"EntitySet {s.name}: binding path '{path}' is not a navigation property of {s.type}")
            if target not in m.sets:
                P.append(f"EntitySet {s.name}: binding target '{target}' does not exist")
            elif nav and not m.is_derived(nav.type, m.sets[target].type) and not m.is_derived(m.sets[target].type, nav.type):
                P.append(f"EntitySet {s.name}: binding '{path}' targets {target} of type {m.sets[target].type}, nav type is {nav.type}")
    for o in m.operations:
        for p in o.params:
            if not known(p.type):
                P.append(f"{o.name}: parameter '{p.name}' has unknown type {p.type}")
        if o.return_type and not known(o.return_type):
            P.append(f"{o.name}: unknown return type {o.return_type}")
    names = {o.name for o in m.operations}
    for i in m.imports:
        if i.operation not in names:
            P.append(f"{i.kind.title()}Import {i.name}: operation {i.operation} not declared")
        if i.entity_set and i.entity_set not in m.sets:
            P.append(f"{i.kind.title()}Import {i.name}: entity set {i.entity_set} not found")


def parse(xml):
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    return _Parser(ET.fromstring(data)).parse()


# ---------- schema inference (no $metadata available) ----------

_DTO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _infer_scalar(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return "Edm.String"
    if all(isinstance(v, bool) for v in vals):
        return "Edm.Boolean"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
        return "Edm.Int32" if all(-2**31 <= v < 2**31 for v in vals) else "Edm.Int64"
    if all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in vals):
        return "Edm.Decimal"
    if all(isinstance(v, str) for v in vals):
        for t, rx in (("Edm.DateTimeOffset", _DTO), ("Edm.Date", _DATE), ("Edm.Guid", _GUID)):
            if all(rx.match(v) for v in vals):
                return t
    return "Edm.String"


def infer_xml(set_name, rows, ns="Inferred"):
    """Synthesize CSDL from sample rows: types, nullability, complex/collection shapes and a best-guess key."""
    complex_xml = []

    def props(objs, tname):
        out = {}
        for k in dict.fromkeys(k for o in objs for k in o if "@" not in k):
            vals = [o.get(k) for o in objs]
            nn = [v for v in vals if v is not None]
            coll = bool(nn) and all(isinstance(v, list) for v in nn)
            items = [x for v in nn for x in v] if coll else nn
            if items and all(isinstance(x, dict) for x in items):
                cname = f"{tname}_{re.sub(r'[^A-Za-z0-9_]', '_', k)}"
                complex_xml.append(f'<ComplexType Name="{cname}">{render(props(items, cname))}</ComplexType>')
                t = f"{ns}.{cname}"
            else:
                t = _infer_scalar(items)
            out[k] = [f"Collection({t})" if coll else t, any(v is None for v in vals) or any(k not in o for o in objs)]
        return out

    def render(ps):
        return "".join(f'<Property Name={quoteattr(k)} Type="{t}" Nullable="{str(n).lower()}"/>' for k, (t, n) in ps.items())

    tname = re.sub(r"[^A-Za-z0-9_]", "_", set_name) + "Type"
    ps = props(rows, tname)
    single = set_name[:-1] if set_name.endswith("s") else set_name
    prefs = ["Id", "ID", "id", f"{set_name}Id", f"{single}Id", f"{single}ID", f"{single}Code", "Code", "Key"]
    if len(rows) >= 2:
        prefs += [k for k in ps if k.lower().endswith(("id", "code", "number", "key"))]

    def unique(k):
        vals = [r.get(k) for r in rows]
        return vals and all(isinstance(v, (str, int)) for v in vals) and len(set(map(str, vals))) == len(vals)
    key = next((k for k in prefs if k in ps and not ps[k][0].startswith("Collection") and unique(k)), None)
    if key:
        ps[key][1] = False
    keyxml = f'<Key><PropertyRef Name={quoteattr(key)}/></Key>' if key else ""
    return (f'<?xml version="1.0" encoding="utf-8"?><edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">'
            f'<edmx:DataServices><Schema Namespace="{ns}" xmlns="http://docs.oasis-open.org/odata/ns/edm">{"".join(complex_xml)}'
            f'<EntityType Name="{tname}">{keyxml}{render(ps)}</EntityType><EntityContainer Name="Container">'
            f'<EntitySet Name={quoteattr(set_name)} EntityType="{ns}.{tname}"/></EntityContainer></Schema></edmx:DataServices></edmx:Edmx>')


def infer_model(set_name, rows):
    m = parse(infer_xml(set_name, rows))
    m.inferred = True
    m.problems = [p for p in m.problems if "has no key" not in p]  # missing key is a guess failure, not a service defect
    return m
