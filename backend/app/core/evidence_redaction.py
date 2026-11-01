"""Credential-value redaction; source references and the documented operator placeholder are exempt.

Quoted expressions are conservatively refused, including prefixed strings and
call defaults. Any modified evidence must be rejected; its sanitized diagnostic
is clipped at the first sensitive RHS so no multiline secret fragments remain.
"""

import ast
import io
import keyword
import re
import tokenize
from pathlib import PurePosixPath


def evidence_name_valid(name: str) -> bool:
    """Same literal relative path policy for backend admission and /tmp staging."""
    return (isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,159}", name) is not None
            and ".." not in name
            and all(part not in ("", ".", "..") for part in name.split("/"))
            and not name.split("/")[0].startswith(("mc-", "claude-")))


def evidence_paths_conflict(files: dict[str, str]) -> bool:
    """A file cannot also be a parent directory of another admitted file."""
    names = {name for name in files if evidence_name_valid(name)}
    return any(str(parent) in names for name in names for parent in PurePosixPath(name).parents)


def python_identifiers(source: str) -> set[tuple[int, int]]:
    """Authentic identifier positions, never names inside comments or strings."""
    names: set[tuple[int, int]] = set()
    try:
        ast.parse(source)
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME:
                names.add(token.start)
    except (SyntaxError, tokenize.TokenError, ValueError):
        return set()  # incomplete lexical context cannot authorize exemptions
    return names


_SHAPED = re.compile(r"(?<![\w-])sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._~+/=-]{16,}")
_STRING = (
    r"(?:[brfu]{0,2})(?:\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|"
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)'
)
_NAMED = re.compile(
    r"(?=(?P<prefix>\b(?P<name>[\w-]+)"
    r"[`'\"]*\s*[=:]\s*)(?P<value>"
    + _STRING
    + r"|[A-Za-z_][\w.]*\([^\r\n]*\)|[^\s`'\"]+|[`'\"][^\r\n]*))",
    re.I,
)
_REFERENCE = re.compile(
    r"(?:\+\+|--)?[A-Za-z_][\w.]*"
    r"(?:[\[(].*|(?:\s*,\s*[A-Za-z_][\w.]*\s*[=:]\s*(?:[A-Za-z_][\w.]*|\d+(?:\.\d+)?))*)\s*[\]});,:]*$"
)
_NUMBER = re.compile(
    r"\d+(?:\.\d+)?(?:\s*,\s*[A-Za-z_][\w.]*\s*[=:]\s*"
    r"(?:[A-Za-z_][\w.]*|\d+(?:\.\d+)?))*\s*[\]});,]*$"
)
_CONTINUATIONS = r"and|or|not|if|else|await|yield|from|in|is|new|typeof|void|delete|function|class|async|lambda|as|instanceof|satisfies"
_COUNTERS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_tokens",
        "reasoning_tokens",
        "max_tokens_budget",
        "budget_tokens",
    }
)


def _closed_reference(rhs: str, following: str = "", *, counter: bool = False) -> bool:
    # ponytail: lexical fragments, not language ASTs; unfamiliar syntax stays blocked.
    if re.search(r"(?m)^(?:[.({\[`?+*/%&|^<>=!,#\\-]|(?:" + _CONTINUATIONS + r")\b)", following):
        return False
    if any(marker in rhs for marker in ("/", "<", ">", "\\", "`")):
        return False
    stack: list[str] = []
    outside = False
    pairs = {")": "(", "]": "[", "}": "{"}
    try:
        for token in tokenize.generate_tokens(io.StringIO(rhs).readline):
            if token.type == tokenize.NAME and (
                (keyword.iskeyword(token.string) and token.string not in ("True", "False", "None"))
                or token.string in ("function", "new", "typeof", "void", "delete")
            ):
                return False  # definitions/operators are not source references
            if not counter and (
                token.type in (tokenize.STRING, tokenize.NUMBER)
                or token.string in ("True", "False", "None", "true", "false", "null", "undefined")
            ):
                return False
            if token.type != tokenize.OP:
                continue
            if token.string in "([{":
                stack.append(token.string)
            elif token.string in pairs:
                if stack:
                    if stack.pop() != pairs[token.string]:
                        return False
                else:
                    # Parameter/property fragments can end their outer container.
                    outside = True
    except tokenize.TokenError as exc:
        if stack or not outside or exc.args[0] != "EOF in multi-line statement":
            return False
    except IndentationError:
        return False
    return (
        not stack
        and not rhs.rstrip().endswith(("\\", "+", "-", "*", "/", "|", "&", "?", "="))
        and not re.search(r"\b(?:" + _CONTINUATIONS + r")\s*$", rhs)
    )


def redact_evidence(text: str, *, code: bool = False, following: str = "", full: bool = False) -> str:
    """Default clips at the first sensitive value; full=True redacts every one in place."""
    spans: list[tuple[int, int]] = []
    for match in _NAMED.finditer(text):
        name, value = match["name"].lower(), match["value"]
        if not re.search(r"token|secret|api[_-]?key|password", name):
            continue
        end = text.find("\n", match.end("value"))
        end = len(text) if end < 0 else end
        rhs = text[match.start("value") : end]
        if name == "mc_token" and rhs.strip() == "<operator token>":
            continue  # Exact public runbook placeholder, never a credential value.
        if rhs.strip() == "[redacted]":
            continue  # already redacted in place
        next_line = following or next(
            filter(
                None,
                (
                    re.sub(r"^[\s\ufeff]+", "", line).rstrip()
                    for line in text[end + 1 :].splitlines()
                ),
            ),
            "",
        )
        literals = re.findall(_STRING, rhs, re.I)
        if (
            name in _COUNTERS
            and _NUMBER.fullmatch(value.strip("`'\""))
            and _closed_reference(rhs, next_line, counter=True)
            and (
                not re.search(r"[`'\"]", rhs)
                or (literals and all(literal.strip("'\"") in _COUNTERS for literal in literals))
            )
        ):
            continue
        if code and _REFERENCE.fullmatch(rhs):
            closed = _closed_reference(rhs, next_line, counter=name in _COUNTERS)
            metadata_keys = name in _COUNTERS and all(
                literal.strip("'\"") in _COUNTERS for literal in literals
            )
            if closed and (not re.search(r"[`'\"]", rhs) or (literals and metadata_keys)):
                continue
        if full:
            spans.append((match.start("value"), end))
            continue
        # No consumer may mistake this diagnostic for complete required evidence.
        return _SHAPED.sub("[redacted]", text[: match.start("value")] + "[redacted]")
    out, last = [], 0
    for start, end in spans:
        if start >= last:  # lookahead matches can overlap on one line
            out.append(text[last:start] + "[redacted]")
            last = end
    return _SHAPED.sub("[redacted]", "".join(out) + text[last:])


def redact_diff(
    diff: str,
    *,
    contexts: dict[tuple[str, str], set[tuple[int, int]]] | None = None,
) -> str:
    """Keep exact bytes; native QA supplies full authenticated lexical contexts."""
    code = False
    original = diff.split("\n")
    original = [line + "\n" for line in original[:-1]] + ([original[-1]] if original[-1] else [])
    following = [""] * len(original)
    next_line = {"+": "", "-": ""}
    for index in range(len(original) - 1, -1, -1):
        line = original[index]
        prefix = line[:1]
        content = re.sub(r"^[\s\ufeff]+", "", line[1:]).rstrip()
        following[index] = (
            next_line[prefix] if prefix in next_line else "\n".join(next_line.values())
        )
        if line.startswith(("diff --git ", "--- ", "+++ ")):
            next_line = {"+": "", "-": ""}
        elif prefix in ("+", "-", " ") and content:
            for version in next_line:
                if prefix in (version, " "):
                    next_line[version] = content
    lines = []
    paths = {"-": "", "+": ""}
    numbers = {"-": 0, "+": 0}
    for index, line in enumerate(original):
        if line.startswith("diff --git "):
            code = False
        if line.startswith(("--- a/", "+++ b/")):
            path = line[6:].rstrip("\r\n")
            code = path.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"))
            paths["-" if line.startswith("---") else "+"] = path
        hunk = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            numbers = {"-": int(hunk[1]), "+": int(hunk[2])}
        eligible = code
        prefix = line[:1]
        if contexts is not None:
            versions = [prefix] if prefix in ("-", "+") else ["-", "+"]
            for match in _NAMED.finditer(line):
                if not re.search(r"token|secret|api[_-]?key|password", match["name"], re.I):
                    continue
                if not all(
                    (numbers[v], match.start("name") - 1) in contexts.get((v, paths[v]), set())
                    for v in versions
                ):
                    eligible = False  # comment/string content is NOT a source reference
                    break
        lines.append(redact_evidence(line, code=eligible, following=following[index]))
        if prefix in ("+", " ") and not line.startswith("+++ "):
            numbers["+"] += 1
        if prefix in ("-", " ") and not line.startswith("--- "):
            numbers["-"] += 1
    return "".join(lines)
