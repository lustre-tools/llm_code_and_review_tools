"""The functions a diff edits (conflict_functions.py) and same_function."""

from gerrit_cli.conflict_functions import defined_name, edited_functions, shared_functions
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


def test_an_edit_inside_a_function_names_it():
    diff = _diff("a.c", "@@ -10 +10 @@ static int foo(void)\n-\tx = 1;\n+\tx = 2;\n")
    assert edited_functions(diff) == {"a.c": {"foo"}}


def test_a_new_function_after_another_does_not_edit_it():
    diff = _diff("a.c", "@@ -20,0 +21,6 @@ static int foo(void)\n+\n"
                 "+/* the new one */\n+static int bar(void)\n+{\n+\treturn 0;\n+}\n")
    assert edited_functions(diff) == {"a.c": {"bar"}}


def test_a_hunk_that_edits_the_tail_and_adds_a_function_names_both():
    diff = _diff("a.c", "@@ -20 +20,3 @@ static int foo(void)\n-\treturn 1;\n"
                 "+\treturn 2;\n+}\n+static int bar(void)\n")
    assert edited_functions(diff) == {"a.c": {"foo", "bar"}}


def test_a_changed_prototype_names_the_function():
    diff = _diff("a.h", "@@ -3 +3 @@ struct x {\n-int foo(int a);\n+int foo(int a, int b);\n")
    assert edited_functions(diff) == {"a.h": {"foo"}}
    diff = _diff("a.h", "@@ -4,0 +5 @@ int foo(int a);\n+#define FOO 1\n")
    assert edited_functions(diff) == {}
    diff = _diff("a.h", "@@ -3,2 +3,2 @@\n-int foo(int a,\n-\t int b);\n"
                 "+int foo(long a,\n+\t int b);\n")
    assert edited_functions(diff) == {"a.h": {"foo"}}


def test_shell_tests_and_several_files():
    diff = (_diff("t.sh", "@@ -5 +5 @@ test_1() {\n-\techo a\n+\techo b\n",
                  "@@ -9,0 +10,3 @@ run_test 1 \"one\"\n+\n+test_1a() {\n+}\n")
            + _diff("b.c", "@@ -1 +1,0 @@\n-int x;\n\\ No newline at end of file\n"))
    assert edited_functions(diff) == {"t.sh": {"test_1", "test_1a"}}


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
