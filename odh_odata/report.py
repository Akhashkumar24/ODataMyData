"""Console summary + report.json + self-contained report.html."""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from dataclasses import asdict

ORDER = ["FAIL", "WARN", "PASS", "SKIP"]


def summary(results):
    return dict(Counter(r.outcome for r in results))


def console(results, log=print):
    by_set = defaultdict(Counter)
    for r in results:
        by_set[r.set][r.outcome] += 1
    log("\n" + "=" * 78)
    log(f"{'entity set':<28}{'tests':>7}{'PASS':>7}{'WARN':>7}{'FAIL':>7}{'SKIP':>7}")
    for s, c in sorted(by_set.items()):
        log(f"{s:<28}{sum(c.values()):>7}{c['PASS']:>7}{c['WARN']:>7}{c['FAIL']:>7}{c['SKIP']:>7}")
    t = Counter(r.outcome for r in results)
    log(f"{'TOTAL':<28}{len(results):>7}{t['PASS']:>7}{t['WARN']:>7}{t['FAIL']:>7}{t['SKIP']:>7}")
    # Group failures by their first message so 40 identical issues read as one line.
    groups = defaultdict(list)
    for r in results:
        if r.outcome in ("FAIL", "WARN"):
            msg = next((m for lvl, m in r.notes if lvl == r.outcome), "")
            groups[(r.outcome, r.category, msg[:140])].append(r)
    if groups:
        log("\nTop findings:")
        for (outcome, cat, msg), rs in sorted(groups.items(), key=lambda kv: (kv[0][0] != "FAIL", -len(kv[1])))[:25]:
            sets = sorted({x.set for x in rs})
            log(f"  {outcome} x{len(rs):<3} [{cat}] {msg}  ({', '.join(sets)[:60]})")


def write(out_dir, results, model, meta):
    data = {"meta": meta, "summary": summary(results), "model": model.summary(),
            "results": [asdict(r) for r in results]}
    (out_dir / "report.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    (out_dir / "report.html").write_text(_html(data), encoding="utf-8")
    return out_dir / "report.json", out_dir / "report.html"


def _html(d):
    e = html.escape
    s = d["summary"]
    cards = "".join(f'<div class="card {k}"><b>{s.get(k, 0)}</b><span>{k}</span></div>' for k in ORDER)
    sets = defaultdict(Counter)
    for r in d["results"]:
        sets[r["set"]][r["outcome"]] += 1
    set_rows = "".join(f"<tr><td>{e(n)}</td>" + "".join(f'<td class="{k}">{c[k] or ""}</td>' for k in ORDER) + "</tr>"
                       for n, c in sorted(sets.items()))
    model_rows = "".join(
        f"<tr><td>{e(n)}</td><td>{e(v['type'])}</td><td>{e(', '.join(v['keys']))}</td><td>{len(v['properties'])}</td>"
        f"<td>{e('; '.join(f'{k}→{x['target_set'] or '?'}' + (f' [{x['fk_kind']}: {', '.join(x['fk'])}]' if x['fk'] else '') for k, x in v['navigation'].items()))}</td></tr>"
        for n, v in d["model"]["entity_sets"].items())
    res_rows = []
    for r in sorted(d["results"], key=lambda r: (ORDER.index(r["outcome"]), r["set"], r["category"])):
        notes = "<br>".join(f"<b>{e(lvl)}</b> {e(m)}" for lvl, m in r["notes"])
        res_rows.append(
            f'<tr class="r {r["outcome"]}"><td class="{r["outcome"]}">{r["outcome"]}</td><td>{e(r["set"])}</td><td>{e(r["category"])}</td>'
            f'<td>{e(r["name"])}<div class="url">{e(r["url"])}</div>'
            + (f'<details><summary>curl</summary><code class="url">{e(r.get("curl") or "")}</code></details>' if r.get("curl") else "")
            + (f'<a href="{e(r["response"])}">response</a>' if r.get("response") else "")
            + f'</td><td>{r["status"] or ""}</td><td>{r["ms"] or ""}</td>'
            f'<td>{"" if r["rows"] is None else r["rows"]}</td><td class="notes">{notes}</td></tr>')
    problems = "".join(f"<li>{e(p)}</li>" for p in d["model"]["problems"]) or "<li>none</li>"
    m = d["meta"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>OData test report – {e(m['service'])}</title>
<style>
body{{font:14px system-ui,Segoe UI,sans-serif;margin:24px;color:#1b1f24}} h1{{font-size:20px;margin:0}} h2{{font-size:16px;margin-top:28px}}
.meta{{color:#57606a;margin:4px 0 16px}} .cards{{display:flex;gap:12px}}
.card{{border-radius:8px;padding:10px 18px;min-width:80px;border:1px solid #d0d7de}} .card b{{display:block;font-size:24px}}
table{{border-collapse:collapse;width:100%;margin-top:8px}} td,th{{border-bottom:1px solid #e5e7eb;padding:5px 8px;text-align:left;vertical-align:top}}
th{{background:#f6f8fa;position:sticky;top:0}} .url{{color:#57606a;font:12px ui-monospace,Consolas,monospace;word-break:break-all}}
.notes{{font-size:12px;max-width:520px}} td.FAIL,.card.FAIL b{{color:#cf222e;font-weight:600}} td.WARN,.card.WARN b{{color:#9a6700;font-weight:600}}
td.PASS,.card.PASS b{{color:#1a7f37}} td.SKIP,.card.SKIP b{{color:#57606a}}
.bar{{margin:12px 0;display:flex;gap:8px;align-items:center}} button{{padding:4px 10px;border:1px solid #d0d7de;border-radius:6px;background:#fff;cursor:pointer}}
button.on{{background:#1b1f24;color:#fff}} input{{padding:5px 8px;border:1px solid #d0d7de;border-radius:6px;width:320px}}
</style></head><body>
<h1>OData test report – {e(m['service'])}</h1>
<div class="meta">{e(m['started'])} · {m['requests']} requests · {m['duration_s']} s · OData {e(d['model']['version'])}</div>
{'<p class="meta"><b>Schema inferred from data</b> (no $metadata) – type findings are heuristics.</p>' if m.get('inferred') else ''}{f'<p class="meta">Scope: {e(str(m.get("scope")))}</p>' if m.get('scope') else ''}<div class="cards">{cards}</div>
<h2>Per entity set</h2><table><tr><th>Entity set</th>{''.join(f'<th>{k}</th>' for k in ORDER)}</tr>{set_rows}</table>
<h2>Model (from $metadata)</h2><table><tr><th>Entity set</th><th>Type</th><th>Keys</th><th>Props</th><th>Navigation → target [FK]</th></tr>{model_rows}</table>
<h2>Metadata problems</h2><ul>{problems}</ul>
<h2>Results</h2>
<div class="bar">{''.join(f'<button data-o="{k}">{k}</button>' for k in ORDER)}<button data-o="" class="on">ALL</button>
<input id="q" placeholder="search name, url, notes…" aria-label="search results"></div>
<table id="t"><tr><th>Outcome</th><th>Set</th><th>Category</th><th>Test / URL</th><th>HTTP</th><th>ms</th><th>Rows</th><th>Notes</th></tr>{''.join(res_rows)}</table>
<script>
let o="";const q=document.getElementById("q");
function f(){{const s=q.value.toLowerCase();document.querySelectorAll("tr.r").forEach(tr=>{{
tr.style.display=(!o||tr.classList.contains(o))&&(!s||tr.textContent.toLowerCase().includes(s))?"":"none"}})}}
document.querySelectorAll("button").forEach(b=>b.onclick=()=>{{o=b.dataset.o;document.querySelectorAll("button").forEach(x=>x.classList.toggle("on",x===b));f()}});
q.oninput=f;
</script></body></html>"""
