# Copyright (c) 2026 Ocean Bennett
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE.md for visible attribution requirements.

from __future__ import annotations

import json
import secrets
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from open_skeleton.mcp_server import OpenSkeletonService


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _dashboard_html(nonce: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Open Skeleton</title>
<style nonce="{nonce}">
:root{{--ink:#171717;--muted:#6b6b6b;--paper:#f6f3ed;--panel:#fff;--line:#ded9cf;--violet:#6545e8;--red:#b33a3a;--amber:#a26412;--green:#216e4e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}}
header{{background:var(--ink);color:#fff;padding:24px max(24px,calc((100vw - 1280px)/2));display:flex;justify-content:space-between;align-items:end}}
h1{{margin:0;font:700 32px/1 system-ui,sans-serif;letter-spacing:-1.2px}}header p{{margin:7px 0 0;color:#bcbcbc}}.badge{{border:1px solid #555;border-radius:999px;padding:6px 10px;font-size:12px}}
main{{max-width:1280px;margin:auto;padding:24px}}.metrics{{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:12px}}.metric,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px}}
.metric{{padding:16px}}.metric strong{{display:block;font:700 27px/1 system-ui,sans-serif;margin-top:8px}}.label{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}}
.layout{{display:grid;grid-template-columns:1fr 340px;gap:18px;margin-top:18px}}.panel{{padding:18px}}h2{{font:700 18px system-ui,sans-serif;margin:0 0 14px}}
.toolbar{{display:grid;grid-template-columns:1fr 150px 190px;gap:8px;margin-bottom:14px}}input,select{{width:100%;padding:10px 11px;border:1px solid var(--line);border-radius:7px;background:#fff;font:inherit}}
.claim{{border-top:1px solid var(--line);padding:14px 2px}}.claim:first-of-type{{border-top:0}}.claim-head{{display:flex;gap:8px;align-items:center;margin-bottom:6px}}.status{{font-size:10px;font-weight:700;padding:3px 6px;border-radius:999px;text-transform:uppercase}}
.verified{{color:var(--green);background:#e8f5ee}}.inferred{{color:var(--amber);background:#fff2dd}}.conflict{{color:var(--red);background:#fdeaea}}.unknown,.stale{{color:#555;background:#eee}}
.category{{color:var(--muted);font-size:12px}}.claim p{{margin:0}}button{{border:0;background:none;color:var(--violet);font:inherit;padding:6px 0;cursor:pointer}}button:hover{{text-decoration:underline}}
.coverage{{margin:12px 0}}progress{{display:block;width:100%;height:8px;margin-top:5px;accent-color:var(--violet)}}.small{{font-size:12px;color:var(--muted)}}.spaced{{margin-top:18px}}
#drawer{{position:fixed;right:0;top:0;width:min(620px,92vw);height:100vh;background:#111;color:#eee;padding:24px;transform:translateX(105%);transition:.18s;overflow:auto;box-shadow:-12px 0 40px #0005}}#drawer.open{{transform:none}}#drawer pre{{white-space:pre-wrap;background:#1d1d1d;padding:14px;border-radius:8px;border:1px solid #333}}#close{{color:#c9bfff;float:right}}
.empty{{padding:30px;text-align:center;color:var(--muted)}}@media(max-width:850px){{.metrics{{grid-template-columns:repeat(2,1fr)}}.layout{{grid-template-columns:1fr}}.toolbar{{grid-template-columns:1fr}}}}
</style></head><body>
<header><div><h1>Open Skeleton</h1><p>Evidence first. Source pinned. Unknowns preserved.</p></div><span class="badge" id="snapshot">loading</span></header>
<main><section class="metrics" id="metrics"></section><section class="layout"><div class="panel"><h2>Prioritized findings</h2><div class="toolbar"><input id="search" aria-label="Filter findings" placeholder="Filter findings"><select id="status" aria-label="Filter by status"><option value="">All statuses</option><option>verified</option><option>inferred</option><option>conflict</option><option>unknown</option><option>stale</option></select><select id="category" aria-label="Filter by category"><option value="">All categories</option></select></div><div id="claims"></div></div><aside><div class="panel"><h2>Coverage</h2><div id="coverage"></div></div><div class="panel spaced"><h2>Latest change</h2><div id="diff" class="small">Loading...</div></div></aside></section></main>
<aside id="drawer"><button id="close">close</button><h2>Evidence receipt</h2><div id="receipt"></div></aside>
<script nonce="{nonce}">
const state={{claims:[]}};
async function api(path){{const r=await fetch(path,{{headers:{{Accept:'application/json'}}}});if(!r.ok)throw new Error(await r.text());return r.json()}}
function el(tag,cls,text){{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}}
function metric(label,value){{const n=el('div','metric');n.append(el('span','label',label),el('strong','',value));return n}}
function renderClaims(){{const q=document.querySelector('#search').value.toLowerCase(),s=document.querySelector('#status').value,c=document.querySelector('#category').value;const root=document.querySelector('#claims');root.replaceChildren();let items=state.claims.filter(x=>(!q||x.claim.toLowerCase().includes(q))&&(!s||x.status===s)&&(!c||x.category===c));if(!items.length)root.append(el('div','empty','No matching findings.'));for(const x of items){{const card=el('article','claim'),head=el('div','claim-head');head.append(el('span','status '+x.status,x.status),el('span','category',x.category+' | '+Math.round(x.confidence*100)+'%'));card.append(head,el('p','',x.claim));for(const id of x.supporting_evidence.slice(0,2)){{const b=el('button','',`view evidence ${{id.slice(0,10)}}...`);b.onclick=()=>showEvidence(id);card.append(b)}}root.append(card)}}}}
async function showEvidence(id){{const d=await api('/api/evidence/'+encodeURIComponent(id)),root=document.querySelector('#receipt');root.replaceChildren(el('p','small',`${{d.path}}:${{d.start_line??''}} | ${{d.excerpt_status}}`),el('pre','',d.excerpt??'No source excerpt for snapshot-level census.'));document.querySelector('#drawer').classList.add('open')}}
async function boot(){{const summary=await api('/api/summary');document.querySelector('#snapshot').textContent=(summary.snapshot_id||'not analyzed').slice(0,12);const m=document.querySelector('#metrics');m.append(metric('Claims',summary.claim_count??0),metric('Verified',summary.status_counts.verified??0),metric('Conflicts',summary.status_counts.conflict??0),metric('Unknown',summary.status_counts.unknown??0),metric('Stale',summary.stale_claim_count??0));state.claims=await api('/api/claims?limit=5000');const categories=[...new Set(state.claims.map(x=>x.category))].sort();for(const c of categories)document.querySelector('#category').append(new Option(c,c));renderClaims();const cov=await api('/api/coverage'),cr=document.querySelector('#coverage');for(const x of cov){{const row=el('div','coverage'),pct=Math.round(x.coverage_ratio*100);row.append(el('div','small',`${{x.analyzer}} | ${{x.analyzed_files}}/${{x.eligible_files}} | ${{pct}}%`));const bar=document.createElement('progress');bar.max=100;bar.value=pct;bar.setAttribute('aria-label',`${{x.analyzer}} coverage`);row.append(bar);cr.append(row)}}const diff=await api('/api/diff');document.querySelector('#diff').textContent=diff.available?`${{diff.added.length}} added | ${{diff.changed.length}} changed | ${{diff.removed.length}} removed`:'No earlier distinct snapshot.'}}
for(const id of ['search','status','category'])document.querySelector('#'+id).addEventListener(id==='search'?'input':'change',renderClaims);document.querySelector('#close').onclick=()=>document.querySelector('#drawer').classList.remove('open');boot().catch(e=>document.querySelector('main').prepend(el('div','panel','Dashboard error: '+e.message)));
</script></body></html>"""


def create_dashboard_server(
    root: Path,
    state_dir: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("Dashboard host must be loopback (127.0.0.1, ::1, or localhost)")
    service = OpenSkeletonService(root, state_dir)
    nonce = secrets.token_urlsafe(18)
    html = _dashboard_html(nonce).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenSkeletonDashboard/0.1"

        def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._headers("application/json; charset=utf-8", len(payload))
            self.end_headers()
            self.wfile.write(payload)

        def _headers(self, content_type: str, length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; connect-src 'self'; img-src 'self'; "
                f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; base-uri 'none'",
            )

        def _host_is_local(self) -> bool:
            host_header = self.headers.get("Host", "")
            hostname = host_header.rsplit(":", 1)[0].strip("[]").casefold()
            return hostname in LOOPBACK_HOSTS

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_is_local():
                self._send_json({"error": "non-loopback Host rejected"}, HTTPStatus.FORBIDDEN)
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    self.send_response(HTTPStatus.OK)
                    self._headers("text/html; charset=utf-8", len(html))
                    self.end_headers()
                    self.wfile.write(html)
                    return
                if parsed.path == "/api/summary":
                    status = service.project_status()
                    claims = service.list_claims(limit=5_000) if status.get("analysis") else []
                    status["status_counts"] = dict(Counter(item["status"] for item in claims))
                    status["category_counts"] = dict(
                        Counter(item["category"] for item in claims)
                    )
                    status["claim_count"] = len(claims)
                    self._send_json(status)
                    return
                if parsed.path == "/api/claims":
                    status_filter = query.get("status", [None])[0]
                    category = query.get("category", [None])[0]
                    limit = int(query.get("limit", ["500"])[0])
                    self._send_json(
                        service.list_claims(status=status_filter, category=category, limit=limit)
                    )
                    return
                if parsed.path == "/api/coverage":
                    self._send_json(service.analysis_coverage())
                    return
                if parsed.path.startswith("/api/evidence/"):
                    evidence_id = unquote(parsed.path.removeprefix("/api/evidence/"))
                    self._send_json(service.get_evidence(evidence_id))
                    return
                if parsed.path == "/api/diff":
                    try:
                        difference = service.latest_diff()
                    except ValueError:
                        difference = {"available": False}
                    else:
                        difference["available"] = True
                    self._send_json(difference)
                    return
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (ValueError, TypeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:  # noqa: N802
            self._send_json({"error": "dashboard API is read-only"}, HTTPStatus.METHOD_NOT_ALLOWED)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve_dashboard(
    root: Path,
    state_dir: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = create_dashboard_server(root, state_dir, host=host, port=port)
    print(f"Open Skeleton dashboard: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
