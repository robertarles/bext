#!/usr/bin/env python3
"""Acceptance tests for bext, written to its documented interface.

Covers container rules C1-C6, history rules H1-H5 and the lint fault list.

Which bext: $BEXT, else the bext script at the repository root.

How bext is run: from the scratch instance root (beans finds .beans.yml by searching upward), with
a guard `beans` first on PATH. The guard logs every call with the FNV-1a of the bean file at that
moment, refuses to run outside the scratch root, adds --config <scratch>/.beans.yml, and then runs
the real beans. Modes (BEXT_STUB_MODE): pass, race-once, race-always (a concurrent hand edit lands
just before an update), noop (update exits 0 and writes nothing).

Interface assumptions the developer should know (where the documented interface is silent, the choice
made here is the most literal reading, and is marked ASSUMED):
  - bext calls `beans` from PATH (ASSUMED; a hard-coded path skips the guard and fails the call checks)
  - `bext get <id> --json` prints the whole block as JSON; `bext get <id> <a.b>` prints a scalar bare
  - `bext set` without --yaml stores a string; with --yaml a YAML value
  - `bext append` key=value pairs land in the entry (value compared as text); --text - reads stdin
  - lint exits 1 on any error; a `Z` stamp is a warning that exits 0 and says "warn" (ASSUMED)
  - lint on a tab fault names "tab"; set/append on a duplicate key says "duplicate" (ASSUMED wording)
  - `by` defaults to some non-empty string; its value is not tested
  - bext refuses (non-zero, file unchanged) to write a block that fails lint

Run: python tests/test_bext.py   (needs the beans CLI on PATH, or BEXT_REAL_BEANS)
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (  # noqa: E402
    FENCE_CLOSE, FENCE_OPEN, PROBE_EXT, REAL_BEANS, VENV_PY, DuplicateKeyError, Instance, check, ext_of_file,
    find_blocks, finish, fnv1a64, load_ext, probe_body, split_file,
)
import yaml  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEXT = os.environ.get("BEXT") or os.path.join(REPO, "bext")
OFFSET_RX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?[+-]\d\d:\d\d$")
_instances = []

STUB = r'''#!%(py)s
import json, os, subprocess, sys
ROOT = %(root)r
REAL = %(real)r
args = sys.argv[1:]
cwd = os.getcwd()
if not os.path.realpath(cwd).startswith(os.path.realpath(ROOT)):
    sys.stderr.write("guard: refusing beans outside scratch (cwd %%s)\n" %% cwd); sys.exit(97)
mode = os.environ.get("BEXT_STUB_MODE", "pass")
log = os.path.join(ROOT, "beans-calls.jsonl")
def fnv(b):
    h = 0xCBF29CE484222325
    for x in b:
        h ^= x; h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return "%%016x" %% h
base = [REAL, "--config", os.path.join(ROOT, ".beans.yml")]
rec = {"argv": args}
sub = [a for a in args if not a.startswith("-")]
if args and args[0] == "update" and len(args) > 1:
    p = json.loads(subprocess.run(base + ["show", "--json", args[1]], stdout=subprocess.PIPE).stdout)
    path = os.path.join(ROOT, ".beans", p["path"])
    rec["disk_fnv"] = fnv(open(path, "rb").read())
    cnt_f = os.path.join(ROOT, "race-count")
    n = int(open(cnt_f).read()) if os.path.exists(cnt_f) else 0
    if mode == "race-adds-block-once" and n == 0:
        with open(path, "a") as f:
            f.write("\n```yaml beans-ext\next: 1\nother_writer: \"x\"\n```\n")
        open(cnt_f, "w").write("1")
        rec["raced"] = 1
    if mode == "race-block-once" and n == 0:
        t = open(path).read()
        open(path, "w").write(t.replace("minutes: 90", "minutes: 91", 1))
        open(cnt_f, "w").write("1")
        rec["raced"] = 1
    if mode == "race-always" or (mode == "race-once" and n == 0):
        with open(path, "a") as f:
            f.write("CONCURRENT edit %%d\n" %% (n + 1))
        open(cnt_f, "w").write(str(n + 1))
        rec["raced"] = n + 1
    if mode == "noop":
        rec["noop"] = True
        with open(log, "a") as f: f.write(json.dumps(rec) + "\n")
        print(json.dumps({"success": True, "message": "Bean updated"})); sys.exit(0)
with open(log, "a") as f: f.write(json.dumps(rec) + "\n")
os.execv(REAL, base + args)
'''


# ------------------------------------------------------------------ plumbing

class Env:
    def __init__(self):
        self.inst = Instance()
        _instances.append(self.inst)
        self.bin = os.path.join(self.inst.root, "bin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "beans")
        with open(stub, "w") as f:
            f.write(STUB % {"py": VENV_PY, "root": self.inst.root, "real": REAL_BEANS})
        os.chmod(stub, 0o755)

    def bext(self, *args, stdin=None, tz="America/Los_Angeles", mode="pass", timeout=90):
        if not os.path.isfile(BEXT):
            return 127, "", "bext not found at %s" % BEXT
        cmd = [VENV_PY, BEXT] + list(args)
        env = self.inst.env({"PATH": self.bin + ":/usr/bin:/bin", "TZ": tz, "BEXT_STUB_MODE": mode,
                             "BEXT_AUTHOR": "bext-test"})
        try:
            p = subprocess.run(cmd, cwd=self.inst.root, env=env, input=stdin, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError as e:
            return 127, "", "bext not found: %s" % e
        except subprocess.TimeoutExpired:
            return 124, "", "bext timed out after %ds" % timeout

    def calls(self):
        p = os.path.join(self.inst.root, "beans-calls.jsonl")
        if not os.path.exists(p):
            return []
        return [json.loads(l) for l in open(p) if l.strip()]

    def bean(self, body, title="bext probe"):
        return self.inst.create(title, body)

    def raw(self, bid):
        return self.inst.read(bid)

    def ext(self, bid):
        return ext_of_file(self.inst.path_of(bid))


def around_block(text):
    """(text before the block, raw block yaml, text after) of a bean file's body."""
    body = split_file(text)[1]
    lines = body.split("\n")
    blocks = find_blocks(body)
    if len(blocks) != 1 or blocks[0][1] is None:
        return None, None, None
    i, j, y = blocks[0]
    return "\n".join(lines[:i]), y, "\n".join(lines[j + 1:])


def same_outside(before_text, after_text):
    b0, _, a0 = around_block(before_text)
    b1, _, a1 = around_block(after_text)
    return b1 is not None and b0.strip("\n") == b1.strip("\n") and a0.rstrip("\n") == a1.rstrip("\n")


def deep(d):
    return json.loads(json.dumps(d))


def expected_offset(tz):
    if ZoneInfo is None:
        return None
    s = datetime.datetime.now(ZoneInfo(tz)).strftime("%z")
    return s[:3] + ":" + s[3:]


def rc_ok(r):
    return r[0] == 0


def ran(r):
    """bext ran and chose its exit itself: not missing (127), not hung (124), not a crash traceback."""
    return r[0] not in (124, 127) and "Traceback (most recent call last)" not in r[2]


def refused(r):
    """A deliberate refusal: non-zero, bext ran, and it said why on stderr or stdout."""
    return r[0] != 0 and ran(r) and (r[1] + r[2]).strip() != ""


def show(r):
    return "rc=%d out=%s err=%s" % (r[0], r[1][-400:], r[2][-400:])


# ------------------------------------------------------------------ get

def t_get():
    e = Env()
    bid = e.bean(probe_body("middle", 1))
    r = e.bext("get", bid, "--json")
    try:
        got = json.loads(r[1])
    except ValueError:
        got = None
    check("get --json: prints the whole block as JSON, equal to the parsed fixture", rc_ok(r) and got == PROBE_EXT, show(r))
    check("get --json: a hand-written unquoted Z stamp comes back as the exact string (no timestamp coercion)",
          isinstance(got, dict) and got.get("jira_log", [{}])[0].get("at") == "2026-10-20T15:04:00Z", got and got.get("jira_log"))
    r = e.bext("get", bid, "jira.key")
    check("get <id> jira.key: prints the scalar at a dotted path", rc_ok(r) and r[1].strip() == "ABC-123", show(r))
    check("get: read-only (get ran ok, no beans update call)",
          rc_ok(r) and e.calls() and not any(c["argv"][:1] == ["update"] for c in e.calls()), show(r))


# ------------------------------------------------------------------ set

def t_set():
    e = Env()
    bid = e.bean(probe_body("middle", 1))
    before = e.raw(bid)
    r = e.bext("set", bid, "jira.key", "XYZ-9")
    after = e.raw(bid)
    want = deep(PROBE_EXT)
    want["jira"]["key"] = "XYZ-9"
    check("set jira.key: exit 0 and the block holds the new value, every other value unchanged",
          rc_ok(r) and e.ext(bid) == want, show(r) + " ext=%r" % (e.ext(bid),))
    wrote = rc_ok(r) and after != before
    check("set: bytes outside the block unchanged (apart from the leading and trailing newlines) and the block did not move",
          wrote and same_outside(before, after))
    _, y, _ = around_block(after)
    if wrote and y:
        keys = [k for k in yaml.load(y, Loader=yaml.SafeLoader)] if y else []
        check("set: top-level key order preserved (no sort_keys)", keys[:len(PROBE_EXT)] == list(PROBE_EXT), keys)
        at_lines = [l for l in y.split("\n") if re.match(r"^\s*(- )?at: ", l)]
        check("set: every `at` stamp is written quoted", at_lines and all(re.match(r"""^\s*(- )?at: ["']""", l) for l in at_lines),
              at_lines)
        check("set: the multi-line text is written as a | literal block", re.search(r"(?m)^\s+text: \|", y) is not None, y)
    else:
        check("set: wrote, and the block is still one parseable block", False, show(r) + after[-300:])
    upd = [c for c in e.calls() if c["argv"][:1] == ["update"]]
    check("set: wrote through beans update --if-match <disk FNV-1a> (guard log)",
          upd and all("--if-match" in c["argv"] and c["argv"][c["argv"].index("--if-match") + 1] == c["disk_fnv"] for c in upd),
          upd)

    r = e.bext("set", bid, "n_str", "5")
    r2 = e.bext("set", bid, "n_yaml", "5", "--yaml")
    x = e.ext(bid) if isinstance(e.ext(bid), dict) else {}
    check("set: a value is a string by default, a YAML value with --yaml",
          rc_ok(r) and rc_ok(r2) and x.get("n_str") == "5" and x.get("n_yaml") == 5, (x.get("n_str"), x.get("n_yaml")))
    r = e.bext("set", bid, "new_map.deep_key", "v")
    x = e.ext(bid) if isinstance(e.ext(bid), dict) else {}
    check("set: a dotted path creates missing mappings", rc_ok(r) and x.get("new_map") == {"deep_key": "v"}, show(r))
    snap = e.raw(bid)
    r = e.bext("set", bid, "Jira.Key", "v")
    check("set: a non-snake_case key (C3) is refused, file unchanged", refused(r) and e.raw(bid) == snap, show(r))

    # no block yet: created at the end, ext: 1 added, body text kept
    e = Env()
    plain = "First line.\n\nSecond paragraph, no block.\n"
    bid = e.bean(plain)
    r = e.bext("set", bid, "jira.key", "ABC-1")
    t = e.raw(bid)
    b, y, a = around_block(t)
    check("set on a bean without a block: a block {ext: 1, jira.key} is created",
          rc_ok(r) and e.ext(bid) == {"ext": 1, "jira": {"key": "ABC-1"}}, show(r) + " ext=%r" % (e.ext(bid),))
    check("set on a bean without a block: the block is at the end and the old body text is unchanged",
          b is not None and b.strip("\n") == plain.strip("\n") and a.strip("\n") == "", repr(t[-300:]))

    # a non-canonical file (legacy id:, tags: [], no blank line after ---): disk hash, not show etag
    e = Env()
    bid = e.bean(probe_body("middle", 1))
    fm, body = split_file(e.raw(bid))
    e.inst.write_raw(bid, "---\nid: legacy-x1\n" + fm.split("\n", 1)[1] + "\ntags: []\n---\n" + body.lstrip("\n"))
    r = e.bext("set", bid, "jira.key", "NC-1")
    x = e.ext(bid)
    check("set on a non-canonical bean (show --json etag would be refused): succeeds",
          rc_ok(r) and isinstance(x, dict) and x["jira"]["key"] == "NC-1", show(r))


def t_set_refusals():
    e = Env()
    bid = e.bean("Intro.\n\n" + FENCE_OPEN + "\next: 1\nok_key: 1\n" + FENCE_CLOSE + "\n")
    r = e.bext("set", bid, "other_key", "v")
    check("set refusals, positive control: the same set on a valid block succeeds",
          rc_ok(r) and e.ext(bid) == {"ext": 1, "ok_key": 1, "other_key": "v"}, show(r))
    cases = [
        ("duplicate key", "ext: 1\ndup_key: 1\ndup_key: 2\n", "duplicate"),
        ("non-snake key already in the block", "ext: 1\njiraKey: \"x\"\n", None),
        ("missing ext", "jira:\n  key: \"x\"\n", None),
    ]
    for name, block, word in cases:
        e = Env()
        bid = e.bean("Intro.\n\n" + FENCE_OPEN + "\n" + block + FENCE_CLOSE + "\n")
        snap = e.raw(bid)
        r = e.bext("set", bid, "other_key", "v")
        ok = refused(r) and e.raw(bid) == snap and not any(c["argv"][:1] == ["update"] for c in e.calls())
        if word:
            ok = ok and word in (r[1] + r[2]).lower()
        check("set refuses a block with a %s (non-zero, no beans update, file unchanged%s)"
              % (name, ", says %r" % word if word else ""), ok, show(r))
    e = Env()
    two = "A.\n\n" + FENCE_OPEN + "\next: 1\n" + FENCE_CLOSE + "\n\nB.\n\n" + FENCE_OPEN + "\next: 1\n" + FENCE_CLOSE + "\n"
    bid = e.bean(two)
    snap = e.raw(bid)
    r = e.bext("set", bid, "k", "v")
    check("set refuses a bean with two blocks (C1), file unchanged", refused(r) and e.raw(bid) == snap, show(r))


# ------------------------------------------------------------------ append

def t_append():
    for tz in ("America/Los_Angeles", "Asia/Kolkata", "UTC"):
        e = Env()
        bid = e.bean(probe_body("middle", 1))
        before = e.raw(bid)
        t0 = time.time()
        r = e.bext("append", bid, "time", "minutes=15", "note=probe", tz=tz)
        x = e.ext(bid)
        lst = x.get("time") if isinstance(x, dict) else None
        new = lst[-1] if isinstance(lst, list) and lst else {}
        at = new.get("at", "")
        check("append[%s]: exit 0, old entries unchanged and in order, the new entry last" % tz,
              rc_ok(r) and isinstance(lst, list) and lst[:-1] == PROBE_EXT["time"] and str(new.get("minutes")) == "15"
              and new.get("note") == "probe", show(r) + " time=%r" % (lst,))
        check("append[%s]: `at` is ISO 8601 local time with a +-HH:MM offset, never Z (H2)" % tz,
              rc_ok(r) and isinstance(at, str) and OFFSET_RX.match(at) is not None, repr(at))
        exp = expected_offset(tz)
        check("append[%s]: the offset is the local one for that zone now (%s)" % (tz, exp),
              isinstance(at, str) and exp is not None and at.endswith(exp), repr(at))
        try:
            when = datetime.datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S") if len(at) >= 19 else None
            sign = 1 if at[-6] == "+" else -1
            off = sign * (int(at[-5:-3]) * 3600 + int(at[-2:]) * 60)
            inst = (when - datetime.datetime(1970, 1, 1)).total_seconds() - off if when else None
        except (ValueError, IndexError):
            inst = None
        check("append[%s]: the stamp is now (within 120 s), not a fixed or UTC-shifted time" % tz,
              inst is not None and abs(inst - t0) < 120, "at=%r now=%s" % (at, t0))
        check("append[%s]: `by` is stamped by default" % tz, isinstance(new.get("by"), str) and new.get("by"), new)
        check("append[%s]: wrote, and bytes outside the block unchanged" % tz,
              rc_ok(r) and e.raw(bid) != before and same_outside(before, e.raw(bid)))

    e = Env()
    bid = e.bean(probe_body("middle", 1))
    r = e.bext("append", bid, "time", "minutes=5", "--at", "2026-11-01T01:10:00-08:00", "--no-by")
    x = e.ext(bid)
    new = x["time"][-1] if isinstance(x, dict) and isinstance(x.get("time"), list) else {}
    check("append --at <stamp with offset>: `at` is exactly that string", rc_ok(r) and new.get("at") == "2026-11-01T01:10:00-08:00",
          show(r) + " new=%r" % new)
    check("append --no-by: no `by` key", rc_ok(r) and "by" not in new, new)
    snap = e.raw(bid)
    r = e.bext("append", bid, "time", "minutes=5", "--at", "2026-11-01T01:10:00")
    check("append --at without an offset is refused, file unchanged", refused(r) and e.raw(bid) == snap, show(r))
    r = e.bext("append", bid, "jira", "note=x")
    check("append to a key that is a mapping, not a list, is refused, file unchanged", refused(r) and e.raw(bid) == snap, show(r))

    r = e.bext("append", bid, "work_log", "kind=start")
    x = e.ext(bid)
    check("append to an absent key creates a one-entry list", rc_ok(r) and isinstance(x, dict) and isinstance(x.get("work_log"), list)
          and len(x["work_log"]) == 1 and x["work_log"][0].get("kind") == "start", show(r))
    text = "Vendor says 4.2.\nSecond line: with a colon.\n"
    r = e.bext("append", bid, "jira_log", "kind=comment", "--text", "-", stdin=text)
    x = e.ext(bid)
    last = x["jira_log"][-1] if isinstance(x, dict) else {}
    _, y, _ = around_block(e.raw(bid))
    check("append --text -: stdin becomes `text`, written as a | literal, value intact",
          rc_ok(r) and isinstance(last.get("text"), str) and last["text"].rstrip("\n") == text.rstrip("\n")
          and y is not None and len(re.findall(r"(?m)^\s+text: \|", y)) >= 2, show(r) + " last=%r" % last)
    check("append: earlier jira_log entries unchanged (the unquoted Z stamp still the same string)",
          rc_ok(r) and isinstance(x, dict) and x["jira_log"][:2] == PROBE_EXT["jira_log"], x.get("jira_log") if isinstance(x, dict) else x)

    e = Env()
    bid = e.bean(probe_body("middle", 1))
    snap = e.raw(bid)
    r = e.bext("append", bid, "time", "Minutes=5")
    check("append with a non-snake_case entry key is refused, file unchanged", refused(r) and e.raw(bid) == snap, show(r))


# ------------------------------------------------------------------ the write path under contention

def t_write_path():
    e = Env()
    bid = e.bean(probe_body("middle", 1))
    r = e.bext("set", bid, "jira.key", "RACE-1", mode="race-once")
    t = e.raw(bid)
    x = e.ext(bid)
    upd = [c for c in e.calls() if c["argv"][:1] == ["update"]]
    check("race once: bext retries and succeeds (exit 0, >= 2 update attempts)", rc_ok(r) and len(upd) >= 2, show(r) + " updates=%d" % len(upd))
    check("race once: no lost update: the concurrent edit AND the new value are both in the file",
          "CONCURRENT edit 1" in t and isinstance(x, dict) and x["jira"]["key"] == "RACE-1", t[-300:])
    check("race once: every update carried --if-match equal to the disk FNV-1a at that moment",
          upd and all(c["argv"][c["argv"].index("--if-match") + 1] == c["disk_fnv"] for c in upd if "--if-match" in c["argv"])
          and all("--if-match" in c["argv"] for c in upd), upd)

    e = Env()
    bid = e.bean(probe_body("middle", 1))
    r = e.bext("set", bid, "jira.key", "RACE-B", mode="race-block-once")
    x = e.ext(bid)
    # beans 0.4.2 applies --body-replace-old before the etag check, so this race surfaces as
    # "text not found in body", not "etag mismatch". bext treats it as
    # stale and retries, so only a retry that keeps both changes passes
    upd = [c for c in e.calls() if c["argv"][:1] == ["update"]]
    retried = rc_ok(r) and isinstance(x, dict) and x["time"][0]["minutes"] == 91 and x["jira"]["key"] == "RACE-B"
    check("race inside the block (another writer changed a value): bext treats 'text not found' as stale, retries, keeps both",
          any(c.get("raced") for c in e.calls()) and len(upd) >= 2 and retried,
          show(r) + " ext=%r" % (x if not isinstance(x, dict) else (x.get("time"), x.get("jira")),))

    # no block yet, and another writer adds one between bext's read and its write: only --if-match stops a second block
    e = Env()
    bid = e.bean("Plain body, no block.\n")
    r = e.bext("set", bid, "jira.key", "RACE-A", mode="race-adds-block-once")
    t = e.raw(bid)
    nblocks = len(find_blocks(split_file(t)[1]))
    x = e.ext(bid)
    check("race adds a block before bext's first write: never two blocks; retried into one block holding both, or refused",
          any(c.get("raced") for c in e.calls()) and nblocks == 1 and isinstance(x, dict) and x.get("other_writer") == "x"
          and (x.get("jira", {}).get("key") == "RACE-A" if rc_ok(r) else refused(r)), show(r) + " blocks=%d" % nblocks)

    e = Env()
    bid = e.bean(probe_body("middle", 1))
    r = e.bext("set", bid, "jira.key", "RACE-2", mode="race-always", timeout=60)
    t = e.raw(bid)
    upd = [c for c in e.calls() if c["argv"][:1] == ["update"]]
    check("race always: bext fails loudly (non-zero, a message on stderr) rather than forcing the write",
          refused(r) and r[2].strip() != "" and len(upd) >= 2, show(r))
    check("race always: it retried, and at most 3 times (2 to 4 update attempts)", 2 <= len(upd) <= 4, "updates=%d" % len(upd))
    check("race always: the file holds every concurrent edit and not the refused value",
          "CONCURRENT edit %d" % len(upd) in t and "RACE-2" not in t, t[-300:])

    e = Env()
    bid = e.bean(probe_body("middle", 1))
    snap = e.raw(bid)
    r = e.bext("set", bid, "jira.key", "NOOP-1", mode="noop")
    check("noop beans (update exits 0 but writes nothing): read-after-write catches it, non-zero exit",
          refused(r) and any(c.get("noop") for c in e.calls()), show(r))
    check("noop beans: bext called update and did not write the file itself",
          any(c.get("noop") for c in e.calls()) and e.raw(bid) == snap)


# ------------------------------------------------------------------ lint

CLEAN = """ext: 1
jira:
  key: "ABC-1"
log:
  - at: "2026-10-20T10:00:00-07:00"
    note: "one"
  - at: "2026-10-21T10:00:00-07:00"
    note: "two"
"""


def clean_body(block=CLEAN):
    return "Intro text.\n\n" + FENCE_OPEN + "\n" + block + FENCE_CLOSE + "\n\nAfter text.\n"


def _swap(old, new):
    def f(body, fm):
        assert old in body, old
        return body.replace(old, new, 1), fm
    return f


FAULTS = [
    # (name, transform(body, frontmatter) -> (body, frontmatter), required word in output or None)
    ("a missing closing fence", lambda b, fm: (b.replace(FENCE_CLOSE + "\n\nAfter", "\nAfter", 1), fm), None),
    ("an indented fence", lambda b, fm: (b.replace(FENCE_OPEN, "  " + FENCE_OPEN, 1).replace("\n" + FENCE_CLOSE + "\n", "\n  " + FENCE_CLOSE + "\n", 1), fm), None),
    ("two blocks", lambda b, fm: (b + "\n" + FENCE_OPEN + "\next: 1\n" + FENCE_CLOSE + "\n", fm), None),
    ("tab indentation", _swap("  key: \"ABC-1\"", "\tkey: \"ABC-1\""), "tab"),
    ("a YAML syntax error (unquoted ': ')", _swap('note: "one"', "note: one: two"), None),
    ("a duplicate key", _swap("ext: 1\n", "ext: 1\ndup_key: 1\ndup_key: 2\n"), None),
    ("a non-snake_case top-level key", _swap("ext: 1\n", "ext: 1\njiraKey: \"x\"\n"), None),
    ("a non-snake_case nested key", _swap("  key: \"ABC-1\"", "  Key: \"ABC-1\""), None),
    ("a missing ext", _swap("ext: 1\n", ""), None),
    ("a history entry without at", _swap('  - at: "2026-10-21T10:00:00-07:00"\n    note: "two"', '  - note: "two"'), None),
    ("an at without an offset", _swap("2026-10-21T10:00:00-07:00", "2026-10-21T10:00:00"), None),
    ("a frontmatter key outside beans' set", lambda b, fm: (b, fm.replace("type: task\n", "type: task\njira_key: ABC-1\n", 1)), None),
]


def lint_fixture_sanity():
    """The fault fixtures must each carry exactly the fault they claim (checked with the reference parser)."""
    blocks = find_blocks(clean_body())
    check("lint fixtures: the clean block parses, has ext: 1 and offset stamps", len(blocks) == 1 and load_ext(blocks[0][2])["ext"] == 1)
    fm0 = "title: x\nstatus: todo\ntype: task\n"
    for name, f, _ in FAULTS:
        b, fm = f(clean_body(), fm0)
        check("lint fixtures: '%s' differs from the clean fixture" % name, (b, fm) != (clean_body(), fm0))
    b, _ = dict((n, f) for n, f, _ in FAULTS)["a duplicate key"](clean_body(), fm0)
    try:
        load_ext(find_blocks(b)[0][2])
        dup = False
    except DuplicateKeyError:
        dup = True
    check("lint fixtures: the duplicate-key fixture loads silently with plain safe_load (so only a dup check sees it)",
          dup and isinstance(yaml.safe_load(find_blocks(b)[0][2]), dict))


def t_lint():
    e = Env()
    clean = e.bean(clean_body(), "clean")
    noblock = e.bean("No block here at all.\n", "noblock")
    r = e.bext("lint", clean)
    check("lint <id>: a clean bean exits 0", r[0] == 0, show(r))
    r = e.bext("lint", noblock)
    check("lint <id>: a bean without a block exits 0", r[0] == 0, show(r))
    for name, f, word in FAULTS:
        e = Env()
        bid = e.bean(clean_body(), "fault")
        t = e.raw(bid)
        fm, body = split_file(t)
        nb, nfm = f(body, fm + "\n")
        e.inst.write_raw(bid, "---\n" + nfm.rstrip("\n") + "\n---\n" + nb)
        snap = e.raw(bid)
        r = e.bext("lint", bid)
        out = (r[1] + r[2])
        ok = r[0] == 1 and bid in out
        if word:
            ok = ok and word in out.lower()
        check("lint catches %s: exit 1, names the bean%s" % (name, ", says %r" % word if word else ""), ok, show(r))
        check("lint on %s: read-only (lint ran, file unchanged, no beans update)" % name,
              ran(r) and r[0] in (0, 1) and e.raw(bid) == snap and not any(c["argv"][:1] == ["update"] for c in e.calls()))
    e = Env()
    bid = e.bean(clean_body(CLEAN.replace("2026-10-21T10:00:00-07:00", "2026-10-21T17:00:00Z")), "zstamp")
    r = e.bext("lint", bid)
    check("lint: an `at` in Z is a warning: exit 0 and the output says warn", r[0] == 0 and "warn" in (r[1] + r[2]).lower(), show(r))

    # by instance directory, and with no argument from inside the instance
    e = Env()
    good = e.bean(clean_body(), "good")
    bad = e.bean(clean_body(CLEAN.replace("ext: 1\n", "")), "bad")
    r = e.bext("lint", e.inst.root)
    check("lint <instance dir>: exit 1 and names the faulty bean", r[0] == 1 and bad in (r[1] + r[2]), show(r))
    r = e.bext("lint")
    check("lint (no argument, inside the instance): exit 1 and names the faulty bean", r[0] == 1 and bad in (r[1] + r[2]), show(r))
    e = Env()
    e.bean(clean_body(), "good1")
    e.bean("plain\n", "good2")
    r = e.bext("lint", e.inst.root)
    check("lint <instance dir> with only clean beans: exit 0", r[0] == 0, show(r))


# ------------------------------------------------------------------ real-bext behaviours 

NESTED_EXAMPLE = ("An example of the block, quoted in a markdown fence (not a block):\n\n"
                  "````markdown\n" + FENCE_OPEN + "\next: 1\nexample_only: \"do not read me\"\n" + FENCE_CLOSE + "\n````\n")


def t_extras():
    # M9: stray frontmatter keys make bext refuse to write; the legacy id: key is exempt
    e = Env()
    bid = e.bean(clean_body(), "stray")
    fm, body = split_file(e.raw(bid))
    e.inst.write_raw(bid, "---\n" + fm.replace("type: task\n", "type: task\njira_key: ABC-1\n", 1) + "\n---\n" + body)
    snap = e.raw(bid)
    r = e.bext("set", bid, "other_key", "v")
    check("set refuses a bean whose frontmatter has a stray key (it would be lost): non-zero, no update, file unchanged",
          refused(r) and e.raw(bid) == snap and not any(c["argv"][:1] == ["update"] for c in e.calls()), show(r))
    r = e.bext("append", bid, "log", "note=x")
    check("append refuses the same stray-key bean, file unchanged", refused(r) and e.raw(bid) == snap, show(r))
    e = Env()
    bid = e.bean(clean_body(), "legacy")
    fm, body = split_file(e.raw(bid))
    e.inst.write_raw(bid, "---\nid: legacy-x1\n" + fm.split("\n", 1)[1] + "\n---\n" + body)
    r = e.bext("lint", bid)
    check("lint: a legacy id: frontmatter key alone is exempt (exit 0)", r[0] == 0 and ran(r), show(r))
    r = e.bext("set", bid, "other_key", "v")
    x = e.ext(bid)
    check("set on a bean whose only extra frontmatter key is legacy id: succeeds (positive control for the refusal)",
          rc_ok(r) and isinstance(x, dict) and x.get("other_key") == "v", show(r))

    # M10: key:=<yaml> parses the value; key=value stays a string
    e = Env()
    bid = e.bean(clean_body(), "typed")
    r = e.bext("append", bid, "log", "n_yaml:=90", "n_str=90", "flag:=true", "items:=[a, b]", "word=true",
               "--at", "2026-10-20T11:00:00-07:00")
    x = e.ext(bid)
    new = x["log"][-1] if isinstance(x, dict) and isinstance(x.get("log"), list) else {}
    check("append key:=<yaml>: parsed (90 -> int, true -> bool, [a, b] -> list)",
          rc_ok(r) and new.get("n_yaml") == 90 and type(new.get("n_yaml")) is int and new.get("flag") is True
          and new.get("items") == ["a", "b"], show(r) + " new=%r" % new)
    check("append key=value: stays a string ('90', 'true')",
          rc_ok(r) and new.get("n_str") == "90" and new.get("word") == "true", "new=%r" % new)
    snap = e.raw(bid)
    r = e.bext("append", bid, "log", "bad:=[unclosed")
    check("append key:=<yaml> that does not parse is refused, file unchanged", refused(r) and e.raw(bid) == snap, show(r))

    # M12: a beans-ext fence inside another fenced block is an example, not a block
    e = Env()
    bid = e.bean(NESTED_EXAMPLE + "\n" + FENCE_OPEN + "\n" + CLEAN + FENCE_CLOSE + "\n\nAfter text.\n", "nested")
    r = e.bext("get", bid, "--json")
    try:
        got = json.loads(r[1])
    except ValueError:
        got = None
    check("nested fence: get reads the real block, not the example inside the ```` fence",
          rc_ok(r) and got == load_ext(CLEAN), show(r))
    r = e.bext("lint", bid)
    check("nested fence: lint does not count the quoted example as a second block (exit 0)", r[0] == 0 and ran(r), show(r))
    r = e.bext("set", bid, "other_key", "v")
    t = e.raw(bid)
    tail = t.split("\n````\n", 1)[1] if "\n````\n" in t else ""
    real = find_blocks(tail)
    check("nested fence: set writes the real block and leaves the quoted example byte-identical",
          rc_ok(r) and NESTED_EXAMPLE in t and len(real) == 1 and load_ext(real[0][2]).get("other_key") == "v", show(r))
    e = Env()
    bid = e.bean(NESTED_EXAMPLE, "nested-only")
    r = e.bext("set", bid, "jira.key", "N-1")
    t = e.raw(bid)
    tail = t.split("\n````\n", 1)[1] if "\n````\n" in t else ""
    real = find_blocks(tail)
    check("nested fence only: set creates a new block after it and leaves the example unchanged",
          rc_ok(r) and NESTED_EXAMPLE in t and len(real) == 1 and load_ext(real[0][2]) == {"ext": 1, "jira": {"key": "N-1"}},
          show(r) + repr(t[-300:]))


def main():
    print("bext under test: %s (%s)" % (BEXT, "present" if os.path.isfile(BEXT) else "MISSING"))
    check("bext is present (set BEXT=...)", os.path.isfile(BEXT))
    try:
        lint_fixture_sanity()
        for t in (t_get, t_set, t_set_refusals, t_append, t_write_path, t_lint, t_extras):
            t()
    finally:
        for inst in _instances:
            inst.remove()
    finish("test_bext")


if __name__ == "__main__":
    main()
