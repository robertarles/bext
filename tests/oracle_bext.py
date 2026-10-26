#!/usr/bin/env python3
"""TEST ORACLE for test_bext.py. NOT the bext implementation.

A minimal stand-in written from bext's documented interface, for two reasons only:
it shows test_bext.py can pass (a test only ever seen failing proves nothing), and
mutants_bext.py makes one-line broken copies of it to show each test can fail. It has no
retries tuning, no error polish, no secret scan, no migrate. Do not install or ship it.

Usage mirrors bext: get / set / append / lint. Calls `beans` from PATH.
"""
import datetime
import getpass
import json
import os
import re
import subprocess
import sys

import yaml

FENCE_OPEN, FENCE_CLOSE = "```yaml beans-ext", "```"
KNOWN_FM = {"title", "status", "type", "priority", "tags", "created_at", "updated_at", "parent", "blocking", "blocked_by"}
KEY_RX = re.compile(r"^[a-z][a-z0-9_]*$")
OFFSET_RX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?[+-]\d\d:\d\d$")
Z_RX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?Z$")
RETRIES = 3


def die(msg, code=1):
    sys.stderr.write("bext: %s\n" % msg)
    sys.exit(code)


class Dup(yaml.YAMLError):
    pass


class Loader(yaml.SafeLoader):
    pass


Loader.yaml_implicit_resolvers = {k: [(t, r) for t, r in v if t != "tag:yaml.org,2002:timestamp"]
                                  for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}


def _mapping(loader, node, deep=False):
    seen = set()
    for kn, _ in node.value:
        k = loader.construct_object(kn, deep=deep)
        if k in seen:
            raise Dup("duplicate key %r" % (k,))
        seen.add(k)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


class Dumper(yaml.SafeDumper):
    pass


PLAIN_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
YAML_WORDS = {"yes", "no", "on", "off", "true", "false", "null", "y", "n", "~"}


def _str(dumper, s):
    if "\n" in s:
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|")
    if PLAIN_OK.match(s) and s.lower() not in YAML_WORDS:
        return dumper.represent_scalar("tag:yaml.org,2002:str", s)
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style='"')


Dumper.add_representer(str, _str)


def dump(ext):
    return yaml.dump(ext, Dumper=Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False, width=4096)


def fnv(b):
    h = 0xCBF29CE484222325
    for x in b:
        h ^= x
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


def beans(*args, stdin=None):
    return subprocess.run(["beans"] + list(args), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)


def instance_root(start=None):
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(d, ".beans.yml")):
            return d
        if os.path.dirname(d) == d:
            die("no .beans.yml found")
        d = os.path.dirname(d)


def beans_dir(root):
    with open(os.path.join(root, ".beans.yml")) as f:
        return os.path.join(root, yaml.safe_load(f)["beans"]["path"])


def show(bid):
    p = beans("show", "--json", bid)
    if p.returncode != 0:
        die("beans show %s failed: %s" % (bid, p.stderr.strip()))
    return json.loads(p.stdout)


def split_fm(text):
    if not text.startswith("---\n"):
        return None, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


OTHER_FENCE_RX = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def blocks(body):
    lines = body.split("\n")
    out, i = [], 0
    while i < len(lines):
        m = OTHER_FENCE_RX.match(lines[i])
        if lines[i] != FENCE_OPEN and m and "beans-ext" not in m.group(2):
            close = re.compile(r"^ {0,3}" + re.escape(m.group(1)[0]) + "{%d,}\\s*$" % len(m.group(1)))
            i += 1
            while i < len(lines) and not close.match(lines[i]):
                i += 1
            i += 1
            continue
        if lines[i] == FENCE_OPEN:
            for j in range(i + 1, len(lines)):
                if lines[j] == FENCE_CLOSE:
                    out.append(("\n".join(lines[i:j + 1]), "\n".join(lines[i + 1:j]) + "\n"))
                    i = j
                    break
            else:
                out.append((None, None))
                break
        i += 1
    return out


# ------------------------------------------------------------------ lint

def lint_ext(ext):
    errs, warns = [], []
    if not isinstance(ext, dict):
        return ["block is not a mapping"], warns

    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                if not isinstance(k, str) or not KEY_RX.match(k):
                    errs.append("key %r at %s is not snake_case" % (k, path or "top"))
                walk(x, "%s.%s" % (path, k) if path else str(k))
        elif isinstance(v, list):
            dicts = [x for x in v if isinstance(x, dict)]
            if any("at" in x for x in dicts):
                for n, x in enumerate(dicts):
                    if "at" not in x:
                        errs.append("history entry %s[%d] has no at" % (path, n))
                    elif not isinstance(x["at"], str):
                        errs.append("at in %s[%d] is not a string" % (path, n))
                    elif Z_RX.match(x["at"]):
                        warns.append("at %s in %s[%d] is Z: valid, locality lost" % (x["at"], path, n))
                    elif not OFFSET_RX.match(x["at"]):
                        errs.append("at %r in %s[%d] has no UTC offset" % (x["at"], path, n))
            for n, x in enumerate(v):
                walk(x, "%s[%d]" % (path, n))
    walk(ext, "")
    if ext.get("ext") != 1:
        errs.append("ext: 1 is missing")
    return errs, warns


def lint_text(text):
    errs, warns = [], []
    fm, body = split_fm(text)
    if fm is not None:
        try:
            d = yaml.safe_load(fm) or {}
            for k in d:
                if k not in KNOWN_FM and k != "id":
                    errs.append("frontmatter key %r is outside beans' set and will be lost" % k)
        except yaml.YAMLError as e:
            errs.append("frontmatter: %s" % e)
    for line in body.split("\n"):
        if re.match(r"^\s+```yaml beans-ext\s*$", line):
            errs.append("indented beans-ext fence")
    bl = blocks(body)
    if len(bl) > 1:
        errs.append("%d beans-ext blocks (at most one)" % len(bl))
    for full, y in bl:
        if full is None:
            errs.append("beans-ext block has no closing fence")
            continue
        if re.search(r"(?m)^[ ]*\t", y):
            errs.append("tab in indentation")
        try:
            ext = yaml.load(y, Loader=Loader)
        except Dup as e:
            errs.append(str(e))
            continue
        except yaml.YAMLError as e:
            errs.append("YAML syntax: %s" % str(e).replace("\n", " "))
            continue
        e2, w2 = lint_ext(ext)
        errs += e2
        warns += w2
    return errs, warns


def bean_files(root):
    d = beans_dir(root)
    for dp, _, fns in sorted(os.walk(d)):
        for n in sorted(fns):
            if n.endswith(".md"):
                yield n.split("--")[0][:-3] if "--" not in n else n.split("--")[0], os.path.join(dp, n)


def cmd_lint(args):
    targets = []
    if not args:
        targets = list(bean_files(instance_root()))
    for a in args:
        if os.path.isdir(a):
            targets += list(bean_files(instance_root(a)))
        else:
            root = instance_root()
            targets.append((a, os.path.join(beans_dir(root), show(a)["path"])))
    bad = 0
    for bid, path in targets:
        with open(path, encoding="utf-8") as f:
            errs, warns = lint_text(f.read())
        for w in warns:
            print("%s: WARN %s" % (bid, w))
        for e in errs:
            print("%s: ERROR %s" % (bid, e))
        bad += bool(errs)
    sys.exit(1 if bad else 0)


# ------------------------------------------------------------------ read / write

def current(bid):
    root = instance_root()
    meta = show(bid)
    path = os.path.join(beans_dir(root), meta["path"])
    raw = open(path, "rb").read()
    fm, _ = split_fm(raw.decode("utf-8"))
    stray = [k for k in (yaml.safe_load(fm) or {}) if k not in KNOWN_FM and k != "id"] if fm else []
    if stray:
        die("refusing to write: frontmatter key(s) %s would be lost on the next beans write" % ", ".join(map(str, stray)))
    bl = blocks(meta["body"])
    if len(bl) > 1:
        die("%s has %d beans-ext blocks; fix by hand first" % (bid, len(bl)))
    if bl and bl[0][0] is None:
        die("%s: beans-ext block has no closing fence" % bid)
    if bl:
        try:
            ext = yaml.load(bl[0][1], Loader=Loader)
        except Dup as e:
            die("%s: refusing to write: %s" % (bid, e))
        except yaml.YAMLError as e:
            die("%s: refusing to write: YAML syntax: %s" % (bid, e))
        return path, raw, bl[0][0], ext
    return path, raw, None, None


def write(bid, mutate):
    for attempt in range(RETRIES + 1):
        path, raw, old_block, ext = current(bid)
        new_ext = {"ext": 1} if ext is None else json.loads(json.dumps(ext))
        mutate(new_ext)
        errs, _ = lint_ext(new_ext)
        if errs:
            die("refusing to write a block that fails lint: %s" % "; ".join(errs))
        new_block = FENCE_OPEN + "\n" + dump(new_ext) + FENCE_CLOSE
        if old_block is None:
            args = ["update", bid, "--if-match", fnv(raw), "--body-append", new_block]
        else:
            args = ["update", bid, "--if-match", fnv(raw), "--body-replace-old", old_block, "--body-replace-new", new_block]
        p = beans(*args)
        if p.returncode != 0:
            # a concurrent edit to the block surfaces as "text not found in body": beans applies the
            # replacement before the etag check (treated as stale: re-read and retry)
            if "etag mismatch" in (p.stderr + p.stdout) or "text not found in body" in (p.stderr + p.stdout):
                continue
            die("beans update failed: %s" % p.stderr.strip())
        _, _, _, back = current(bid)
        if back != new_ext:
            die("read-back mismatch: the write did not land")
        return
    die("etag mismatch %d times: another writer keeps changing %s; nothing written" % (RETRIES + 1, bid))


def walk_to(d, keys, create):
    for k in keys:
        if not isinstance(d, dict):
            die("%s is not a mapping" % k)
        if k not in d:
            if not create:
                die("no key %s" % k)
            d[k] = {}
        d = d[k]
    return d


def stamp(given):
    if given:
        if not OFFSET_RX.match(given):
            die("--at %r must be ISO 8601 with a UTC offset" % given)
        return given
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def main():
    a = sys.argv[1:]
    if not a:
        die("usage: bext get|set|append|lint ...", 2)
    cmd, rest = a[0], a[1:]
    if cmd == "lint":
        return cmd_lint(rest)
    if cmd == "get":
        as_json = "--json" in rest
        rest = [x for x in rest if x != "--json"]
        _, _, _, ext = current(rest[0])
        v = ext
        for k in (rest[1].split(".") if len(rest) > 1 else []):
            v = v[int(k)] if isinstance(v, list) else v[k]
        if as_json:
            print(json.dumps(v, ensure_ascii=False))
        elif isinstance(v, (dict, list)):
            sys.stdout.write(dump(v))
        else:
            print(v)
        return
    if cmd == "set":
        as_yaml = "--yaml" in rest
        rest = [x for x in rest if x != "--yaml"]
        bid, path, value = rest[0], rest[1].split("."), rest[2]
        val = yaml.load(value, Loader=Loader) if as_yaml else value

        def m(ext):
            walk_to(ext, path[:-1], True)[path[-1]] = val
        return write(bid, m)
    if cmd == "append":
        bid, path = rest[0], rest[1].split(".")
        opts, pairs, i = {"at": None, "by": True, "text": None}, [], 2
        while i < len(rest):
            x = rest[i]
            if x == "--at":
                opts["at"] = rest[i + 1]
                i += 2
                continue
            if x == "--no-by":
                opts["by"] = False
            elif x == "--text":
                opts["text"] = sys.stdin.read() if rest[i + 1] == "-" else rest[i + 1]
                i += 2
                continue
            else:
                k, _, v = x.partition("=")
                if k.endswith(":"):
                    k = k[:-1]
                    try:
                        v = yaml.load(v, Loader=Loader)
                    except yaml.YAMLError as e:
                        die("%s value does not parse: %s" % (k, e))
                pairs.append((k, v))
            i += 1
        entry = {"at": stamp(opts["at"])}
        if opts["by"]:
            entry["by"] = os.environ.get("BEXT_AUTHOR") or getpass.getuser()
        for k, v in pairs:
            entry[k] = v
        if opts["text"] is not None:
            entry["text"] = opts["text"]

        def m(ext):
            parent = walk_to(ext, path[:-1], True)
            lst = parent.setdefault(path[-1], [])
            if not isinstance(lst, list):
                die("%s is not a list" % rest[1])
            lst.append(entry)
        return write(bid, m)
    die("unknown command %s" % cmd, 2)


if __name__ == "__main__":
    main()
