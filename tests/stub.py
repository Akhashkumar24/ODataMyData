"""An in-memory OData service for end-to-end tests. It ignores every query option on purpose:
each test category must notice that ($top, $filter, $orderby, $select, $expand, negative tests)."""
import json
import re
from urllib.parse import unquote

from odh_odata.http import Resp

AIRCRAFT = [{"Id": i, "Status": "Active", "Tail": f"C-F{c}", "Home": {"City": city}, "Tags": ["a", t]}
            for i, c, city, t in [(3, "CCC", "Toronto", "x"), (1, "AAA", "Montreal", "y"), (2, "BBB", "Calgary", "x"),
                                  (5, "EEE", "Toronto", "z"), (4, "DDD", "Vancouver", "y")]]
SENSORS = [{"AircraftId": a, "Code": c} for a, c in [(1, "TEMP Probe"), (1, "PITOT"), (3, "TEMP Probe"), (2, "AOA")]]
DATA = {"Aircraft": AIRCRAFT, "Sensors": SENSORS}


class StubService:
    extra, connect, host, public = {}, None, "h", "http://h"

    def __init__(self, token="a.b.c", fail_500=None, metadata_xml=""):
        self.token, self.fail_500, self.metadata_xml = token, fail_500, metadata_xml
        self.urls, self.calls = [], 0

    def _r(self, url, status, body=None, text=None, ctype="application/json"):
        text = text if text is not None else json.dumps(body)
        return Resp(url, status, 1, {"Content-Type": ctype, "OData-Version": "4.0"}, text, body)

    def get(self, url, headers=None, auth=True, token=None, **kw):
        self.calls += 1
        self.urls.append(url)
        if not auth or token:
            return self._r(url, 401, {"error": {"message": "unauthorized"}})
        path, _, qs = unquote(url).partition("?")
        if self.fail_500 and re.search(self.fail_500, qs):
            return self._r(url, 500, {"error": {"message": "An unexpected error occurred."}})
        if path == "$metadata":
            return self._r(url, 200, text=self.metadata_xml, ctype="application/xml")
        if path == "":
            return self._r(url, 200, {"value": [{"name": n, "url": n} for n in ("Aircraft", "Sensors", "Fleet", "Ping")]})
        if path in ("Ping()", "Fleet"):
            return self._r(url, 200, {"value": "pong"} if path == "Ping()" else dict(AIRCRAFT[0]))
        m = re.fullmatch(r"(\w+)(\((.*?)\))?(/(\$?\w+))?", path)
        if not m or m.group(1) not in DATA:
            return self._r(url, 404, {"error": {"message": "not found"}})
        rows, key, sub = DATA[m.group(1)], m.group(3), m.group(5)
        if sub == "$count" and not key:
            return self._r(url, 200, text=str(len(rows)), ctype="text/plain")
        if key is None:
            return self._r(url, 200, {"@odata.context": f"$metadata#{m.group(1)}", "value": rows, "@odata.count": len(rows)})
        vals = [v.split("=")[-1].strip("'") for v in key.split(",")]
        hit = next((r for r in rows if [str(r.get(k)) for k in (["Id"] if m.group(1) == "Aircraft" else ["AircraftId", "Code"])] == vals), None)
        if hit is None:
            return self._r(url, 404, {"error": {"message": "not found"}})
        if sub:
            return self._r(url, 200, {"value": hit.get(sub)}) if sub in hit else self._r(url, 404, {"error": {}})
        return self._r(url, 200, {"@odata.context": f"$metadata#{m.group(1)}/$entity", **hit})
