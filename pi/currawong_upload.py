#!/usr/bin/env python3
"""Currawong's "add music" page.

On any phone or computer on the same Wi-Fi, open http://currawong.local (or the address on the
player's ADD MUSIC screen), choose songs or a whole folder, and they're copied into the music
library. When they're all in, MPD is asked to look for them.

Run by currawong_setup.sh as the currawong-upload service:
    python3 currawong_upload.py --music ~/music --port 80

Standard library only. It accepts music and cover art (nothing else), only from addresses on the
local network, and never writes outside the music folder.
"""
import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUDIO = {".flac", ".mp3", ".m4a", ".aac", ".alac", ".ogg", ".oga", ".opus", ".wav", ".aif",
         ".aiff", ".wv", ".ape", ".dsf", ".dff", ".mka"}
ART = {".jpg", ".jpeg", ".png"}
EXTRA = {".cue", ".lrc"}
ALLOWED = AUDIO | ART | EXTRA
MAX_FILE = 4 * 1024 ** 3        # 4 GB: plenty for any single song
KEEP_FREE = 200 * 1024 ** 2     # always leave this much room on the card
CHUNK = 256 * 1024


def safe_path(root, rel):
    """'Artist/Album/01 Song.flac' -> a path inside root, or None: no '..', no hidden files,
    nothing absolute, at most six folders deep, and only music, cover art, .cue and .lrc."""
    parts = [p.strip() for p in rel.replace("\\", "/").split("/")]
    parts = [p for p in parts if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") or "\0" in p for p in parts):
        return None
    parts = parts[-7:]
    if os.path.splitext(parts[-1])[1].lower() not in ALLOWED:
        return None
    base = os.path.realpath(root)
    path = os.path.realpath(os.path.join(base, *parts))
    return path if path.startswith(base + os.sep) else None


def local_client(addr):
    """Only phones and computers on the home network (or this player itself)."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Add music to Currawong</title>
<style>
:root { --bg: #f6f5f1; --card: #fff; --ink: #222; --soft: #6b6a66; --line: #dddbd4; --accent: #2e2d29; --ok: #2f7d32; --bad: #b3261e; }
@media (prefers-color-scheme: dark) { :root { --bg: #1b1b19; --card: #262624; --ink: #eee; --soft: #a5a39c; --line: #3a3936; --accent: #e9e7df; --ok: #7cc47f; --bad: #f2b8b5; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 17px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 560px; margin: 0 auto; padding: 28px 18px 60px; }
h1 { font-size: 26px; margin: 0 0 6px; }
p { margin: 0 0 14px; color: var(--soft); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 18px; margin: 18px 0; }
.buttons { display: flex; gap: 10px; flex-wrap: wrap; }
label.btn { flex: 1 1 200px; text-align: center; padding: 16px; border-radius: 12px; background: var(--accent); color: var(--bg); font-weight: 600; cursor: pointer; }
label.btn.alt { background: transparent; color: var(--ink); border: 2px solid var(--accent); }
input[type=file] { display: none; }
#drop { margin-top: 12px; border: 2px dashed var(--line); border-radius: 12px; padding: 22px; text-align: center; color: var(--soft); }
#drop.over { border-color: var(--accent); color: var(--ink); }
#summary { font-weight: 600; color: var(--ink); }
ul { list-style: none; padding: 0; margin: 10px 0 0; }
li { display: flex; justify-content: space-between; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--line); font-size: 15px; }
li span.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
li span.state { color: var(--soft); white-space: nowrap; }
li.done span.state { color: var(--ok); } li.fail span.state { color: var(--bad); }
.bar { height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; margin-top: 10px; }
.bar div { height: 100%; width: 0; background: var(--accent); transition: width .2s; }
#free { font-size: 14px; }
</style></head>
<body><main>
<h1>Add music to Currawong</h1>
<p>Choose songs or a whole album folder. They copy over Wi-Fi, so keep this page open until it says it's done.</p>
<div class="card">
  <div class="buttons">
    <label class="btn">Choose songs<input id="files" type="file" multiple accept="audio/*,.flac,.m4a,.alac,.opus,.ogg,.wav,.aiff,.jpg,.png"></label>
    <label class="btn alt" id="folderbtn">Choose a folder<input id="folder" type="file" webkitdirectory multiple></label>
  </div>
  <div id="drop">or drag music here</div>
</div>
<div class="card" id="progress" hidden>
  <div id="summary"></div>
  <div class="bar"><div id="bar"></div></div>
  <ul id="list"></ul>
</div>
<p id="free"></p>
</main>
<script>
const OK = /\.(flac|mp3|m4a|aac|alac|ogg|oga|opus|wav|aiff?|wv|ape|dsf|dff|mka|jpe?g|png|cue|lrc)$/i;
const AUDIO = /\.(flac|mp3|m4a|aac|alac|ogg|oga|opus|wav|aiff?|wv|ape|dsf|dff|mka)$/i;
let queue = [], busy = false, total = 0, sent = 0, songs = 0;
const $ = id => document.getElementById(id);
function mb(n) { return (n / 1048576).toFixed(n > 104857600 ? 0 : 1) + " MB"; }
async function status() {
  try { const s = await (await fetch("/api/status")).json();
        $("free").textContent = `Space left on Currawong: ${(s.free / 1073741824).toFixed(1)} GB`; } catch (e) {}
}
function add(file, rel) {
  if (!OK.test(file.name)) return;
  const li = document.createElement("li");
  li.innerHTML = '<span class="name"></span><span class="state">waiting</span>';
  li.firstChild.textContent = rel;
  $("list").appendChild(li);
  queue.push({file, rel, li}); total += file.size;
  if (AUDIO.test(file.name)) songs++;
  $("progress").hidden = false; show(); if (!busy) next();
}
function show() {
  $("summary").textContent = busy ? `Copying… ${mb(sent)} of ${mb(total)}` : $("summary").textContent;
  $("bar").style.width = total ? (100 * sent / total) + "%" : "0";
}
function next() {
  const job = queue.shift();
  if (!job) { busy = false; finish(); return; }
  busy = true;
  const xhr = new XMLHttpRequest(); let base = sent;
  xhr.open("PUT", "/api/upload?path=" + encodeURIComponent(job.rel));
  xhr.upload.onprogress = e => { sent = base + e.loaded; job.li.lastChild.textContent = Math.round(100 * e.loaded / e.total) + "%"; show(); };
  xhr.onload = () => {
    sent = base + job.file.size;
    if (xhr.status < 300) { job.li.className = "done"; job.li.lastChild.textContent = "✓"; }
    else { job.li.className = "fail"; let why = "failed"; try { why = JSON.parse(xhr.responseText).error; } catch (e) {} job.li.lastChild.textContent = why; }
    show(); next();
  };
  xhr.onerror = () => { job.li.className = "fail"; job.li.lastChild.textContent = "lost connection"; next(); };
  xhr.send(job.file);
}
async function finish() {
  try { await fetch("/api/done", {method: "POST"}); } catch (e) {}
  const bad = document.querySelectorAll("li.fail").length;
  $("summary").textContent = bad
    ? `Finished, but ${bad} file${bad > 1 ? "s" : ""} didn't copy. You can try those again.`
    : `Done! ${songs} song${songs === 1 ? "" : "s"} added. They'll show up in Library within a minute.`;
  $("bar").style.width = "100%"; status();
}
$("files").onchange = e => { for (const f of e.target.files) add(f, "Uploaded/" + f.name); e.target.value = ""; };
$("folder").onchange = e => { for (const f of e.target.files) add(f, f.webkitRelativePath || f.name); e.target.value = ""; };
if (!("webkitdirectory" in document.createElement("input"))) $("folderbtn").hidden = true;
function walk(entry, path) {
  if (entry.isFile) entry.file(f => add(f, path + f.name));
  else if (entry.isDirectory) { const r = entry.createReader(); const read = () => r.readEntries(es => { es.forEach(x => walk(x, path + entry.name + "/")); if (es.length) read(); }); read(); }
}
const drop = $("drop");
drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = e => {
  e.preventDefault(); drop.classList.remove("over");
  for (const item of e.dataTransfer.items) {
    const entry = item.webkitGetAsEntry && item.webkitGetAsEntry();
    if (entry && entry.isDirectory) walk(entry, "");
    else if (item.kind === "file") { const f = item.getAsFile(); add(f, "Uploaded/" + f.name); }
  }
};
status();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "Currawong"
    music = "."
    update_cmd = ["mpc", "-q", "update"]

    def log_message(self, fmt, *args):  # quiet: the journal doesn't need every request
        pass

    def _reply(self, code, body, kind="application/json; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._reply(code, json.dumps(obj))

    def _allowed(self):
        if local_client(self.client_address[0]):
            return True
        self._json(403, {"error": "only from the home network"})
        return False

    def do_GET(self):
        if not self._allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._reply(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/status":
            usage = shutil.disk_usage(self.music)
            self._json(200, {"name": socket.gethostname(), "free": max(0, usage.free - KEEP_FREE)})
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self):
        if not self._allowed():
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/api/upload":
            return self._json(404, {"error": "not found"})
        rel = urllib.parse.parse_qs(url.query).get("path", [""])[0]
        dest = safe_path(self.music, rel)
        if dest is None:
            return self._json(415, {"error": "not a music file"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._json(411, {"error": "size missing"})
        if length <= 0 or length > MAX_FILE:
            return self._json(413, {"error": "too big"})
        if shutil.disk_usage(self.music).free - length < KEEP_FREE:
            return self._json(507, {"error": "Currawong is full"})
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        got = 0
        try:
            with open(tmp, "wb") as f:
                while got < length:
                    chunk = self.rfile.read(min(CHUNK, length - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
            if got != length:
                os.remove(tmp)
                return self._json(400, {"error": "copy interrupted"})
            os.replace(tmp, dest)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._json(500, {"error": "couldn't save it"})
        self._json(201, {"ok": True, "path": os.path.relpath(dest, self.music)})

    def do_POST(self):
        if not self._allowed():
            return
        if urllib.parse.urlsplit(self.path).path != "/api/done":
            return self._json(404, {"error": "not found"})
        try:  # MPD looks for the new files (in the background: this returns at once)
            subprocess.run(self.update_cmd, timeout=15, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass
        self._json(200, {"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--music", default=os.path.expanduser("~/music"))
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--bind", default="")
    args = ap.parse_args()
    os.makedirs(args.music, exist_ok=True)
    Handler.music = args.music
    with Server((args.bind, args.port), Handler) as httpd:
        print(f"Add-music page on port {args.port}, saving into {args.music}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
