#!/usr/bin/env python3
"""PaperArena — local-host swipe deck for triaging candidate papers.

Zero third-party dependencies. Python 3.8+ stdlib only.

Subcommands
-----------
  serve        Run the web server (serves the UI + JSON API). Run in background.
  ensure-server
               Start the server if needed and wait until it is reachable.
  new-round    Publish a new batch of candidates for the open browser to pick up.
  validate-candidates
               Check candidate ids/URLs/PDF links before publishing.
  wait         Block until the current round is submitted, then print feedback JSON.
  status       Print the current round/state as JSON and exit.

The agent never needs to manage round numbers or ports by hand beyond the
defaults below; new-round assigns the next round id automatically.

State files (under --state, default ./.paperarena):
  candidates.json   current round, written by new-round       (agent -> UI)
  feedback.json     submitted swipes for the current round    (UI -> agent)
  history.json      append-only decisions across the chat
  library.json      persistent liked/disliked paper library
  .done             marker file; present once the round is submitted
"""

import argparse
import difflib
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from xml.etree import ElementTree

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_UI = os.path.join(HERE, "ui")
DEFAULT_STATE = os.path.join(os.getcwd(), ".paperarena")
DEFAULT_PORT = 8765

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

EVENT_COND = threading.Condition()
EVENT_SEQ = 0
ARXIV_ID_RE = re.compile(r"(?<![\w.])(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)(?![\w.])", re.I)
CA_BUNDLE_PATHS = (
    # macOS system Python and Python.org installs
    "/etc/ssl/cert.pem",
    # Debian/Ubuntu/Gentoo
    "/etc/ssl/certs/ca-certificates.crt",
    # RHEL/Fedora/CentOS
    "/etc/pki/tls/certs/ca-bundle.crt",
    # Alpine/OpenSUSE
    "/etc/ssl/ca-bundle.pem",
    # RHEL/Fedora extracted trust store
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _paper_key(paper):
    for key in ("id", "doi", "url", "pdf_url", "title"):
        value = paper.get(key) if isinstance(paper, dict) else None
        if value:
            return str(value)
    return ""


def _server_record_path(state):
    return os.path.join(state, "server.json")


def _state_paths(state):
    return {
        "candidates": os.path.join(state, "candidates.json"),
        "feedback": os.path.join(state, "feedback.json"),
        "history": os.path.join(state, "history.json"),
        "library": os.path.join(state, "library.json"),
        "done": os.path.join(state, ".done"),
    }


def _read_candidates(state):
    paths = _state_paths(state)
    return _read_json(paths["candidates"], {"round": 0, "items": []})


def _read_library(state):
    paths = _state_paths(state)
    data = _read_json(paths["library"], {"liked": [], "disliked": []})
    if not isinstance(data, dict):
        data = {"liked": [], "disliked": []}
    data.setdefault("liked", [])
    data.setdefault("disliked", [])
    return data


def _read_history(state):
    paths = _state_paths(state)
    data = _read_json(paths["history"], {"events": []})
    if not isinstance(data, dict):
        data = {"events": []}
    data.setdefault("events", [])
    return data


def _normalize_arxiv_id(raw):
    if not raw:
        return None
    value = str(raw).strip()
    parsed = urlparse(value)
    if parsed.netloc.lower().endswith("arxiv.org"):
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("abs", "pdf"):
            value = parts[1]
    if value.endswith(".pdf"):
        value = value[:-4]
    match = ARXIV_ID_RE.search(value)
    return match.group(1) if match else None


def _arxiv_id_from_paper(paper):
    for key in ("id", "url", "pdf_url"):
        found = _normalize_arxiv_id(paper.get(key))
        if found:
            return found
    return None


def _arxiv_pdf_url(arxiv_id):
    return f"https://arxiv.org/pdf/{arxiv_id}"


def _arxiv_abs_url(arxiv_id):
    return f"https://arxiv.org/abs/{arxiv_id}"


def _compact_title(title):
    return " ".join(re.findall(r"[a-z0-9]+", str(title).lower()))


def _title_similarity(a, b):
    ca, cb = _compact_title(a), _compact_title(b)
    if not ca or not cb:
        return 0.0
    return difflib.SequenceMatcher(None, ca, cb).ratio()


def _https_contexts():
    """Return portable TLS contexts without requiring a Python dependency.

    The default context comes first so SSL_CERT_FILE, SSL_CERT_DIR, and
    platform-managed trust stores keep working. Some Python.org macOS installs
    have no configured OpenSSL bundle, so also try certifi when installed and
    common operating-system CA bundle paths.
    """
    contexts = []
    seen_cafiles = set()

    try:
        contexts.append(ssl.create_default_context())
    except Exception:
        pass

    try:
        import certifi  # optional
        cafile = certifi.where()
        if cafile and os.path.isfile(cafile):
            seen_cafiles.add(os.path.realpath(cafile))
            contexts.append(ssl.create_default_context(cafile=cafile))
    except Exception:
        pass

    for cafile in CA_BUNDLE_PATHS:
        real = os.path.realpath(cafile)
        if real in seen_cafiles or not os.path.isfile(cafile):
            continue
        try:
            contexts.append(ssl.create_default_context(cafile=cafile))
            seen_cafiles.add(real)
        except Exception:
            continue

    return contexts or [None]


def _urlopen_with_tls_fallback(req, timeout):
    """Open an outbound URL, retrying with available trusted CA bundles."""
    last_exc = None
    for context in _https_contexts():
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=context)
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("no TLS context available")


def _fetch_arxiv_titles(arxiv_ids):
    unique = []
    for arxiv_id in arxiv_ids:
        base = re.sub(r"v\d+$", "", arxiv_id)
        if base not in unique:
            unique.append(base)
    if not unique:
        return {}
    url = "https://export.arxiv.org/api/query?id_list=" + ",".join(quote(v) for v in unique)
    req = urllib.request.Request(url, headers={"User-Agent": "PaperArena/1.0"})
    with _urlopen_with_tls_fallback(req, timeout=15) as resp:
        xml = resp.read()
    root = ElementTree.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    titles = {}
    for entry in root.findall("atom:entry", ns):
        id_text = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
        arxiv_id = _normalize_arxiv_id(id_text)
        if arxiv_id and title:
            titles[re.sub(r"v\d+$", "", arxiv_id)] = title
    return titles


def _validate_and_normalize_candidates(items, verify_links=False):
    errors = []
    warnings = []
    normalized = []
    arxiv_checks = []

    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            errors.append({"index": index, "error": "candidate must be an object"})
            continue
        item = dict(raw)
        title = str(item.get("title") or "").strip()
        if not item.get("id"):
            errors.append({"index": index, "title": title, "error": "missing required id"})
        if not title:
            errors.append({"index": index, "id": item.get("id", ""), "error": "missing required title"})

        arxiv_ids = {}
        for key in ("id", "url", "pdf_url"):
            arxiv_id = _normalize_arxiv_id(item.get(key))
            if arxiv_id:
                arxiv_ids[key] = arxiv_id
        arxiv_bases = {re.sub(r"v\d+$", "", v) for v in arxiv_ids.values()}
        if len(arxiv_bases) > 1:
            errors.append({
                "index": index,
                "id": item.get("id", ""),
                "title": title,
                "error": "arXiv id/url/pdf_url disagree",
                "arxiv_ids": arxiv_ids,
            })
        elif arxiv_bases:
            arxiv_id = arxiv_ids.get("id") or arxiv_ids.get("url") or arxiv_ids.get("pdf_url")
            item["url"] = _arxiv_abs_url(arxiv_id)
            item["pdf_url"] = _arxiv_pdf_url(arxiv_id)
            arxiv_checks.append((index, arxiv_id, title))

        if item.get("pdf_url") and not str(item["pdf_url"]).lower().startswith(("http://", "https://")):
            errors.append({"index": index, "id": item.get("id", ""), "error": "pdf_url must be http(s)"})
        if item.get("url") and not str(item["url"]).lower().startswith(("http://", "https://")):
            errors.append({"index": index, "id": item.get("id", ""), "error": "url must be http(s)"})

        normalized.append(item)

    if verify_links and arxiv_checks and not errors:
        try:
            titles = _fetch_arxiv_titles([arxiv_id for _, arxiv_id, _ in arxiv_checks])
        except Exception as exc:
            errors.append({"error": f"could not verify arXiv metadata: {exc}"})
        else:
            for index, arxiv_id, title in arxiv_checks:
                base = re.sub(r"v\d+$", "", arxiv_id)
                official = titles.get(base)
                if not official:
                    errors.append({
                        "index": index,
                        "id": arxiv_id,
                        "title": title,
                        "error": "arXiv id not found by metadata API",
                    })
                    continue
                similarity = _title_similarity(title, official)
                if similarity < 0.72:
                    errors.append({
                        "index": index,
                        "id": arxiv_id,
                        "title": title,
                        "official_title": official,
                        "similarity": round(similarity, 3),
                        "error": "candidate title does not match arXiv metadata",
                    })

    return normalized, errors, warnings


def _seen_ids(state):
    seen = set()
    for event in _read_history(state).get("events", []):
        if event.get("id"):
            seen.add(str(event["id"]))
    library = _read_library(state)
    for paper in library.get("liked", []) + library.get("disliked", []):
        key = _paper_key(paper)
        if key:
            seen.add(key)
    return seen


def _upsert_by_key(items, paper):
    key = _paper_key(paper)
    out = [item for item in items if _paper_key(item) != key]
    out.append(paper)
    return out


def _remove_by_key(items, paper):
    key = _paper_key(paper)
    return [item for item in items if _paper_key(item) != key]


def _record_feedback(state, payload):
    paths = _state_paths(state)
    candidates = _read_candidates(state)
    by_id = {}
    for item in candidates.get("items", []):
        key = _paper_key(item)
        if key:
            by_id[key] = item

    history = _read_history(state)
    library = _read_library(state)
    timestamp = _now()
    round_id = payload.get("round", candidates.get("round", 0))

    for response in payload.get("items", []):
        decision = response.get("decision")
        if decision not in ("like", "dislike"):
            continue
        key = _paper_key(response)
        paper = dict(by_id.get(key, response))
        paper.update({
            "last_decision": decision,
            "reasons": response.get("reasons", []),
            "note": response.get("note", ""),
            "round": round_id,
            "decided_at": timestamp,
        })
        event = {
            "round": round_id,
            "decided_at": timestamp,
            "id": key,
            "title": response.get("title") or paper.get("title", ""),
            "decision": decision,
            "reasons": response.get("reasons", []),
            "note": response.get("note", ""),
        }
        history["events"].append(event)
        if decision == "like":
            library["liked"] = _upsert_by_key(library.get("liked", []), paper)
            library["disliked"] = _remove_by_key(library.get("disliked", []), paper)
        else:
            library["disliked"] = _upsert_by_key(library.get("disliked", []), paper)
            library["liked"] = _remove_by_key(library.get("liked", []), paper)

    _write_json_atomic(paths["feedback"], payload)
    _write_json_atomic(paths["history"], history)
    _write_json_atomic(paths["library"], library)
    open(paths["done"], "w").close()
    return {"history": history, "library": library}


def _snapshot(state):
    candidates = _read_candidates(state)
    library = _read_library(state)
    candidates["library"] = library
    candidates["history"] = {
        "liked": len(library.get("liked", [])),
        "disliked": len(library.get("disliked", [])),
        "events": len(_read_history(state).get("events", [])),
    }
    return candidates


def _compact_feedback(state, max_feedback=12, max_liked=12, max_dislikes=12, include_seen=False, max_seen=0):
    max_feedback = max(0, max_feedback)
    max_liked = max(0, max_liked)
    max_dislikes = max(0, max_dislikes)
    max_seen = max(0, max_seen)

    feedback = _read_json(_state_paths(state)["feedback"], {})
    history = _read_history(state)
    library = _read_library(state)

    def compact_paper(paper):
        return {
            "id": _paper_key(paper),
            "title": paper.get("title", ""),
            "reasons": paper.get("reasons", []),
            "note": paper.get("note", ""),
        }

    events = history.get("events", [])
    recent_feedback = list(feedback.get("items", []))[-max_feedback:] if max_feedback else []
    liked = [compact_paper(p) for p in library.get("liked", [])]
    disliked_events = [e for e in events if e.get("decision") == "dislike"]
    seen_ids = sorted(_seen_ids(state))

    result = {
        "round": feedback.get("round"),
        "counts": {
            "liked": len(library.get("liked", [])),
            "disliked": len(library.get("disliked", [])),
            "history_events": len(events),
            "seen": len(seen_ids),
        },
        "new_feedback": recent_feedback,
        "liked_library_recent": liked[-max_liked:] if max_liked else [],
        "recent_dislikes": [
            {
                "id": e.get("id", ""),
                "title": e.get("title", ""),
                "reasons": e.get("reasons", []),
                "note": e.get("note", ""),
            }
            for e in (disliked_events[-max_dislikes:] if max_dislikes else [])
        ],
        "seen_filter": {
            "enforced_by": "new-round",
            "note": "Previously liked/disliked papers are filtered locally unless --allow-seen is used.",
        },
    }
    if include_seen:
        result["seen_ids"] = seen_ids if not max_seen else seen_ids[-max_seen:]
    return result


def _notify_event():
    global EVENT_SEQ
    with EVENT_COND:
        EVENT_SEQ += 1
        EVENT_COND.notify_all()


def _host_is_public(host):
    """SSRF guard: reject loopback/private/link-local/reserved targets."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            return False
    return True


class ArenaHandler(BaseHTTPRequestHandler):
    state_dir = DEFAULT_STATE
    ui_dir = DEFAULT_UI

    # --- helpers -----------------------------------------------------------
    def _state(self, name):
        return os.path.join(self.state_dir, name)

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # keep background output quiet
        pass

    # --- routing -----------------------------------------------------------
    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == "/api/health":
            return self._send_json({
                "ok": True,
                "pid": os.getpid(),
                "state": os.path.abspath(self.state_dir),
                "ui": os.path.abspath(self.ui_dir),
            })
        if path == "/api/candidates":
            return self._send_json(_snapshot(self.state_dir))
        if path == "/api/library":
            return self._send_json(_read_library(self.state_dir))
        if path == "/api/history":
            return self._send_json(_read_history(self.state_dir))
        if path == "/api/events":
            return self._events()
        if path == "/api/wait-round":
            qs = parse_qs(parsed_url.query)
            after_round = int((qs.get("round") or ["0"])[0] or 0)
            timeout = float((qs.get("timeout") or ["300"])[0] or 300)
            return self._wait_round(after_round, timeout)
        if path == "/api/wait-feedback":
            qs = parse_qs(parsed_url.query)
            timeout = float((qs.get("timeout") or ["0"])[0] or 0)
            return self._wait_feedback(timeout)
        if path == "/api/status":
            cand = _read_candidates(self.state_dir)
            library = _read_library(self.state_dir)
            return self._send_json({
                "round": cand.get("round", 0),
                "candidates": len(cand.get("items", [])),
                "liked": len(library.get("liked", [])),
                "disliked": len(library.get("disliked", [])),
                "done": os.path.exists(self._state(".done")),
            })
        if path == "/api/pdf":
            target = (parse_qs(parsed_url.query).get("url") or [""])[0]
            return self._proxy_pdf(target)
        return self._serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/reload":
            _notify_event()
            return self._send_json({"ok": True})
        if path != "/api/feedback":
            return self._send_json({"error": "not found"}, 404)
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send_json({"error": "invalid json"}, 400)
        recorded = _record_feedback(self.state_dir, payload)
        _notify_event()
        return self._send_json({
            "ok": True,
            "liked": len(recorded["library"].get("liked", [])),
            "disliked": len(recorded["library"].get("disliked", [])),
        })

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def write_event(name, data):
            body = "event: %s\ndata: %s\n\n" % (name, json.dumps(data, ensure_ascii=False))
            self.wfile.write(body.encode("utf-8"))
            self.wfile.flush()

        last_seen = EVENT_SEQ
        try:
            write_event("candidates", _snapshot(self.state_dir))
            while True:
                with EVENT_COND:
                    EVENT_COND.wait(timeout=25)
                    changed = EVENT_SEQ != last_seen
                    last_seen = EVENT_SEQ
                if changed:
                    write_event("candidates", _snapshot(self.state_dir))
                else:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _wait_feedback(self, timeout):
        deadline = time.time() + timeout if timeout else None
        while not os.path.exists(self._state(".done")):
            remaining = None if deadline is None else max(0, deadline - time.time())
            if remaining == 0:
                return self._send_json({"error": "timeout"}, 408)
            with EVENT_COND:
                EVENT_COND.wait(timeout=remaining)
        return self._send_json(_read_json(self._state("feedback.json"), {}))

    def _wait_round(self, after_round, timeout):
        deadline = time.time() + timeout if timeout else None
        while True:
            snap = _snapshot(self.state_dir)
            if int(snap.get("round", 0)) > after_round:
                return self._send_json(snap)
            remaining = None if deadline is None else max(0, deadline - time.time())
            if remaining == 0:
                return self._send_json(snap)
            with EVENT_COND:
                EVENT_COND.wait(timeout=remaining)

    # --- static files ------------------------------------------------------
    def _serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(self.ui_dir, rel))
        if not full.startswith(os.path.abspath(self.ui_dir)):
            return self._send_json({"error": "forbidden"}, 403)
        if not os.path.isfile(full):
            return self._send_json({"error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- PDF proxy ---------------------------------------------------------
    def _proxy_pdf(self, target):
        """Fetch a remote PDF and stream it back same-origin so it embeds."""
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return self._send_json({"error": "url must be http(s)"}, 400)
        if not _host_is_public(parsed.hostname):
            return self._send_json({"error": "blocked host"}, 403)
        req = urllib.request.Request(target, headers={
            "Accept": "application/pdf,*/*;q=0.8",
            "User-Agent": "PaperArena/1.0",
        })
        try:
            with _urlopen_with_tls_fallback(req, timeout=30) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "application/pdf")
        except Exception as exc:
            return self._send_json({"error": "fetch failed: %s" % exc}, 502)
        if "pdf" not in ctype.lower():
            ctype = "application/pdf"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", "inline")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# --- subcommands -----------------------------------------------------------
def _server_health(port, timeout=0.5):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _port_accepts_connection(port, timeout=0.2):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _server_matches(health, state, ui):
    if not health or not health.get("ok"):
        return False
    return (
        os.path.abspath(health.get("state", "")) == os.path.abspath(state)
        and os.path.abspath(health.get("ui", "")) == os.path.abspath(ui)
    )


def _find_server_port(state, ui, preferred, port_max):
    for port in range(preferred, port_max + 1):
        health = _server_health(port)
        if _server_matches(health, state, ui):
            return port, health
    return None, None


def _find_available_port(preferred, port_max):
    for port in range(preferred, port_max + 1):
        if _server_health(port):
            continue
        if not _port_accepts_connection(port):
            return port
    return None


def _ensure_server(state, ui, port=DEFAULT_PORT, port_max=8799, open_browser=False, wait_timeout=5.0):
    state = os.path.abspath(state)
    ui = os.path.abspath(ui)
    os.makedirs(state, exist_ok=True)

    found_port, health = _find_server_port(state, ui, port, port_max)
    if found_port is not None:
        url = f"http://127.0.0.1:{found_port}"
        _write_json_atomic(_server_record_path(state), {
            "url": url,
            "port": found_port,
            "pid": health.get("pid"),
            "state": state,
            "ui": ui,
            "checked_at": _now(),
        })
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        return {"ok": True, "url": url, "port": found_port, "pid": health.get("pid"), "started": False}

    selected_port = _find_available_port(port, port_max)
    if selected_port is None:
        raise RuntimeError(f"no available PaperArena port in {port}-{port_max}")

    log_path = os.path.join(state, "server.log")
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        [
            sys.executable,
            os.path.abspath(__file__),
            "serve",
            "--state",
            state,
            "--ui",
            ui,
            "--port",
            str(selected_port),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    log.close()

    deadline = time.time() + wait_timeout
    health = None
    while time.time() < deadline:
        health = _server_health(selected_port, timeout=0.5)
        if _server_matches(health, state, ui):
            break
        if proc.poll() is not None:
            raise RuntimeError(f"PaperArena server exited early; see {log_path}")
        time.sleep(0.1)
    else:
        raise RuntimeError(f"PaperArena server did not become ready; see {log_path}")

    url = f"http://127.0.0.1:{selected_port}"
    _write_json_atomic(_server_record_path(state), {
        "url": url,
        "port": selected_port,
        "pid": health.get("pid", proc.pid),
        "state": state,
        "ui": ui,
        "started_at": _now(),
    })
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    return {"ok": True, "url": url, "port": selected_port, "pid": health.get("pid", proc.pid), "started": True}


def _notify_running_server(port):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/reload",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=0.5).read()
    except Exception:
        pass


def _read_server_port(state, default=DEFAULT_PORT):
    record = _read_json(_server_record_path(state), {})
    try:
        return int(record.get("port", default))
    except (TypeError, ValueError):
        return default


def cmd_serve(args):
    ArenaHandler.state_dir = os.path.abspath(args.state)
    ArenaHandler.ui_dir = os.path.abspath(args.ui)
    os.makedirs(ArenaHandler.state_dir, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ArenaHandler)
    url = f"http://127.0.0.1:{args.port}"
    _write_json_atomic(_server_record_path(ArenaHandler.state_dir), {
        "url": url,
        "port": args.port,
        "pid": os.getpid(),
        "state": ArenaHandler.state_dir,
        "ui": ArenaHandler.ui_dir,
        "started_at": _now(),
    })
    print(f"PaperArena serving {url}  (ui={ArenaHandler.ui_dir})", flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_new_round(args):
    state = os.path.abspath(args.state)
    os.makedirs(state, exist_ok=True)
    items = _read_json(args.candidates)
    if items is None:
        print(f"error: could not read candidates from {args.candidates}", file=sys.stderr)
        return 1
    if isinstance(items, dict) and "items" in items:
        items = items["items"]
    if not isinstance(items, list):
        print("error: candidates must be a JSON list (or {\"items\": [...]})", file=sys.stderr)
        return 1
    items, validation_errors, validation_warnings = _validate_and_normalize_candidates(
        items,
        verify_links=args.verify_links,
    )
    if validation_errors:
        print(json.dumps({
            "error": "candidate validation failed",
            "details": validation_errors,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    skipped_seen = 0
    if not args.allow_seen:
        seen = _seen_ids(state)
        filtered = []
        for item in items:
            if _paper_key(item) in seen:
                skipped_seen += 1
            else:
                filtered.append(item)
        items = filtered
    prev = _read_candidates(state)
    rnd = int(prev.get("round", 0)) + 1
    _write_json_atomic(os.path.join(state, "candidates.json"), {
        "round": rnd,
        "items": items,
        "created_at": _now(),
        "skipped_seen": skipped_seen,
    })
    # clear previous round's submission so `wait` blocks on the new one
    for fname in ("feedback.json", ".done"):
        try:
            os.remove(os.path.join(state, fname))
        except FileNotFoundError:
            pass
    server = None
    notify_port = args.port
    if args.ensure_server or args.open:
        try:
            server = _ensure_server(
                state,
                args.ui,
                port=args.port,
                port_max=args.port_max,
                open_browser=args.open,
            )
            notify_port = server["port"]
        except Exception as exc:
            print(json.dumps({"error": f"could not start PaperArena server: {exc}"}), file=sys.stderr)
            return 1
    else:
        stored_port = _read_server_port(state, args.port)
        notify_port = stored_port
    _notify_running_server(notify_port)
    print(json.dumps({
        "round": rnd,
        "count": len(items),
        "skipped_seen": skipped_seen,
        "url": server["url"] if server else f"http://127.0.0.1:{notify_port}",
        "validation_warnings": validation_warnings,
    }, ensure_ascii=False))
    return 0


def cmd_ensure_server(args):
    try:
        result = _ensure_server(
            args.state,
            args.ui,
            port=args.port,
            port_max=args.port_max,
            open_browser=args.open,
            wait_timeout=args.timeout,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_validate_candidates(args):
    items = _read_json(args.candidates)
    if isinstance(items, dict) and "items" in items:
        items = items["items"]
    if not isinstance(items, list):
        print(json.dumps({"ok": False, "error": "candidates must be a JSON list (or {\"items\": [...]})"}))
        return 1
    normalized, errors, warnings = _validate_and_normalize_candidates(items, verify_links=args.verify_links)
    print(json.dumps({
        "ok": not errors,
        "count": len(normalized),
        "errors": errors,
        "warnings": warnings,
    }, ensure_ascii=False, indent=2))
    return 1 if errors else 0


def cmd_wait(args):
    state = os.path.abspath(args.state)
    if not args.no_server:
        try:
            port = _read_server_port(state, args.port)
            url = f"http://127.0.0.1:{port}/api/wait-feedback?timeout={args.timeout or 0}"
            with urllib.request.urlopen(url, timeout=(args.timeout or None)) as resp:
                body = resp.read().decode("utf-8")
                sys.stdout.write(body)
                if not body.endswith("\n"):
                    print()
                return 0
        except Exception:
            pass
    done = os.path.join(state, ".done")
    deadline = time.time() + args.timeout if args.timeout else None
    while not os.path.exists(done):
        if deadline and time.time() > deadline:
            print(json.dumps({"error": "timeout"}), file=sys.stderr)
            return 2
        time.sleep(args.poll)
    feedback = _read_json(os.path.join(state, "feedback.json"), {})
    print(json.dumps(feedback, ensure_ascii=False, indent=2))
    return 0


def cmd_feedback(args):
    state = os.path.abspath(args.state)
    if args.compact:
        print(json.dumps(_compact_feedback(
            state,
            max_feedback=args.max_feedback,
            max_liked=args.max_liked,
            max_dislikes=args.max_dislikes,
            include_seen=args.include_seen,
            max_seen=args.max_seen,
        ), ensure_ascii=False, indent=2))
        return 0

    print(json.dumps({
        "feedback": _read_json(_state_paths(state)["feedback"], {}),
        "history": _read_history(state),
        "library": _read_library(state),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args):
    state = os.path.abspath(args.state)
    cand = _read_candidates(state)
    library = _read_library(state)
    history = _read_history(state)
    print(json.dumps({
        "round": cand.get("round", 0),
        "candidates": len(cand.get("items", [])),
        "liked": len(library.get("liked", [])),
        "disliked": len(library.get("disliked", [])),
        "history_events": len(history.get("events", [])),
        "done": os.path.exists(os.path.join(state, ".done")),
    }, indent=2))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="paperarena", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web server (background this)")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--state", default=DEFAULT_STATE)
    s.add_argument("--ui", default=DEFAULT_UI)
    s.add_argument("--open", action="store_true", help="open the browser on start")
    s.set_defaults(func=cmd_serve)

    es = sub.add_parser("ensure-server", help="start the server if needed and wait until ready")
    es.add_argument("--port", type=int, default=DEFAULT_PORT)
    es.add_argument("--port-max", type=int, default=8799, help="highest fallback port to try")
    es.add_argument("--state", default=DEFAULT_STATE)
    es.add_argument("--ui", default=DEFAULT_UI)
    es.add_argument("--open", action="store_true", help="open the browser after readiness check")
    es.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for readiness")
    es.set_defaults(func=cmd_ensure_server)

    n = sub.add_parser("new-round", help="publish a new batch of candidates")
    n.add_argument("--candidates", required=True, help="path to a JSON list of candidate papers")
    n.add_argument("--state", default=DEFAULT_STATE)
    n.add_argument("--ui", default=DEFAULT_UI)
    n.add_argument("--port", type=int, default=DEFAULT_PORT, help="notify a running server on this port")
    n.add_argument("--port-max", type=int, default=8799, help="highest fallback port to try with --ensure-server")
    n.add_argument("--allow-seen", action="store_true", help="do not filter papers already liked/disliked")
    n.add_argument("--ensure-server", action="store_true", help="start PaperArena if needed before returning")
    n.add_argument("--open", action="store_true", help="open the browser; implies --ensure-server")
    n.add_argument("--verify-links", action="store_true", help="verify arXiv candidate titles against arXiv metadata")
    n.set_defaults(func=cmd_new_round)

    vc = sub.add_parser("validate-candidates", help="validate candidate ids/URLs/PDF links")
    vc.add_argument("--candidates", required=True, help="path to a JSON list of candidate papers")
    vc.add_argument("--verify-links", action="store_true", help="verify arXiv candidate titles against arXiv metadata")
    vc.set_defaults(func=cmd_validate_candidates)

    w = sub.add_parser("wait", help="block until the round is submitted, print feedback JSON")
    w.add_argument("--state", default=DEFAULT_STATE)
    w.add_argument("--poll", type=float, default=1.0, help="seconds between checks")
    w.add_argument("--timeout", type=float, default=0, help="give up after N seconds (0 = never)")
    w.add_argument("--port", type=int, default=DEFAULT_PORT, help="use the running server's event wait endpoint")
    w.add_argument("--no-server", action="store_true", help="skip server wait endpoint and fall back to file polling")
    w.set_defaults(func=cmd_wait)

    fb = sub.add_parser("feedback", help="print compact feedback/history for recommendation refinement")
    fb.add_argument("--state", default=DEFAULT_STATE)
    fb.add_argument("--compact", action="store_true", help="omit full paper metadata and print only refinement signals")
    fb.add_argument("--max-feedback", type=int, default=12, help="max submitted decisions in compact output")
    fb.add_argument("--max-liked", type=int, default=12, help="max recent liked papers in compact output")
    fb.add_argument("--max-dislikes", type=int, default=12, help="max recent dislikes in compact output")
    fb.add_argument("--include-seen", action="store_true", help="include seen_ids in compact output")
    fb.add_argument("--max-seen", type=int, default=0, help="max seen ids when --include-seen is used (0 = all)")
    fb.set_defaults(func=cmd_feedback)

    st = sub.add_parser("status", help="print current state as JSON")
    st.add_argument("--state", default=DEFAULT_STATE)
    st.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
