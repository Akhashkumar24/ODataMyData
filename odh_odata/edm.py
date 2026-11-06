"""EDM primitive type rules: validate JSON values, convert to comparable Python values, build URL literals."""
from __future__ import annotations

import base64
import re
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation

INT_RANGES = {"Edm.Byte": (0, 255), "Edm.SByte": (-128, 127), "Edm.Int16": (-2**15, 2**15 - 1),
              "Edm.Int32": (-2**31, 2**31 - 1), "Edm.Int64": (-2**63, 2**63 - 1)}
FLOATS = {"Edm.Double", "Edm.Single"}
NUMERIC = set(INT_RANGES) | FLOATS | {"Edm.Decimal"}
DATES = {"Edm.DateTimeOffset", "Edm.DateTime", "Edm.Date"}
ORDERABLE = NUMERIC | DATES | {"Edm.String", "Edm.Boolean", "Edm.Guid", "Edm.TimeOfDay", "Edm.Duration"}
RANGED = NUMERIC | DATES | {"Edm.TimeOfDay", "Edm.String"}  # types where gt/lt make sense

_FRAC = re.compile(r"\.(\d+)")
_V2DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")
_DURATION = re.compile(r"^-?P(?=\d|T\d)(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$")


def _iso(s):
    s = s.strip()
    if s[-1:] in "Zz":
        s = s[:-1] + "+00:00"
    return _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)  # Python takes max 6 fraction digits


def parse_datetime(s):
    m = _V2DATE.match(s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
    return datetime.fromisoformat(_iso(s))


def to_py(v, t):
    """JSON value -> comparable Python value. None stays None; raises ValueError/TypeError on a bad value."""
    if v is None:
        return None
    if t in INT_RANGES:
        if isinstance(v, bool):
            raise TypeError("bool is not an integer")
        return int(v)
    if t == "Edm.Decimal":
        return Decimal(str(v))
    if t in FLOATS:
        return float(v)
    if t in ("Edm.DateTimeOffset", "Edm.DateTime"):
        d = parse_datetime(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    if t == "Edm.Date":
        return date.fromisoformat(v)
    if t == "Edm.TimeOfDay":
        return time.fromisoformat(_FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], v, count=1))
    if t == "Edm.Guid":
        return str(uuid.UUID(v))
    if t == "Edm.Boolean" and not isinstance(v, bool):
        raise TypeError("not a boolean")
    return v


def check(v, t, max_length=None):
    """Error message if JSON value v is not a valid t, else None. Callers handle null/nullable."""
    try:
        if t == "Edm.String":
            if not isinstance(v, str):
                return f"expected string, got {type(v).__name__}"
            if max_length and str(max_length).isdigit() and len(v) > int(max_length):
                return f"length {len(v)} exceeds MaxLength {max_length}"
        elif t == "Edm.Boolean":
            if not isinstance(v, bool):
                return f"expected boolean, got {type(v).__name__}"
        elif t in INT_RANGES:
            if isinstance(v, bool) or not isinstance(v, (int, str)) or (isinstance(v, str) and t != "Edm.Int64"):
                return f"expected integer, got {type(v).__name__} {v!r}"
            lo, hi = INT_RANGES[t]
            if not lo <= int(v) <= hi:
                return f"{v} out of range for {t}"
        elif t in NUMERIC:
            if isinstance(v, bool) or not isinstance(v, (int, float, Decimal, str)):
                return f"expected number, got {type(v).__name__}"
            if isinstance(v, str) and v not in ("INF", "-INF", "NaN"):
                Decimal(v)
        elif t in ("Edm.DateTimeOffset", "Edm.DateTime"):
            if not isinstance(v, str):
                return f"expected date-time string, got {type(v).__name__}"
            d = parse_datetime(v)
            if t == "Edm.DateTimeOffset" and d.tzinfo is None:
                return f"DateTimeOffset without offset: {v!r}"
        elif t == "Edm.Date":
            date.fromisoformat(v)
        elif t == "Edm.TimeOfDay":
            to_py(v, t)
        elif t == "Edm.Guid":
            uuid.UUID(v)
        elif t == "Edm.Duration":
            if not isinstance(v, str) or not _DURATION.match(v):
                return f"invalid Duration {v!r}"
        elif t == "Edm.Binary":
            base64.b64decode(v.replace("-", "+").replace("_", "/") + "=" * (-len(v) % 4), validate=True)
        elif t.startswith(("Edm.Geography", "Edm.Geometry")):
            if not isinstance(v, dict) or "type" not in v:
                return "expected GeoJSON object"
    except (ValueError, TypeError, InvalidOperation, AttributeError):
        return f"invalid {t} value {v!r}"
    return None


def literal(v, t, v4=True, enum=None):
    """OData URL literal for value v of type t (None -> 'null'). Returns None if the type has no usable literal."""
    if v is None:
        return "null"
    if enum:
        return f"{enum}'{v}'"
    if t == "Edm.String":
        return "'" + str(v).replace("'", "''") + "'"
    if t == "Edm.Boolean":
        return "true" if v else "false"
    if t in INT_RANGES:
        return f"{int(v)}L" if (not v4 and t == "Edm.Int64") else str(int(v))
    if t == "Edm.Decimal":
        return str(v) if v4 else f"{v}M"
    if t in FLOATS:
        return str(v) if v4 else f"{v}d"
    if t == "Edm.DateTimeOffset":
        return v if v4 else f"datetimeoffset'{v}'"
    if t == "Edm.DateTime":
        return f"datetime'{parse_datetime(v).strftime('%Y-%m-%dT%H:%M:%S')}'"
    if t in ("Edm.Date", "Edm.TimeOfDay"):
        return v
    if t == "Edm.Guid":
        return v if v4 else f"guid'{v}'"
    if t == "Edm.Duration":
        return f"duration'{v}'"
    if t == "Edm.Binary":
        return f"binary'{v}'"
    return None


def fake_value(t):
    """A valid value of type t that is very unlikely to exist (for not-found tests)."""
    return {"Edm.String": f"zz-odh-nonexistent-{uuid.uuid4().hex[:8]}", "Edm.Guid": str(uuid.uuid4()),
            "Edm.Byte": 254, "Edm.SByte": 126, "Edm.Int16": 32766, "Edm.Int32": 2147483000,
            "Edm.Int64": 9223372036854770000, "Edm.Decimal": "987654321.123", "Edm.Double": 987654321.5,
            "Edm.DateTimeOffset": "1901-01-01T00:00:00Z", "Edm.Date": "1901-01-01"}.get(t)
