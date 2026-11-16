"""The functions a diff edits (conflict_functions.py) and same_function."""

from gerrit_cli.conflict_functions import (
    defined_name, edited_functions, parse_diff, shared_functions,
)
from gerrit_cli.conflicts import Repo
from gerrit_cli.tests.test_graph_conflicts import _check, _git, gerrit, local  # noqa: F401


def _diff(path, *hunks):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n" + "".join(hunks)


def test_definition_lines():
    assert defined_name("static int foo(struct bar *b)") == "foo"
    assert defined_name("ll_file_ioctl(struct file *file, unsigned int cmd,") == "ll_file_ioctl"
    assert defined_name("static inline struct lu_env *env_get(void)") == "env_get"
    assert defined_name("test_12() {") == "test_12"
    assert defined_name("test_12 () {") == "test_12"
    assert defined_name("int foo(int a, int b);") == "foo"
    assert defined_name("int foo(int a, int b);", declarations=False) is None
    assert defined_name("EXPORT_SYMBOL(foo);") is None
    assert defined_name("static DEFINE_MUTEX(lock);") is None
    assert defined_name('run_test 12 "check foo (bar)"') is None
    assert defined_name("static struct ops o = OPS_INIT(x)") is None
    assert defined_name("out:") is None
    assert defined_name("struct foo {") is None


def _edited(files, diff):
    """edited_functions of `diff`, the old text of each path in files."""
    return edited_functions(parse_diff(diff), files)


# line 1 foo, 9 a label, 12 bar, 16 a prototype
OLD_C = "\n".join(["static int foo(void)", "{", "\tint x;", "", "\tx = 1;", "\tx = 2;",
                   "\treturn x;", "}", "out:", "", "", "static int bar(void)", "{",
                   "\treturn 0;", "}", "int qux(int a);", ""]) + "\n"


def test_an_edit_inside_a_function_names_it():
    diff = _diff("a.c", "@@ -5 +5 @@ hostile header text\n-\tx = 1;\n+\tx = 3;\n")
    assert _edited({"a.c": OLD_C}, diff) == {"a.c": {"foo"}}


def test_a_label_in_column_0_does_not_hide_the_function():
    old = "static int foo(void)\n{\n\tx = 1;\nout:\n\tx = 2;\n\treturn x;\n}\n"
    diff = _diff("a.c", "@@ -5 +5 @@ out:\n-\tx = 2;\n+\tx = 3;\n")
    assert _edited({"a.c": old}, diff) == {"a.c": {"foo"}}


def test_an_insertion_after_a_line_looks_up_from_that_line():
    # inserted after line 12, the definition line itself
    diff = _diff("a.c", "@@ -12,0 +13 @@\n+\tint unused;\n")
    assert _edited({"a.c": OLD_C}, diff) == {"a.c": {"bar"}}


def test_a_new_function_after_another_does_not_edit_it():
    diff = _diff("a.c", "@@ -15,0 +16,6 @@\n+\n"
                 "+/* the new one */\n+static int baz(void)\n+{\n+\treturn 0;\n+}\n")
    assert _edited({"a.c": OLD_C}, diff) == {"a.c": {"baz"}}


def test_a_hunk_that_edits_the_tail_and_adds_a_function_names_both():
    diff = _diff("a.c", "@@ -14 +14,3 @@\n-\treturn 0;\n"
                 "+\treturn 2;\n+}\n+static int baz(void)\n")
    assert _edited({"a.c": OLD_C}, diff) == {"a.c": {"bar", "baz"}}


def test_a_changed_prototype_names_the_function():
    diff = _diff("a.c", "@@ -16 +16 @@\n-int qux(int a);\n+int qux(int a, int b);\n")
    assert _edited({"a.c": OLD_C}, diff) == {"a.c": {"qux"}}
    # after a prototype is between functions, not in one
    diff = _diff("a.c", "@@ -16,0 +17 @@\n+#define FOO 1\n")
    assert _edited({"a.c": OLD_C}, diff) == {}
    diff = _diff("a.h", "@@ -3,2 +3,2 @@\n-int foo(int a,\n-\t int b);\n"
                 "+int foo(long a,\n+\t int b);\n")
    assert _edited({}, diff) == {"a.h": {"foo"}}


OLD_SH = "\n".join(["test_1() {", "\techo a", "}", 'run_test 1 "one"', "",
                    "if (( $MDS1_VERSION > 1 )); then", "\techo b", "fi", ""]) + "\n"


def test_shell_tests_keywords_and_several_files():
    diff = (_diff("t.sh", "@@ -2 +2 @@\n-\techo a\n+\techo b\n",
                  "@@ -4,0 +5,3 @@\n+\n+test_1a() {\n+}\n",
                  "@@ -7 +7 @@\n-\techo b\n+\techo c\n")
            + _diff("b.c", "@@ -1 +1,0 @@\n-int x;\n\\ No newline at end of file\n"))
    assert _edited({"t.sh": OLD_SH, "b.c": "int x;"}, diff) == {"t.sh": {"test_1", "test_1a"}}


def test_keywords_and_upper_case_macros_are_not_functions():
    for line in ("if (( a > 1 )); then", "while (true); do", "for ((i = 0; i < 3; i++)); do",
                 "WRAP_FILLDIR_FN(ll_, foo)", "LUSTRE_RW_ATTR(max_read_ahead_mb);",
                 "TGT_OBD_HDL(0, OBD_PING, tgt_obd_ping),", "LU_CONTEXT_KEY_DEFINE(vvp, LCT_CL)",
                 "return (x);", "switch (a) {"):
        assert defined_name(line) is None, line
    assert defined_name("LNetGet(lnet_nid_t self)") == "LNetGet"


def test_context_lines_and_quoted_paths():
    # interHunkContext merges hunks with context lines between them
    diff = ('--- "a/x\\ty.c"\n+++ "b/x\\ty.c"\n'
            "@@ -5,3 +5,3 @@\n-\tx = 1;\n \tx = 2;\n-\treturn x;\n+\tx = 3;\n+\treturn 1;\n")
    [h] = parse_diff(diff)
    assert (h.path, h.removed, h.added) == ("x\ty.c", ["\tx = 1;", "\treturn x;"],
                                            ["\tx = 3;", "\treturn 1;"])


def test_shared_functions_per_file():
    ours = {"a.c": {"foo", "bar"}, "b.c": {"baz"}}
    theirs = {"a.c": {"bar", "qux"}, "b.c": {"other"}, "c.c": {"foo"}}
    assert shared_functions(ours, theirs) == [{"file": "a.c", "functions": ["bar"]}]


C_FILE = "\n".join([
    "static int foo(int a)",       # 1
    "{",
    "\tint x = 0;",                # 3
    "",
    "\tif (a)",
    "\t\tgoto out;",
    "\tx = 1;",
    "\tx = 2;",
    "\tx = 3;",
    "out:",                        # 10, a label in column 0
    "\tx = 4;",
    "\tx = 5;",
    "\tx = 6;",
    "\treturn x;",                 # 14
    "}",
    "",
    "static int bar(void)",
    "{",
    "\treturn 0;",
    "}",
]) + "\n"


def _edit(lines: dict[int, str]) -> str:
    out = C_FILE.split("\n")
    for n, text in lines.items():
        out[n - 1] = text
    return "\n".join(out)


def test_a_label_in_column_0_still_names_the_function(gerrit, local):
    base = gerrit.advance({"f.c": C_FILE})
    t = gerrit.change(1, base, {"f.c": _edit({13: "\tx = 7;"})})
    _git(local, "fetch", "-q", str(gerrit.path), "+refs/heads/*:refs/remotes/o/*",
         "+refs/changes/*:refs/remotes/c/*")
    with Repo(local) as repo:
        assert repo.functions(t.commit, base) == {"f.c": {"foo"}}


def test_a_clean_merge_editing_the_same_function_carries_same_function(gerrit, local):
    base = gerrit.advance({"f.c": C_FILE})
    t = gerrit.change(1, base, {"f.c": _edit({3: "\tint x = 9;"})})
    c = gerrit.change(2, base, {"f.c": _edit({14: "\treturn -x;"})})
    d = gerrit.change(3, base, {"f.c": _edit({19: "\treturn 1;"})})
    r = _check(gerrit, local, [t, c, d], focus={1})
    pairs = {p["other"]: p for p in r["focus_pairs"]}
    assert pairs[2]["landing"]["target_first"] == "clean"
    assert pairs[2]["same_function"] == [{"file": "f.c", "functions": ["foo"]}]
    assert pairs[3]["landing"]["target_first"] == "clean"
    assert "same_function" not in pairs[3]


def test_a_forward_declaration_against_a_signature_change(gerrit, local):
    base = gerrit.advance({"f.c": C_FILE})
    t = gerrit.change(1, base, {"f.c": _edit({17: "static int bar(int a)"})})
    c = gerrit.change(2, base, {"f.c": "static int bar(void);\n\n" + C_FILE})
    r = _check(gerrit, local, [t, c], focus={1})
    [pair] = r["focus_pairs"]
    assert pair["landing"]["target_first"] == "clean"
    assert pair["same_function"] == [{"file": "f.c", "functions": ["bar"]}]


def test_the_users_diff_configuration_changes_nothing(gerrit, local):
    base = gerrit.advance({"f.c": C_FILE})
    t = gerrit.change(1, base, {"f.c": _edit({11: "\tx = 8;", 13: "\tx = 7;",
                                             19: "\treturn 2;"})})
    _git(local, "fetch", "-q", str(gerrit.path), "+refs/heads/*:refs/remotes/o/*",
         "+refs/changes/*:refs/remotes/c/*")
    for key, value in (("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"),
                       ("diff.cpp.xfuncname", "^zzz"), ("diff.default.xfuncname", "^zzz"),
                       ("diff.interHunkContext", "10"), ("diff.external", "false"),
                       ("diff.suppressBlankEmpty", "true"), ("core.quotePath", "true")):
        _git(local, "config", key, value)
    info = local / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("*.c diff=cpp\n")
    (local / ".gitattributes").write_text("*.c diff=zzz\n")
    _git(local, "config", "diff.zzz.xfuncname", "^zzz")
    with Repo(local) as repo:
        assert repo.functions(t.commit, base) == {"f.c": {"foo", "bar"}}
