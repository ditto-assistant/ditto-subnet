"""Language-aware comment and string-literal masking for untrusted source.

Every deterministic scanner in the screener reads a *masked* view of a file:
comments are blanked so prose cannot raise a lead or a malicious finding, and
string literals are blanked where an effect must be a real operation rather
than a word inside a prompt. Masking is therefore security-relevant in both
directions:

- masking too much erases real code, so a genuine finding loses its citations
  and can be demoted or cleared;
- masking too little leaves prose visible, so a comment can fabricate a
  confidence-1.0 malicious finding before the build.

A single C-style lexer applied to every file got both wrong: a Python
``'data/*.json'`` opened a block comment that blanked the rest of the file,
Python/shell/Dockerfile ``#`` comments stayed visible, and a Rust lifetime
(``<'a>``) desynchronized the string masker. Each language is now lexed by its
own rules, chosen from a small explicit table keyed by file name.

The module fails closed. A path whose language is not in the table, or a file
the lexer cannot bring to a clean end (an unterminated string or block comment,
unbalanced JavaScript nesting or JSX-like markup), is returned unchanged: more
stays visible to the scanners rather than less. Masking only ever replaces
characters with spaces and never touches a line break, so line numbers and
columns stay aligned with the raw source for every language.
"""

from __future__ import annotations

import bisect
import re
import tokenize
import warnings
from collections.abc import Callable, Iterable, Iterator
from functools import lru_cache, partial

__all__ = [
    "CHAR_LITERAL",
    "language_for_path",
    "mask_comments",
    "mask_lead_comments",
    "mask_proven_comments",
    "mask_python_code",
    "mask_rust_literals",
    "mask_string_literals",
]

# A Go rune or Rust char literal: one character or one escape (``\n``,
# ``\xHH``, ``\u{...}``, Go ``\uXXXX`` / ``\UXXXXXXXX`` / octal). In Rust,
# anything else after an apostrophe is a lifetime or loop label.
CHAR_LITERAL = re.compile(
    r"'(?:[^'\\\n]|\\(?:u\{[0-9a-fA-F]{1,6}\}|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}"
    r"|U[0-9a-fA-F]{8}|[0-7]{3}|[^\n]))'"
)

_CODE = 0
_COMMENT = 1
_STRING = 2

# Anything but a character ``str.splitlines`` treats as a line boundary. Those
# are never replaced, so the masked view always has exactly the raw line count.
_BLANKABLE = re.compile(r"[^\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
_RUNS = {
    kind: re.compile(re.escape(bytes([kind])) + b"+") for kind in (_COMMENT, _STRING)
}

_LANGUAGE_BY_SUFFIX = {
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cc": "c",
    ".cpp": "c",
    ".cxx": "c",
    ".hh": "c",
    ".hpp": "c",
    ".hxx": "c",
    # JSX text (``<p>Don't</p>``) is not lexable without a JSX parser, but a
    # JSX element can only start where an expression does, where the lexer
    # leaves any ``<`` unmasked; a JSX/TSX file without one lexes as JS/TS.
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".dart": "dart",
    ".php": "php",
    ".zig": "zig",
    ".swift": "swift",
    ".scala": "scala",
    ".sc": "scala",
    ".groovy": "groovy",
    ".gradle": "groovy",
    ".fs": "fsharp",
    ".fsi": "fsharp",
    ".fsx": "fsharp",
    # Objective-C++ lexes as C++. ``.m`` is left out: MATLAB and Octave share
    # it, and there ``'`` transposes and a backslash in a string is literal.
    ".mm": "c",
    ".py": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".pyx": "python",
    ".pxd": "python",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ksh": "shell",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".mk": "make",
    ".ini": "ini",
    ".cfg": "ini",
}
_LANGUAGE_BY_NAME = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "go.mod": "go",
    "go.work": "go",
    "makefile": "make",
    "gnumakefile": "make",
    "pipfile": "toml",
    "cargo.lock": "toml",
    "poetry.lock": "toml",
    "uv.lock": "toml",
    ".env": "dotenv",
}
# Only these languages have string literals that are inert data. In shell,
# Dockerfile, and configuration formats a quoted word is routinely the command
# itself (``sh -c '...'``, ``"$(cat ...)"``, a ``[tool.*]`` script), so
# blanking it would erase the effect a scanner is looking for. Java, Kotlin,
# C#, Dart, and PHP are masked for comments only: their strings are often the
# command too (``shell_exec('...')``, ``Runtime.exec("...")``), and the
# preflight's process-execution exception does not name those APIs.
_STRING_MASKED_LANGUAGES = frozenset(
    {"rust", "go", "c", "javascript", "typescript", "python"}
)
# The kernel executes a script's ``#!`` line (``env -S`` accepts a whole
# command), so it is never masked as a comment.
_SHEBANG_LANGUAGES = frozenset(
    {
        "python",
        "shell",
        "javascript",
        "typescript",
        "kotlin",
        "csharp",
        "dart",
        "php",
        "swift",
        "scala",
        "groovy",
        "fsharp",
    }
)
# Lexers that read left to right and look ahead no further than the end of
# the current line, so any prefix that lexes on its own is masked as the whole
# file is: the decisive preflight keeps their comments masked where the whole
# file cannot be lexed. The Python fallback is approximate, and the
# hash-comment languages were never masked before, so they are not listed.
_PREFIX_EXACT_LANGUAGES = frozenset(
    {
        "rust",
        "go",
        "c",
        "javascript",
        "typescript",
        "java",
        "kotlin",
        "csharp",
        "dart",
        "php",
        "zig",
        "swift",
        "scala",
        "groovy",
        "fsharp",
    }
)
_MAX_PROOF_CUTS = 16
# Line boundaries as ``str.splitlines`` finds them, so ``lines`` numbering
# matches every scanner's.
_SPLITLINES_BREAK = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")


def language_for_path(path: str) -> str | None:
    """Return the masking language for ``path``, or ``None`` when unknown."""
    name = path.rsplit("/", 1)[-1].casefold()
    language = _LANGUAGE_BY_NAME.get(name)
    if language is not None:
        return language
    if name.startswith(("dockerfile.", "containerfile.")) or name.endswith(
        (".dockerfile", ".containerfile")
    ):
        return "dockerfile"
    if name.startswith(("requirements", "constraints")) and name.endswith(".txt"):
        return "requirements"
    if name.startswith(".env."):
        return "dotenv"
    dot = name.rfind(".")
    return _LANGUAGE_BY_SUFFIX.get(name[dot:]) if dot > 0 else None


def mask_comments(text: str, path: str) -> str:
    """Blank comments in ``text`` by the rules of ``path``'s language.

    Strings, layout, and line count are preserved. Unknown languages and
    sources that do not lex cleanly are returned unchanged.
    """
    kinds = _kinds_for(text, path)
    return text if kinds is None else _blank(text, kinds, _COMMENT)


def mask_lead_comments(text: str, path: str) -> str:
    """Blank comments for automatic role matching: leads, guards, fingerprints.

    Where a lexer masks the file this is ``mask_comments``. For a language
    without one (Ruby, Lua, Haskell, ...), or a source its lexer leaves
    unmasked, whole comment lines are blanked by the language's markers: a
    line whose first text opens a line comment, and a block comment that
    opens a line, through its first closer. That can also blank a line of a
    multi-line string, or code after a block comment closes on a later line,
    so this view is only for rules whose miss costs attention: a role that
    fires on prose raises an unsupported lead, while a missed lead leaves the
    source fully read by the reviewer. The decisive preflight, static proofs,
    and citation admissibility read ``mask_comments``, which leaves such a
    file whole.
    """
    kinds = _kinds_for(text, path)
    if kinds is not None:
        return _blank(text, kinds, _COMMENT)
    family = _comment_family(path)
    if family is None or not text:
        return text
    return _blank_comment_lines(text, family)


def mask_proven_comments(text: str, path: str, lines: Iterable[int]) -> str:
    """Blank the comments an exact lexer can prove, even in a file it cannot end.

    For a file its lexer masks this is ``mask_comments``. Where the lexer
    gives up (a Swift ``/`` that may open a regular expression, a Scala XML
    literal, ...), comments are still blanked in the longest prefix that
    lexes on its own, cut after a line feed that follows one of ``lines``
    (1-based). These lexers read left to right and look ahead no further than
    the end of the current line, so such a prefix ends outside every literal
    and comment and is masked exactly as the whole file would be; the rest is
    left as it is. A language without such a lexer is returned unchanged.
    """
    language = language_for_path(path)
    if language is None or not text:
        return text
    kinds = _cached_kinds(text, language)
    if kinds is not None:
        return _blank(text, kinds, _COMMENT)
    if language not in _PREFIX_EXACT_LANGUAGES:
        return text
    starts = [0]
    for match in _SPLITLINES_BREAK.finditer(text):
        starts.append(match.end())
    for line in sorted(set(lines), reverse=True)[:_MAX_PROOF_CUTS]:
        if not 1 <= line < len(starts):
            continue
        cut = text.find("\n", starts[line] - 1)
        if cut < 0:
            continue
        prefix = text[: cut + 1]
        prefix_kinds = _cached_kinds(prefix, language)
        if prefix_kinds is not None:
            return _blank(prefix, prefix_kinds, _COMMENT) + text[cut + 1 :]
    return text


def mask_string_literals(text: str, path: str) -> str:
    """Blank string-literal text in ``text`` by the rules of ``path``'s language.

    Callers pass the comment-masked view. Only languages whose literals are
    inert data are masked; interpolated code (``f"{call()}"``, JavaScript
    ``${call()}``) stays visible. Everything else is returned unchanged.
    """
    if language_for_path(path) not in _STRING_MASKED_LANGUAGES:
        return text
    kinds = _kinds_for(text, path)
    return text if kinds is None else _blank(text, kinds, _STRING)


def mask_effect_literals(text: str, path: str) -> str:
    """Exact code view for operational roles, retaining interpolation code.

    Unlike the general scanner view, this also masks literals in languages
    whose strings can become process payloads. The decisive scanner restores
    those payloads only at an observed command sink. Unknown or undecidable
    syntax is left whole; this never guesses where a literal ends.
    """
    if language_for_path(path) not in {
        *_STRING_MASKED_LANGUAGES,
        "java",
        "kotlin",
        "csharp",
        "dart",
        "php",
        "zig",
        "swift",
        "scala",
        "groovy",
        "fsharp",
    }:
        return text
    kinds = _kinds_for(text, path)
    return text if kinds is None else _blank(text, kinds, _STRING)


def mask_python_code(text: str) -> str | None:
    """Blank Python comments and literals, or ``None`` if it does not tokenize.

    For callers that skip sources they cannot tokenize rather than falling
    back to the approximate lexer.
    """
    kinds = _cached_python_tokens(text)
    if kinds is None:
        return None
    return _blank(_blank(text, kinds, _COMMENT), kinds, _STRING)


def mask_rust_literals(text: str) -> str | None:
    """Blank Rust string and char literals, or ``None`` if the source does not lex.

    For callers that must not guess item boundaries in source they cannot
    lex (``rust_test_items``), rather than seeing it unchanged.
    """
    if not text:
        return text
    kinds = _cached_kinds(text, "rust")
    return None if kinds is None else _blank(text, kinds, _STRING)


def _kinds_for(text: str, path: str) -> bytes | None:
    language = language_for_path(path)
    if language is None or not text:
        return None
    return _cached_kinds(text, language)


def _blank(text: str, kinds: bytes, kind: int) -> str:
    pieces: list[str] = []
    position = 0
    for run in _RUNS[kind].finditer(kinds):
        start, end = run.span()
        pieces.append(text[position:start])
        pieces.append(_BLANKABLE.sub(" ", text[start:end]))
        position = end
    if not pieces:
        return text
    pieces.append(text[position:])
    return "".join(pieces)


@lru_cache(maxsize=8)
def _cached_kinds(text: str, language: str) -> bytes | None:
    kinds = _LEXERS[language](text)
    if kinds is None:
        return None
    if language in _SHEBANG_LANGUAGES:
        _keep_shebang(text, kinds)
    return bytes(kinds)


@lru_cache(maxsize=4)
def _cached_python_tokens(text: str) -> bytes | None:
    kinds = _python_tokens(text)
    if kinds is None:
        return None
    _keep_shebang(text, kinds)
    return bytes(kinds)


def _keep_shebang(text: str, kinds: bytearray) -> None:
    if text.startswith("#!"):
        _mark(kinds, 0, _line_end(text, 0, _CR_LF), _CODE)


def _mark(kinds: bytearray, start: int, end: int, kind: int) -> None:
    end = min(end, len(kinds))
    if end > start:
        kinds[start:end] = bytes([kind]) * (end - start)


def _line_end(text: str, start: int, breaks: re.Pattern[str]) -> int:
    match = breaks.search(text, start)
    return len(text) if match is None else match.start()


_LF = re.compile(r"\n")
_CR_LF = re.compile(r"[\n\r]")
_JS_LINE_BREAK = re.compile(r"[\n\r\u2028\u2029]")


def _quoted(quote: str, *, breaks: str = "", escapes: bool = True) -> re.Pattern:
    """Match a literal body and its closing ``quote``, starting after the opener."""
    other = re.escape(quote + ("\\" if escapes else "") + breaks)
    if not escapes:
        return re.compile(rf"[^{other}]*{re.escape(quote)}")
    return re.compile(rf"[^{other}]*(?:\\[\s\S][^{other}]*)*{re.escape(quote)}")


_DOUBLE_MULTILINE = _quoted('"')
_DOUBLE_LF = _quoted('"', breaks="\n")
_SINGLE_LF = _quoted("'", breaks="\n")
_DOUBLE_CRLF = _quoted('"', breaks="\n\r")
_SINGLE_CRLF = _quoted("'", breaks="\n\r")
_SINGLE_LF_RAW = _quoted("'", breaks="\n", escapes=False)


# --- C family -----------------------------------------------------------------

_BLOCK_EDGE = re.compile(r"/\*|\*/")
_RUST_TOKEN = re.compile(r"//|/\*|\"|'|(?<!\w)(?:br|cr|r)(#*)\"")


def _nested_block_end(text: str, start: int) -> int | None:
    depth = 1
    for edge in _BLOCK_EDGE.finditer(text, start):
        depth += 1 if edge.group() == "/*" else -1
        if not depth:
            return edge.end()
    return None


def _lex_rust(text: str) -> bytearray | None:
    """Rust: nested block comments, raw strings, char literals vs lifetimes."""
    kinds = bytearray(len(text))
    index = 0
    while (token := _RUST_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        if value == "//":
            end = _line_end(text, start, _LF)
            _mark(kinds, start, end, _COMMENT)
        elif value == "/*":
            block_end = _nested_block_end(text, start + 2)
            if block_end is None:
                return None
            end = block_end
            _mark(kinds, start, end, _COMMENT)
        elif value == '"':
            body = _DOUBLE_MULTILINE.match(text, start + 1)
            if body is None:
                return None
            end = body.end()
            _mark(kinds, start, end, _STRING)
        elif value == "'":
            # ``'x'``, ``'\n'``, ``'\u{1F600}'`` are literals; ``'a`` in
            # ``<'a>``/``&'a str`` and ``'outer:`` labels are code.
            literal = CHAR_LITERAL.match(text, start)
            if literal is None:
                index = start + 1
                continue
            end = literal.end()
            _mark(kinds, start, end, _STRING)
        else:
            terminator = '"' + token.group(1)
            close = text.find(terminator, token.end())
            if close < 0:
                return None
            end = close + len(terminator)
            _mark(kinds, start, end, _STRING)
        index = end
    return kinds


_GO_TOKEN = re.compile(r"//|/\*|[\"'`]")


def _lex_go(text: str) -> bytearray | None:
    """Go: flat block comments, raw backtick strings, rune literals."""
    kinds = bytearray(len(text))
    index = 0
    while (token := _GO_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        if value == "//":
            end = _line_end(text, start, _LF)
            _mark(kinds, start, end, _COMMENT)
        elif value == "/*":
            close = text.find("*/", start + 2)
            if close < 0:
                return None
            end = close + 2
            _mark(kinds, start, end, _COMMENT)
        elif value == "`":
            close = text.find("`", start + 1)
            if close < 0:
                return None
            end = close + 1
            _mark(kinds, start, end, _STRING)
        else:
            literal = (
                _DOUBLE_LF.match(text, start + 1)
                if value == '"'
                else CHAR_LITERAL.match(text, start)
            )
            if literal is None:
                return None
            end = literal.end()
            _mark(kinds, start, end, _STRING)
        index = end
    return kinds


_C_SPLICE = re.compile(r"\\(?:\r\n|\n|\r)")
# A trigraph (``??/`` is a backslash, ``??'`` a caret) moves where literals and
# splices end for a compiler that honors them, as ``gcc -std=c11`` does.
_C_TRIGRAPH = re.compile(r"\?\?[=/'()!<>-]")
# A header name is one token: ``#include <x/*y.h>`` opens no comment, and a
# comment may sit between ``#`` and ``include``.
_C_UNSAFE_HEADER_NAME = re.compile(
    r"(?:include(?:_next)?|import|__has_include(?:_next)?)[^\n<]*<[^>\n]*(?:/[/*]|['\"\\])"
)
# A pp-number (``1'000'000``, ``0x1'F``, ``1e+5``) is consumed whole, so a C++14
# digit separator is never mistaken for a character literal.
_C_TOKEN = re.compile(
    r"//|/\*|(?<![\w.])\.?\d(?:[eEpP][+-]|'\w|[\w.])*"
    r"|(?<!\w)(?:u8|[uUL])?R\"|\"|'"
)
_CPP_RAW_DELIMITER = re.compile(r"([^\s()\\]{0,16})\(")


def _lex_c(text: str) -> bytearray | None:
    """C/C++: lexed after line splicing, so ``*\\<newline>/`` closes a comment.

    A trigraph, or a header name holding comment or quote characters, leaves
    the file unmasked.
    """
    if _C_TRIGRAPH.search(text):
        return None
    if "\\" not in text or _C_SPLICE.search(text) is None:
        return _lex_c_logical(text)
    pieces: list[str] = []
    logical_starts: list[int] = []
    original_starts: list[int] = []
    logical_position = 0
    original_position = 0
    for splice in _C_SPLICE.finditer(text):
        piece = text[original_position : splice.start()]
        pieces.append(piece)
        logical_starts.append(logical_position)
        original_starts.append(original_position)
        logical_position += len(piece)
        original_position = splice.end()
    pieces.append(text[original_position:])
    logical_starts.append(logical_position)
    original_starts.append(original_position)
    logical_kinds = _lex_c_logical("".join(pieces))
    if logical_kinds is None:
        return None

    def original(logical_index: int) -> int:
        piece = bisect.bisect_right(logical_starts, logical_index) - 1
        return original_starts[piece] + logical_index - logical_starts[piece]

    kinds = bytearray(len(text))
    for kind in (_COMMENT, _STRING):
        for run in _RUNS[kind].finditer(logical_kinds):
            start, end = run.span()
            _mark(kinds, original(start), original(end - 1) + 1, kind)
    return kinds


def _lex_c_logical(text: str) -> bytearray | None:
    if _C_UNSAFE_HEADER_NAME.search(text):
        return None
    kinds = bytearray(len(text))
    index = 0
    while (token := _C_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        if value == "//":
            end = _line_end(text, start, _CR_LF)
            _mark(kinds, start, end, _COMMENT)
        elif value == "/*":
            close = text.find("*/", start + 2)
            if close < 0:
                return None
            end = close + 2
            _mark(kinds, start, end, _COMMENT)
        elif value.endswith('R"'):
            delimiter = _CPP_RAW_DELIMITER.match(text, token.end())
            if delimiter is None:
                # Not a raw string: an identifier ending in R, then a string.
                index = token.end() - 1
                continue
            terminator = ")" + delimiter.group(1) + '"'
            close = text.find(terminator, delimiter.end())
            if close < 0:
                return None
            end = close + len(terminator)
            _mark(kinds, start, end, _STRING)
        elif value[0] not in "\"'":
            end = token.end()  # a pp-number is code, skipped whole
        else:
            body = (_DOUBLE_CRLF if value == '"' else _SINGLE_CRLF).match(
                text, start + 1
            )
            if body is None:
                return None
            end = body.end()
            _mark(kinds, start, end, _STRING)
        index = end
    return kinds


# --- JavaScript / TypeScript --------------------------------------------------

_JS_TOKEN = re.compile(r"//|/\*|<!--|-->|[/'\"`(){}<]")
_JS_TEMPLATE_STOP = re.compile(r"\\[\s\S]|`|\$\{")
_JS_REGEX_BODY = re.compile(
    r"(?:[^\\/\[\n\r\u2028\u2029]|\\[^\n\r\u2028\u2029]"
    r"|\[(?:[^\]\\\n\r\u2028\u2029]|\\[^\n\r\u2028\u2029])*\])+/[\w$]*"
)
# Longer than any keyword the regex rule looks for.
_JS_MAX_WORD = 16
# Reserved words that are always followed by an expression.
_JS_EXPRESSION_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "default",
        "extends",
    }
)
# Keywords in some contexts and plain identifiers in others (``let of = 4``,
# ``var yield``, ``var await`` in a script), so a slash after one either
# divides or opens a regular expression.
_JS_CONTEXTUAL_KEYWORDS = frozenset({"of", "yield", "await"})
_JS_CONDITION_KEYWORDS = frozenset({"if", "while", "for", "with"})
_JS_KEYWORDS = (
    _JS_EXPRESSION_KEYWORDS | _JS_CONTEXTUAL_KEYWORDS | _JS_CONDITION_KEYWORDS
)


def _lex_javascript(text: str, *, typescript: bool = False) -> bytearray | None:
    """JavaScript/TypeScript: strings, nested templates, regex literals.

    A ``/`` begins a regular expression only where an expression may start;
    the usual previous-token rule decides it, including ``)`` that closes an
    ``if``/``while``/``for`` condition. Where that rule cannot decide (after
    a ``}`` that may end a block or an object literal, a contextual keyword,
    ``++``/``--``, a TypeScript ``!`` or ``>``), a guess would let a crafted
    line fold real code into a regular expression, string, or comment, so the
    file is left unmasked. So is any construct that cannot be closed on a
    valid program (a string or regular expression reaching a line end,
    unbalanced brackets, a ``<`` where an expression starts, which can only
    open JSX or a TypeScript assertion) and any HTML-like comment (``<!--``,
    ``-->``), which a script reads as a comment and a module as operators.
    """
    kinds = bytearray(len(text))
    # Frames: "(" / "(if" for parentheses, "(?" after ``await`` (a call or a
    # ``for await`` head), "{" for braces, "${" for template substitutions
    # whose closing brace resumes the template literal.
    frames: list[str] = []
    # True where a regular expression may start, False where a slash divides,
    # None where the preceding code does not decide.
    expression_start: bool | None = True
    previous_word = ""
    # Whether the code before ``index`` (comments aside) ends in a member-access
    # dot, so a following keyword is a property name; None if it may be a
    # number's decimal point instead.
    after_dot: bool | None = False
    index = 0
    if text.startswith("#!"):
        # An interpreter line, not JavaScript; see ``_SHEBANG_LANGUAGES``.
        index = _line_end(text, 0, _JS_LINE_BREAK)
    while True:
        token = _JS_TOKEN.search(text, index)
        if token is None:
            return None if frames else kinds
        start, value = token.start(), token.group()
        code = text[index:start].rstrip()
        if code:
            expression_start, previous_word = _js_state_after(
                code, after_dot, expression_start, typescript
            )
            after_dot = _js_member_dot(code)
        end = start + 1
        if value == "//":
            end = _line_end(text, start, _JS_LINE_BREAK)
            _mark(kinds, start, end, _COMMENT)
            index = end
            continue
        if value == "/*":
            close = text.find("*/", start + 2)
            if close < 0:
                return None
            end = close + 2
            _mark(kinds, start, end, _COMMENT)
            index = end
            continue
        if value in {"<!--", "-->"}:
            return None
        after_dot = False
        word, previous_word = previous_word, ""
        if value == "/":
            if expression_start is None:
                return None
            if expression_start:
                # Where an expression starts, ``/`` can only open a regular
                # expression. One that does not close on its line means the
                # token rule misjudged the code (and retrying per slash would
                # be quadratic), so the file is left unmasked.
                regex = _JS_REGEX_BODY.match(text, start + 1)
                if regex is None:
                    return None
                end = regex.end()
                expression_start = False
            else:
                expression_start = True
        elif value in {"'", '"'}:
            body = (_SINGLE_CRLF if value == "'" else _DOUBLE_CRLF).match(
                text, start + 1
            )
            if body is None:
                return None
            end = body.end()
            _mark(kinds, start, end, _STRING)
            expression_start = False
        elif value == "`" or (value == "}" and frames and frames[-1] == "${"):
            if value == "}":
                frames.pop()
                literal_start = start + 1
            else:
                literal_start = start
            stop = _js_template_stop(text, start + 1)
            if stop is None:
                return None
            if text.startswith("`", stop):
                end = stop + 1
                _mark(kinds, literal_start, end, _STRING)
                expression_start = False
            else:
                end = stop + 2
                _mark(kinds, literal_start, stop, _STRING)
                frames.append("${")
                expression_start = True
        elif value == "(":
            if word in _JS_CONDITION_KEYWORDS:
                frames.append("(if")
            else:
                frames.append("(?" if word == "await" else "(")
            expression_start = True
        elif value == ")":
            if not frames or not frames[-1].startswith("("):
                return None
            closed = frames.pop()
            expression_start = {"(if": True, "(?": None}.get(closed, False)
        elif value == "{":
            frames.append("{")
            expression_start = True
        elif value == "}":
            if not frames or frames.pop() != "{":
                return None
            # A block ends before a statement, where a slash opens a regular
            # expression; an object literal or function expression ends an
            # operand, where it divides.
            expression_start = None
        elif value == "<":
            if expression_start is not False:
                # Only JSX (or a TypeScript assertion or generic) puts ``<``
                # where an expression starts, and JSX text is not code:
                # Babel takes ``< div>``, ``</* c */div>`` and ``<é>`` as
                # tags, so no test of the next character can rule JSX out.
                return None
            expression_start = True
        index = end


def _js_template_stop(text: str, start: int) -> int | None:
    index = start
    while (stop := _JS_TEMPLATE_STOP.search(text, index)) is not None:
        if stop.group().startswith("\\"):
            index = stop.end()
            continue
        return stop.start()
    return None


def _js_state_after(
    code: str, after_dot: bool | None, previous: bool | None, typescript: bool
) -> tuple[bool | None, str]:
    """Return (a regex may start next, trailing identifier) after ``code``.

    ``after_dot`` and ``previous`` describe what preceded ``code``: whether it
    ended in a member dot, and whether an expression could start there.
    """
    last = code[-1]
    if not _js_word_char(last):
        if last == "]":
            return False, ""
        if last in "+-":
            # ``+``/``-`` expect an operand. ``++``/``--`` end one as postfix
            # operators but start one as prefix operators after a line break.
            run = len(code) - len(code.rstrip(last))
            return (True if run == 1 else None), ""
        if last == ".":
            # ``1.`` is a numeric literal: a following ``/`` divides. ``...``
            # spreads an expression, so a regex may follow. ``obj.`` needs a
            # property name, so ``obj./x/`` is not valid and stays undecidable.
            return {None: False, False: True}.get(_js_member_dot(code)), ""
        if typescript and last == ">" and not code.endswith("=>"):
            # ``f<T> / x`` divides an instantiation expression.
            return None, ""
        if typescript and last == "!":
            return _ts_state_after_bang(code, after_dot, previous), ""
        return True, ""
    start = len(code) - 1
    while start > 0 and _js_word_char(code[start - 1]):
        start -= 1
        if len(code) - start > _JS_MAX_WORD:
            return False, ""
    name = code[start:]
    if name not in _JS_KEYWORDS:
        return False, name
    if start and code[start - 1] == "#":
        # A private name (``this.#return``) is an operand.
        return False, ""
    before = code[:start]
    member = _js_member_dot(before) if before.strip() else after_dot
    if member is None:
        return None, ""
    if member:
        # A property named like a keyword (``obj.return``) is an operand.
        return False, ""
    if name in _JS_CONTEXTUAL_KEYWORDS:
        return None, name
    return name in _JS_EXPRESSION_KEYWORDS, name


def _ts_state_after_bang(
    code: str, after_dot: bool | None, previous: bool | None
) -> bool | None:
    """TypeScript ``!`` is a prefix (``!/x/``) or a non-null postfix (``x!``)."""
    before = code.rstrip("!").rstrip()
    if not before:
        return True if previous is True else None
    if not (_js_word_char(before[-1]) or before[-1] == "]"):
        return True
    state, _ = _js_state_after(before, after_dot, previous, True)
    return True if state is True else None


def _js_member_dot(code: str) -> bool | None:
    """Whether ``code`` ends in a member-access dot (None: maybe a number's)."""
    code = code.rstrip()
    if not code.endswith(".") or code.endswith("..."):
        return False
    return None if len(code) > 1 and code[-2].isdigit() else True


def _js_word_char(char: str) -> bool:
    return char.isalnum() or char in "_$"


# --- Java, Kotlin, C#, Dart, PHP -----------------------------------------------
#
# Each lexer bounds exactly the literal forms its compiler accepts and returns
# None for anything else: a form it does not bound (a C# raw string, a PHP
# here-document), a construct that moves where a comment ends (a Java Unicode
# escape, PHP ``?>``), or source the compiler rejects. A line comment ends at
# the first line break any reading of the file could give it, so it never runs
# over a line the compiler starts afresh.


def _shebang_end(text: str) -> int:
    """Offset after a leading ``#!`` interpreter line, else 0."""
    return _line_end(text, 0, _CR_LF) if text.startswith("#!") else 0


def _block_comment_end(text: str, start: int, *, nested: bool) -> int | None:
    """End of the block comment whose ``/*`` ends at ``start``, or None."""
    if nested:
        return _nested_block_end(text, start)
    close = text.find("*/", start)
    return None if close < 0 else close + 2


class _Template:
    """A string form whose ``${...}`` fields are code (Kotlin, Dart).

    ``stop`` finds the next escape, field, closing quote, or (for a one-line
    form) line break. ``run`` closes on a run of three or more quotes, the
    extras being content, as a Kotlin raw string does.
    """

    __slots__ = ("quote", "run", "stop")

    def __init__(
        self, quote: str, *, escapes: bool, multiline: bool, run: bool = False
    ) -> None:
        stop = [r"\\[\s\S]" if multiline else r"\\[^\r\n]"] if escapes else []
        stop += [r"\$\{", re.escape(quote)]
        if not multiline:
            stop.append(r"[\r\n]")
        self.stop = re.compile("|".join(stop))
        self.quote = quote
        self.run = run


def _template_rest(
    text: str, kinds: bytearray, mark_from: int, scan_from: int, form: _Template
) -> tuple[int, bool] | None:
    """Mark string text up to its close or its next field.

    Returns (resume offset, whether a ``${`` field opened), or None when the
    string meets a line end it may not span, or the end of the file.
    """
    index = scan_from
    while (stop := form.stop.search(text, index)) is not None:
        value = stop.group()
        if value.startswith("\\"):
            index = stop.end()
            continue
        if value == "${":
            _mark(kinds, mark_from, stop.start(), _STRING)
            return stop.end(), True
        if value != form.quote:
            return None
        end = stop.end()
        if form.run:
            while text.startswith(form.quote[0], end):
                end += 1
        _mark(kinds, mark_from, end, _STRING)
        return end, False
    return None


# Marks the literal opened by ``value`` at ``start`` and returns its end.
_LiteralLexer = Callable[[str, bytearray, int, str], int | None]


def _lex_templated(
    text: str,
    tokens: re.Pattern[str],
    templates: dict[str, _Template],
    literal: _LiteralLexer,
    *,
    nested: bool,
) -> bytearray | None:
    """Comments, strings, and ``${...}`` fields for Kotlin and Dart.

    ``tokens`` finds ``//``, ``/*``, ``{``, ``}``, and every literal opener.
    An opener in ``templates`` starts a string with code fields; ``literal``
    bounds any other. A field's closing ``}`` resumes the string it
    interrupted, so a quote or comment inside a field is lexed as code.
    """
    kinds = bytearray(len(text))
    # For each open field: the string it resumes, and the braces opened in it.
    forms: list[_Template] = []
    depths: list[int] = []
    index = _shebang_end(text)
    while True:
        token = tokens.search(text, index)
        if token is None:
            return None if forms else kinds
        start, value = token.start(), token.group()
        end: int | None = token.end()
        rest: tuple[int, bool] | None = None
        if value == "//":
            end = _line_end(text, start, _CR_LF)
            _mark(kinds, start, end, _COMMENT)
        elif value == "/*":
            end = _block_comment_end(text, start + 2, nested=nested)
            if end is None:
                return None
            _mark(kinds, start, end, _COMMENT)
        elif value == "{":
            if depths:
                depths[-1] += 1
        elif value == "}":
            if depths and depths[-1]:
                depths[-1] -= 1
            elif depths:
                depths.pop()
                form = forms.pop()
                rest = _template_rest(text, kinds, start + 1, start + 1, form)
                if rest is None:
                    return None
        elif value in templates:
            form = templates[value]
            rest = _template_rest(text, kinds, start, token.end(), form)
            if rest is None:
                return None
        else:
            end = literal(text, kinds, start, value)
            if end is None:
                return None
        if rest is not None:
            end, opened = rest
            if opened:
                forms.append(form)
                depths.append(0)
        assert end is not None
        index = end


# A backslash starts a Unicode escape, which Java translates before it lexes
# anything (``\u000a`` ends a ``//`` comment), when an even number of
# backslashes precedes it (JLS 3.3).
_JAVA_UNICODE_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\u")
_JAVA_TOKEN = re.compile(r"//|/\*|\"\"\"|[\"']")
# ``\{`` opens an embedded expression in a (preview) string template.
_JAVA_STRING = re.compile(r"\"(?:[^\"\\\r\n]|\\[^\r\n{])*\"")
_JAVA_CHAR = re.compile(r"'(?:[^'\\\r\n]|\\(?:[btnfrs\"'\\]|[0-7]{1,3}))'")
_JAVA_TEXT_BLOCK_OPEN = re.compile(r"[ \t\f]*(?:\r\n|\r|\n)")
_JAVA_TEXT_BLOCK_STOP = re.compile(r"\\[\s\S]|\"\"\"")


def _lex_java(text: str) -> bytearray | None:
    """Java: strings, text blocks, char literals, flat block comments.

    A file with a Unicode escape is left unmasked: Java translates escapes
    first, so one can end a comment or a literal early.
    """
    if _JAVA_UNICODE_ESCAPE.search(text):
        return None
    kinds = bytearray(len(text))
    index = 0
    while (token := _JAVA_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        end: int | None
        if value == "//":
            end = _line_end(text, start, _CR_LF)
            _mark(kinds, start, end, _COMMENT)
        elif value == "/*":
            end = _block_comment_end(text, start + 2, nested=False)
            if end is None:
                return None
            _mark(kinds, start, end, _COMMENT)
        else:
            if value == '"""':
                end = _java_text_block_end(text, token.end())
            else:
                pattern = _JAVA_STRING if value == '"' else _JAVA_CHAR
                literal = pattern.match(text, start)
                end = None if literal is None else literal.end()
            if end is None:
                return None
            _mark(kinds, start, end, _STRING)
        index = end
    return kinds


def _java_text_block_end(text: str, start: int) -> int | None:
    # The opening delimiter must end its line (JLS 3.10.6).
    if _JAVA_TEXT_BLOCK_OPEN.match(text, start) is None:
        return None
    index = start
    while (stop := _JAVA_TEXT_BLOCK_STOP.search(text, index)) is not None:
        if stop.group() == '"""':
            return stop.end()
        if stop.group() == "\\{":
            return None
        index = stop.end()
    return None


_KOTLIN_TOKEN = re.compile(r"//|/\*|\"\"\"|[\"'`{}]")
_KOTLIN_TEMPLATES = {
    '"': _Template('"', escapes=True, multiline=False),
    '"""': _Template('"""', escapes=False, multiline=True, run=True),
}
_KOTLIN_CHAR = re.compile(r"'(?:[^'\\\r\n]|\\(?:[tbnr'\"\\$]|u[0-9A-Fa-f]{4}))'")
_KOTLIN_BACKTICK = re.compile(r"`[^`\r\n]+`")


def _kotlin_literal(text: str, kinds: bytearray, start: int, value: str) -> int | None:
    if value == "`":
        # A backtick name is code, but may hold ``//`` or a quote.
        name = _KOTLIN_BACKTICK.match(text, start)
        return None if name is None else name.end()
    char = _KOTLIN_CHAR.match(text, start)
    if char is None:
        return None
    _mark(kinds, start, char.end(), _STRING)
    return char.end()


def _lex_kotlin(text: str) -> bytearray | None:
    """Kotlin: nested block comments, raw strings, ``${...}`` templates."""
    return _lex_templated(
        text, _KOTLIN_TOKEN, _KOTLIN_TEMPLATES, _kotlin_literal, nested=True
    )


_DART_TOKEN = re.compile(r"//|/\*|(?<![\w$])r(?:'''|\"\"\"|['\"])|'''|\"\"\"|['\"{}]")
_DART_TEMPLATES = {
    quote: _Template(quote, escapes=True, multiline=len(quote) == 3)
    for quote in ("'", '"', "'''", '"""')
}
_DART_RAW = {
    "r'": re.compile(r"r'[^'\r\n]*'"),
    'r"': re.compile(r"r\"[^\"\r\n]*\""),
    "r'''": re.compile(r"r'''[\s\S]*?'''"),
    'r"""': re.compile(r"r\"\"\"[\s\S]*?\"\"\""),
}


def _dart_literal(text: str, kinds: bytearray, start: int, value: str) -> int | None:
    raw = _DART_RAW[value].match(text, start)
    if raw is None:
        return None
    _mark(kinds, start, raw.end(), _STRING)
    return raw.end()


def _lex_dart(text: str) -> bytearray | None:
    """Dart: nested block comments, raw and multi-line strings, templates."""
    return _lex_templated(
        text, _DART_TOKEN, _DART_TEMPLATES, _dart_literal, nested=True
    )


# C# line breaks, which end a ``//`` comment and may not sit in a string.
_CS_BREAKS = "\r\n\x85\u2028\u2029"
_CS_BREAK = re.compile(f"[{_CS_BREAKS}]")
# Raw strings, and conditional sections (whose skipped lines the compiler does
# not lex, so a ``/*`` there opens nothing), are not bounded here.
_CS_UNSUPPORTED = re.compile(r"\"\"\"|#[ \t]*(?:if|elif|else|endif)\b")
_CS_TOKEN = re.compile(r"//|/\*|\$@\"|@\$\"|\$\"|@\"|[\"'#]")
# These directives take the rest of their line as a message, not code.
_CS_MESSAGE_DIRECTIVE = re.compile(r"#[ \t]*(?:region|endregion|error|warning)\b")
_CS_STRING = re.compile(rf"\"(?:[^\"\\{_CS_BREAKS}]|\\[^{_CS_BREAKS}])*\"")
_CS_VERBATIM = re.compile(r"@\"(?:[^\"]|\"\")*\"")
_CS_CHAR = re.compile(
    rf"'(?:[^'\\{_CS_BREAKS}]|\\(?:x[0-9A-Fa-f]{{1,4}}|u[0-9A-Fa-f]{{4}}"
    rf"|U[0-9A-Fa-f]{{8}}|['\"\\0abefnrtv]))'"
)
_CS_INTERPOLATED_STOP = re.compile(
    rf"\\[^{_CS_BREAKS}]|\{{\{{|\}}\}}|[{{}}\"{_CS_BREAKS}]"
)
_CS_VERBATIM_INTERPOLATED_STOP = re.compile(r"\"\"|\{\{|\}\}|[{}\"]")
# An interpolation field is bounded only when it holds no literal, comment,
# brace, or line break: names, operators, and a format such as ``:N2``.
_CS_FIELD = re.compile(rf"[^\"'@$/\\{{}}{_CS_BREAKS}]*\}}")


def _lex_csharp(text: str) -> bytearray | None:
    """C#: regular, verbatim, and interpolated strings; char literals."""
    if _CS_UNSUPPORTED.search(text):
        return None
    kinds = bytearray(len(text))
    index = _shebang_end(text)
    while (token := _CS_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        end: int | None
        if value == "//":
            end = _line_end(text, start, _CS_BREAK)
            _mark(kinds, start, end, _COMMENT)
        elif value == "/*":
            end = _block_comment_end(text, start + 2, nested=False)
            if end is None:
                return None
            _mark(kinds, start, end, _COMMENT)
        elif value == "#":
            end = token.end()
            if _csharp_line_start(text, start) and _CS_MESSAGE_DIRECTIVE.match(
                text, start
            ):
                end = _line_end(text, start, _CS_BREAK)
        elif value.endswith('"') and "$" in value:
            stop = (
                _CS_INTERPOLATED_STOP
                if value == '$"'
                else _CS_VERBATIM_INTERPOLATED_STOP
            )
            end = _csharp_interpolated(text, kinds, start, token.end(), stop)
        else:
            pattern = {'"': _CS_STRING, '@"': _CS_VERBATIM, "'": _CS_CHAR}[value]
            literal = pattern.match(text, start)
            end = None if literal is None else literal.end()
            if end is not None:
                _mark(kinds, start, end, _STRING)
        if end is None:
            return None
        index = end
    return kinds


def _csharp_line_start(text: str, index: int) -> bool:
    """Whether only blanks precede ``index`` on its line (a directive may start)."""
    while index and text[index - 1] not in _CS_BREAKS and text[index - 1].isspace():
        index -= 1
    return not index or text[index - 1] in _CS_BREAKS


def _csharp_interpolated(
    text: str, kinds: bytearray, start: int, index: int, stop_pattern: re.Pattern[str]
) -> int | None:
    fields: list[tuple[int, int]] = []
    while (stop := stop_pattern.search(text, index)) is not None:
        value = stop.group()
        if value == '"':
            _mark(kinds, start, stop.end(), _STRING)
            for field_start, field_end in fields:
                _mark(kinds, field_start, field_end, _CODE)
            return stop.end()
        if value == "{":
            field = _CS_FIELD.match(text, stop.end())
            if field is None:
                return None
            fields.append((stop.end(), field.end() - 1))
            index = field.end()
        elif len(value) == 1 and value in "}" + _CS_BREAKS:
            # A lone ``}`` is an error; a line break may not sit in a
            # regular interpolated string.
            return None
        else:
            index = stop.end()
    return None


# The file must be PHP from its first byte (after an interpreter line):
# markup before ``<?php`` is output, and ``?>`` ends PHP code even inside a
# ``//`` or ``#`` comment, so only a final ``?>`` with nothing after it is
# accepted. Here-documents, and data after ``__halt_compiler`` (which a script
# may read back and run), are not bounded.
_PHP_OPEN = re.compile(r"(?:#![^\r\n]*(?:\r\n|\r|\n))?<\?php(?=[ \t\r\n])", re.I)
_PHP_UNSUPPORTED = re.compile(r"<<<|__halt_compiler", re.I)
# ``#[`` opens a PHP 8 attribute, not a comment.
_PHP_TOKEN = re.compile(r"//|#(?!\[)|/\*|['\"`]")
_PHP_QUOTED = {
    quote: re.compile(rf"{quote}(?:[^{quote}\\]|\\[\s\S])*{quote}")
    for quote in ("'", '"', "`")
}
# ``{$...}`` and ``${...}`` embed expressions that may hold quotes.
_PHP_COMPLEX_FIELD = re.compile(r"\{\$|\$\{")


def _lex_php(text: str) -> bytearray | None:
    """PHP: ``//``, ``#``, and block comments; quoted and backtick strings."""
    opening = _PHP_OPEN.match(text)
    if opening is None or _PHP_UNSUPPORTED.search(text):
        return None
    limit = text.find("?>")
    if limit < 0:
        limit = len(text)
    elif text[limit + 2 :].strip(" \t\r\n"):
        return None
    kinds = bytearray(len(text))
    index = opening.end()
    while (token := _PHP_TOKEN.search(text, index, limit)) is not None:
        start, value = token.start(), token.group()
        end: int | None
        if value in {"//", "#"}:
            end = min(_line_end(text, start, _CR_LF), limit)
            _mark(kinds, start, end, _COMMENT)
        elif value == "/*":
            end = _block_comment_end(text, start + 2, nested=False)
            if end is None or end > limit:
                return None
            _mark(kinds, start, end, _COMMENT)
        else:
            literal = _PHP_QUOTED[value].match(text, start, limit)
            if literal is None or (
                value != "'" and _PHP_COMPLEX_FIELD.search(text, start, literal.end())
            ):
                return None
            end = literal.end()
            if value != "`":
                # A backtick string is a shell command, not data.
                _mark(kinds, start, end, _STRING)
        index = end
    return kinds


# Zig has only line comments (``//``, and ``///``/``//!`` doc comments), no
# block comments, and three literal forms: one-line strings and char literals
# (``@"..."`` names use the string form) and ``\\`` multi-line string lines,
# which run raw to the end of their line.
_ZIG_TOKEN = re.compile(r"//|\\\\|[\"']")
_ZIG_QUOTED = {
    quote: re.compile(rf"{quote}(?:[^{quote}\\\n]|\\[^\n])*{quote}") for quote in "\"'"
}


def _lex_zig(text: str) -> bytearray | None:
    """Zig: line comments, one-line strings and chars, ``\\\\`` string lines."""
    kinds = bytearray(len(text))
    index = 0
    while (token := _ZIG_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        if value == "//":
            end = _line_end(text, start, _CR_LF)
            _mark(kinds, start, end, _COMMENT)
        elif value == "\\\\":
            end = _line_end(text, start, _CR_LF)
            _mark(kinds, start, end, _STRING)
        else:
            literal = _ZIG_QUOTED[value].match(text, start)
            if literal is None:
                return None
            end = literal.end()
            _mark(kinds, start, end, _STRING)
        index = end
    return kinds


# Swift: nested block comments; strings with ``\(...)`` interpolation, whose
# code may hold further strings and comments; multi-line ``"""`` strings; and
# extended ``#"..."#`` delimiters, where only ``\#`` escapes and ``\#(``
# interpolates. Swift 6 also lexes a bare ``/.../`` as a regular expression
# where an expression can start, which moves where a ``//`` begins, so any
# slash that could open one leaves the file unmasked.
_SWIFT_TOKEN = re.compile(r'//|/\*|#+(?:"""|"|/)|"""|"|/|[`\'()]')
_SWIFT_MULTILINE_OPEN = re.compile(r"[ \t]*(?:\r\n|\r|\n)")
_SWIFT_BACKTICK = re.compile(r"`[^`\r\n]+`")


class _SwiftString:
    """One string form: its closing delimiter and the stops inside it."""

    __slots__ = ("close", "interpolation", "stop")

    def __init__(self, hashes: int, *, multiline: bool) -> None:
        pounds = "#" * hashes
        escape = re.escape("\\" + pounds)
        self.close = ('"""' if multiline else '"') + pounds
        self.interpolation = "\\" + pounds + "("
        stops = [
            escape + r"\(",
            escape + (r"[\s\S]" if multiline else r"[^\r\n]"),
            re.escape(self.close),
        ]
        if not multiline:
            stops.append(r"[\r\n]")
        self.stop = re.compile("|".join(stops))


@lru_cache(maxsize=64)
def _swift_string(hashes: int, multiline: bool) -> _SwiftString:
    return _SwiftString(hashes, multiline=multiline)


def _swift_string_rest(
    text: str, kinds: bytearray, mark_from: int, scan_from: int, form: _SwiftString
) -> tuple[int, bool] | None:
    """Mark string text up to its close or its next ``\\(`` interpolation.

    Returns (resume offset, whether an interpolation opened), or None when a
    one-line string meets a line break or the file ends.
    """
    index = scan_from
    while (stop := form.stop.search(text, index)) is not None:
        value = stop.group()
        if value == form.interpolation:
            _mark(kinds, mark_from, stop.start(), _STRING)
            return stop.end(), True
        if value == form.close:
            _mark(kinds, mark_from, stop.end(), _STRING)
            return stop.end(), False
        if value in "\r\n":
            return None
        index = stop.end()
    return None


def _swift_may_open_regex(text: str, start: int) -> bool:
    """Whether the ``/`` at ``start`` could begin a bare regular expression.

    Swift never lexes one right after an operand, before a blank, or without
    a closing ``/`` on the same line; anything else could be one.
    """
    previous = text[start - 1] if start else "\n"
    if previous.isalnum() or previous in '_)]}"`':
        return False
    following = text[start + 1 : start + 2]
    if not following or following in " \t\r\n":
        return False
    return text.find("/", start + 1, _line_end(text, start + 1, _CR_LF)) >= 0


def _swift_extended_regex_end(text: str, start: int, hashes: int) -> int | None:
    """End of a ``#/.../#`` literal whose opener ends at ``start``."""
    close = text.find("/" + "#" * hashes, start)
    if close < 0:
        return None
    if _SWIFT_MULTILINE_OPEN.match(text, start) is None and _CR_LF.search(
        text, start, close
    ):
        # Only an opener that ends its line starts a multi-line literal.
        return None
    return close + 1 + hashes


def _lex_swift(text: str) -> bytearray | None:
    """Swift: nested comments, interpolated and extended strings, regexes."""
    kinds = bytearray(len(text))
    # For each open ``\\(`` interpolation: the string it resumes, and the
    # parentheses opened inside it.
    forms: list[_SwiftString] = []
    depths: list[int] = []
    index = _shebang_end(text)
    while True:
        token = _SWIFT_TOKEN.search(text, index)
        if token is None:
            return None if forms else kinds
        start, value = token.start(), token.group()
        end: int | None = token.end()
        rest: tuple[int, bool] | None = None
        form: _SwiftString | None = None
        if value == "//":
            end = _line_end(text, start, _CR_LF)
            _mark(kinds, start, end, _COMMENT)
        elif value == "/*":
            end = _nested_block_end(text, start + 2)
            if end is None:
                return None
            _mark(kinds, start, end, _COMMENT)
        elif value == "/":
            if _swift_may_open_regex(text, start):
                return None
        elif value.endswith("/"):
            end = _swift_extended_regex_end(text, token.end(), len(value) - 1)
            if end is None:
                return None
            _mark(kinds, start, end, _STRING)
        elif value.endswith('"'):
            hashes = len(value) - len(value.lstrip("#"))
            multiline = value.endswith('"""')
            if multiline and _SWIFT_MULTILINE_OPEN.match(text, token.end()) is None:
                return None
            form = _swift_string(hashes, multiline)
            rest = _swift_string_rest(text, kinds, start, token.end(), form)
            if rest is None:
                return None
        elif value == "(":
            if depths:
                depths[-1] += 1
        elif value == ")":
            if depths and depths[-1]:
                depths[-1] -= 1
            elif depths:
                depths.pop()
                form = forms.pop()
                rest = _swift_string_rest(text, kinds, start + 1, start + 1, form)
                if rest is None:
                    return None
        elif value == "`":
            name = _SWIFT_BACKTICK.match(text, start)
            if name is None:
                return None
            end = name.end()
        else:
            # A single quote is not Swift syntax.
            return None
        if rest is not None:
            assert form is not None
            end, opened = rest
            if opened:
                forms.append(form)
                depths.append(0)
        assert end is not None
        index = end


# Scala: nested block comments; plain strings with escapes; raw ``"""``
# strings; ``id"..."``/``id"""..."""`` interpolations whose ``${...}`` fields
# are code; char literals (``'a'``) beside symbol literals and quotes
# (``'sym``, ``'{...}``); backquoted names. Left unmasked: an XML literal,
# which starts at a ``<`` after a blank, ``(`` or ``{`` and before a name (its
# text is not Scala, and ``{...}`` inside it is code); a Unicode escape, which
# Scala 2.12 translates before lexing; and ``\\"`` or ``$"`` inside an
# interpolation, which Scala versions end at different places.
_SCALA_TOKEN = re.compile(r'//|/\*|[^\W\d]\w*"""|[^\W\d]\w*"|"""|"|[\'`{}<]')
_SCALA_UNSUPPORTED = _JAVA_UNICODE_ESCAPE
_SCALA_STRING = re.compile(r'"(?:[^"\\\r\n]|\\[^\r\n])*"')
_SCALA_CHAR = re.compile(r"'(?:[^'\\\r\n]|\\(?:u+[0-9A-Fa-f]{4}|[0-7]{1,3}|[^\r\n]))'")
_SCALA_BACKTICK = re.compile(r"`[^`\r\n]+`")
_SCALA_XML_NAME = re.compile(r"[^\W\d]|[_!?]")
_SCALA_INTERPOLATION_STOP = {
    False: re.compile(r'\$\$|\$\{|\$"|\\"|"|[\r\n]'),
    True: re.compile(r'\$\$|\$\{|\$"|"""'),
}


def _scala_interpolation_rest(
    text: str, kinds: bytearray, mark_from: int, scan_from: int, multiline: bool
) -> tuple[int, bool] | None:
    """Mark interpolated text up to its close or its next ``${`` field."""
    stop_pattern = _SCALA_INTERPOLATION_STOP[multiline]
    index = scan_from
    while (stop := stop_pattern.search(text, index)) is not None:
        value = stop.group()
        if value == "$$":
            index = stop.end()
            continue
        if value == "${":
            _mark(kinds, mark_from, stop.start(), _STRING)
            return stop.end(), True
        if value in {'"', '"""'}:
            end = stop.end()
            if multiline:
                while text.startswith('"', end):
                    end += 1
            _mark(kinds, mark_from, end, _STRING)
            return end, False
        return None
    return None


def _lex_scala(text: str) -> bytearray | None:
    """Scala: nested comments, raw and interpolated strings, char literals."""
    if _SCALA_UNSUPPORTED.search(text):
        return None
    kinds = bytearray(len(text))
    # For each open ``${`` field: whether it resumes a multi-line string, and
    # the braces opened inside it.
    forms: list[bool] = []
    depths: list[int] = []
    index = _shebang_end(text)
    while True:
        token = _SCALA_TOKEN.search(text, index)
        if token is None:
            return None if forms else kinds
        start, value = token.start(), token.group()
        end: int | None = token.end()
        rest: tuple[int, bool] | None = None
        multiline = value.endswith('"""')
        if value == "//":
            end = _line_end(text, start, _CR_LF)
            _mark(kinds, start, end, _COMMENT)
        elif value == "/*":
            end = _nested_block_end(text, start + 2)
            if end is None:
                return None
            _mark(kinds, start, end, _COMMENT)
        elif value == '"""':
            close = text.find('"""', token.end())
            if close < 0:
                return None
            end = close + 3
            while text.startswith('"', end):
                end += 1
            _mark(kinds, start, end, _STRING)
        elif value == '"':
            literal = _SCALA_STRING.match(text, start)
            if literal is None:
                return None
            end = literal.end()
            _mark(kinds, start, end, _STRING)
        elif value.endswith('"'):
            quote = token.end() - (3 if multiline else 1)
            rest = _scala_interpolation_rest(text, kinds, quote, token.end(), multiline)
            if rest is None:
                return None
        elif value == "'":
            char = _SCALA_CHAR.match(text, start)
            if char is not None:
                end = char.end()
                _mark(kinds, start, end, _STRING)
            # Otherwise a symbol literal or a quote: code.
        elif value == "`":
            name = _SCALA_BACKTICK.match(text, start)
            if name is None:
                return None
            end = name.end()
        elif value == "<":
            previous = text[start - 1] if start else " "
            if (previous.isspace() or previous in "({") and _SCALA_XML_NAME.match(
                text, start + 1
            ):
                return None
        elif value == "{":
            if depths:
                depths[-1] += 1
        elif depths and depths[-1]:  # "}"
            depths[-1] -= 1
        elif depths:
            depths.pop()
            multiline = forms.pop()
            rest = _scala_interpolation_rest(
                text, kinds, start + 1, start + 1, multiline
            )
            if rest is None:
                return None
        if rest is not None:
            end, opened = rest
            if opened:
                forms.append(multiline)
                depths.append(0)
        assert end is not None
        index = end


# Groovy (and Gradle build scripts): flat block comments; ``'...'`` and
# ``'''...'''`` strings; ``"..."`` and ``"""..."""`` GStrings whose ``${...}``
# fields are code. A ``/`` begins a slashy string, which may span lines,
# wherever the lexer allows a regular expression, so a slash is only taken as
# division straight after an operand (a name that is not a keyword, a number,
# a closing bracket or quote); any other slash, a dollar-slashy ``$/``, or a
# Unicode escape (translated before lexing) leaves the file unmasked.
_GROOVY_TOKEN = re.compile(r"//|/\*|'''|\"\"\"|\$/|['\"/{}]")
_GROOVY_STRING = {
    "'": re.compile(r"'(?:[^'\\\r\n]|\\[\s\S])*'"),
    "'''": re.compile(r"'''(?:[^'\\]|\\[\s\S]|'(?!''))*'''"),
}
_GROOVY_TEMPLATES = {
    '"': _Template('"', escapes=True, multiline=False),
    '"""': _Template('"""', escapes=True, multiline=True),
}
_GROOVY_KEYWORDS = frozenset(
    [
        "as",
        "assert",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "def",
        "default",
        "do",
        "else",
        "enum",
        "extends",
        "false",
        "final",
        "finally",
        "for",
        "goto",
        "if",
        "implements",
        "import",
        "in",
        "instanceof",
        "interface",
        "native",
        "new",
        "non-sealed",
        "null",
        "package",
        "permits",
        "private",
        "protected",
        "public",
        "record",
        "return",
        "sealed",
        "static",
        "strictfp",
        "super",
        "switch",
        "synchronized",
        "this",
        "threadsafe",
        "throw",
        "throws",
        "trait",
        "transient",
        "true",
        "try",
        "var",
        "void",
        "volatile",
        "while",
        "yield",
    ]
)


def _groovy_divides(text: str, start: int) -> bool:
    """Whether the ``/`` at ``start`` is division rather than a slashy string."""
    cursor = start - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    if cursor < 0:
        return False
    previous = text[cursor]
    if previous in ")]}'\"":
        return True
    if not (previous.isalnum() or previous in "_$"):
        return False
    word_start = cursor
    while word_start > 0 and (
        text[word_start - 1].isalnum() or text[word_start - 1] in "_$"
    ):
        word_start -= 1
    return text[word_start : cursor + 1] not in _GROOVY_KEYWORDS


def _groovy_literal(text: str, kinds: bytearray, start: int, value: str) -> int | None:
    if value == "/":
        return start + 1 if _groovy_divides(text, start) else None
    if value == "$/":
        return None
    literal = _GROOVY_STRING[value].match(text, start)
    if literal is None:
        return None
    _mark(kinds, start, literal.end(), _STRING)
    return literal.end()


def _lex_groovy(text: str) -> bytearray | None:
    """Groovy: comments, quoted strings, GStrings; slashy strings unmasked."""
    if _JAVA_UNICODE_ESCAPE.search(text):
        return None
    return _lex_templated(
        text, _GROOVY_TOKEN, _GROOVY_TEMPLATES, _groovy_literal, nested=False
    )


# F#: ``//`` and nested ``(* *)`` comments, whose text is lexed for strings
# and char literals so a ``"*)"`` inside does not end them; ``(*)`` is the
# multiplication operator. Strings may span lines: regular (escapes),
# verbatim ``@"..."`` (``""``), triple ``"""..."""``, and interpolated forms
# whose ``{...}`` fields are bounded only when they hold no literal, comment,
# or brace. A ``'`` after a name char is part of the name (``x'``), and one
# that opens no char literal starts a type variable (``'T``). Left unmasked:
# ``#if`` sections (the compiler skips inactive lines unlexed), ``$$``
# interpolation, and OCaml-compatible ``(*IF-FSHARP``/``(*F#`` comments, whose
# text F# compiles as code.
_FS_UNSUPPORTED = re.compile(
    r"^[ \t]*#(?:if|elif|else|endif)\b|\(\*(?:IF-|F#)|ENDIF-|\$\$", re.M
)
_FS_TOKEN = re.compile(r'//|\(\*|\$@"|@\$"|\$"""|\$"|@"|"""|"|``|\'')
_FS_STRING = {
    '"': re.compile(r'"(?:[^"\\]|\\[\s\S])*"'),
    '@"': re.compile(r'@"(?:[^"]|"")*"'),
    '"""': re.compile(r'"""[\s\S]*?"""'),
}
_FS_CHAR = re.compile(
    r"'(?:[^'\\\r\n]|\\(?:[ntbrafv\\\"'0]|[0-9]{3}|x[0-9A-Fa-f]{2}"
    r"|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}))'"
)
_FS_BACKTICK = re.compile(r"``(?:[^`\r\n]|`(?!`))+``")
_FS_COMMENT_STOP = re.compile(r'\(\*\)|\(\*|\*\)|"""|@"|"|\'')
_FS_INTERPOLATED_STOP = {
    '$"': re.compile(r'\\[\s\S]|\{\{|\}\}|[{}"]'),
    '$@"': re.compile(r'""|\{\{|\}\}|[{}"]'),
    '@$"': re.compile(r'""|\{\{|\}\}|[{}"]'),
    '$"""': re.compile(r'\{\{|\}\}|[{}]|"""'),
}
_FS_FIELD = re.compile(r"(?:[^\"'@$/\\{}(]|\((?!\*))*\}")


def _fsharp_comment_end(text: str, start: int) -> int | None:
    """End of the ``(*`` comment whose opener ends at ``start``."""
    depth = 1
    index = start
    while (stop := _FS_COMMENT_STOP.search(text, index)) is not None:
        value = stop.group()
        index = stop.end()
        if value == "(*)":
            continue
        if value == "(*":
            depth += 1
        elif value == "*)":
            depth -= 1
            if not depth:
                return index
        elif value == "'":
            char = _FS_CHAR.match(text, stop.start())
            if char is not None:
                index = char.end()
        else:
            literal = _FS_STRING[value].match(text, stop.start())
            if literal is None:
                return None
            index = literal.end()
    return None


def _fsharp_interpolated(text: str, index: int, opener: str) -> int | None:
    stop_pattern = _FS_INTERPOLATED_STOP[opener]
    while (stop := stop_pattern.search(text, index)) is not None:
        value = stop.group()
        if value in {'"', '"""'}:
            return stop.end()
        if value == "{":
            field = _FS_FIELD.match(text, stop.end())
            if field is None:
                return None
            index = field.end()
        elif value == "}":
            return None
        else:
            index = stop.end()
    return None


def _lex_fsharp(text: str) -> bytearray | None:
    """F#: nested comments, regular, verbatim, triple and interpolated strings."""
    if _FS_UNSUPPORTED.search(text):
        return None
    kinds = bytearray(len(text))
    index = _shebang_end(text)
    while (token := _FS_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        end: int | None = token.end()
        if value == "//":
            end = _line_end(text, start, _CR_LF)
            _mark(kinds, start, end, _COMMENT)
        elif value == "(*":
            end = start + 3
            if not text.startswith(")", start + 2):
                end = _fsharp_comment_end(text, start + 2)
                if end is None:
                    return None
                _mark(kinds, start, end, _COMMENT)
        elif value == "``":
            name = _FS_BACKTICK.match(text, start)
            if name is None:
                return None
            end = name.end()
        elif value == "'":
            previous = text[start - 1] if start else " "
            if not (previous.isalnum() or previous in "_'"):
                char = _FS_CHAR.match(text, start)
                if char is not None:
                    end = char.end()
                    _mark(kinds, start, end, _STRING)
        else:
            if value.startswith(("$", "@$")):
                end = _fsharp_interpolated(text, token.end(), value)
            else:
                literal = _FS_STRING[value].match(text, start)
                end = None if literal is None else literal.end()
            if end is None:
                return None
            _mark(kinds, start, end, _STRING)
        assert end is not None
        index = end
    return kinds


# --- Python -------------------------------------------------------------------

_PY_NEWLINE = re.compile(r"\r\n|\r|\n")
_PY_FSTRING_START = frozenset(
    getattr(tokenize, name)
    for name in ("FSTRING_START", "TSTRING_START")
    if hasattr(tokenize, name)
)
_PY_FSTRING_END = frozenset(
    getattr(tokenize, name)
    for name in ("FSTRING_END", "TSTRING_END")
    if hasattr(tokenize, name)
)
_PY_FSTRING_PARTS = frozenset(
    getattr(tokenize, name)
    for name in ("FSTRING_MIDDLE", "TSTRING_MIDDLE")
    if hasattr(tokenize, name)
)
_PY_LAYOUT = frozenset(
    {
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
    }
)
_PY_FALLBACK_TOKEN = re.compile(r"#|'''|\"\"\"|'|\"")
_PY_TRIPLE_STOP = {q: re.compile(r"\\[\s\S]|" + q) for q in ("'''", '"""')}


class _TokenMismatch(Exception):
    """tokenize reported a position that does not match the source text."""


def _lex_python(text: str) -> bytearray | None:
    """Python: ``tokenize`` for exact comments, strings, and f-string fields.

    ``//`` and ``/*`` are never comment syntax in Python. If the source does
    not tokenize, a hash-comment lexer that honors quotes and triple quotes is
    used instead; the C lexer never is.
    """
    kinds = _python_tokens(text)
    return kinds if kinds is not None else _python_fallback(text)


def _python_tokens(text: str) -> bytearray | None:
    # Python treats ``\r\n``, ``\r``, and ``\n`` as newlines. Split on exactly
    # those so tokenize rows map back to raw offsets, whatever else
    # ``str.splitlines`` might consider a boundary.
    lines: list[tuple[int, str]] = []
    position = 0
    for newline in _PY_NEWLINE.finditer(text):
        lines.append((position, text[position : newline.start()]))
        position = newline.end()
    if position < len(text):
        lines.append((position, text[position:]))
    feed = iter([line + "\n" for _, line in lines])

    def offset(row: int, column: int) -> int:
        start, line = lines[row - 1]
        if column > len(line):
            raise _TokenMismatch
        return start + column

    fstrings: list[tuple[int, int]] = []
    open_fstrings: list[int] = []
    code: list[tuple[int, int]] = []
    strings: list[tuple[int, int]] = []
    comments: list[tuple[int, int]] = []
    try:
        for token in _quiet_tokens(feed):
            kind = token.type
            if kind in _PY_LAYOUT or kind in _PY_FSTRING_PARTS:
                continue
            if token.start[0] > len(lines) or token.end[0] > len(lines):
                raise _TokenMismatch
            span = (offset(*token.start), offset(*token.end))
            if _PY_NEWLINE.sub("\n", text[span[0] : span[1]]) != token.string:
                raise _TokenMismatch
            if kind in _PY_FSTRING_START:
                open_fstrings.append(span[0])
            elif kind in _PY_FSTRING_END:
                if not open_fstrings:
                    raise _TokenMismatch
                fstrings.append((open_fstrings.pop(), span[1]))
            elif kind == tokenize.STRING:
                strings.append(span)
            elif kind == tokenize.COMMENT:
                comments.append(span)
            elif open_fstrings:
                code.append(span)
    except (tokenize.TokenError, SyntaxError, _TokenMismatch):
        return None
    if open_fstrings:
        return None
    kinds = bytearray(len(text))
    # An f-string is literal text except its replacement fields, which are
    # code; nested literals and comments inside a field keep their own kind.
    for spans, kind in (
        (fstrings, _STRING),
        (code, _CODE),
        (strings, _STRING),
        (comments, _COMMENT),
    ):
        for start, end in spans:
            _mark(kinds, start, end, kind)
    return kinds


def _quiet_tokens(feed: Iterator[str]) -> Iterator[tokenize.TokenInfo]:
    # Invalid escapes in submitted source are not screener diagnostics.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        yield from tokenize.generate_tokens(lambda: next(feed, ""))


def _python_fallback(text: str) -> bytearray | None:
    kinds = bytearray(len(text))
    index = 0
    while (token := _PY_FALLBACK_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        if value == "#":
            end = _line_end(text, start, _CR_LF)
            _mark(kinds, start, end, _COMMENT)
        elif len(value) == 3:
            stop_pattern = _PY_TRIPLE_STOP[value]
            cursor = start + 3
            while (stop := stop_pattern.search(text, cursor)) is not None:
                if stop.group() == value:
                    break
                cursor = stop.end()
            if stop is None:
                return None
            end = stop.end()
            _mark(kinds, start, end, _STRING)
        else:
            body = (_SINGLE_CRLF if value == "'" else _DOUBLE_CRLF).match(
                text, start + 1
            )
            if body is None:
                return None
            end = body.end()
            _mark(kinds, start, end, _STRING)
        index = end
    return kinds


# --- Shell --------------------------------------------------------------------

_SH_TOKEN = re.compile(
    r"\\[\s\S]|#|\$'|\$\(\(|\$\(|\$\{|\$\[|\(\(|['\"`(){}]|<<-?|\n"
    r"|(?<![^\s;&|(])(?:case|esac)(?![^\s;&|)])"
)
_SH_WORD_BREAK = frozenset(" \t\n;&|()<>")
_SH_HEREDOC_WORD = re.compile(
    r"[ \t]*((?:[^\s;&|()<>'\"\\]|\\.|'[^'\n]*'|\"[^\"\n]*\")+)"
)
_SH_ANSI_C = _quoted("'")


class _ShellFrame:
    __slots__ = ("depth", "kind", "quoted")

    def __init__(self, kind: str, *, quoted: bool = False) -> None:
        # "top", "cmd" ($(...)), "dq", "bt" (`...`), "param" (${...}), and
        # "arith" ($((...)) and ((...)), where ``<<`` shifts and ``#`` is text).
        self.kind = kind
        self.depth = 0
        self.quoted = quoted


def _lex_shell(text: str) -> bytearray | None:
    """POSIX/bash: ``#`` starts a comment only at the start of an unquoted word.

    Quotes, ``$(...)``, backticks, ``${...}`` (where ``#`` is an operator),
    arithmetic (where ``<<`` is a shift) and here-document bodies are tracked
    so a ``#`` inside any of them is never read as a comment. Here-document
    bodies are left verbatim: they are often the script that actually runs.

    bash parses and runs a script one command at a time, so text after an
    ``exit`` can rebalance any quote or bracket this lexer tracks without ever
    running. Where the lexer cannot tell how bash reads a construct (``$[``,
    ``<<`` inside an assignment subscript, a ``case`` word inside ``$(...)``,
    ``$((`` that turns out to hold a subshell), the file is left unmasked
    rather than guessed at.
    """
    kinds = bytearray(len(text))
    frames = [_ShellFrame("top")]
    heredocs: list[tuple[str, bool]] = []
    index = 0
    while (token := _SH_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        end = token.end()
        frame = frames[-1]
        if value.startswith("\\"):
            index = end
            continue
        if value == "$[":
            # Legacy arithmetic: ``<<`` inside it is a shift.
            return None
        if frame.kind == "dq":
            if value == '"':
                frames.pop()
            elif value == "$((":
                frames.append(_ShellFrame("arith"))
            elif value == "$(":
                frames.append(_ShellFrame("cmd"))
            elif value == "${":
                frames.append(_ShellFrame("param", quoted=True))
            elif value == "`":
                frames.append(_ShellFrame("bt"))
            else:
                end = start + 1
            index = end
            continue
        if value == '"':
            frames.append(_ShellFrame("dq"))
        elif value == "'" and not (frame.kind == "param" and frame.quoted):
            close = text.find("'", start + 1)
            if close < 0:
                return None
            end = close + 1
        elif value == "$'":
            body = _SH_ANSI_C.match(text, end)
            if body is None:
                return None
            end = body.end()
        elif value == "$((":
            frames.append(_ShellFrame("arith"))
        elif value == "$(":
            frames.append(_ShellFrame("cmd"))
        elif value == "${":
            frames.append(_ShellFrame("param"))
        elif value == "`":
            if frame.kind == "bt":
                frames.pop()
            else:
                frames.append(_ShellFrame("bt"))
        elif frame.kind == "param":
            if value == "{":
                frame.depth += 1
            elif value == "}":
                if frame.depth:
                    frame.depth -= 1
                else:
                    frames.pop()
            else:
                end = start + 1
        elif frame.kind == "arith":
            if value in {"(", "(("}:
                frame.depth += len(value)
            elif value == ")":
                if frame.depth:
                    frame.depth -= 1
                elif text.startswith(")", end):
                    frames.pop()
                    end += 1
                else:
                    # ``$((cmd) )`` is a command substitution holding a
                    # subshell after all.
                    return None
            else:
                end = start + 1
        elif value == "((":
            frames.append(_ShellFrame("arith"))
        elif value == "#":
            if _sh_word_start(text, start):
                end = _line_end(text, start, _LF)
                if frame.kind == "bt":
                    backtick = text.find("`", start, end)
                    end = end if backtick < 0 else backtick
                _mark(kinds, start, end, _COMMENT)
        elif value == "(":
            frame.depth += 1
        elif value == ")":
            if frame.depth:
                frame.depth -= 1
            elif frame.kind == "cmd":
                frames.pop()
        elif value in {"case", "esac"}:
            if frame.kind == "cmd":
                # A pattern's ``)`` does not close ``$(``, but a ``case`` word
                # may also be an argument (``$(echo case)``).
                return None
        elif value.startswith("<<"):
            if text.startswith("<", end):
                end += 1
            elif _sh_in_subscript(text, start):
                # ``a[1<<2]=x`` shifts inside an assignment subscript.
                return None
            else:
                word = _SH_HEREDOC_WORD.match(text, end)
                if word is not None:
                    delimiter = _sh_unquote(word.group(1))
                    if delimiter:
                        heredocs.append((delimiter, value == "<<-"))
                    end = word.end()
        elif value == "\n" and heredocs:
            end = _skip_heredoc_bodies(text, end, heredocs)
            heredocs.clear()
        index = end
    return kinds if len(frames) == 1 else None


def _sh_unquote(word: str) -> str:
    """Remove shell quoting from a here-document delimiter word, as bash does.

    A backslash inside single quotes stays literal (``<<'a\\b'`` ends at
    ``a\\b``); an unquoted backslash quotes the next character, and a double
    quote unescapes only ``$`` ``` ` ``` ``"`` and ``\\``. Stripping every
    backslash (as a plain quote-removal would) shortens the delimiter and can
    end the body early, blanking script the shell runs verbatim.
    """
    out: list[str] = []
    index = 0
    while index < len(word):
        char = word[index]
        if char == "\\":
            if index + 1 < len(word):
                out.append(word[index + 1])
            index += 2
        elif char == "'":
            close = word.find("'", index + 1)
            close = len(word) if close < 0 else close
            out.append(word[index + 1 : close])
            index = close + 1
        elif char == '"':
            index += 1
            while index < len(word) and word[index] != '"':
                if word[index] == "\\" and word[index + 1 : index + 2] in {
                    "$",
                    "`",
                    '"',
                    "\\",
                }:
                    out.append(word[index + 1])
                    index += 2
                else:
                    out.append(word[index])
                    index += 1
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _sh_in_subscript(text: str, index: int) -> bool:
    """Whether ``text[index]`` follows an unclosed ``[`` in the same word."""
    word_start = index
    while word_start > 0 and text[word_start - 1] not in _SH_WORD_BREAK:
        word_start -= 1
    word = text[word_start:index]
    return word.count("[") > word.count("]")


def _sh_word_start(text: str, index: int) -> bool:
    """Whether ``text[index]`` begins a word; an escaped blank joins words."""
    if index == 0:
        return True
    if text[index - 1] not in _SH_WORD_BREAK:
        return False
    backslashes = 0
    cursor = index - 2
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 0


def _skip_heredoc_bodies(
    text: str, start: int, heredocs: list[tuple[str, bool]]
) -> int:
    """Return the offset after every pending here-document body."""
    position = start
    for delimiter, strip_tabs in heredocs:
        while position < len(text):
            newline = text.find("\n", position)
            line_end = len(text) if newline < 0 else newline
            line = text[position:line_end]
            position = line_end + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
    return min(position, len(text))


# --- Dockerfile ----------------------------------------------------------------

_DOCKER_HEREDOC_OPEN = re.compile(r"(?<!<)<<(-?)(?!<)")
# The rest of a shell word after ``<<``: BuildKit ends it at an unquoted blank
# and removes its quotes. A backslash, ``$`` or ``<`` stops the match.
_DOCKER_HEREDOC_NAME = re.compile(r"(?:[^\s'\"\\<$]|'[^'\n]*'|\"[^\"\\$\n]*\")+")
_DOCKER_HEREDOC_QUOTE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_DOCKER_DIRECTIVE = re.compile(r"#[ \t]*(?:syntax|escape|check)[ \t]*=", re.I)
_DOCKER_INSTRUCTION = re.compile(r"([A-Za-z]+)(?=[ \t]|$)")
_DOCKER_SHELL_FORM = frozenset({"RUN", "CMD", "ENTRYPOINT"})
_DOCKER_EXEC_FORM = re.compile(r"(?:[ \t]+--\S+)*[ \t]*\[")
# A SHELL instruction or an escape directive changes how RUN text is read, so
# shell comments inside RUN/CMD/ENTRYPOINT are then left visible.
_DOCKER_OTHER_SHELL = re.compile(
    r"^[ \t]*(?:shell\b|#[ \t]*escape[ \t]*=)", re.I | re.M
)


def _lex_dockerfile(text: str) -> bytearray | None:
    """Dockerfile: whole-line ``#`` comments, plus shell comments in RUN.

    Docker strips a line whose first non-blank character is ``#``, even inside
    a continued instruction; elsewhere ``#`` is an argument. Shell-form
    RUN/CMD/ENTRYPOINT text goes to ``/bin/sh``, so the shell lexer decides its
    trailing comments. Parser directives at the top (``# syntax=`` selects the
    BuildKit frontend) and here-document bodies (script content) stay visible.
    A here-document whose delimiter BuildKit may read differently leaves the
    file unmasked, since ending its body early would blank script lines.
    """
    kinds = bytearray(len(text))
    lines = _lf_lines(text)
    shell_rules = _DOCKER_OTHER_SHELL.search(text) is None
    directives = True
    index = 0
    while index < len(lines):
        start, line = lines[index]
        index += 1
        content = line.removesuffix("\r")
        body = content.lstrip(" \t")
        if body.startswith("#"):
            if not (directives and _DOCKER_DIRECTIVE.match(body)):
                directives = False
                comment = start + len(content) - len(body)
                _mark(kinds, comment, start + len(content), _COMMENT)
            continue
        directives = False
        if not body:
            continue
        end = start + len(content)
        physical = content
        # BuildKit joins the whole continued instruction before it reads any
        # here-document body, so an opener on a later line (``RUN cat <<A \``
        # then ``&& sh <<B``) opens a second body. Collect every segment's
        # openers, then skip all their bodies in order.
        markers: list[tuple[str, bool]] = []
        while True:
            segment = _dockerfile_heredocs(physical)
            if segment is None:
                return None
            markers.extend(segment)
            if not physical.rstrip(" \t").endswith("\\") or index >= len(lines):
                break
            next_start, next_line = lines[index]
            index += 1
            physical = next_line.removesuffix("\r")
            stripped = physical.lstrip(" \t")
            if stripped.startswith("#"):
                comment = next_start + len(physical) - len(stripped)
                _mark(kinds, comment, next_start + len(physical), _COMMENT)
            if not stripped or stripped.startswith("#"):
                # Docker drops the line; the instruction stays open.
                physical = "\\"
                continue
            end = next_start + len(physical)
        heredoc = bool(markers)
        if heredoc:
            index = _skip_dockerfile_heredocs(lines, index, markers)
        instruction = _DOCKER_INSTRUCTION.match(body)
        if (
            heredoc
            or not shell_rules
            or instruction is None
            or instruction.group(1).upper() not in _DOCKER_SHELL_FORM
        ):
            continue
        argument_start = start + len(content) - len(body) + instruction.end()
        argument = text[argument_start:end]
        if _DOCKER_EXEC_FORM.match(argument):
            continue
        shell = _lex_shell(argument)
        if shell is None:
            continue
        for run in _RUNS[_COMMENT].finditer(shell):
            _mark(
                kinds,
                argument_start + run.start(),
                argument_start + run.end(),
                _COMMENT,
            )
    return kinds


def _dockerfile_heredocs(line: str) -> list[tuple[str, bool]] | None:
    """The (delimiter, strip tabs) of each here-document ``line`` opens.

    BuildKit opens one at a shell word ``<<NAME`` (optionally after
    file-descriptor digits, with ``-`` to strip tabs) or at a bare ``<<``
    followed by blanks and a NAME word, and ends it at a line equal to NAME
    with quotes removed (``<<"E F"`` at ``E F``, ``<< EOF;x`` at ``EOF;x``).
    Only a ``<<`` that follows another printable character in the same word
    (``cat<<EOF``) is ruled out; any other opener counts, because skipping
    lines that are not a body only leaves them visible. None means a delimiter
    this lexer cannot predict (escapes, variables, an unclosed quote, unusual
    blanks).
    """
    heredocs: list[tuple[str, bool]] = []
    for opener in _DOCKER_HEREDOC_OPEN.finditer(line):
        before = opener.start()
        while before and line[before - 1].isascii() and line[before - 1].isdigit():
            before -= 1
        if before:
            previous = line[before - 1]
            if previous.isascii() and previous.isprintable() and previous != " ":
                continue
        cursor = opener.end()
        while cursor < len(line) and line[cursor] in " \t":
            cursor += 1
        name = _DOCKER_HEREDOC_NAME.match(line, cursor)
        stop = cursor if name is None else name.end()
        if stop < len(line) and line[stop] not in " \t":
            return None
        if name is None:
            continue
        delimiter = _DOCKER_HEREDOC_QUOTE.sub(
            lambda quoted: quoted.group(1) or quoted.group(2) or "", name.group()
        )
        if delimiter:
            heredocs.append((delimiter, opener.group(1) == "-"))
    return heredocs


def _skip_dockerfile_heredocs(
    lines: list[tuple[int, str]], index: int, markers: list[tuple[str, bool]]
) -> int:
    """Return the index of the first line after every here-document body."""
    for delimiter, strip_tabs in markers:
        while index < len(lines):
            candidate = lines[index][1].removesuffix("\r")
            index += 1
            if (candidate.lstrip("\t") if strip_tabs else candidate) == delimiter:
                break
    return index


def _lf_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    position = 0
    while position <= len(text):
        newline = text.find("\n", position)
        if newline < 0:
            if position < len(text):
                lines.append((position, text[position:]))
            break
        lines.append((position, text[position:newline]))
        position = newline + 1
    return lines


# --- Configuration formats ------------------------------------------------------

_TOML_TOKEN = re.compile(r"#|\"\"\"|'''|\"|'")
_TOML_BASIC_STOP = re.compile(r'\\[\s\S]|"""')


def _lex_toml(text: str) -> bytearray | None:
    """TOML: ``#`` outside basic, literal, and multi-line strings."""
    kinds = bytearray(len(text))
    index = 0
    while (token := _TOML_TOKEN.search(text, index)) is not None:
        start, value = token.start(), token.group()
        if value == "#":
            end = _line_end(text, start, _LF)
            _mark(kinds, start, end, _COMMENT)
        elif len(value) == 3:
            if value == "'''":
                close = text.find("'''", start + 3)
            else:
                cursor = start + 3
                close = -1
                while (stop := _TOML_BASIC_STOP.search(text, cursor)) is not None:
                    if stop.group() == '"""':
                        close = stop.start()
                        break
                    cursor = stop.end()
            if close < 0:
                return None
            end = close + 3
            # A closing delimiter may be preceded by up to two more quotes.
            extra = 0
            while extra < 2 and text.startswith(value[0], end):
                end += 1
                extra += 1
        else:
            body = (_DOUBLE_LF if value == '"' else _SINGLE_LF_RAW).match(
                text, start + 1
            )
            if body is None:
                return None
            end = body.end()
        index = end
    return kinds


_YAML_BLOCK_SCALAR = re.compile(r"(?:^|[\s:-])[|>][0-9+-]*$")
_YAML_DOCUMENT_BLOCK = re.compile(r"^(?:---[ \t]+)?(?:[!&][^\s]*[ \t]+)*[|>][0-9+-]*$")


def _lex_yaml(text: str) -> bytearray | None:
    """YAML: ``#`` after whitespace outside quoted and block scalars.

    Quotes only open a scalar where a scalar may begin, so ``it's`` in a plain
    scalar is text. Block scalars (``run: |``) are left verbatim: their lines
    are content, often a script, never YAML comments. A quote that begins a
    line may instead continue the previous line's plain scalar (``a: plain``
    then ``  'x``), where it is literal; if it would stay open past its line,
    the file is left unmasked rather than inverting every later quote.
    """
    kinds = bytearray(len(text))
    quote = ""
    block_indent: int | None = None
    plain_open = False
    # Open ``[``/``{`` collections: only inside one is ``,`` an indicator.
    flow = 0
    for start, raw in _lf_lines(text):
        line = raw.removesuffix("\r")
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if block_indent < 0:
                if not line.startswith(("---", "...")):
                    continue
            elif not line.strip() or indent > block_indent:
                continue
            block_indent = None
            plain_open = False
        continuing = not quote and plain_open and line.lstrip(" \t")[:1] in "'\""
        scalar_start = True
        comment = len(line)
        # What the line's last token was: "plain" scalar text, a "quoted"
        # scalar or closed collection, or an "indicator" awaiting a value.
        last = ""
        cursor = 0
        while cursor < len(line):
            char = line[cursor]
            if quote:
                if quote == '"' and char == "\\":
                    cursor += 2
                    continue
                if char == quote:
                    if quote == "'" and line.startswith("'", cursor + 1):
                        cursor += 2
                        continue
                    quote = ""
                    scalar_start = False
                    last = "quoted"
                cursor += 1
                continue
            if char == "#" and (cursor == 0 or line[cursor - 1] in " \t"):
                comment = cursor
                break
            if char in " \t":
                pass
            elif char in "'\"" and scalar_start:
                quote = char
            elif char in "[{" and scalar_start:
                flow += 1
                last = "indicator"
            elif char in "]}" and flow:
                flow -= 1
                scalar_start = False
                last = "quoted"
            elif (char == "," and flow) or (
                char in "-?:" and (cursor + 1 == len(line) or line[cursor + 1] in " \t")
            ):
                scalar_start = True
                last = "indicator"
            elif char in "&!" and scalar_start:
                while cursor + 1 < len(line) and line[cursor + 1] not in " \t":
                    cursor += 1
                last = "indicator"
            else:
                scalar_start = False
                last = "plain"
            cursor += 1
        if continuing and quote:
            return None
        _mark(kinds, start + comment, start + len(line), _COMMENT)
        code = line[:comment].rstrip()
        if quote:
            plain_open = False
        elif code.strip():
            plain_open = last == "plain" and code.strip() not in {"---", "..."}
        if not quote and _YAML_BLOCK_SCALAR.search(code):
            # An indicator alone on its line belongs to a parent at some lower
            # indentation, possibly the document itself, which bounds the body.
            standalone = _YAML_DOCUMENT_BLOCK.match(code.lstrip(" \t"))
            block_indent = -1 if standalone else indent
            plain_open = False
    return None if quote else kinds


_MAKE_REFERENCE_CLOSE = {"(": ")", "{": "}"}
_MAKE_DEFINE = re.compile(r"^\s*(?:(?:override|export)\s+)*define\b")
_MAKE_ENDEF = re.compile(r"^\s*endef\b")
_MAKE_RECIPE_PREFIX = re.compile(r"\.RECIPEPREFIX\b")


def _lex_make(text: str) -> bytearray | None:
    """Make: ``#`` outside recipes, ``define`` bodies, and ``$(...)`` calls.

    Make does not honor quotes, so a ``#`` in ``VAR = "a # b"`` is a comment,
    but within a variable reference or function call it is literal. Recipe
    lines belong to the shell and are left verbatim, including a recipe after
    ``;`` on a rule line. ``.RECIPEPREFIX`` changes which lines are recipes,
    so a file that sets it is left unmasked.
    """
    if _MAKE_RECIPE_PREFIX.search(text):
        return None
    kinds = bytearray(len(text))
    references: list[str] = []
    defines = 0
    recipe = False
    continued = False
    for start, raw in _lf_lines(text):
        line = raw.removesuffix("\r")
        continues = (len(line) - len(line.rstrip("\\"))) % 2 == 1
        if defines:
            if _MAKE_DEFINE.match(line):
                defines += 1
            elif _MAKE_ENDEF.match(line):
                defines -= 1
            continue
        if not continued:
            references.clear()
            recipe = line.startswith("\t")
            if not recipe and _MAKE_DEFINE.match(line):
                defines = 1
                continue
        continued = continues
        if recipe:
            continue
        cursor = 0
        while cursor < len(line):
            char = line[cursor]
            if char == "\\":
                cursor += 2
                continue
            if char == "$":
                opener = line[cursor + 1 : cursor + 2]
                if opener in _MAKE_REFERENCE_CLOSE:
                    references.append(_MAKE_REFERENCE_CLOSE[opener])
                cursor += 2
                continue
            if references:
                if char == references[-1]:
                    references.pop()
                elif char in _MAKE_REFERENCE_CLOSE:
                    references.append(_MAKE_REFERENCE_CLOSE[char])
            elif char == "#":
                # Make continues a comment across a trailing backslash; the
                # next line is left visible rather than guessed at.
                _mark(kinds, start + cursor, start + len(line), _COMMENT)
                continued = False
                break
            elif char == ";":
                # On a rule line the rest, and its continuations, is recipe.
                recipe = True
                break
            cursor += 1
    return kinds


def _regex_comments(pattern: re.Pattern[str]) -> Callable[[str], bytearray]:
    def lex(text: str) -> bytearray:
        kinds = bytearray(len(text))
        for match in pattern.finditer(text):
            _mark(kinds, match.start(1), match.end(1), _COMMENT)
        return kinds

    return lex


# pip: a ``#`` at the start of a line or after whitespace; no quoting.
_lex_requirements = _regex_comments(re.compile(r"(?:^|(?<=[ \t]))(#[^\n]*)", re.M))
# Consumers disagree about inline ``#`` in dotenv/INI values, so only whole-line
# comments are masked.
_lex_dotenv = _regex_comments(re.compile(r"^[ \t]*(#[^\n]*)", re.M))
_lex_ini = _regex_comments(re.compile(r"^[ \t]*([#;][^\n]*)", re.M))


# --- Comment lines for automatic role matching -------------------------------


class _CommentFamily:
    """Markers that open a comment at the start of a line in some language."""

    __slots__ = ("block", "line", "not_line")

    def __init__(
        self,
        line: tuple[str, ...],
        block: tuple[str, str] | None = None,
        not_line: tuple[str, ...] = (),
    ) -> None:
        self.line = line
        self.block = block
        # A line opener that starts something else: ``#{...}`` interpolation
        # at the start of a Ruby or Elixir heredoc line, a PHP ``#[`` attribute.
        self.not_line = not_line


_SLASH = _CommentFamily(("//",), ("/*", "*/"))
_HASH = _CommentFamily(("#",))
_HASH_INTERPOLATING = _CommentFamily(("#",), not_line=("#{",))
_COMMENT_FAMILIES = {
    "rust": _SLASH,
    "go": _SLASH,
    "c": _SLASH,
    "javascript": _SLASH,
    "typescript": _SLASH,
    "java": _SLASH,
    "kotlin": _SLASH,
    "csharp": _SLASH,
    "dart": _SLASH,
    "zig": _SLASH,
    "swift": _SLASH,
    "scala": _SLASH,
    "groovy": _SLASH,
    "fsharp": _CommentFamily(("//",), ("(*", "*)")),
    "php": _CommentFamily(("//", "#"), ("/*", "*/"), not_line=("#[",)),
    "python": _HASH,
    "shell": _HASH,
    "dockerfile": _HASH,
    "toml": _HASH,
    "yaml": _HASH,
    "make": _HASH,
    "requirements": _HASH,
    "dotenv": _HASH,
    "ini": _CommentFamily(("#", ";")),
}
# Languages the lexers do not cover, by file name or suffix: every executable
# suffix and build file the screener treats as runtime surface, and languages
# that may sit under ``src/``.
_UNLEXED_FAMILY_BY_NAME = {
    "deno.json": _SLASH,
    "deno.jsonc": _SLASH,
    "cmakelists.txt": _HASH,
    "gemfile": _HASH_INTERPOLATING,
    "rakefile": _HASH_INTERPOLATING,
    "pom.xml": _CommentFamily((), ("<!--", "-->")),
}
_UNLEXED_FAMILY_BY_SUFFIX = {
    ".m": _SLASH,
    ".rb": _HASH_INTERPOLATING,
    ".rake": _HASH_INTERPOLATING,
    ".gemspec": _HASH_INTERPOLATING,
    ".ex": _HASH_INTERPOLATING,
    ".exs": _HASH_INTERPOLATING,
    ".cr": _HASH_INTERPOLATING,
    ".pl": _HASH,
    ".pm": _HASH,
    ".r": _HASH,
    ".jl": _HASH,
    ".nim": _HASH,
    ".cmake": _HASH,
    ".lua": _CommentFamily(("--",)),
    ".hs": _CommentFamily(("--",), ("{-", "-}")),
    ".sql": _CommentFamily(("--",), ("/*", "*/")),
    ".erl": _CommentFamily(("%",)),
    ".hrl": _CommentFamily(("%",)),
    ".ml": _CommentFamily((), ("(*", "*)")),
    ".mli": _CommentFamily((), ("(*", "*)")),
    ".clj": _CommentFamily((";",)),
    ".cljs": _CommentFamily((";",)),
    ".el": _CommentFamily((";",)),
}


def _comment_family(path: str) -> _CommentFamily | None:
    language = language_for_path(path)
    if language is not None:
        return _COMMENT_FAMILIES.get(language)
    name = path.rsplit("/", 1)[-1].casefold()
    family = _UNLEXED_FAMILY_BY_NAME.get(name)
    if family is not None:
        return family
    dot = name.rfind(".")
    return _UNLEXED_FAMILY_BY_SUFFIX.get(name[dot:]) if dot > 0 else None


def _blank_comment_lines(text: str, family: _CommentFamily) -> str:
    """Blank whole comment lines, and block comments opened at a line start."""
    pieces: list[str] = []
    closer_after = -1 if family.block is None else text.rfind(family.block[1])
    open_block = False
    position = 0
    for number, line in enumerate(text.splitlines(keepends=True)):
        position += len(line)
        if open_block:
            assert family.block is not None
            close = line.find(family.block[1])
            if close < 0:
                pieces.append(_BLANKABLE.sub(" ", line))
                continue
            open_block = False
            end = close + len(family.block[1])
            pieces.append(_BLANKABLE.sub(" ", line[:end]) + line[end:])
            continue
        head = line.lstrip(" \t")
        if number == 0 and head.startswith("#!"):
            pieces.append(line)
            continue
        if head.startswith(family.line) and not head.startswith(family.not_line):
            pieces.append(_BLANKABLE.sub(" ", line))
            continue
        if family.block is not None and head.startswith(family.block[0]):
            opener, closer = family.block
            offset = len(line) - len(head)
            close = line.find(closer, offset + len(opener))
            if close >= 0:
                end = close + len(closer)
                pieces.append(_BLANKABLE.sub(" ", line[:end]) + line[end:])
            else:
                # Blank on through the first closer; without one, only this line.
                open_block = closer_after >= position
                pieces.append(_BLANKABLE.sub(" ", line))
            continue
        pieces.append(line)
    return "".join(pieces)


_LEXERS: dict[str, Callable[[str], bytearray | None]] = {
    "rust": _lex_rust,
    "go": _lex_go,
    "c": _lex_c,
    "javascript": _lex_javascript,
    "typescript": partial(_lex_javascript, typescript=True),
    "java": _lex_java,
    "kotlin": _lex_kotlin,
    "csharp": _lex_csharp,
    "dart": _lex_dart,
    "php": _lex_php,
    "zig": _lex_zig,
    "swift": _lex_swift,
    "scala": _lex_scala,
    "groovy": _lex_groovy,
    "fsharp": _lex_fsharp,
    "python": _lex_python,
    "shell": _lex_shell,
    "dockerfile": _lex_dockerfile,
    "toml": _lex_toml,
    "yaml": _lex_yaml,
    "make": _lex_make,
    "requirements": _lex_requirements,
    "dotenv": _lex_dotenv,
    "ini": _lex_ini,
}
