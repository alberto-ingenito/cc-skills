#!/usr/bin/env python3
"""Checks the live server's three addresses for one document: /d/<id>/,
/f/<name> and /p/<path>, and the refusals around them.

Self-contained and safe to run next to a live review: the documents are built
in a temp folder, the server state lives in another (REVIEWDOC_STATE), and
the server instance is this script's own, on a free port. It never touches
~/.cache/reviewdoc or the default server. Python 3 stdlib only.

    python3 test_routes.py          # add -v for one line per test

Where a sandbox forbids listening on a port, the same request handler is
driven over a socket pair instead. The script says which it used.
"""

import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="reviewdoc-test-")      # /var/folders/..., itself a symlink
os.environ["REVIEWDOC_STATE"] = os.path.join(TMP, "state")
sys.path.insert(0, HERE)

import reviewdoc as rd      # noqa: E402  (after REVIEWDOC_STATE on purpose)
import live                 # noqa: E402

SOURCE = """<h1>%s</h1>
<p class="lede">%s</p>
<h2>The part worth a note</h2>
<p>Alpha beta gamma delta.</p>
"""


def quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = fn(*args)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def build(rel, title, lede="The whole answer, first."):
    """Write <rel>.src.html under TMP and build it -> (path of the build, id)."""
    src = os.path.join(TMP, rel + rd.SRC_EXT)
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, "w", encoding="utf-8") as f:
        f.write(SOURCE % (title, lede))
    code, out, err = quiet(rd.main, ["build", src])
    assert code == 0, err
    out_path = src[: -len(rd.SRC_EXT)] + ".html"
    return out_path, rd.doc_id(rd.read(out_path))


def unregister(docid):
    os.remove(os.path.join(rd.state_dir("docs"), docid + ".json"))


def p_url(path):
    return "/p/" + quote(path.lstrip("/"))


# --------------------------------------------------------------------------
# a server of our own: TCP on a free port, or the handler over a socket pair
# --------------------------------------------------------------------------

class Wire(object):
    def __init__(self):
        live.Handler.docs = live.Docs(live.token())
        try:
            self.httpd = live.Server(("127.0.0.1", 0), live.Handler)
        except OSError:
            self.httpd = None
        if self.httpd:
            self.port = self.httpd.server_address[1]
            self.mode = "TCP server on 127.0.0.1:%d" % self.port
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        else:
            self.port = 47999           # only ever written into the Host header
            self.mode = "socket pair (this sandbox cannot listen on a port)"

    def close(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def request(self, method, path, headers=None, body=None):
        """Send `path` byte for byte, `..` and all. -> (status, headers, body)."""
        h = {"Host": "127.0.0.1:%d" % self.port, "Connection": "close"}
        h.update(headers or {})
        if body is not None:
            body = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
            h["Content-Length"] = str(len(body))
        raw = ("%s %s HTTP/1.1\r\n" % (method, path)
               + "".join("%s: %s\r\n" % kv for kv in h.items()) + "\r\n").encode("latin-1")
        raw += body or b""
        if self.httpd:
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            worker = None
        else:
            sock, theirs = socket.socketpair()
            worker = threading.Thread(target=self._serve_one, args=(theirs,))
            worker.start()
        try:
            sock.sendall(raw)
            resp = http.client.HTTPResponse(sock, method=method)
            resp.begin()
            data = resp.read()
            return resp.status, dict((k.lower(), v) for k, v in resp.getheaders()), data
        finally:
            sock.close()
            if worker:
                worker.join(10)

    @staticmethod
    def _serve_one(conn):
        try:
            live.Handler(conn, ("127.0.0.1", 0), None)
        finally:
            conn.close()


WIRE = None
DOCS = {}


def setUpModule():
    global WIRE
    os.chdir(TMP)               # no document of these names in the working folder
    DOCS["A"] = build("alpha/plan", "Plan A")
    DOCS["B"] = build("beta dir/plan", "Plan B")                      # same name as A
    DOCS["C"] = build("gamma/lifecycle analysis/html/lean model review", "The Lean model")
    DOCS["D"] = build("delta/unreg", "Not registered yet")
    DOCS["E"] = build("epsilon/cross", "Not registered either")
    unregister(DOCS["D"][1])
    unregister(DOCS["E"][1])
    eps = os.path.join(TMP, "eps")
    os.makedirs(os.path.join(eps, "folder.html"))
    with open(os.path.join(eps, "plain.html"), "w") as f:
        f.write("<!DOCTYPE html><html><head><title>secret</title></head><body>hi</body></html>")
    with open(os.path.join(eps, "fake.html"), "w") as f:         # the id meta, but no notes
        f.write('<!DOCTYPE html>\n<html><head><meta name="reviewdoc-id" content="feedfacecafe0001">'
                "<title>fake</title></head><body></body></html>")
    with open(os.path.join(eps, "secret.txt"), "w") as f:
        f.write("not html at all")
    os.symlink(os.path.join(eps, "plain.html"), os.path.join(eps, "link-to-plain.html"))
    os.symlink(DOCS["C"][0], os.path.join(eps, "link-to-c.html"))
    os.makedirs(os.path.join(TMP, "copy"))
    shutil.copy(DOCS["C"][0], os.path.join(TMP, "copy", "lean model review.html"))
    WIRE = Wire()
    print("transport: %s\nscratch: %s" % (WIRE.mode, TMP), file=sys.stderr)


def tearDownModule():
    if WIRE:
        WIRE.close()
    os.chdir(HERE)
    shutil.rmtree(TMP, ignore_errors=True)


def get(path, **headers):
    return WIRE.request("GET", path, headers)


def rd_live(body):
    """The live config the server injects into a page."""
    m = re.search(rb"<script>window\.RD_LIVE=(\{.*?\});</script>", body)
    return json.loads(m.group(1)) if m else None


class Routes(unittest.TestCase):

    def assertPage(self, path, docid, **headers):
        status, h, body = get(path, **headers)
        self.assertEqual(status, 200, "%s -> %d %r" % (path, status, body[:200]))
        self.assertTrue(h["content-type"].startswith("text/html"))
        self.assertEqual(rd.doc_id(body.decode("utf-8")), docid, path)
        self.assertEqual(rd_live(body)["api"], "/api/d/%s" % docid, path)
        return body

    def assertNotFound(self, path, **headers):
        status, _h, body = get(path, **headers)
        self.assertEqual((status, body), (404, b'{"error": "not found"}'), path)

    # ---- by id: unchanged ----

    def test_id_route(self):
        path, docid = DOCS["A"]
        self.assertPage("/d/%s/" % docid, docid)
        status, h, _ = get("/d/%s" % docid)
        self.assertEqual((status, h.get("location")), (301, "/d/%s/" % docid))
        status, _h, _b = get("/d/0123456789abcdef/")
        self.assertEqual(status, 404)

    # ---- by name ----

    def test_name_route(self):
        _path, docid = DOCS["C"]
        for name in ("lean%20model%20review.html", "lean%20model%20review",
                     "lean%20model%20review.src.html"):
            self.assertPage("/f/" + name, docid)
        self.assertNotFound("/f/no-such-document.html")
        self.assertNotFound("/f/lean model review.html".replace(" ", "%2520"))  # double-encoded

    def test_ambiguous_name_lists_candidates(self):
        status, h, body = get("/f/plan.html")
        self.assertEqual(status, 300)
        self.assertTrue(h["content-type"].startswith("text/html"))
        text = body.decode("utf-8")
        self.assertIsNone(rd_live(body), "a chooser, not a document")
        for key in ("A", "B"):
            path, docid = DOCS[key]
            self.assertIn(rd.esc(path), text)
            link = rd.esc(p_url(path))
            self.assertIn('href="%s"' % link, text)
            self.assertPage(p_url(path), docid)          # each candidate link works

    # ---- by path ----

    def test_path_route(self):
        path, docid = DOCS["C"]
        self.assertIn(" ", path)
        self.assertPage(p_url(path), docid)
        self.assertPage(p_url(os.path.realpath(path)), docid)        # /private/var/...
        self.assertPage(p_url(path[: -len(".html")] + rd.SRC_EXT), docid)
        home = os.environ.get("HOME")
        os.environ["HOME"] = TMP
        try:
            self.assertPage("/p/~/" + quote(os.path.relpath(path, TMP)), docid)
        finally:
            os.environ["HOME"] = home
        self.assertNotFound(p_url(path) + "/")       # a trailing slash means a folder

    def test_unregistered_valid_path_registers(self):
        path, docid = DOCS["D"]
        self.assertIsNone(live.registered(docid))
        self.assertNotFound("/f/unreg.html")         # names only find registered ones
        self.assertPage(p_url(path), docid)
        self.assertEqual(live.registered(docid), os.path.realpath(path))
        self.assertPage("/f/unreg.html", docid)
        self.assertPage("/d/%s/" % docid, docid)

    def test_not_a_reviewdoc_is_a_plain_404(self):
        eps = os.path.join(TMP, "eps")
        for name in ("plain.html", "fake.html", "secret.txt", "folder.html",
                     "link-to-plain.html", "missing.html"):
            self.assertNotFound(p_url(os.path.join(eps, name)))
        self.assertNotFound("/p/etc/passwd")
        self.assertNotFound("/p/etc/hosts")
        self.assertNotFound("/p/")
        self.assertNotFound(p_url(DOCS["C"][0])[: -len(".html")])    # the build, minus .html

    def test_other_spelling_of_the_same_file(self):
        path, docid = DOCS["C"]
        shouty = path.replace("/gamma/", "/GAMMA/")
        if not os.path.exists(shouty):
            self.skipTest("case-sensitive file system")
        self.assertPage(p_url(shouty), docid)      # not "a copy of itself"

    def test_symlink_to_a_build_is_that_build(self):
        _path, docid = DOCS["C"]
        self.assertPage(p_url(os.path.join(TMP, "eps", "link-to-c.html")), docid)

    def test_dotdot_is_resolved_before_checking(self):
        alpha = os.path.join(TMP, "alpha")
        up = "/.." * (alpha.count("/"))
        self.assertNotFound(p_url(alpha) + up + "/etc/passwd")
        self.assertNotFound(p_url(alpha) + "/%2e%2e" * alpha.count("/") + "/etc/passwd")
        self.assertNotFound(p_url(alpha) + "%2f%2e%2e" * alpha.count("/") + "%2fetc%2fpasswd")
        self.assertNotFound(p_url(alpha) + "/../eps/plain.html")
        # `..` that lands on a genuine build is just another spelling of its path
        self.assertPage(p_url(alpha) + "/../beta%20dir/plan.html", DOCS["B"][1])
        self.assertNotFound("/f/..%2f..%2fetc%2fpasswd")
        self.assertNotFound("/f/..")

    def test_percent_encoding(self):
        path, docid = DOCS["C"]
        self.assertPage("/p/" + quote(path.lstrip("/"), safe=""), docid)   # even %2F works
        self.assertPage("/p/" + quote(path.lstrip("/"), safe="").replace("%2F", "%2f"), docid)
        self.assertNotFound(p_url(path).replace("%20", "+"))   # + is a plus in a path
        self.assertNotFound(p_url(path) + "%00")
        self.assertNotFound(p_url(path)[:-5] + "%00.html")
        self.assertNotFound(p_url(path)[:-5] + "%ff.html")      # not UTF-8
        self.assertNotFound("/f/%ff.html")

    def test_refusals_look_alike(self):
        """Missing, unreadable-as-build and unknown-route answers are identical."""
        eps = os.path.join(TMP, "eps")
        seen = set()
        for path in (p_url(os.path.join(eps, "plain.html")), p_url(os.path.join(eps, "nope.html")),
                     "/p/etc/passwd", "/f/nope.html", "/nowhere"):
            status, h, body = get(path)
            seen.add((status, h["content-type"], h["content-length"], body))
        self.assertEqual(len(seen), 1, seen)

    def test_copy_of_a_registered_document(self):
        path, docid = DOCS["C"]
        copy = os.path.join(TMP, "copy", "lean model review.html")
        status, _h, body = get(p_url(copy))
        self.assertEqual(status, 409)
        self.assertIn(rd.esc(path).encode("utf-8"), body)
        self.assertEqual(live.registered(docid), path, "the original keeps its id")

    # ---- other sites, other hosts ----

    def test_another_site_may_link_but_not_embed_or_register(self):
        path, docid = DOCS["C"]
        xs = {"Sec-Fetch-Site": "cross-site"}
        self.assertPage(p_url(path), docid, **dict(xs, **{"Sec-Fetch-Dest": "document"}))
        self.assertPage("/f/lean%20model%20review.html", docid,
                        **dict(xs, **{"Sec-Fetch-Dest": "iframe"}))
        for dest in ("image", "script", "object", "empty", "style"):
            self.assertNotFound(p_url(path), **dict(xs, **{"Sec-Fetch-Dest": dest}))
            self.assertNotFound("/f/lean%20model%20review.html",
                                **{"Sec-Fetch-Site": "same-site", "Sec-Fetch-Dest": dest})
        # our own pages may fetch them, and a typed URL is not "another site"
        self.assertPage("/f/lean%20model%20review.html", docid,
                        **{"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "empty"})
        self.assertPage(p_url(path), docid, **{"Sec-Fetch-Site": "none", "Sec-Fetch-Dest": "document"})
        # no registration on another site's say-so
        epath, eid = DOCS["E"]
        self.assertNotFound(p_url(epath), **dict(xs, **{"Sec-Fetch-Dest": "document"}))
        self.assertIsNone(live.registered(eid))

    def test_dns_rebinding_host_is_refused(self):
        path, _docid = DOCS["C"]
        for url in (p_url(path), "/f/lean%20model%20review.html", "/f/plan.html"):
            status, _h, _b = get(url, Host="rebind.example:47700")
            self.assertEqual(status, 403, url)

    # ---- the page behaves the same under every address ----

    def test_same_document_under_every_address(self):
        path, docid = DOCS["C"]
        tok = live.token()
        api = {"X-Reviewdoc-Token": tok}
        urls = ("/d/%s/" % docid, "/f/lean%20model%20review.html", p_url(path))
        pages = [self.assertPage(u, docid) for u in urls]
        self.assertEqual(len(set(pages)), 1, "byte-identical page, same RD_LIVE")

        # the reader's note, saved through the API the page uses
        sid = rd.SEC_RE.search(rd.read(path)).group(1)
        note = {"id": "uroute1", "kind": "note", "tag": "question", "quote": "Alpha beta",
                "sectionId": sid, "start": 0, "body": "Why alpha?", "status": "open",
                "replies": []}
        status, _h, body = WIRE.request("POST", "/api/d/%s/notes" % docid, api,
                                        {"doc": docid, "notes": [note]})
        self.assertEqual(status, 200, body)
        for u in urls:
            self.assertIn(b'"uroute1"', get(u)[2])

        # Claude replies from the CLI, by bare file name: the page's poll sees it
        code, _o, err = quiet(rd.main, ["reply", "lean model review.html", "uroute1", "Because."])
        self.assertEqual(code, 0, err)
        self.assertIn(path, err)
        st = json.loads(WIRE.request("GET", "/api/d/%s/state" % docid, api)[2])
        replies = [r for n in st["notes"] if n["id"] == "uroute1" for r in n["replies"]]
        self.assertEqual([(r["author"], r["body"]) for r in replies], [("claude", "Because.")])

        # a rebuild changes `content`: the page compares it and shows Reload
        before = rd_live(get(urls[1])[2])["content"]
        build("gamma/lifecycle analysis/html/lean model review", "The Lean model", "Rewritten.")
        st = json.loads(WIRE.request("GET", "/api/d/%s/state" % docid, api)[2])
        self.assertNotEqual(st["content"], before)
        self.assertEqual(rd_live(get(urls[2])[2])["content"], st["content"])
        self.assertIn("uroute1", [n["id"] for n in st["notes"]], "the note survived the rebuild")

        # Send to Claude
        seq = live.sent_seq(docid)
        status, _h, body = WIRE.request("POST", "/api/d/%s/send" % docid, api, {"doc": docid})
        self.assertEqual(status, 200, body)
        self.assertEqual(live.sent_seq(docid), seq + 1)

    def test_head(self):
        status, h, body = WIRE.request("HEAD", "/f/lean%20model%20review.html")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(h["content-length"]), 1000)


class Cli(unittest.TestCase):

    def daemon(self, version):
        live.write_private(live.spath("daemon.json"),
                           {"pid": os.getpid(), "port": WIRE.port, "version": version})

    def tearDown(self):
        with contextlib.suppress(OSError):
            os.remove(live.spath("daemon.json"))

    def test_friendly_url(self):
        self.daemon(2)
        base = "http://127.0.0.1:%d" % WIRE.port
        c_path, c_id = DOCS["C"]
        self.assertEqual(live.url_for(c_id), base + "/f/lean%20model%20review.html")
        for key in ("A", "B"):                       # plan.html is taken twice: by path
            path, docid = DOCS[key]
            self.assertEqual(live.url_for(docid), base + p_url(path))
        for key in ("A", "B", "C"):
            url = live.url_for(DOCS[key][1])
            status, _h, body = get(url[len(base):])
            self.assertEqual((status, rd.doc_id(body.decode("utf-8"))), (200, DOCS[key][1]))

    def test_old_server_gets_id_urls(self):
        self.daemon(1)
        _path, docid = DOCS["C"]
        self.assertEqual(live.url_for(docid), "http://127.0.0.1:%d/d/%s/" % (WIRE.port, docid))

    def test_status_prints_them(self):
        self.daemon(2)
        code, out, _err = quiet(live.cmd_status, None)
        self.assertIn("/f/lean%20model%20review.html", out)
        self.assertIn(p_url(DOCS["A"][0]), out)

    def test_bare_name(self):
        code, out, err = quiet(rd.main, ["list", "lean model review"])
        self.assertEqual(code, 0, err)
        self.assertIn(DOCS["C"][0], err)
        code, out, err = quiet(rd.main, ["list", "plan.html"])
        self.assertNotEqual(code, 0)
        self.assertIn(DOCS["A"][0], err)
        self.assertIn(DOCS["B"][0], err)
        code, out, err = quiet(rd.main, ["list", "never-built.html"])
        self.assertNotEqual(code, 0)
        self.assertIn("cannot read never-built.html", err)


if __name__ == "__main__":
    unittest.main()
