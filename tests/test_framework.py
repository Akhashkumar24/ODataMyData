"""Offline self-checks: python -m unittest discover -s tests"""
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

from odh_odata import baseline, edm, metadata
from odh_odata.http import Client, Resp, TokenExpired, candidates, fetch_token
from odh_odata.suite import FAIL, PASS, SKIP, WARN, Exprs, Result, Runner
from tests.stub import StubService

V4 = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
 <edmx:Reference Uri="https://oasis-tcs.github.io/odata-vocabularies/vocabularies/Org.OData.Capabilities.V1.xml">
  <edmx:Include Namespace="Org.OData.Capabilities.V1" Alias="Capabilities"/></edmx:Reference>
 <edmx:DataServices>
  <Schema Namespace="Fleet.Models" Alias="self" xmlns="http://docs.oasis-open.org/odata/ns/edm">
   <EnumType Name="Status" IsFlags="false"><Member Name="Active" Value="1"/><Member Name="Retired"/></EnumType>
   <TypeDefinition Name="Code" UnderlyingType="Edm.String"/>
   <ComplexType Name="Address"><Property Name="City" Type="Edm.String"/></ComplexType>
   <EntityType Name="Asset" Abstract="true"><Key><PropertyRef Name="Id"/></Key>
    <Property Name="Id" Type="Edm.Int32" Nullable="false"/><Property Name="Status" Type="self.Status"/></EntityType>
   <EntityType Name="Aircraft" BaseType="self.Asset">
    <Property Name="Tail" Type="self.Code"/><Property Name="Home" Type="self.Address"/>
    <Property Name="Tags" Type="Collection(Edm.String)"/>
    <NavigationProperty Name="Sensors" Type="Collection(self.Sensor)" Partner="Aircraft"/></EntityType>
   <EntityType Name="Sensor"><Key><PropertyRef Name="AircraftId"/><PropertyRef Name="Code"/></Key>
    <Property Name="AircraftId" Type="Edm.Int32" Nullable="false"/><Property Name="Code" Type="Edm.String" Nullable="false"/>
    <NavigationProperty Name="Aircraft" Type="self.Aircraft" Nullable="false" Partner="Sensors">
     <ReferentialConstraint Property="AircraftId" ReferencedProperty="Id"/></NavigationProperty></EntityType>
   <Function Name="TopSensors" IsBound="true"><Parameter Name="bindingParameter" Type="Collection(self.Sensor)"/>
    <ReturnType Type="Collection(self.Sensor)"/></Function>
   <Function Name="Ping"><ReturnType Type="Edm.String"/></Function>
   <Action Name="Reset"><Parameter Name="id" Type="Edm.Int32" Nullable="false"/></Action>
   <EntityContainer Name="Container">
    <EntitySet Name="Aircraft" EntityType="self.Aircraft"><NavigationPropertyBinding Path="Sensors" Target="Sensors"/></EntitySet>
    <EntitySet Name="Sensors" EntityType="self.Sensor"><NavigationPropertyBinding Path="Aircraft" Target="Aircraft"/>
     <Annotation Term="Capabilities.CountRestrictions"><Record><PropertyValue Property="Countable" Bool="false"/></Record></Annotation>
    </EntitySet>
    <Singleton Name="Fleet" Type="self.Aircraft"/>
    <FunctionImport Name="Ping" Function="self.Ping"/><ActionImport Name="Reset" Action="self.Reset"/>
   </EntityContainer>
   <Annotations Target="self.Container/Aircraft">
    <Annotation Term="Capabilities.SortRestrictions"><Record><PropertyValue Property="NonSortableProperties">
     <Collection><PropertyPath>Tail</PropertyPath></Collection></PropertyValue></Record></Annotation>
    <Annotation Term="Capabilities.TopSupported" Bool="false"/>
   </Annotations>
  </Schema></edmx:DataServices></edmx:Edmx>"""

V2 = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"
  xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
 <edmx:DataServices m:DataServiceVersion="2.0">
  <Schema Namespace="NW" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
   <EntityType Name="Order"><Key><PropertyRef Name="OrderID"/></Key><Property Name="OrderID" Type="Edm.Int32" Nullable="false"/>
    <Property Name="CustomerID" Type="Edm.String"/>
    <NavigationProperty Name="Customer" Relationship="NW.FK_Order_Customer" FromRole="Order" ToRole="Customer"/></EntityType>
   <EntityType Name="Customer"><Key><PropertyRef Name="CustomerID"/></Key><Property Name="CustomerID" Type="Edm.String" Nullable="false"/>
    <NavigationProperty Name="Orders" Relationship="NW.FK_Order_Customer" FromRole="Customer" ToRole="Order"/></EntityType>
   <Association Name="FK_Order_Customer"><End Role="Customer" Type="NW.Customer" Multiplicity="0..1"/><End Role="Order" Type="NW.Order" Multiplicity="*"/>
    <ReferentialConstraint><Principal Role="Customer"><PropertyRef Name="CustomerID"/></Principal>
     <Dependent Role="Order"><PropertyRef Name="CustomerID"/></Dependent></ReferentialConstraint></Association>
   <EntityContainer Name="Ctx" m:IsDefaultEntityContainer="true">
    <EntitySet Name="Orders" EntityType="NW.Order"/><EntitySet Name="Customers" EntityType="NW.Customer"/>
    <AssociationSet Name="FK_Order_Customer" Association="NW.FK_Order_Customer">
     <End Role="Customer" EntitySet="Customers"/><End Role="Order" EntitySet="Orders"/></AssociationSet>
    <FunctionImport Name="TopOrders" ReturnType="Collection(NW.Order)" EntitySet="Orders" m:HttpMethod="GET"/>
   </EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"""


class MetadataTests(unittest.TestCase):
    def test_v4_model(self):
        m = metadata.parse(V4)
        self.assertEqual(m.problems, [])
        a = m.types["Fleet.Models.Aircraft"]
        self.assertEqual(a.keys, ["Id"])  # inherited from abstract base
        self.assertIn("Status", a.properties)
        self.assertEqual(a.properties["Status"].type, "Fleet.Models.Status")  # alias resolved
        self.assertEqual(m.scalar_type(a.properties["Tail"]), ("Edm.String", None))  # TypeDefinition unwrapped
        self.assertEqual(m.scalar_type(a.properties["Status"]), ("Fleet.Models.Status", "Fleet.Models.Status"))
        self.assertTrue(a.properties["Tags"].collection)
        self.assertEqual(m.enums["Fleet.Models.Status"].members, {"Active": 1, "Retired": 2})
        self.assertEqual(m.sets["Aircraft"].bindings, {"Sensors": "Sensors"})
        self.assertTrue(m.sets["Fleet"].singleton)
        self.assertEqual(m.fk_pairs("Fleet.Models.Aircraft", a.navs["Sensors"]), ([("Id", "AircraftId")], "declared"))
        s = m.types["Fleet.Models.Sensor"]
        self.assertEqual(m.fk_pairs("Fleet.Models.Sensor", s.navs["Aircraft"]), ([("AircraftId", "Id")], "declared"))
        self.assertFalse(m.caps("Sensors").countable)
        self.assertEqual(m.caps("Aircraft").non_sortable, {"Tail"})
        self.assertFalse(m.caps("Aircraft").top)
        self.assertTrue(m.caps("Aircraft").countable)
        self.assertEqual({(o.name, o.kind, o.bound) for o in m.operations},
                         {("Fleet.Models.TopSensors", "function", True), ("Fleet.Models.Ping", "function", False),
                          ("Fleet.Models.Reset", "action", False)})
        self.assertEqual([(i.name, i.kind) for i in m.imports], [("Ping", "function"), ("Reset", "action")])

    def test_lint_finds_problems(self):
        bad = V4.replace('Target="Sensors"/>', 'Target="Nope"/>').replace('ReferencedProperty="Id"', 'ReferencedProperty="Missing"')
        probs = " | ".join(metadata.parse(bad).problems)
        self.assertIn("binding target 'Nope' does not exist", probs)
        self.assertIn("referenced property 'Missing'", probs)

    def test_v2_model(self):
        m = metadata.parse(V2)
        self.assertFalse(m.v4)
        o = m.types["NW.Order"]
        self.assertEqual(o.navs["Customer"].type, "NW.Customer")
        self.assertFalse(o.navs["Customer"].collection)
        self.assertEqual(o.navs["Customer"].constraints, {"CustomerID": "CustomerID"})
        self.assertTrue(m.types["NW.Customer"].navs["Orders"].collection)
        self.assertEqual(m.sets["Orders"].bindings, {"Customer": "Customers"})
        self.assertEqual(m.imports[0].name, "TopOrders")
        self.assertEqual(m.problems, [])

    def test_real_vehicle_metadata(self):
        m = metadata.parse(Path(__file__).with_name("fixtures").joinpath("vehicle_metadata.xml").read_bytes())
        v = m.types["API.Models.Vehicle"]
        self.assertEqual(m.fk_pairs(v.name, v.navs["CurrentEmployee"]), ([("CurrentEmployeePersonalNumber", "PersonalNumber")], "inferred"))
        self.assertEqual(m.fk_pairs(v.name, v.navs["Metrics"]), ([("Id", "VehicleId")], "declared"))
        aa = m.types["API.Models.AirportArea"]
        self.assertEqual(m.fk_pairs(aa.name, aa.navs["VehicleAirportAreas"])[0],
                         [("AirportAreaTypeCode", "AirportAreaTypeCode"), ("Name", "AirportAreaName")])
        self.assertEqual(len(m.problems), 3)  # nullable key parts are real spec violations in this service


class EdmTests(unittest.TestCase):
    def test_literals(self):
        self.assertEqual(edm.literal("O'Hare", "Edm.String"), "'O''Hare'")
        self.assertEqual(edm.literal(5, "Edm.Int64", v4=False), "5L")
        self.assertEqual(edm.literal(True, "Edm.Boolean"), "true")
        self.assertEqual(edm.literal(None, "Edm.String"), "null")
        self.assertEqual(edm.literal("Active", "Edm.String", enum="NS.Status"), "NS.Status'Active'")
        self.assertEqual(edm.literal("2024-01-01T00:00:00Z", "Edm.DateTimeOffset"), "2024-01-01T00:00:00Z")

    def test_check(self):
        self.assertIsNone(edm.check("2026-11-05T17:47:47.9306351+00:00", "Edm.DateTimeOffset"))
        self.assertIn("offset", edm.check("2026-11-05T17:47:47", "Edm.DateTimeOffset"))
        self.assertIsNotNone(edm.check(40000, "Edm.Int16"))
        self.assertIsNotNone(edm.check(True, "Edm.Int32"))
        self.assertIsNone(edm.check("abc", "Edm.String", "3"))
        self.assertIsNotNone(edm.check("abcd", "Edm.String", "3"))
        self.assertIsNotNone(edm.check("not-a-guid", "Edm.Guid"))
        self.assertIsNone(edm.check("PT1H30M", "Edm.Duration"))


class ExprTests(unittest.TestCase):
    def test_eval_and_text(self):
        X = Exprs(True)
        e = X.cmp("Code", "eq", "YYZ", "Edm.String") & ~X.cmp("N", "gt", 3, "Edm.Int32")
        self.assertEqual(e.text, "Code eq 'YYZ' and not (N gt 3)")
        self.assertTrue(e.fn({"Code": "YYZ", "N": 1}, False))
        self.assertFalse(e.fn({"Code": "yyz", "N": 1}, False))
        self.assertTrue(e.fn({"Code": "yyz", "N": 1}, True))  # case-insensitive mode
        self.assertTrue(X.any_("Items", "Sku", "eq", "A", "Edm.String").fn({"Items": [{"Sku": "B"}, {"Sku": "A"}]}, False))
        self.assertTrue(X.cmp("Home/City", "eq", "YUL", "Edm.String").fn({"Home": {"City": "YUL"}}, False))
        self.assertFalse(X.cmp("T", "gt", "2024-01-01T00:00:00Z", "Edm.DateTimeOffset").fn({"T": None}, False))
        self.assertEqual(Exprs(False).strfn("contains", "P", "x").text, "substringof('x',P)")


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.r = Runner(None, metadata.parse(V4), "", SimpleNamespace(verbose=False, slow_ms=1e9), log=lambda *_: None)

    def test_order(self):
        rows = [{"a": 1}, {"a": 2}, {"a": None}]
        self.assertEqual(self.r.order_notes([{"a": None}, {"a": 1}, {"a": 2}], [("a", False, "Edm.Int32")]), [])
        self.assertEqual(self.r.order_notes(rows, [("a", False, "Edm.Int32")])[0][0], WARN)  # nulls last
        self.assertEqual(self.r.order_notes([{"a": 2}, {"a": 1}, {"a": 3}], [("a", False, "Edm.Int32")])[0][0], FAIL)
        self.assertEqual(self.r.order_notes([{"s": "b"}, {"s": "B"}, {"s": "a"}], [("s", True, "Edm.String")])[0][0], WARN)


class InferenceTests(unittest.TestCase):
    def test_infer_model(self):
        rows = [{"@odata.etag": "x", "SensorId": "A1", "Reading": 1.5, "Count": 3, "Ok": True, "At": "2026-11-05T17:47:47Z",
                 "Loc": {"Gate": "B12"}, "Tags": ["x"], "Note": None},
                {"SensorId": "A2", "Reading": 2, "Count": 4, "Ok": False, "At": "2026-11-05T18:00:00+00:00",
                 "Loc": {"Gate": "B13"}, "Tags": [], "Note": "hi"}]
        m = metadata.infer_model("AircraftSensor", rows)
        T = m.types[m.sets["AircraftSensor"].type]
        self.assertTrue(m.inferred)
        self.assertEqual(T.keys, ["SensorId"])
        types = {k: (p.type, p.collection, p.nullable) for k, p in T.properties.items()}
        self.assertEqual(types["Reading"][0], "Edm.Decimal")
        self.assertEqual(types["Count"][0], "Edm.Int32")
        self.assertEqual(types["At"][0], "Edm.DateTimeOffset")
        self.assertEqual(types["Tags"], ("Edm.String", True, False))
        self.assertTrue(types["Note"][2])
        self.assertFalse(types["SensorId"][2])
        self.assertIn(types["Loc"][0], m.types)  # complex type synthesized
        self.assertNotIn("@odata.etag", T.properties)

    def test_scope(self):
        r = Runner(None, metadata.parse(V4), "svc", SimpleNamespace(verbose=False, slow_ms=1e9), log=lambda *_: None,
                   base_filters={"Sensors": "Code eq 'X'"})
        self.assertEqual(r.scope("Sensors", {"$top": 1}), {"$top": 1, "$filter": "Code eq 'X'"})
        self.assertEqual(r.scope("Sensors", {"$filter": "A eq 1"})["$filter"], "(Code eq 'X') and (A eq 1)")
        self.assertEqual(r.scope("Sensors/$count", None), {"$filter": "Code eq 'X'"})
        self.assertIsNone(r.scope("Sensors(AircraftId=1,Code='a')", None))  # key access is not scoped
        self.assertIsNone(r.scope("Aircraft", None))


class FakeClient:
    extra, connect, host, public, token = {}, None, "h", "http://h", None

    def __init__(self, status):
        self.status, self.calls, self.urls = status, 0, []

    def get(self, url, **kw):
        self.calls += 1
        self.urls.append(url)
        return Resp(url, self.status, 5, {}, "{}", {})


class FocusTests(unittest.TestCase):
    def runner(self, client=None, **opts):
        o = SimpleNamespace(verbose=False, slow_ms=1e9, **opts)
        return Runner(client, metadata.parse(V4), "", o, log=lambda *_: None)

    def test_limit_drops_weak_columns_and_caps_per_type(self):
        S = SimpleNamespace(rows=[{"const": "x", "empty": None, "a": i, "b": i % 3, "c": i % 2} for i in range(10)])
        r = self.runner()
        got = r._limit(["const", "empty", "a", "b", "c"], key=lambda n: "Edm.Int32", S=S)
        self.assertEqual(got, ["a", "b"])  # most distinct first, capped at 2 per type, constant/all-null dropped
        self.assertEqual(self.runner(thorough=True)._limit(["const", "empty", "a"], S=S), ["const", "empty", "a"])

    def test_key_route_probed_once_and_gates_the_rest(self):
        c = FakeClient(404)
        r = self.runner(c)
        T = r.m.types["Fleet.Models.Aircraft"]
        self.assertFalse(r.key_works("Aircraft", T, {"Id": 1}))
        self.assertFalse(r.key_works("Aircraft", T, {"Id": 2}))
        self.assertEqual(c.calls, 1)  # one request, one finding, no matter how many callers
        self.assertEqual([x.outcome for x in r.results], [FAIL])

    def test_key_route_ok(self):
        r = self.runner(FakeClient(200))
        T = r.m.types["Fleet.Models.Aircraft"]
        self.assertTrue(r.key_works("Aircraft", T, {"Id": 1, "Tail": "C-FABC"}))

    def test_requires_filter_without_scope_skips_instead_of_failing_everything(self):
        c = FakeClient(400)
        r = self.runner(c, sample=20, negative=True)
        r.m.set_annotations["Sensors"] = {metadata.CAP + "FilterRestrictions": {"RequiresFilter": True, "RequiredProperties": ["Code"]}}
        r.test_set("Sensors")
        self.assertEqual(c.calls, 1)  # only the "unfiltered list is rejected" probe
        self.assertEqual([x.outcome for x in r.results], [PASS, SKIP])
        self.assertIn("['Code']", r.results[1].notes[0][1])

    def test_search_only_when_declared_and_uses_a_sampled_word(self):
        c = FakeClient(200)
        r = self.runner(c, sample=5)
        S = SimpleNamespace(name="Sensors", T=r.m.types["Fleet.Models.Sensor"], caps=r.m.caps("Sensors"), total=None,
                            scalars=r.scalars(r.m.types["Fleet.Models.Sensor"]), rows=[{"AircraftId": 1, "Code": "TEMP1 x"}])
        r.t_search(S)
        self.assertEqual(c.calls, 0)  # not declared searchable
        r.m.set_annotations["Sensors"] = {metadata.CAP + "SearchRestrictions": {"Searchable": True}}
        S.caps = r.m.caps("Sensors")
        r.t_search(S)
        self.assertIn("$search=TEMP1", c.urls[-1])


class ClientTests(unittest.TestCase):
    def test_401_stops_the_run_once_armed(self):
        class R:
            status_code, headers, encoding = 401, {}, "utf-8"
            def iter_content(self, n): return [b""]
            def close(self): pass
        c = Client("https://h", "tok")
        c.s.request = lambda *a, **k: R()
        self.assertEqual(c.get("x").status, 401)  # not armed yet (discovery phase)
        c.stop_on_401 = True
        self.assertEqual(c.get("x").status, 401)
        self.assertEqual(c.get("x", auth=False).status, 401)  # unauthenticated probes still run
        with self.assertRaises(TokenExpired):
            c.get("x")


def jwt(exp):
    import base64
    import json
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'none'})}.{b({'exp': exp})}.sig"


class TokenRequestTests(unittest.TestCase):
    def test_fetch_token_posts_the_form_and_hides_the_secret_on_error(self):
        import requests
        from unittest import mock
        ok = SimpleNamespace(status_code=200, text="", json=lambda: {"access_token": "T"})
        with mock.patch.object(requests, "post", return_value=ok) as post:
            self.assertEqual(fetch_token("https://login/x", "id", "SECRET", "api://a/.default"), "T")
        self.assertEqual(post.call_args.kwargs["data"], {"client_id": "id", "client_secret": "SECRET",
                                                          "grant_type": "client_credentials", "scope": "api://a/.default"})
        bad = SimpleNamespace(status_code=401, text="", json=lambda: {"error": "invalid_client", "error_description": "AADSTS7000215"})
        with mock.patch.object(requests, "post", return_value=bad):
            with self.assertRaises(RuntimeError) as cm:
                fetch_token("https://login/x", "id", "SECRET")
        self.assertIn("invalid_client", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_client_renews_an_expiring_token_once(self):
        import time
        class R:
            status_code, headers, encoding = 200, {}, "utf-8"
            def iter_content(self, n): return [b"{}"]
            def close(self): pass
        c = Client("https://h", jwt(time.time() + 10))
        c.s.request = lambda *a, **k: (seen.append(k["headers"]["Authorization"]), R())[1]
        seen, calls = [], []
        c.refresh = lambda: (calls.append(1), jwt(time.time() + 3600))[1]
        c.get("x")
        c.get("x")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(set(seen)), 1)
        self.assertNotIn(jwt(0)[:20] + "x", seen)
        c2 = Client("https://h", jwt(time.time() + 10))
        c2.refresh = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        with self.assertRaises(TokenExpired):
            c2.get("x")


class DiscoveryTests(unittest.TestCase):
    def test_candidates(self):
        c = candidates("/api/v1.0/odata/Vehicles")
        self.assertLess(c.index(("api/v1.0/odata/Vehicle", ["Vehicles"])), c.index(("api/v1.0/odata/Vehicle", [])))
        names = [r for r, _ in candidates("AircraftSensor")]
        self.assertIn("api/v1.0/odata/AircraftSensor", names)
        self.assertIn("api/v1.0/odata/AircraftSensors", names)


class EndToEndTests(unittest.TestCase):
    """The whole Runner against a stub that ignores every query option: every step runs, nothing crashes,
    and each category catches the misbehaviour it exists for."""

    def run_stub(self, xml=V4, capture_dir=None, opts=None, **kw):
        stub = StubService(**kw)
        r = Runner(stub, metadata.parse(xml), "", SimpleNamespace(**{**stub_opts(), **(opts or {})}), log=lambda *_: None,
                   capture_dir=capture_dir)
        return stub, r.run()

    def test_top_not_sent_where_top_unsupported(self):
        _, results = self.run_stub()  # V4 declares TopSupported=false on Aircraft
        sent = [x for x in results if x.set == "Aircraft" and x.category not in ("negative", "security")
                and re.search(r"/Aircraft\?(.*&)?\$top=", x.url)]
        self.assertEqual([x.name for x in sent], [])
        self.assertFalse(any("rows for $top" in m for x in results if x.set == "Aircraft" for _, m in x.notes))

    def test_nested_expand_without_the_navigation_fails(self):
        _, results = self.run_stub()
        nested = [x for x in results if x.name == "$expand=Sensors($top=2)"]
        self.assertEqual([x.outcome for x in nested], [FAIL])

    def test_repeated_500_on_a_property_stops_requests_on_it(self):
        _, results = self.run_stub(fail_500="Tail")
        errs = [x for x in results if x.set == "Aircraft" and any("involving property 'Tail'" in m for _, m in x.notes)]
        skipped = [x for x in results if x.set == "Aircraft" and x.outcome == SKIP and "'Tail' already caused" in x.notes[0][1]]
        self.assertEqual(len(errs), Runner.BREAK_5XX)
        self.assertTrue(skipped)

    def test_filter_functions_declared_limits_function_tests(self):
        xml = V4.replace("<FunctionImport", '<Annotation Term="Capabilities.FilterFunctions"><Collection>'
                         "<String>contains</String></Collection></Annotation><FunctionImport", 1)
        stub, results = self.run_stub(xml)
        self.assertFalse([u for u in stub.urls if re.search(r"(startswith|endswith|tolower|year)\(", unquote(u))])
        self.assertTrue([u for u in stub.urls if "contains(" in unquote(u)])
        skip = [x for x in results if x.name == "functions not in Capabilities.FilterFunctions"]
        self.assertTrue(skip and "startswith" in skip[0].notes[0][1])

    def test_batch_matches_direct_requests(self):
        stub, results = self.run_stub(opts={"batch": True})
        b = [x for x in results if x.category == "batch" and x.name.startswith("$batch")]
        self.assertEqual([(x.outcome, x.notes) for x in b], [(PASS, [])])
        _, off = self.run_stub()
        self.assertFalse([x for x in off if x.category == "batch"])  # opt-in only

    def test_replay_reproduces_a_recorded_run(self):
        from odh_odata import replay, report
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "responses").mkdir()
            (run / "metadata.xml").write_text(V4, encoding="utf-8")
            opts = {**stub_opts(), "capture": "all", "batch": True}
            _, results = self.run_stub(capture_dir=run / "responses", opts=opts)
            report.write(run, results, metadata.parse(V4), {"service": "http://h", "inferred": False, "scope": {},
                                                             "started": "", "duration_s": 0, "requests": 0, "only": None,
                                                             "options": opts})
            log = []
            code, again = replay.run(run, SimpleNamespace(**stub_opts()), log=log.append)
        self.assertEqual(code, 0, log)
        self.assertIn("0 not in the recording", log[0])
        self.assertEqual([(baseline.test_id(x), x.outcome) for x in again], [(baseline.test_id(x), x.outcome) for x in results])


def stub_opts():
    from odh_odata.__main__ import build_parser
    return {**vars(build_parser().parse_args(["x"])), "workers": 1, "verbose": False}


class CapabilityTests(unittest.TestCase):
    def test_container_set_and_navigation_annotations(self):
        xml = V4.replace("<FunctionImport", '<Annotation Term="Capabilities.BatchSupported" Bool="false"/><FunctionImport', 1)
        xml = xml.replace("</Annotations>", """</Annotations>
   <Annotations Target="self.Container/Aircraft/Sensors">
    <Annotation Term="Capabilities.CountRestrictions"><Record><PropertyValue Property="Countable" Bool="false"/></Record></Annotation>
   </Annotations>
   <Annotations Target="self.Container/Sensors">
    <Annotation Term="Capabilities.ExpandRestrictions"><Record><PropertyValue Property="MaxLevels" Int="1"/></Record></Annotation>
    <Annotation Term="Capabilities.NavigationRestrictions"><Record><PropertyValue Property="RestrictedProperties"><Collection>
     <Record><PropertyValue Property="NavigationProperty" NavigationPropertyPath="Aircraft"/>
      <PropertyValue Property="Navigability" EnumMember="Capabilities.NavigationType/None"/>
      <PropertyValue Property="TopSupported" Bool="false"/></Record></Collection></PropertyValue></Record></Annotation>
   </Annotations>""", 1)
        m = metadata.parse(xml)
        a, s = m.caps("Aircraft"), m.caps("Sensors")
        self.assertFalse(a.batch)  # container-level term reaches every set
        self.assertFalse(a.nav("Sensors").countable)  # Container/Set/Nav target
        self.assertTrue(a.nav("Sensors").top)
        self.assertEqual(s.max_levels, 1)
        self.assertFalse(s.nav("Aircraft").navigable)
        self.assertFalse(s.nav("Aircraft").top)
        self.assertIsNone(s.filter_functions)
        self.assertEqual(m.summary()["entity_sets"]["Sensors"]["capabilities"]["navs"]["Aircraft"]["navigable"], False)


class ReplayTests(unittest.TestCase):
    def test_old_windows_text_mode_capture_is_read(self):
        from odh_odata.replay import load_capture
        text = ("# S | read | list\ncurl -sk 'https://h/svc/S?$top=1' -H 'Accept: application/json' -H \"Authorization: Bearer "
                "$ODATA_TOKEN\"\n\nHTTP 200 (12 ms)\nContent-Type: application/json\n\n{\"value\": [{\"A\": 1.50}]}").replace("\n", "\r\n")
        key, r = load_capture(text)
        self.assertEqual(key, ("GET", "https://h/svc/S?$top=1", "token"))
        self.assertEqual((r.status, r.ms, str(r.json["value"][0]["A"])), (200, 12, "1.50"))  # Decimal kept exactly


class BaselineTests(unittest.TestCase):
    def R(self, name, outcome, set_="S", cat="filter"):
        return Result(set_, cat, name, outcome=outcome)

    def test_test_id_masks_sampled_values_only(self):
        a = baseline.test_id(self.R("Code eq 'A1' and Start ge 2026-01-02T03:04:05Z and Address2 lt 15", FAIL))
        b = baseline.test_id(self.R("Code eq 'B''x' and Start ge 2025-12-31T00:00:00Z and Address2 lt 7", FAIL))
        self.assertEqual(a, b)
        self.assertIn("Address2", a)
        self.assertNotEqual(a, baseline.test_id(self.R("Other eq 'A1'", FAIL)))

    def test_compare_new_fixed_and_known(self):
        known = {"S | filter | A eq '~'": FAIL, "S | filter | B eq '~'": WARN, "S | filter | C eq '~'": FAIL,
                 "T | key | gone": FAIL}
        now = [self.R("A eq 'x'", FAIL), self.R("B eq 'x'", FAIL), self.R("C eq 'x'", PASS), self.R("D eq 'x'", WARN)]
        new, fixed, still = baseline.compare(now, known)
        self.assertEqual(new, {"S | filter | B eq '~'": FAIL, "S | filter | D eq '~'": WARN})  # WARN->FAIL counts as new
        self.assertEqual(fixed, {"S | filter | C eq '~'": FAIL})  # T|key|gone did not run: not claimed as fixed
        self.assertEqual(still, {"S | filter | A eq '~'": FAIL})

    def test_save_keeps_entries_for_tests_that_did_not_run(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.json"
            baseline.save(p, [self.R("A eq 'x'", FAIL, set_="S"), self.R("B", FAIL, set_="T")], "svc")
            baseline.save(p, [self.R("A eq 'x'", PASS, set_="S")], "svc")  # e.g. a later --only S run
            self.assertEqual(baseline.load(p), {"T | filter | B": FAIL})


if __name__ == "__main__":
    unittest.main()

    def test_ignoring_service_is_caught_everywhere(self):
        stub, results = self.run_stub()
        crashes = [x for x in results if x.category == "internal" or any("validator crashed" in m for _, m in x.notes)]
        self.assertEqual(crashes, [])
        failed = {x.category for x in results if x.outcome == FAIL}
        for cat in ("filter", "orderby", "negative", "count", "select", "expand", "paging"):
            self.assertIn(cat, failed, f"no FAIL in {cat}")
        self.assertTrue(any(x.category == "key" and x.outcome == PASS for x in results))
        self.assertTrue(any(x.category == "security" and x.outcome == PASS for x in results))
        self.assertTrue(any(x.category == "crawl" for x in results))
