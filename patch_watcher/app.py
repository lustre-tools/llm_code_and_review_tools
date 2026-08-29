#!/usr/bin/env python3
"""Small, dependency-free Patch Watcher web skeleton."""
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

PATCHES = []


def valid_url(value):
    """Return true for canonical Whamcloud Gerrit change URLs only."""
    parsed = urlparse(value.strip())
    return (
        parsed.scheme == "https"
        and parsed.hostname == "review.whamcloud.com"
        and not parsed.username
        and not parsed.password
        and parsed.path.startswith("/c/")
        and bool(parsed.path.removeprefix("/c/").strip("/").split("/")[0])
    )


def add_patch(url, title=""):
    """Add a patch, returning (patch, error). Keeps the web handler testable."""
    url = url.strip().rstrip("/")
    if not valid_url(url):
        return None, "Use an HTTPS Whamcloud Gerrit URL containing /c/."
    if any(p["url"] == url for p in PATCHES):
        return None, "That patch is already being watched."
    patch = {
        "url": url,
        "title": title.strip() or url.rsplit("/", 1)[-1],
        "status": "Pending",
        "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lifecycle": "Open", "patchset": "—", "wip": False,
        "review": "—", "unresolved": 0, "jenkins": "—", "maloo": "—",
    }
    PATCHES.append(patch)
    return patch, None


def page(message=""):
    rows = "".join(
        f"<tr><td><a href='{escape(p['url'])}'>{escape(p['title'])}</a><div class='url'>{escape(p['url'])}</div></td>"
        f"<td><span class='badge'>{escape(p.get('lifecycle', p.get('status', 'Pending')))}</span></td>"
        f"<td>{escape(str(p.get('patchset', '—')))}</td><td>{'Yes' if p.get('wip') else 'No'}</td>"
        f"<td>{escape(str(p.get('review', '—')))} / {escape(str(p.get('unresolved', 0)))}</td>"
        f"<td>{escape(str(p.get('jenkins', '—')))} / {escape(str(p.get('maloo', '—')))}</td>"
        f"<td>{escape(p.get('last_updated', '—'))}</td>"
        f"<td><form method='post' action='/remove'><input type='hidden' name='url' value='{escape(p['url'])}'><button class='danger'>Remove</button></form></td></tr>"
        for p in PATCHES
    ) or "<tr><td colspan='8' class='empty'>No patches yet. Add a Gerrit change to start watching.</td></tr>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Patch Watcher</title><style>
body{{margin:0;background:#f5f7fb;color:#172033;font:15px system-ui,sans-serif}}main{{max-width:1050px;margin:48px auto;padding:0 24px}}h1{{margin-bottom:6px}}.sub{{color:#667085;margin-top:0}}.card{{background:white;border:1px solid #e4e7ec;border-radius:14px;padding:22px;margin-top:28px;box-shadow:0 4px 16px #1018280a}}form.add{{display:flex;gap:10px;flex-wrap:wrap}}input{{border:1px solid #d0d5dd;border-radius:8px;padding:11px 12px;font-size:14px;flex:1;min-width:240px}}button{{border:0;border-radius:8px;padding:11px 16px;background:#315efb;color:white;font-weight:600;cursor:pointer}}button.danger{{background:#fff;color:#b42318;border:1px solid #fecdca;padding:7px 11px}}table{{width:100%;border-collapse:collapse;margin-top:18px}}th,td{{text-align:left;padding:14px 10px;border-top:1px solid #eaecf0}}th{{font-size:12px;text-transform:uppercase;color:#667085}}.url{{color:#667085;font-size:12px;margin-top:4px;word-break:break-all}}.badge{{background:#eef4ff;color:#315efb;border-radius:999px;padding:4px 9px;font-size:12px}}.empty{{text-align:center;color:#667085;padding:35px}}.notice{{background:#fffaeb;color:#b54708;padding:10px 12px;border-radius:8px;margin-top:16px}}</style></head>
<body><main><h1>Patch Watcher</h1><p class='sub'>Track Gerrit patches and follow their review state.</p>
<section class='card'><h2>Add a patch</h2><form class='add' method='post' action='/add'><input name='url' required placeholder='https://review.whamcloud.com/c/...'><input name='title' placeholder='Patch title (optional)'><button>Add patch</button></form>{f"<div class='notice'>{escape(message)}</div>" if message else ''}</section>
<section class='card'><h2>Watched patches <small>({len(PATCHES)})</small></h2><table><thead><tr><th>Patch</th><th>Lifecycle</th><th>Patchset</th><th>WIP</th><th>Review / unresolved</th><th>Jenkins / Maloo</th><th>Last updated</th><th></th></tr></thead><tbody>{rows}</tbody></table></section></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(page().encode())
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0)); data = parse_qs(self.rfile.read(length).decode()); path = urlparse(self.path).path
        if path == "/add":
            url = data.get("url", [""])[0]; title = data.get("title", [""])[0]
            _, error = add_patch(url, title)
            if error: self.respond(page(error)); return
        elif path == "/remove": PATCHES[:] = [p for p in PATCHES if p["url"] != data.get("url", [""])[0]]
        self.send_response(303); self.send_header("Location", "/"); self.end_headers()
    def respond(self, body):
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body.encode())


if __name__ == "__main__":
    print("Patch Watcher listening on http://127.0.0.1:8080")
    HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
