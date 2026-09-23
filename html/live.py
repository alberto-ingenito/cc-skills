#!/usr/bin/env python3
"""live — the real-time half of the html review skill.

One small HTTP server per user, shared by every Claude Code session on the
machine. It serves registered review documents on 127.0.0.1, writes the
reader's notes into the document file as they type, and records when they
press "Send to Claude".

Claude never talks to it over the network. The CLI and the server meet in
files: the document itself (under the lock in reviewdoc.doc_lock) and a few
small records under ~/.cache/reviewdoc. That works inside Claude Code's
sandbox, which may not allow a local connection, and it works the same for
one session or fifty.

Lifecycle: born with the first Claude Code session that opens or waits on a
review, gone GRACE seconds after the last such session has exited. Each of
those commands records its Claude process under sessions/. How the server
gets started depends on where Claude runs:

- Sandboxed (Apple Claude Code): nothing Claude spawns may open a port. A
  LaunchAgent (`reviewdoc.py install`, once, from a normal Terminal) watches
  sessions/ and launchd starts the server outside the sandbox the moment a
  session writes there. launchd also guarantees a single instance, however
  many sessions start at once.
- Unsandboxed: the CLI starts it itself, detached, under daemon.lock.

Nothing is lost when it stops: every note it accepted is already in the
document file, and the page carries on offline.

State directory layout (all per user):
  daemon.json            pid and port of the running server
  daemon.lock            serialises starting it
  token                  shared secret the served pages send back
  sessions/<pid>.json    one per Claude Code process that used the skill;
                         the LaunchAgent watches this folder
  docs/<id>.json         registered documents: id -> path
  docs/<id>.sent         bumped when the reader presses Send to Claude
  waiters/<id>/<pid>     heartbeat of a `wait` running for that document
  locks/                 per-document write locks
"""

import base64
import errno
import fcntl
import glob
import hashlib
import json
import os
import re
import secrets
import plistlib
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import reviewdoc as rd

VERSION = 1
GRACE = 90          # seconds the server outlives the last Claude session
HEARTBEAT = 15      # a `wait` counts as waiting if it touched its file this recently
MAX_BODY = 8 << 20
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reviewdoc.py")
LABEL = "local.reviewdoc.live"


# --------------------------------------------------------------------------
# small shared records
# --------------------------------------------------------------------------

def spath(*parts):
    """A file inside the state directory; its folder is created on demand."""
    return os.path.join(rd.state_dir(*parts[:-1]), parts[-1])


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_private(path, obj):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))
    os.replace(tmp, path)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True             # exists, just not ours to signal
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def token():
    """Stable across restarts, so a page left open keeps working when the
    server comes back. Only same-origin pages can read it."""
    path = spath("token")
    try:
        with open(path, "r", encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    write_private(path, t)
    return t


def running():
    """-> the daemon record if its process is alive, else None."""
    d = read_json(spath("daemon.json"))
    if d and pid_alive(d.get("pid")):
        return d
    return None


def registered(docid):
    """-> the path of a registered document that still carries this id."""
    if not docid or not re.fullmatch(r"[\w-]{6,64}", docid):
        return None
    rec = read_json(spath("docs", docid + ".json"))
    path = rec and rec.get("path")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return None
    return path if rd.doc_id(head) == docid else None


def url_for(docid):
    d = running()
    if d and registered(docid):
        return "http://127.0.0.1:%d/d/%s/" % (d["port"], docid)
    return None


def sent_seq(docid):
    return int((read_json(spath("docs", docid + ".sent")) or {}).get("seq", 0))


def waiters(docid):
    now, n = time.time(), 0
    for f in glob.glob(os.path.join(rd.state_dir("waiters", docid), "*")):
        try:
            fresh = now - os.path.getmtime(f) < HEARTBEAT
        except OSError:
            continue
        if fresh and pid_alive(os.path.basename(f)):
            n += 1
    return n


# --------------------------------------------------------------------------
# Claude Code sessions: the server lives as long as one of them does
# --------------------------------------------------------------------------

def claude_pid():
    """The Claude Code process this command runs under. ACC exports it; for
    plain Claude Code, fall back to walking up from our parent."""
    for var in ("CLAUDE_PID", "APPLE_CLAUDE_CODE_SESSION_PID"):
        v = os.environ.get(var)
        if v and v.isdigit() and pid_alive(v):
            return int(v)
    pid = os.getppid()
    for _ in range(8):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.split(None, 1)
        except (OSError, subprocess.SubprocessError):
            break
        if len(out) < 2:
            break
        if looks_like_claude(out[1]):
            return pid
        pid = int(out[0])
    return None


SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ksh", "tcsh", "csh"}


def looks_like_claude(args):
    """Is this command line the Claude Code process itself? Its short name is
    just `node` for an npm install, and the shells Claude spawns mention
    ~/.claude in their arguments, so look at the executable and the package."""
    parts = args.strip().split()
    if not parts:
        return False
    exe = os.path.basename(parts[0]).lstrip("-").lower()
    if exe in SHELLS:
        return False
    return exe == "claude" or exe.startswith("claude-") or bool(
        re.search(r"(@anthropic-ai/claude-code|/claude-code/)", args))


def touch_session(pid, session_id=None):
    if not pid:
        return
    rec = {"pid": pid, "session": session_id or os.environ.get("CLAUDE_CODE_SESSION_ID"),
           "cwd": os.getcwd(), "ts": rd.now_iso()}
    try:
        write_private(spath("sessions", "%d.json" % pid), rec)
    except OSError:
        pass


def live_sessions():
    alive = []
    for f in glob.glob(os.path.join(rd.state_dir("sessions"), "*.json")):
        pid = os.path.basename(f)[:-5]
        if pid_alive(pid):
            alive.append(int(pid))
        else:
            try:
                os.remove(f)
            except OSError:
                pass
    return alive


# --------------------------------------------------------------------------
# the documents, as the server sees them (no sockets here, so it is testable)
# --------------------------------------------------------------------------

class Docs(object):
    def __init__(self, tok):
        self.token = tok
        self._cache = {}
        self._lock = threading.Lock()

    def snapshot(self, docid):
        """-> (path, html, notes, rev, content), cached on the file's mtime."""
        path = registered(docid)
        if not path:
            raise KeyError(docid)
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(docid)
            if hit and hit[0] == key:
                return hit[1]
        html = rd.read(path)
        m = rd.NOTES_RE.search(html)
        block = m.group(2) if m else ""
        snap = (path, html, rd.read_notes(html, path),
                hashlib.sha1(block.encode("utf-8")).hexdigest()[:16],
                hashlib.sha1(rd.NOTES_RE.sub("", html).encode("utf-8")).hexdigest()[:16])
        with self._lock:
            self._cache[docid] = (key, snap)
        return snap

    def page(self, docid):
        _p, html, _n, rev, content = self.snapshot(docid)
        cfg = rd.js_json({"token": self.token, "api": "/api/d/%s" % docid,
                          "rev": rev, "content": content})
        return html.replace('<script id="rd-notes"',
                            "<script>window.RD_LIVE=%s;</script>\n"
                            '<script id="rd-notes"' % cfg, 1)

    def state(self, docid):
        _p, _h, notes, rev, content = self.snapshot(docid)
        return {"rev": rev, "content": content, "notes": notes,
                "waiting": waiters(docid), "sent": sent_seq(docid)}

    def post_notes(self, docid, body):
        path = registered(docid)
        if not path:
            raise KeyError(docid)
        with rd.doc_lock(path):
            html = rd.read(path)
            notes = rd.read_notes(html, path)
            incoming = rd.unwrap_export(body, html, "the page")
            rd.merge_notes(notes, incoming)
            rd.reanchor(notes, rd.doc_sections(html))
            rd.write(path, rd.write_notes(html, notes))
        return self.state(docid)

    def send(self, docid):
        if not registered(docid):
            raise KeyError(docid)
        path = spath("docs", docid + ".sent")
        with open(spath("docs", docid + ".sent.lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            seq = sent_seq(docid) + 1
            write_private(path, {"seq": seq, "ts": rd.now_iso()})
        return {"sent": seq, "waiting": waiters(docid)}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}


def loopback(hostport):
    """True for "127.0.0.1:47700", "localhost:51234", "[::1]:8000" and the like."""
    host = hostport.strip().lower()
    host = host.rsplit(":", 1)[0] if re.search(r":\d+$", host) else host
    return host in LOOPBACK


class Handler(BaseHTTPRequestHandler):
    server_version = "reviewdoc/%d" % VERSION
    docs = None             # set by serve()
    port = 0

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self):
        # A DNS-rebinding page arrives with its own host name: refuse it. The
        # port is not checked, because a forwarder (VS Code Remote-SSH, ssh -L)
        # may expose this server on a different port on the reader's machine.
        return loopback(self.headers.get("Host", ""))

    def _api_ok(self):
        origin = self.headers.get("Origin")
        if origin and not (origin.startswith("http://") and loopback(origin[7:])):
            return False
        # a custom header forces a CORS preflight, which is never granted, so
        # no other site can send this even if it guessed the token
        return secrets.compare_digest(self.headers.get("X-Reviewdoc-Token", ""),
                                      self.docs.token)

    def _route(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        path = urlsplit(self.path).path
        try:
            if method == "GET" and path == "/api/ping":
                return self._send(200, {"app": "reviewdoc", "version": VERSION,
                                        "pid": os.getpid(), "uid": os.getuid()})
            m = re.fullmatch(r"/probe/([\w-]+)\.gif", path)
            if method == "GET" and m:
                if not registered(m.group(1)):
                    return self._send(404, {"error": "unknown document"})
                return self._send(200, GIF, "image/gif")
            m = re.fullmatch(r"/d/([\w-]+)(/?)", path)
            if method == "GET" and m:
                if not m.group(2):
                    return self._send(301, b"", extra=[("Location", path + "/")])
                return self._send(200, self.docs.page(m.group(1)), "text/html; charset=utf-8",
                                  extra=[("Referrer-Policy", "no-referrer")])
            m = re.fullmatch(r"/api/d/([\w-]+)/(state|notes|send)", path)
            if m:
                if not self._api_ok():
                    return self._send(403, {"error": "forbidden"})
                docid, what = m.groups()
                if method == "GET" and what == "state":
                    return self._send(200, self.docs.state(docid))
                if method == "POST" and what in ("notes", "send"):
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        return self._send(413, {"error": "too large"})
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if isinstance(body, dict) and body.get("doc") not in (None, docid):
                        return self._send(409, {"error": "notes for another document"})
                    if what == "notes":
                        return self._send(200, self.docs.post_notes(docid, body))
                    return self._send(200, self.docs.send(docid))
            return self._send(404, {"error": "not found"})
        except KeyError:
            return self._send(404, {"error": "unknown document"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except SystemExit:
            # rd.die() on a document that no longer parses
            return self._send(500, {"error": "the document could not be read"})

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


class Server(ThreadingHTTPServer):
    daemon_threads = True


def port_owner(port):
    """Who is listening on 127.0.0.1:port -> "free", "ours" or "other".

    A connection is the only reliable test on macOS. http.server sets
    SO_REUSEADDR, which a restart needs: it lets the server reclaim a port
    whose old connections are still in TIME_WAIT. But on BSD it also lets a
    bind to 127.0.0.1 succeed on top of another app's 0.0.0.0 listener, and
    the more specific address wins, so we would quietly take that app's local
    traffic. Closed-connection leftovers refuse a connect; a listener does not."""
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=0.3)
    except ConnectionRefusedError:
        return "free"
    except OSError:
        return "other"          # no answer in time, or stranger: leave it alone
    data = b""
    try:
        c.settimeout(0.5)
        c.sendall(b"GET /api/ping HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
        while len(data) < 4096:
            chunk = c.recv(1024)
            if not chunk:
                break
            data += chunk
    except OSError:
        return "other"
    finally:
        c.close()
    try:
        ping = json.loads(data.split(b"\r\n\r\n", 1)[-1])
    except ValueError:
        return "other"
    # another user's server on a shared Mac is not ours to lean on
    ok = isinstance(ping, dict) and ping.get("app") == "reviewdoc" and ping.get("uid") == os.getuid()
    return "ours" if ok else "other"


def serve(port=None, forever=False, spawned=False, launchd=False):
    if launchd and not live_sessions():
        # launchd fires on any change to sessions/, including our own clean-up
        # of dead entries: with no live session there is nothing to serve
        return 0
    lk = open(spath("daemon.lock"), "w")
    if not spawned:             # when ensure() spawned us it holds the lock for us
        fcntl.flock(lk, fcntl.LOCK_EX)
    d = running()
    if d and d.get("pid") != os.getpid():
        print("already running: pid %s on http://127.0.0.1:%s/" % (d["pid"], d["port"]))
        return 0
    ports = [port] if port else rd.live_ports()
    httpd = None
    for p in ports:
        owner = port_owner(p)
        if owner == "ours":
            print("a reviewdoc server already answers on %d; not starting another" % p, flush=True)
            return 0
        if owner == "other":
            print("port %d is in use by another program; trying the next" % p, flush=True)
            continue
        try:
            httpd = Server(("127.0.0.1", p), Handler)
            break
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
    if httpd is None:
        rd.die("no free port in %d-%d (set REVIEWDOC_PORT)" % (ports[0], ports[-1]))
    port = httpd.server_address[1]
    Handler.docs, Handler.port = Docs(token()), port
    write_private(spath("daemon.json"), {"pid": os.getpid(), "port": port,
                                         "version": VERSION, "started": rd.now_iso()})
    if not spawned:
        lk.close()
    print("reviewdoc live server on http://127.0.0.1:%d/ (pid %d)" % (port, os.getpid()),
          flush=True)

    def stop(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def reaper():
        idle = None
        while True:
            time.sleep(5)
            me = read_json(spath("daemon.json")) or {}
            if me.get("pid") != os.getpid():
                return stop()   # someone else owns the record now
            if forever or live_sessions():
                idle = None
                continue
            idle = idle or time.time()
            if time.time() - idle > GRACE:
                print("no Claude Code session left; stopping", flush=True)
                return stop()

    threading.Thread(target=reaper, daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        me = read_json(spath("daemon.json")) or {}
        if me.get("pid") == os.getpid():
            try:
                os.remove(spath("daemon.json"))
            except OSError:
                pass
    return 0


def ensure():
    """Start the server unless one is running. Safe to call from many
    sessions at once: the lock makes all but the first find it running."""
    with open(spath("daemon.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d = running()
        if d:
            return d
        log = open(spath("daemon.log"), "a")
        child = subprocess.Popen(
            [sys.executable, SCRIPT, "serve", "--spawned"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True)
        for _ in range(50):
            d = running()
            if d and d.get("pid") == child.pid:
                return d
            if child.poll() is not None:
                break
            time.sleep(0.1)
    return None


# --------------------------------------------------------------------------
# commands (dispatched from reviewdoc.py)
# --------------------------------------------------------------------------

def cmd_serve(a):
    sys.exit(serve(port=a.port, forever=a.forever, spawned=a.spawned, launchd=a.launchd))


def agent_plist():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def can_listen():
    """False inside a sandbox that forbids opening ports (Apple Claude Code)."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def agent_script():
    """The script the installed LaunchAgent runs, or None if not installed."""
    try:
        with open(agent_plist(), "rb") as f:
            return plistlib.load(f)["ProgramArguments"][1]
    except (OSError, KeyError, IndexError, ValueError, plistlib.InvalidFileException):
        return None


INSTALL_HINT = ("live review is off: this Claude Code sandbox cannot start servers. "
                "Once, in a normal Terminal: python3 %s install" % SCRIPT)


def ensure_live():
    """Record this Claude session and make sure the server is up for it.
    -> the daemon record, or None when live mode is not available here."""
    touch_session(claude_pid())     # with the LaunchAgent, this write starts it
    d = running()
    if d:
        return d
    if os.path.exists(agent_plist()):
        if agent_script() != SCRIPT:
            print("live review is off: the LaunchAgent runs %s, which is not this copy of "
                  "the skill (moved or updated?). Once, in a normal Terminal: python3 %s install"
                  % (agent_script(), SCRIPT))
            return None
        for _ in range(40):
            time.sleep(0.1)
            d = running()
            if d:
                return d
        return None
    if can_listen():
        return ensure()
    print(INSTALL_HINT)
    return None


def cmd_install(a):
    if sys.platform != "darwin":
        rd.die("install sets up a macOS LaunchAgent; elsewhere the CLI starts the "
               "server by itself, or run `reviewdoc.py serve`")
    if not can_listen():
        rd.die("this shell cannot open a listening port (Claude Code's sandbox?). "
               "Run install from a normal Terminal.")
    log = spath("daemon.log")
    env = dict((k, os.environ[k]) for k in ("REVIEWDOC_STATE", "REVIEWDOC_PORT")
               if os.environ.get(k))
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, SCRIPT, "serve", "--launchd"],
        "WatchPaths": [rd.state_dir("sessions")],
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
    }
    if env:
        plist["EnvironmentVariables"] = env
    path = agent_plist()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    domain = "gui/%d" % os.getuid()
    subprocess.run(["launchctl", "bootout", domain, path], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", domain, path], capture_output=True, text=True)
    if r.returncode:
        rd.die("launchctl bootstrap failed: %s" % (r.stderr.strip() or r.returncode))
    print("installed %s\n  runs: %s serve\n  when: a Claude Code session writes to %s\n"
          "  undo: python3 %s uninstall" % (path, SCRIPT, rd.state_dir("sessions"), SCRIPT))


def cmd_uninstall(a):
    path = agent_plist()
    subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), path], capture_output=True)
    if os.path.exists(path):
        os.remove(path)
        print("removed %s" % path)
    else:
        print("no LaunchAgent installed")
    cmd_stop(a)


def exports_for(doc, slug):
    """Where an Export may land: the document's folder, then Downloads."""
    dirs = [os.path.dirname(os.path.abspath(doc)), os.path.expanduser("~/Downloads")]
    out = {}
    for d in dirs:
        for f in glob.glob(os.path.join(d, slug + "-notes*.json")):
            try:
                out[f] = os.path.getmtime(f)
            except OSError:
                pass
    return out


def cmd_wait(a):
    """Block until the reader is done, then exit so Claude wakes. Run it as a
    background task. Done means Send to Claude in the live page, or an Export
    saved next to the document or in Downloads."""
    html = rd.read(a.doc)
    docid = rd.doc_id(html)
    if not docid:
        rd.die("%s has no document id; rebuild it" % a.doc)
    rd.register_doc(a.doc, docid, rd.plain(rd.DOC_TITLE_RE.search(html).group(1)))
    slug = json.loads(re.search(r"var DOC = (\{.*?\});", html).group(1))["slug"]
    pid = claude_pid()
    ensure_live()
    seq0, seen = sent_seq(docid), exports_for(a.doc, slug)
    beat = os.path.join(rd.state_dir("waiters", docid), str(pid or os.getpid()))
    url = url_for(docid)
    print("waiting on %s (%s)" % (a.doc, url or "offline: the reader exports when done"),
          flush=True)
    start = time.time()
    try:
        while True:
            with open(beat, "w") as f:
                f.write(rd.now_iso())
            if sent_seq(docid) > seq0:
                notes = rd.read_notes(rd.read(a.doc), a.doc)
                opn = sum(1 for n in notes if n.get("kind") != "highlight"
                          and n.get("status") not in ("resolved",))
                hl = sum(1 for n in notes if n.get("kind") == "highlight")
                print("sent: the reader pressed Send to Claude. %d open thread(s), %d "
                      "highlight(s), already saved in %s. Next: reviewdoc.py list %s --json"
                      % (opn, hl, a.doc, a.doc))
                return 0
            for f, mt in exports_for(a.doc, slug).items():
                if seen.get(f) != mt:
                    seen[f] = mt
                    body = read_json(f)
                    if isinstance(body, dict) and body.get("doc") == docid:
                        print("exported: %s. Next: reviewdoc.py import %s %s" % (f, a.doc, f))
                        return 0
            if a.timeout and time.time() - start > a.timeout:
                print("no notes yet after %ds; run wait again to keep waiting" % a.timeout)
                sys.exit(3)
            if pid and not pid_alive(pid):
                sys.exit(4)     # our session is gone; nobody to wake
            time.sleep(1)
    finally:
        try:
            os.remove(beat)
        except OSError:
            pass


def cmd_status(a):
    d = running()
    if d:
        print("server: pid %s, http://127.0.0.1:%s/ since %s" % (d["pid"], d["port"], d.get("started")))
    else:
        print("server: not running (offline mode)")
    if os.path.exists(agent_plist()):
        ok = agent_script() == SCRIPT
        print("launch agent: installed, %s%s" % (agent_plist(), "" if ok else
              " (STALE: runs %s; reinstall)" % agent_script()))
    else:
        print("launch agent: not installed%s" % ("" if can_listen() else
              " (needed here: %s)" % INSTALL_HINT))
    s = live_sessions()
    print("claude sessions keeping it alive: %s" % (" ".join(map(str, s)) or "none"))
    docs = sorted(glob.glob(os.path.join(rd.state_dir("docs"), "*.json")))
    print("registered documents: %d" % len(docs))
    for f in docs[-10:]:
        docid = os.path.basename(f)[:-5]
        rec = read_json(f) or {}
        w = waiters(docid)
        print("  %s  %s%s" % (docid, rec.get("path", "?"), "  (%d waiting)" % w if w else ""))
    print("state: %s" % rd.state_dir())


def cmd_stop(a):
    d = running()
    if not d:
        print("not running")
        return
    os.kill(int(d["pid"]), signal.SIGTERM)
    print("stopped pid %s" % d["pid"])


def cmd_open(a):
    html = rd.read(a.doc)
    rd.register_doc(a.doc, rd.doc_id(html), rd.plain(rd.DOC_TITLE_RE.search(html).group(1)))
    rd.open_doc(a.doc, html)
