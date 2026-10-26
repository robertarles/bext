# bext

bext gets, sets, appends and lints a `beans-ext` YAML block inside the body of a bean, the markdown file format of the [beans](https://github.com/hmans/beans) issue tracker. It lets a bean carry arbitrary extra fields (a linked ticket, dated comments, time spent) without touching the frontmatter that beans reads and writes.

## The beans-ext block

A bean body may hold one fenced block whose info string is exactly `yaml beans-ext`, with the opening fence at column 0:

````markdown
```yaml beans-ext
ext: 1
jira:
  key: "ABC-123"
time:
  - at: "2026-11-01T01:30:00-07:00"
    by: someone
    minutes: 90
    note: "pairing session"
```
````

Rules, as `bext lint` enforces them:

- C1: at most one block per body, closed, with the fence written exactly as above.
- C2: the block is one YAML mapping.
- C3: every key is lowercase snake_case (`[a-z][a-z0-9_]*`).
- C4: `ext: 1` is present (the format version).
- No tabs in indentation and no duplicate keys. Timestamps are never coerced; they stay strings.
- H1, H2: a list of maps in which any map has an `at` is a history, and every entry needs an `at`, quoted, as an ISO 8601 stamp with a UTC offset (`2026-11-01T01:30:00-07:00`). A `Z` stamp is a warning, because the local offset is lost.

bext also refuses to write when the bean's frontmatter holds keys beans does not keep, since beans would drop them on its next write.

## Usage

Run bext inside a beans instance (beans finds `.beans.yml` by searching upward).

```
bext get <id> [<key.path>] [--json]
bext set <id> <key.path> <value> [--yaml]
bext append <id> <key.path> [key=value | key:=<yaml> ...] [--text -] [--at <stamp>] [--no-by]
bext lint [<id> | <instance dir> ...]
```

- `get` prints the whole block, or the value at a dotted key path; `--json` prints JSON.
- `set` stores a string, or a YAML value with `--yaml`. Missing maps on the path are created.
- `append` adds an entry to a list, stamped with the current local time and offset (`--at` overrides it). `by` defaults to `$BEXT_AUTHOR`, else the login name; `--no-by` omits it. `key=value` stores a string, `key:=<yaml>` a YAML value, and `--text -` reads the entry's `text` from stdin.
- `lint` checks one bean, or every bean of an instance, and exits 1 if any error is found.

Every write goes through `beans update --if-match <hash of the file bytes read>`, so a concurrent edit makes the write fail instead of overwriting it. bext re-reads and retries, three attempts in all, then fails loudly. After a write it reads the file back and checks the block holds the change. Bytes outside the block are not touched.

## Install

bext needs Python 3.9 or later, PyYAML, and the `beans` CLI on `PATH`.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bext lint
```

Or put `bext` and `beans_ext.py` together in a directory on `PATH` (the script finds `beans_ext.py` next to itself, through symlinks) and run it with a Python that has PyYAML.

## Tests

```
.venv/bin/python tests/test_bext.py        # acceptance tests; needs the beans CLI
.venv/bin/python tests/mutants_bext.py     # shows the tests fail against broken oracle copies
.venv/bin/python tests/mutants_bext.py --real   # the same against broken copies of the real bext
```

The tests build throwaway beans instances in temporary directories and put a guard `beans` first on `PATH`. Set `BEXT_REAL_BEANS` to use a particular beans binary, `BEXT` to test another bext.

## License

MIT, see `LICENSE`.
