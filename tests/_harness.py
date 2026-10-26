"""Shared harness for the bext acceptance tests.

Plain-script style, check() counts, finish() prints N/M and
exits non-zero on any failure. Python 3.9+, stdlib plus PyYAML.

Every beans call goes through Instance.beans(), which passes --config <scratch>/.beans.yml
so beans can never find a real instance by searching upward. Instance roots must be
mkdtemp directories outside the live roots below; anything else is refused.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_BEANS = os.environ.get("BEXT_REAL_BEANS") or shutil.which("beans") or "beans"
VENV_PY = sys.executable
# directories a scratch instance must never live under; extend with BEXT_LIVE_ROOTS (os.pathsep-separated)
LIVE_ROOTS = [os.path.realpath(os.path.expanduser(p)) for p in ("~/Documents",) + tuple(filter(None, os.environ.get("BEXT_LIVE_ROOTS", "").split(os.pathsep)))]

FENCE_OPEN = "```yaml beans-ext"
FENCE_CLOSE = "```"

# ---------------------------------------------------------------- counting

_results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    _results.append((name, ok))
    line = ("PASS " if ok else "FAIL ") + name
    if not ok and detail:
        line += "\n     " + str(detail).replace("\n", "\n     ")[:2000]
    print(line, flush=True)
    return ok


def finish(label):
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print("%s: %d/%d passed" % (label, passed, total))
    if total == 0:
        print("%s: ran 0 checks, which is a failure" % label)
        sys.exit(1)
    sys.exit(0 if passed == total else 1)


# ---------------------------------------------------------------- scratch guard

def _under(path, root):
    path = os.path.realpath(path)
    return path == root or path.startswith(root + os.sep)


def scratch_root(prefix="bext-"):
    """A fresh mkdtemp directory, refused if it resolves under a live root."""
    base = os.environ.get("BEXT_SCRATCH_BASE") or None
    d = tempfile.mkdtemp(prefix=prefix, dir=base)
    assert_scratch(d)
    return d


def assert_scratch(d):
    if not d or not os.path.isdir(d):
        raise SystemExit("refusing: scratch dir %r does not exist" % (d,))
    for root in LIVE_ROOTS:
        if _under(d, root):
            raise SystemExit("refusing: %r is under live root %r" % (d, root))
    base = os.path.basename(os.path.realpath(d))
    if not re.match(r"^[A-Za-z0-9_.-]*bext[A-Za-z0-9_.-]*$", base):
        raise SystemExit("refusing: %r is not a bext scratch dir" % (d,))


# ---------------------------------------------------------------- hashing

def fnv1a64(data):
    """FNV-1a 64-bit, hex: the value beans --if-match compares against."""
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


def sha256_file(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------- file anatomy

def split_file(text):
    """(frontmatter_text, body) where body is everything after the closing '---' line."""
    if not text.startswith("---\n"):
        return None, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def interior(body):
    """The body minus the edges: leading and trailing newlines only."""
    return body.strip("\n")


def frontmatter_keys(text):
    fm, _ = split_file(text)
    if fm is None:
        return []
    d = yaml.safe_load(fm) or {}
    return sorted(d.keys())


def find_blocks(body):
    """List of (start_line, end_line, block_text) for every col-0 beans-ext fence pair."""
    lines = body.split("\n")
    out = []
    i = 0
    while i < len(lines):
        if lines[i] == FENCE_OPEN:
            for j in range(i + 1, len(lines)):
                if lines[j] == FENCE_CLOSE:
                    out.append((i, j, "\n".join(lines[i + 1:j]) + "\n"))
                    i = j
                    break
            else:
                out.append((i, None, None))
                break
        i += 1
    return out


class DuplicateKeyError(yaml.YAMLError):
    pass


class ExtLoader(yaml.SafeLoader):
    """SafeLoader without the implicit timestamp resolver, refusing duplicate keys."""


ExtLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for knode, _ in node.value:
        k = loader.construct_object(knode, deep=deep)
        if k in seen:
            raise DuplicateKeyError("duplicate key %r at line %d" % (k, knode.start_mark.line + 1))
        seen.add(k)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


ExtLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_ext(block_text):
    return yaml.load(block_text, Loader=ExtLoader)


def ext_of_file(path):
    """Parsed value of the single beans-ext block in a bean file, or a string describing why not."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    _, body = split_file(text)
    blocks = find_blocks(body)
    if len(blocks) != 1:
        return "<%d blocks>" % len(blocks)
    if blocks[0][2] is None:
        return "<unclosed block>"
    return load_ext(blocks[0][2])


# ---------------------------------------------------------------- scratch beans instance

BEANS_YML = """beans:
    path: .beans
    prefix: T.-
    id_length: 4
    default_status: todo
    default_type: task
"""


class Instance:
    """A scratch beans instance. Never touches anything outside its root."""

    def __init__(self, root=None, copy_from=None):
        self.root = root or scratch_root("bext-inst-")
        assert_scratch(self.root)
        if copy_from:
            shutil.copytree(os.path.join(copy_from, ".beans"), os.path.join(self.root, ".beans"))
            shutil.copy2(os.path.join(copy_from, ".beans.yml"), os.path.join(self.root, ".beans.yml"))
        else:
            os.makedirs(os.path.join(self.root, ".beans"), exist_ok=True)
            with open(os.path.join(self.root, ".beans.yml"), "w") as f:
                f.write(BEANS_YML)
        with open(os.path.join(self.root, ".beans.yml")) as f:
            cfg = yaml.safe_load(f)
        if cfg["beans"]["path"] != ".beans":
            raise SystemExit("refusing: scratch .beans.yml path is %r" % cfg["beans"]["path"])
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home, exist_ok=True)

    @property
    def config(self):
        return os.path.join(self.root, ".beans.yml")

    def env(self, extra=None):
        e = dict(os.environ)
        e.update({"HOME": self.home, "TZ": e.get("TZ", "America/Los_Angeles"), "NO_COLOR": "1"})
        e.pop("BEANS_PATH", None)
        if extra:
            e.update(extra)
        return e

    def beans(self, *args, stdin=None, check_rc=True, beans_bin=None):
        exe = beans_bin or REAL_BEANS
        cmd = ([sys.executable] if exe.endswith(".py") else []) + [exe, "--config", self.config] + list(args)
        p = subprocess.run(cmd, cwd=self.root, env=self.env(), input=stdin,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        if check_rc and p.returncode != 0:
            raise RuntimeError("beans %s failed rc=%d: %s" % (" ".join(args), p.returncode, p.stderr.strip()[:500]))
        return p

    def show(self, bid):
        return json.loads(self.beans("show", "--json", bid).stdout)

    def path_of(self, bid):
        """Absolute path of a bean file, active or archived, by filename (no beans call)."""
        for sub in ("", "archive"):
            d = os.path.join(self.root, ".beans", sub)
            if not os.path.isdir(d):
                continue
            for n in sorted(os.listdir(d)):
                if n.startswith(bid + "--") and n.endswith(".md"):
                    return os.path.join(d, n)
        return None

    def read(self, bid):
        with open(self.path_of(bid), encoding="utf-8") as f:
            return f.read()

    def create(self, title, body, *extra):
        bf = os.path.join(self.root, "body-%d.txt" % abs(hash(title)))
        with open(bf, "w", encoding="utf-8") as f:
            f.write(body)
        out = json.loads(self.beans("create", title, "-t", "task", "-s", "todo", "--body-file", bf, "--json", *extra).stdout)
        os.remove(bf)
        return out["bean"]["id"]

    def write_raw(self, bid, text):
        """Hand edit: replace a bean file's bytes directly (what an editor or a script does)."""
        p = self.path_of(bid)
        assert_scratch(self.root)
        if not _under(p, os.path.realpath(self.root)):
            raise SystemExit("refusing: %r outside scratch" % p)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def remove(self):
        assert_scratch(self.root)
        shutil.rmtree(self.root)


# ---------------------------------------------------------------- the probe fixture

# An awkward body: a bare --- line, an HTML comment, a tab, trailing spaces, a second fenced
# block, unicode, and the beans-ext block in the middle with a hand-written unquoted Z stamp,
# a | literal, nesting, a comment and key order that is not sorted.
PROBE_BLOCK = """ext: 1
zeta: "last-sorted key written first"
jira:
  key: "ABC-123"
  url: "https://jira.example.invalid/browse/ABC-123"
jira_log:
  - at: 2026-10-20T15:04:00Z
    by: robert
    kind: comment
    text: |
      Vendor confirmed the fix lands in 4.2.
      Waiting on their build.
  - at: "2026-10-21T09:30:00-07:00"
    by: agent-b
    text: "In Progress -> In Review"
# a hand comment
time:
  - at: "2026-11-01T01:30:00-07:00"
    minutes: 90
    note: "café, naïve — ok"
  - at: "2026-11-01T01:10:00-08:00"
    minutes: -30
    corrects: "2026-11-01T01:30:00-07:00"
alpha: "  padded  "
"""

PROBE_EXT = {
    "ext": 1,
    "zeta": "last-sorted key written first",
    "jira": {"key": "ABC-123", "url": "https://jira.example.invalid/browse/ABC-123"},
    "jira_log": [
        {"at": "2026-10-20T15:04:00Z", "by": "robert", "kind": "comment",
         "text": "Vendor confirmed the fix lands in 4.2.\nWaiting on their build.\n"},
        {"at": "2026-10-21T09:30:00-07:00", "by": "agent-b", "text": "In Progress -> In Review"},
    ],
    "time": [
        {"at": "2026-11-01T01:30:00-07:00", "minutes": 90, "note": "café, naïve — ok"},
        {"at": "2026-11-01T01:10:00-08:00", "minutes": -30, "corrects": "2026-11-01T01:30:00-07:00"},
    ],
    "alpha": "  padded  ",
}


def probe_body(position="middle", trailing_newlines=3):
    block = FENCE_OPEN + "\n" + PROBE_BLOCK + FENCE_CLOSE + "\n"
    head = ("Intro paragraph with trailing spaces   \n"
            "\tA tab-indented line.\n"
            "\n"
            "---\n"
            "<!-- an HTML comment with ```yaml beans-ext inside it, not at column 0 -->\n"
            "\n"
            "```python\n"
            "print('not the block')\n"
            "```\n"
            "\n")
    tail = ("\n"
            "TAIL-MARKER text after the block, ünïcödé.\n")
    if position == "middle":
        body = head + block + tail
    elif position == "end":
        body = head + block
    elif position == "start":
        body = block + tail
    else:
        raise ValueError(position)
    return body.rstrip("\n") + "\n" * trailing_newlines


def stub_bin_dir(root):
    d = os.path.join(root, "bin")
    os.makedirs(d, exist_ok=True)
    return d
