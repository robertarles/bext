"""beans-ext: parser, emitter and lint for the extension block in a bean body.

Shared by `bext` (the writer) and any tool that reads the block, so both apply the same rules.
Container rules C1-C4 and history rules H1-H2 are numbered in the messages. Python 3.9, PyYAML only.

Loading never coerces timestamps (they stay strings) and refuses duplicate keys.
"""
import re

import yaml

FENCE_OPEN = "```yaml beans-ext"
FENCE_CLOSE = "```"

# the frontmatter keys beans keeps; anything else is dropped on the next beans write (beans rewrites the file from its own fields).
# `id` is the legacy key on ~225 old beans: harmless, dropped silently, so not reported.
BEANS_KEYS = {"title", "status", "type", "priority", "tags", "created_at", "updated_at", "parent",
              "blocking", "blocked_by"}
LEGACY_KEYS = {"id"}

KEY_RX = re.compile(r"^[a-z][a-z0-9_]*$")
OFFSET_RX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?([+-]\d\d:\d\d)$")
Z_RX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?Z$")
_ANY_OPEN_RX = re.compile(r"^\s*(`{3,}|~{3,})\s*yaml\s+beans-ext\s*$")
_FENCE_RX = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


class ExtError(ValueError):
    """The block is unusable; the message says why."""


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
        try:
            dup = k in seen
        except TypeError:
            dup = False
        if dup:
            raise DuplicateKeyError("duplicate key %r at line %d" % (k, knode.start_mark.line + 1))
        try:
            seen.add(k)
        except TypeError:
            pass
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


ExtLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_ext(text):
    return yaml.load(text, Loader=ExtLoader)


# ------------------------------------------------------------------ file anatomy

def split_file(text):
    """(frontmatter_text, body); frontmatter is None when the file has none."""
    if not text.startswith("---\n"):
        return None, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def scan_body(body):
    """Find beans-ext fences in a body, outside any other fenced code block.

    Returns (blocks, faults). blocks: list of dicts {start, end, text, raw} where start/end are the
    line indexes of the fences, text the YAML between them and raw the exact fenced text (fences
    included, as it sits in the body). end is None when the fence is never closed. faults: messages
    for fence lines that look like a beans-ext fence but break C1 (indented, wrong fence).
    """
    lines = body.split("\n")
    blocks, faults = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line == FENCE_OPEN:
            for j in range(i + 1, len(lines)):
                if lines[j] == FENCE_CLOSE:
                    blocks.append({"start": i, "end": j, "text": "\n".join(lines[i + 1:j]) + "\n",
                                   "raw": "\n".join(lines[i:j + 1])})
                    i = j
                    break
            else:
                blocks.append({"start": i, "end": None, "text": None, "raw": None})
                break
        else:
            m = _FENCE_RX.match(line)
            if m and _ANY_OPEN_RX.match(line):
                faults.append("line %d: the beans-ext fence must be exactly '%s' at column 0" % (i + 1, FENCE_OPEN))
            elif m:
                fence, info = m.group(1), m.group(2)
                if not (fence[0] == "`" and "`" in info):  # a real opening fence
                    closer = re.compile(r"^ {0,3}" + re.escape(fence[0]) + "{%d,}\\s*$" % len(fence))
                    j = i + 1
                    while j < len(lines) and not closer.match(lines[j]):
                        j += 1
                    i = j
            elif _ANY_OPEN_RX.match(line):
                faults.append("line %d: the beans-ext fence must be exactly '%s' at column 0" % (i + 1, FENCE_OPEN))
        i += 1
    return blocks, faults


# ------------------------------------------------------------------ checks

def _walk_keys(node, path, out):
    if isinstance(node, dict):
        for k, v in node.items():
            p = path + [str(k)]
            if not isinstance(k, str):
                out.append(("error", "key %r at %s is not a string" % (k, ".".join(path) or "top level")))
            elif not KEY_RX.match(k):
                out.append(("error", "key '%s' is not lowercase snake_case (C3)" % ".".join(p)))
            _walk_keys(v, p, out)
    elif isinstance(node, list):
        for n, v in enumerate(node):
            _walk_keys(v, path + ["[%d]" % n], out)


def _walk_history(node, path, out):
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_history(v, path + [str(k)], out)
    elif isinstance(node, list):
        maps = [x for x in node if isinstance(x, dict)]
        if maps and any("at" in x for x in maps):
            for n, x in enumerate(node):
                where = "%s[%d]" % (".".join(path), n)
                if not isinstance(x, dict) or "at" not in x:
                    out.append(("error", "history entry %s has no 'at' (H1)" % where))
                    continue
                at = x["at"]
                if not isinstance(at, str):
                    out.append(("error", "history entry %s: 'at' is not a string (quote it, H2)" % where))
                elif OFFSET_RX.match(at):
                    pass
                elif Z_RX.match(at):
                    out.append(("warn", "history entry %s: 'at' is in Z, the local offset is lost (H2)" % where))
                else:
                    out.append(("error", "history entry %s: 'at' %r has no UTC offset or is not ISO 8601 (H2)" % (where, at)))
        for n, x in enumerate(node):
            _walk_history(x, path + ["[%d]" % n], out)


def check_data(ext):
    """Findings [(level, message)] for a parsed block: ext, snake_case keys, history stamps."""
    out = []
    if not isinstance(ext, dict):
        return [("error", "the block must be one YAML mapping (C2)")]
    if ext.get("ext") != 1 or isinstance(ext.get("ext"), bool):
        out.append(("error", "'ext: 1' is missing (C4)"))
    _walk_keys(ext, [], out)
    _walk_history(ext, [], out)
    return out


def check_block_text(text):
    """(ext or None, findings) for the YAML text between the fences."""
    for n, line in enumerate(text.split("\n"), 1):
        if re.match(r"^ *\t", line):
            return None, [("error", "tab character in indentation at block line %d; YAML needs spaces" % n)]
    try:
        ext = load_ext(text)
    except DuplicateKeyError as e:
        return None, [("error", "duplicate key: %s" % e)]
    except yaml.YAMLError as e:
        msg = str(e).replace("\n", " ")
        return None, [("error", "YAML error: %s" % msg)]
    return ext, check_data(ext)


def parse_body(body):
    """(ext, findings) for a bean body. ext is None when there is no block or it is unusable."""
    blocks, faults = scan_body(body)
    findings = [("error", f) for f in faults]
    if not blocks:
        return None, findings
    if len(blocks) > 1:
        findings.append(("error", "%d beans-ext blocks; at most one is allowed (C1)" % len(blocks)))
        return None, findings
    if blocks[0]["end"] is None:
        findings.append(("error", "line %d: the beans-ext fence is never closed" % (blocks[0]["start"] + 1)))
        return None, findings
    ext, more = check_block_text(blocks[0]["text"])
    return ext, findings + more


def check_frontmatter(fm):
    if fm is None:
        return []
    try:
        d = yaml.safe_load(fm) or {}
    except yaml.YAMLError as e:
        return [("error", "frontmatter does not parse: %s" % str(e).replace("\n", " "))]
    if not isinstance(d, dict):
        return [("error", "frontmatter is not a mapping")]
    stray = sorted(str(k) for k in d if k not in BEANS_KEYS and k not in LEGACY_KEYS)
    if stray:
        return [("error", "frontmatter key(s) %s are outside beans' set and are lost on the next beans write; "
                          "move them into the beans-ext block" % ", ".join(stray))]
    return []


def lint_text(text):
    """Findings for a whole bean file: frontmatter keys plus the block."""
    fm, body = split_file(text)
    _, findings = parse_body(body)
    return check_frontmatter(fm) + findings


def extract(text):
    """(ext, block_dict) of a bean file; ext None when there is no block. Raises ExtError on any error."""
    _, body = split_file(text)
    ext, findings = parse_body(body)
    errs = [m for lvl, m in findings if lvl == "error"]
    if errs:
        raise ExtError("; ".join(errs))
    blocks, _ = scan_body(body)
    return (ext, blocks[0]) if blocks else (None, None)


# ------------------------------------------------------------------ emitter

class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


_PLAIN_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*( [A-Za-z0-9_.-]+)*$")


def _str(dumper, s):
    if "\n" in s:
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|")
    # PyYAML also quotes a plain scalar that would resolve to another type (yes, 15, null)
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style=None if _PLAIN_OK.match(s) else '"')


_Dumper.add_representer(str, _str)


def dump_ext(ext):
    """The YAML text for a block: key order kept, timestamps quoted, multi-line strings as |."""
    return yaml.dump(ext, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1 << 30,
                     default_flow_style=False, indent=2)


def render_block(ext):
    return FENCE_OPEN + "\n" + dump_ext(ext) + FENCE_CLOSE


def fnv1a64(data):
    """FNV-1a 64-bit hex of bytes: what beans --if-match compares against."""
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h
