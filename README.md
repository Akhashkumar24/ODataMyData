# odh_odata – metadata-driven OData API tester

Point it at an ODH OData service. It finds the service root, reads `$metadata`, generates tests for every
entity set, property, navigation link and function, runs them with real sample data, and then crawls the
related entities. The results go into an HTML and a JSON report. Nothing about a particular service is
hard-coded: everything comes from the metadata and the live data.

It only sends GET requests. It never sends POST, PATCH or DELETE, and it never invokes actions.

## Setup

- **Software:** Python 3.11+ and `requests` (`pip install requests`). The automatic tunnel also needs
  AWS CLI v2 and the Session Manager plugin.
- **Token:** put your bearer token in `.env`, next to this README:
  ```
  ODATA_TOKEN=eyJ...
  ```
  Never commit `.env`.
- **AWS login:** log in once with `aws sso login --profile odh-uat`.

## Run

The ODH UAT hosts are only reachable through an SSM tunnel via a bastion instance. Pass `--ssm-target` and
the tool opens the tunnel itself, sending the real host name in both TLS SNI and the `Host` header:

```powershell
python -m odh_odata https://odh-airport-operations-data.odh-uat.cloud.aircanada.ca/api/v1.0/odata/Vehicle `
  --ssm-target i-0d2e146c63bcd9023 --ssm-profile odh-uat --ssm-region ca-central-1
```

If you already have a tunnel open on `localhost:8443`, use `--connect localhost:8443` instead.

### What you can pass as the target

| You pass | What happens |
|---|---|
| `https://host/api/v1.0/odata/Vehicle` | tests every entity set in the service |
| `https://host/api/v1.0/odata/Vehicle/Vehicles` | tests only `Vehicles` |
| `https://host/api/v1.0/odata/Vehicles` | discovery works out that the service is `Vehicle` and the set is `Vehicles` |
| `AircraftSensor` together with `--base https://host` | tries the common prefixes (`api/v1.0/odata/…`, `api/v1/odata/…`, `odata/…`, …) with singular and plural forms |

### How it handles an endpoint it has never seen

1. **Direct probe.** It sends a GET to your exact URL, including the query string. The response's
   `@odata.context` gives the exact `$metadata` URL and entity set, so there is no guessing.
2. **Path discovery.** If the probe doesn't settle it, it walks up the path and tries singular and plural
   forms. It only accepts a service that actually contains the requested entity set. If none matches, it
   stops and tells you the nearest service it found. It never silently tests a different API.
3. **Schema inference.** If `$metadata` is hidden or broken, it builds a schema from the response data:
   types, nullability, complex and collection shapes, and a best-guess key. It then runs the same tests,
   with type findings downgraded to WARN. Use `--no-metadata` to force this mode.
4. **Your `$filter` is a scope.** A `$filter` in the target URL is AND-ed into every collection query on
   that set (`$count` included). This is how endpoints that require a filter get tested, e.g.
   `.../AircraftSensor?$filter=AircraftFleetTypeCode eq 'FWD Cabin'`.
5. **Every request is saved.** Each result keeps a reproducible `curl` (the token is written as
   `$ODATA_TOKEN`), and its full response goes to `runs/<run>/responses/NNNNN.txt`. Both are linked
   from `report.html`. Use `--no-capture` to turn this off.
6. **Diagnosis.** If nothing is found, it prints the token's audience and number of roles. If the gateway
   also returns 404 without a token, it points out that 404 can mean a missing app role, not a missing API.
7. **Token expiry or rejection.** If the token expires mid-run, or the server starts answering 401 to it, the
   remaining tests are marked SKIP, not FAIL.
8. **`RequiresFilter`.** If the metadata says a set requires a `$filter` and the URL gives none, the set gets one
   check that an unfiltered list is rejected plus a SKIP, instead of dozens of identical 400s.

### Focused by default

The default run aims for few tests that each mean something, not for the largest possible number:

- **Informative columns only.** For `$select`, `$orderby` and `$filter` it takes, per Edm type, the two
  properties with the most distinct sampled values. Constant and all-null columns are dropped, because a
  range filter on a column that never changes proves nothing.
- **Core operators and functions.** Comparison operators, `in`, null checks, `contains`, `startswith`,
  `endswith`, `tolower`, `year`, `month`, `day`, and `and`/`or`/`not` combinations. The rarer functions
  (`toupper`, `length`, `indexof`, `trim`, `concat`, `substring`, `hour`, `minute`, `date`, `now()`,
  `add`, `mod`, `floor`), the optional URL forms (named key, key-as-segment, `/$value`, `$expand=*`,
  `$ref`, `odata.metadata=full|none`, `$format`) and the deep `$expand` variants beyond the first two
  navigations run only with `--thorough`.
- **One probe per route.** "Get by key" is probed once per entity set. If the service doesn't serve it,
  that is one FAIL. The key-based variants and the crawl's key lookups are skipped, not repeated as dozens
  of identical failures. The crawl then relies on `$expand` and foreign-key filters.
- **No data, no data tests.** A set that returns no rows gets one SKIP instead of empty filter tests.
- **Null semantics, once.** The "`ne` drops null rows" behaviour is reported once per set. It only counts
  when the null rows exactly explain the gap, because live data changes during a run.

On Flight (29 properties, composite key) this took a run from 438 tests to 116 and found the same classes
of defect (500s, missing key route, 403 on `/$count`). It no longer finds one subtle case, a case-insensitive
`lt` returning an equal value on a column outside the top-2 per type. Run `--thorough` for releases and
the default for day-to-day checks.

### Useful options

| Option | Default | Meaning |
|---|---|---|
| `--only A B` | all | limit the run to these entity sets |
| `--plan` | | print the parsed model, run no tests |
| `--metadata-file f.xml` | | parse a saved `$metadata` offline |
| `--sample N` | 20 | rows sampled per set; their real values drive the filter tests |
| `--depth N` / `--width N` / `--roots N` | 2 / 3 / 2 | how far, how wide and from how many rows the crawl starts |
| `--thorough` | | exhaustive mode: every property, every function, every optional URL form (default is focused) |
| `--no-negative` | | skip the negative and security tests |
| `--workers N` | 4 | number of entity sets tested in parallel |
| `--max-requests N` | 5000 | hard limit on requests per run |
| `-v` | | print every test, not only WARN and FAIL |

Every option can also come from `.env`: `ODATA_URL`, `ODATA_BASE_URL`, `ODATA_CONNECT`, `ODATA_SSM_TARGET`,
`ODATA_SSM_PROFILE`, `ODATA_SSM_REGION`, `ODATA_LOCAL_PORT`.

The exit code is `1` if any test fails, so you can use it in CI.

## What gets tested

| Layer | Checks |
|---|---|
| Metadata | Every type reference resolves. Keys exist and are non-nullable. Navigation bindings and their targets exist. Referential constraints and partners are valid. Operation signatures resolve. |
| Service | The service document matches the metadata. `OData-Version` header. `$metadata` without credentials. Unknown entity set returns 404. `odata.metadata=full` and `none`. `$format`. Function imports and bound functions that take no parameters. |
| Read and schema | Every row is checked against its declared type: Edm type and range, Nullable, MaxLength, enum members, complex and collection values, undeclared properties, null keys, duplicate keys. |
| `$search` | Only where `SearchRestrictions/Searchable` is declared: a word from the sampled data must find rows, and the count must not exceed the total. |
| Count | `$count=true`, `/$count`, filtered count. The counts must agree. |
| Paging | `$top=0` and `$top=1`. A `$skip`/`$top` window must match the same ordering. `$skip` past the end. Server-driven paging and `nextLink`, with no duplicate keys across pages. |
| `$select` | Each property on its own, a group of properties, `*`, and keys only. Selected properties must be present and unselected ones absent. |
| `$orderby` | Each sortable property, ascending and descending, plus a two-key ordering. The order is checked locally. |
| `$filter` | Uses real values from the sample. Covers `eq` `ne` `gt` `ge` `lt` `le` `in`, null checks, ranges, `contains` `startswith` `endswith` `tolower` `toupper` `length` `indexof` `trim` `concat` `substring`, `year` `month` `day` `hour` `minute` `date` `now()`, `add` `mod` `floor`, complex paths, `any()` on collections, filters on navigation properties, `and`/`or`/`not` combinations, and key filters. Every returned row is re-evaluated locally against the filter. |
| `$expand` | Each navigation property, nested `$top` `$orderby` `$count` `$filter` `$select`, two-level `$expand`, all navigations at once, `*`, and `$ref`. Expanded data must satisfy the referential constraints. |
| Keys | Get by key for several rows, compared with the copy from the list. Named-key and key-as-segment forms. `$select` and `$expand` on a single entity. Property access, `/$value`, navigation paths, a non-existent key (must be 404), and malformed keys. |
| Crawl | Starting from sampled rows, expand each navigation, then resolve every related entity in its own entity set, down to depth N. A related entity that can't be fetched by key is flagged as dangling. Reverse foreign-key filters must return the same children as `$expand`. |
| Negative and security | Unknown properties, malformed expressions, bad `$top`/`$skip`, type mismatches, unknown system options. A quote-escaping (injection) probe. No token, an invalid token, and a token with a tampered signature. |

Foreign keys come from the metadata's referential constraints. Where the metadata doesn't declare them, the
tool infers them from naming conventions, e.g. `CurrentEmployeePersonalNumber` → `Employee.PersonalNumber`.
A mismatch on a declared key is a FAIL. A mismatch on an inferred key is only a WARN.

## Reading the results

- **FAIL**: wrong data, an HTTP 5xx, a broken spec contract, a broken reference, or a security problem.
- **WARN**: an optional OData feature the server doesn't support, or behaviour that the database
  explains. Examples: case-insensitive collation, nulls sorted last, SQL null semantics, slow responses.
- **SKIP**: deliberately not run, e.g. actions, or functions that need parameters.

Each run writes a folder `runs/<service>-<timestamp>/` containing:
- `report.html`: filterable and searchable.
- `report.json`: every request with its status, timing and notes, plus the parsed model.
- `metadata.xml`: the metadata the run was based on.

The reports contain URLs and sample key values. Treat them as internal data.

## Self-test

```
python -m unittest tests/test_framework.py
```

This runs offline. It covers the parser (v2 and v4, inheritance, aliases, enums, type definitions,
annotations, functions, linting), literals, value checks, local filter evaluation, sort checks and
discovery.
