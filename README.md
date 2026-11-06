# odh_odata – metadata-driven OData API tester

Point it at an ODH OData service. It finds the service root, reads `$metadata`, generates tests for every
entity set, property, navigation link and function, runs them with real sample data, and then crawls the
related entities. The results go into an HTML and a JSON report. Nothing about a particular service is
hard-coded: everything comes from the metadata and the live data.

It only sends GET requests. It never sends PATCH or DELETE and never invokes actions. The one exception is
the opt-in `--batch` test: a single POST to `$batch` that carries only GET requests.

## Setup

- **Software:** Python 3.11+ and `pip install -r requirements.txt`. The automatic tunnel also needs
  AWS CLI v2 and the Session Manager plugin.
- **Token:** either put a bearer token in `.env` (`ODATA_TOKEN=eyJ...`), or let the tool request one with
  the client-credentials flow. Fill these in `.env` (names are listed in `.env.example`):
  ```
  ODATA_TOKEN_URL=https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token
  ODATA_GRANT_TYPE=client_credentials
  ODATA_CLIENT_ID=...
  ODATA_CLIENT_SECRET=...
  ODATA_SCOPE=api://<app-id>/.default
  ```
  A valid `ODATA_TOKEN` is used as is. If it is empty or expired, a new token is requested at the start, and
  it is renewed automatically during a long run. The secret is never printed or written to a report.
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
5. **Evidence for every finding.** Each result keeps a reproducible `curl` (the token is written as
   `$ODATA_TOKEN`). The full response of every WARN and FAIL goes to `runs/<run>/responses/NNNNN.txt`,
   linked from `report.html`. Passing responses are not saved by default, because they hold real data
   (for example employee records) that nobody needs to read. `--capture all` saves every response, which
   `--replay` needs. `--capture none` saves nothing.
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
| `--batch` | | also test `$batch` (a POST carrying only GETs) |
| `--capture failures\|all\|none` | failures | which responses are saved under `responses/` |
| `--baseline FILE` | | known-issues file: only findings that are not in it fail the run |
| `--update-baseline` | | accept this run's findings into the `--baseline` file |
| `--replay RUN_DIR` | | offline: re-judge a run recorded with `--capture all` using the current code |
| `-v` | | print every test, not only WARN and FAIL |

Every option can also come from `.env`: `ODATA_URL`, `ODATA_BASE_URL`, `ODATA_CONNECT`, `ODATA_SSM_TARGET`,
`ODATA_SSM_PROFILE`, `ODATA_SSM_REGION`, `ODATA_LOCAL_PORT`, and the token settings above. With the SSM and
token settings in `.env`, a run is just `python -m odh_odata <url>`.

The exit code is `1` if any test fails. With `--baseline` it is `1` only for new FAILs (see below).

### Baseline: fail on regressions, not on known defects

A service with known defects fails every run, so a red build stops meaning anything. A baseline file
records the accepted findings:

```powershell
python -m odh_odata <url> --baseline baselines/flight.json --update-baseline   # once: accept today's findings
python -m odh_odata <url> --baseline baselines/flight.json                     # every run after that
```

Each run then prints, and marks in `report.html` (the **NEW** filter):
- **new**: a WARN or FAIL that is not in the baseline, or a WARN that became a FAIL. Only a new FAIL sets exit code 1.
- **fixed**: a baseline finding whose test ran and now passes. Remove it with `--update-baseline`.
- **known**: still open, accepted.

Findings are matched by entity set, category and test name, with the sampled values masked, so
`Status eq 'Active'` today and `Status eq 'Retired'` tomorrow count as the same test. `--update-baseline`
keeps entries for sets that did not run, so `--only` runs don't drop them. Commit the baseline file: it
holds test names and outcomes only, no response data.

### Replay: change the framework without the tunnel

```powershell
python -m odh_odata <url> --capture all                       # record once
python -m odh_odata --replay runs/api-v1-0-20261106-003304    # after every code change, offline
```

The replay serves every request from the recording, runs the current code and lists the outcomes that
changed. Use it to check that a new validator or test generator does what you expect against real
service behaviour. Exit code `1` means something changed. Runs recorded before this version can't be
replayed reliably: their responses were re-serialised and cut at 256 KB.

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
| `$batch` (`--batch`) | Up to three sets in one multipart batch, plus one invalid request. Each answer inside must match the same GET sent alone, and the invalid one must fail on its own (404) without failing the batch. |
| Negative and security | Unknown properties, malformed expressions, bad `$top`/`$skip`, type mismatches, unknown system options. A quote-escaping (injection) probe. No token, an invalid token, and a token with a tampered signature. |

### Capability annotations it follows

The test plan comes from the `Org.OData.Capabilities.V1` annotations, whether they are inline, on
`Container/Set`, on `Container/Set/Navigation`, or on the container itself (which applies to every set):

| Annotation | Effect |
|---|---|
| `CountRestrictions`, `FilterRestrictions`, `SortRestrictions`, `ExpandRestrictions` | Restricted options and properties are not tested as if they should work. `Countable=false` expects `$count` to be rejected. |
| `FilterRestrictions/RequiresFilter` | Without a `$filter` in the URL: one "unfiltered list is rejected" check and a SKIP, not dozens of 400s. |
| `FilterFunctions` | Only the listed functions are tested, and they must work (no "optional" leniency). The others are listed in one SKIP. |
| `TopSupported=false` | `$top` is never sent to that set, and row limits are not checked. |
| `ExpandRestrictions/MaxLevels` | No two-level `$expand` when it is 1. |
| `NavigationRestrictions` (`Navigability`, per-navigation `TopSupported`, `CountRestrictions`, …) | Navigation paths that are not offered are not requested. Nested `$expand` options follow the navigation's own restrictions. |
| `SearchRestrictions/Searchable` | `$search` is tested only where declared. |
| `BatchSupported=false` | `--batch` is skipped. |

### Server errors are grouped by cause

A 5xx on a request that tests one property is reported as `server error 500 involving property 'X'`, so
the console's top findings read as one line per broken property. After 3 server errors on one property,
the remaining tests on it are skipped: the defect is already proven, and more requests only add noise.

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
python -m unittest discover -s tests
```

This runs offline in a few seconds. It covers the parser (v2 and v4, inheritance, aliases, enums, type
definitions, capability annotations at every target level, functions, linting), literals, value checks,
local filter evaluation, sort checks, discovery, the baseline, and replay. `tests/stub.py` is an in-memory
service that ignores every query option. The end-to-end tests run the whole suite against it and check that
every test category notices, and that a recorded run replays with identical outcomes.

## Code layout

| Module | Role |
|---|---|
| `metadata.py` | CSDL parser (v2/v3/v4), capability model, metadata lint, schema inference |
| `edm.py` | Edm type checks, URL literals, Python conversions |
| `http.py` | Client (SSM tunnel, SNI), token handling, service discovery |
| `suite/runner.py` | Request plumbing, judging, validators, orchestration |
| `suite/{service,read,expand,filters,keys,negative,batch}.py` | One test family each |
| `baseline.py`, `replay.py`, `report.py` | Known-issue gating, offline replay, console/JSON/HTML reports |
