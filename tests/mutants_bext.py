#!/usr/bin/env python3
"""Proves test_bext.py discriminates: runs it against one-line broken copies of oracle_bext.py.

Each mutant is a plausible mistake a competent developer could make in bext, with the check
family expected to catch it. Killed = test_bext exits non-zero AND a FAIL line contains the
expected text. The unmodified oracle runs first as the control (must pass every check).
Copies live in a mkdtemp scratch dir; the oracle file itself is never edited.

Run: python tests/mutants_bext.py [name ...]
     python tests/mutants_bext.py --real   (mutants of the real bext; see REAL_MUTANTS)
"""
import concurrent.futures
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import scratch_root  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ORACLE = os.path.join(HERE, "oracle_bext.py")
PY = sys.executable

# name: (old, new, replace-all?, expected text in a FAIL line)
MUTANTS = {
    "no-if-match": ('"--if-match", fnv(raw), ', "", True, "never two blocks"),
    "show-etag": ("fnv(raw)", 'show(bid)["etag"]', True, "set on a non-canonical bean"),
    "naive-yaml-load": ("ext = yaml.load(bl[0][1], Loader=Loader)", "ext = yaml.safe_load(bl[0][1])", False,
                        "unquoted Z stamp comes back as the exact string"),
    "duplicates-allowed": ('raise Dup("duplicate key %r" % (k,))', "pass", False, "set refuses a block with a duplicate key"),
    "stamp-utc-z": ('datetime.datetime.now().astimezone().isoformat(timespec="seconds")',
                    'datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")', False,
                    "never Z"),
    "stamp-utc-offset": ('datetime.datetime.now().astimezone().isoformat(timespec="seconds")',
                         'datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")', False,
                         "the offset is the local one"),
    "stamp-naive": ('datetime.datetime.now().astimezone().isoformat(timespec="seconds")',
                    'datetime.datetime.now().isoformat(timespec="seconds")', False, "+-HH:MM offset"),
    "no-retry-on-text-not-found": (' or "text not found in body" in (p.stderr + p.stdout)', "", False,
                                   "treats 'text not found' as stale"),
    "no-retry": ("RETRIES = 3", "RETRIES = 0", False, "race once: bext retries"),
    "too-many-retries": ("RETRIES = 3", "RETRIES = 6", False, "at most 3 times"),
    "retry-forever": ("for attempt in range(RETRIES + 1):", "while True:", False, "race always: bext fails loudly"),
    "no-read-back": ("if back != new_ext:", "if False:", False, "noop beans (update exits 0 but writes nothing)"),
    "writes-file-directly": ("p = beans(*args)",
                             'p = (open(path, "w").write(raw.decode().replace(old_block, new_block)) if old_block else '
                             'open(path, "a").write("\\n" + new_block + "\\n")) and subprocess.CompletedProcess([], 0, "", "")',
                             False, "wrote through beans update --if-match"),
    "moves-block-to-end": ('"--body-replace-new", new_block]', '"--body-replace-new", "\\n", "--body-append", new_block]', False,
                           "the block did not move"),
    "sort-keys": ("sort_keys=False", "sort_keys=True", False, "key order preserved"),
    "no-literal": ('style="|"', "style='\"'", False, "| literal"),
    # PyYAML quotes a plain scalar that would resolve as a timestamp, so widening PLAIN_OK alone is an
    # equivalent mutant (seen surviving 2026-10-20); dropping the dumper's timestamp resolver too is the real mistake
    "stamps-unquoted": ("Dumper.add_representer(str, _str)",
                        'Dumper.add_representer(str, _str); Dumper.yaml_implicit_resolvers = Loader.yaml_implicit_resolvers; '
                        'PLAIN_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:+-]*$")', False, "every `at` stamp is written quoted"),
    "prepend": ("lst.append(entry)", "lst.insert(0, entry)", False, "the new entry last"),
    # alone this is equivalent: the lint-on-write guard refuses the same stamp (seen surviving 2026-10-20)
    "at-without-offset-ok+write-skips-lint": ([("if not OFFSET_RX.match(given):", "if False:"),
                                               ("        if errs:\n            die(\"refusing to write", "        if False:\n            die(\"refusing to write")],
                                              None, False, "--at without an offset is refused"),
    "no-by-default": ('if opts["by"]:', "if False:", False, "`by` is stamped by default"),
    "no-by-ignored": ('opts["by"] = False', 'opts["by"] = True', False, "append --no-by"),
    "set-yaml-ignored": ("val = yaml.load(value, Loader=Loader) if as_yaml else value", "val = value", False,
                         "a YAML value with --yaml"),
    "write-skips-lint": ("        if errs:\n            die(\"refusing to write", "        if False:\n            die(\"refusing to write",
                         False, "non-snake_case key (C3) is refused"),
    "lint-no-indent-check": ('if re.match(r"^\\s+```yaml beans-ext\\s*$", line):', "if False:", False, "lint catches an indented fence"),
    "lint-two-blocks-ok": ("if len(bl) > 1:\n        errs.append", "if len(bl) > 99:\n        errs.append", False, "lint catches two blocks"),
    "lint-unclosed-ok": ('errs.append("beans-ext block has no closing fence")', "pass", False, "lint catches a missing closing fence"),
    "lint-no-tab-check": ('if re.search(r"(?m)^[ ]*\\t", y):', "if False:", False, "lint catches tab indentation"),
    "lint-no-syntax-error": ('errs.append("YAML syntax: %s" % str(e).replace("\\n", " "))', "pass", False,
                             "lint catches a YAML syntax error"),
    "lint-no-dup-check": ("except Dup as e:\n            errs.append(str(e))", "except Dup as e:\n            pass", False,
                          "lint catches a duplicate key"),
    "lint-snake-top-only": ("if not isinstance(k, str) or not KEY_RX.match(k):",
                            'if path == "" and (not isinstance(k, str) or not KEY_RX.match(k)):', False,
                            "lint catches a non-snake_case nested key"),
    "lint-no-ext-check": ('if ext.get("ext") != 1:', "if False:", False, "lint catches a missing ext"),
    "lint-at-missing-ok": ('errs.append("history entry %s[%d] has no at" % (path, n))', "pass", False,
                           "lint catches a history entry without at"),
    "lint-offset-ok": ('errs.append("at %r in %s[%d] has no UTC offset" % (x["at"], path, n))', "pass", False,
                       "lint catches an at without an offset"),
    "lint-z-is-error": ('warns.append("at %s in %s[%d] is Z', 'errs.append("at %s in %s[%d] is Z', False, "is a warning"),
    "lint-z-silent": ('print("%s: WARN %s" % (bid, w))', "pass", False, "is a warning"),
    "lint-no-frontmatter-check": ('if k not in KNOWN_FM and k != "id":', "if False:", False, "lint catches a frontmatter key"),
    "lint-dir-ignored": ("targets += list(bean_files(instance_root(a)))", "pass", False, "lint <instance dir>: exit 1"),
    "lint-always-exit-0": ("sys.exit(1 if bad else 0)", "sys.exit(0)", False, "lint catches"),
    "lint-flags-everything": ("bad += bool(errs)", "bad += 1", False, "a clean bean exits 0"),
}


# Mutants of the REAL bext: (file, old, new, expected text in a FAIL line).
# Run with --real: bext and beans_ext.py are copied into scratch together (bext imports beans_ext from its
# own directory), one is mutated, the real files are never edited.
REAL_DIR = os.path.dirname(HERE)
REAL_MUTANTS = {
    "M9-write-ignores-stray-frontmatter": ("bext", "    if bad:\n        die(\"refusing to write", "    if False:\n        die(\"refusing to write",
                                           "frontmatter has a stray key"),
    "M9b-legacy-id-not-exempt": ("beans_ext.py", "if k not in BEANS_KEYS and k not in LEGACY_KEYS", "if k not in BEANS_KEYS",
                                 "legacy id:"),
    "M10-colon-equals-is-string": ("bext", 'if op == ":=":', "if False:", "append key:=<yaml>: parsed"),
    "M10b-equals-parses-yaml": ("bext", 'if op == ":=":', "if True:", "append key=value: stays a string"),
    "M12-nested-fences-not-skipped": ("beans_ext.py", 'if not (fence[0] == "`" and "`" in info):  # a real opening fence',
                                      "if False:", "nested fence"),
}


def make_real(root, name):
    rel, old, new, _ = REAL_MUTANTS[name]
    d = os.path.join(root, "bext-real-" + name.split("-")[0])
    os.makedirs(d)
    for f in ("bext", "beans_ext.py"):
        shutil.copy2(os.path.join(REAL_DIR, f), os.path.join(d, f))
    p = os.path.join(d, rel)
    src = open(p, encoding="utf-8").read()
    if old not in src:
        raise SystemExit("real mutant %s: pattern not found in %s (the mutant would equal the real code)" % (name, rel))
    mut = src.replace(old, new, 1)
    assert mut != src
    with open(p, "w", encoding="utf-8") as f:
        f.write(mut)
    return os.path.join(d, "bext")


def judge_real(root, name):
    rc, out = run(make_real(root, name))
    fails = [l for l in out.splitlines() if l.startswith("FAIL ")]
    hit = [l for l in fails if REAL_MUTANTS[name][3] in l]
    return name, rc != 0 and bool(hit), rc, fails, hit, out.strip().splitlines()[-1]


def main_real():
    root = scratch_root("bext-mutreal-")
    try:
        rc, out = run(os.path.join(REAL_DIR, "bext"))
        control = rc == 0 and "FAIL" not in out
        print("control (real bext unmodified): %s  [%s]" % ("PASS" if control else "FAIL", out.strip().splitlines()[-1]))
        killed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            for name, ok, rc, fails, hit, last in ex.map(lambda n: judge_real(root, n), list(REAL_MUTANTS)):
                killed += ok
                print("%-36s %s rc=%d fails=%-3d %s" % (name, "KILLED  " if ok else "SURVIVED", rc, len(fails),
                                                        ((hit or fails or [last])[0])[:120]))
        print("mutants_bext --real: %d/%d killed, control %s" % (killed, len(REAL_MUTANTS), "ok" if control else "FAILED"))
    finally:
        shutil.rmtree(root)
    sys.exit(0 if killed == len(REAL_MUTANTS) and control else 1)


def make(root, name):
    old, new, every, _ = MUTANTS[name]
    pairs = old if isinstance(old, list) else [(old, new)]
    src = mut = open(ORACLE, encoding="utf-8").read()
    for o, n in pairs:
        if o not in mut:
            raise SystemExit("mutant %s: pattern %r not found (the mutant would equal the oracle)" % (name, o[:40]))
        nxt = mut.replace(o, n) if every else mut.replace(o, n, 1)
        assert nxt != mut
        mut = nxt
    p = os.path.join(root, "bext_%s.py" % name.replace("-", "_"))
    with open(p, "w", encoding="utf-8") as f:
        f.write(mut)
    return p


def run(bext):
    env = dict(os.environ, BEXT=bext)
    p = subprocess.run([PY, os.path.join(HERE, "test_bext.py")], env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True, timeout=900)
    return p.returncode, p.stdout


def judge(root, name):
    rc, out = run(make(root, name))
    fails = [l for l in out.splitlines() if l.startswith("FAIL ")]
    hit = [l for l in fails if MUTANTS[name][3] in l]
    return name, rc != 0 and bool(hit), rc, fails, hit, out.strip().splitlines()[-1]


def main():
    if sys.argv[1:2] == ["--real"]:
        return main_real()
    names = sys.argv[1:] or list(MUTANTS)
    root = scratch_root("bext-mut-")
    try:
        rc, out = run(ORACLE)
        control = rc == 0 and "FAIL" not in out
        print("control (oracle unmodified): %s  [%s]" % ("PASS" if control else "FAIL", out.strip().splitlines()[-1]))
        killed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            for name, ok, rc, fails, hit, last in ex.map(lambda n: judge(root, n), names):
                killed += ok
                print("%-26s %s rc=%d fails=%-3d %s" % (name, "KILLED  " if ok else "SURVIVED", rc, len(fails),
                                                        ((hit or fails or [last])[0])[:140]))
        print("mutants_bext: %d/%d killed, control %s" % (killed, len(names), "ok" if control else "FAILED"))
    finally:
        shutil.rmtree(root)
    sys.exit(0 if killed == len(names) and control else 1)


if __name__ == "__main__":
    main()
