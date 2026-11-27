"""Taint lives on an access path, not on a whole object.

``box.raw = tainted`` taints ``box.raw``; it does not taint ``box`` or
``box.other``.  The attribute keeps its taint when the object crosses a call:
as the value a method is called on, as an argument, as a return value, and
back out of a method that stored into it.
"""
from emend.lint import LintRule, run_lint

BOX = '''class Box:
    def __init__(self, raw=None):
        self.raw = raw
        self.other = "fixed"

    def value(self):
        return self.raw

    def constant(self):
        return "fixed"

    def store(self, item):
        self.raw = item

    def store_through_helper(self, item):
        self._set(item)

    def _set(self, item):
        self.raw = item

    def value_through_helper(self):
        return self.value()


def read_raw(box):
    return box.raw


def fill(box, item):
    box.raw = item


def make_tainted():
    made = Box()
    made.raw = input("x")
    return made

'''


def _reported(tmp_path, body, *, not_through=None, prelude=BOX, name="app.py",
              flows_from="input($X)", flows_to="eval($CODE)", language=None):
    """Whether the sink on the last line of ``body`` is reported."""
    source = prelude + "def handler():\n" + "".join(
        f"    {line}\n" for line in body.strip().splitlines()
    )
    path = tmp_path / name
    path.write_text(source)
    rule = LintRule(
        name="flow", find="", message="tainted", flows_from=flows_from,
        flows_to=flows_to, not_through=not_through,
    )
    kwargs = {"language": language} if language else {}
    lines = {violation.line for violation in run_lint([rule], [str(path)], **kwargs)}
    return source.count("\n") in lines


# ---------------------------------------------------------------------------
# Within one function
# ---------------------------------------------------------------------------

def test_attribute_taint_stays_on_the_attribute(tmp_path):
    assert _reported(tmp_path, "box = make()\nbox.raw = input('x')\neval(box.raw)")
    assert not _reported(tmp_path, "box = make()\nbox.raw = input('x')\neval(box.other)")
    assert not _reported(tmp_path, "box = make()\nbox.raw = input('x')\neval(box)")


def test_a_tainted_object_taints_what_is_read_from_it(tmp_path):
    assert _reported(tmp_path, "box = input('x')\neval(box.anything)")
    assert _reported(tmp_path, "box = input('x')\neval(box.inner.anything)")


def test_taint_reaches_a_read_below_the_tainted_attribute(tmp_path):
    assert _reported(tmp_path, "box = make()\nbox.raw = input('x')\neval(box.raw.strip())")
    assert _reported(tmp_path, "box = make()\nbox.raw = input('x')\neval(box.raw.name)")


def test_an_opaque_reader_of_the_object_is_not_reported(tmp_path):
    """Nothing says ``render`` reads ``raw``; reporting it is opt-in."""
    assert not _reported(tmp_path, "box = make()\nbox.raw = input('x')\neval(box.render())")
    assert not _reported(tmp_path, "box = make()\nbox.raw = input('x')\neval(render(box))")


def test_attribute_taint_follows_an_alias_of_the_object(tmp_path):
    assert _reported(
        tmp_path, "box = make()\nbox.raw = input('x')\nsame = box\neval(same.raw)")
    assert not _reported(
        tmp_path, "box = make()\nbox.raw = input('x')\nsame = box\neval(same.other)")


def test_an_instance_attribute_is_not_a_local_of_the_same_name(tmp_path):
    """``self.raw`` was recorded as the variable ``raw``, so storing a clean
    value in the attribute cleaned a tainted local, and the reverse."""
    prelude = (
        "class Holder:\n"
        "    def clean_attribute(self):\n"
        "        raw = input('x')\n"
        "        self.raw = 'fixed'\n"
        "        eval(raw)\n"
        "\n"
        "    def tainted_attribute(self):\n"
        "        raw = 'fixed'\n"
        "        self.raw = input('x')\n"
        "        eval(raw)\n"
        "        eval(self.raw)\n"
    )
    path = tmp_path / "holder.py"
    path.write_text(prelude)
    rule = LintRule(name="flow", find="", message="tainted",
                    flows_from="input($X)", flows_to="eval($CODE)")
    assert sorted(v.line for v in run_lint([rule], [str(path)])) == [5, 11]


# ---------------------------------------------------------------------------
# Across a call
# ---------------------------------------------------------------------------

def test_attribute_taint_enters_a_method_called_on_the_object(tmp_path):
    assert _reported(tmp_path, "b = Box()\nb.raw = input('x')\neval(b.value())")
    assert not _reported(tmp_path, "b = Box()\nb.raw = input('x')\neval(b.constant())")
    assert _reported(
        tmp_path, "b = Box()\nb.raw = input('x')\neval(b.value_through_helper())")


def test_attribute_taint_enters_a_function_given_the_object(tmp_path):
    assert _reported(tmp_path, "b = Box()\nb.raw = input('x')\neval(read_raw(b))")
    assert not _reported(tmp_path, "b = Box()\nb.other = input('x')\neval(read_raw(b))")


def test_a_method_that_stores_taints_the_attribute_for_the_caller(tmp_path):
    assert _reported(tmp_path, "b = Box()\nb.store(input('x'))\neval(b.value())")
    assert _reported(tmp_path, "b = Box()\nb.store(input('x'))\neval(b.raw)")
    assert not _reported(tmp_path, "b = Box()\nb.store(input('x'))\neval(b.other)")
    assert not _reported(tmp_path, "b = Box()\nb.store(input('x'))\neval(b)")
    assert _reported(
        tmp_path, "b = Box()\nb.store_through_helper(input('x'))\neval(b.value())")


def test_a_function_that_stores_taints_the_attribute_for_the_caller(tmp_path):
    assert _reported(tmp_path, "b = Box()\nfill(b, input('x'))\neval(b.raw)")
    assert not _reported(tmp_path, "b = Box()\nfill(b, input('x'))\neval(b.other)")


def test_a_store_on_one_object_does_not_taint_another(tmp_path):
    assert not _reported(
        tmp_path,
        "a = Box()\nb = Box()\na.store(input('x'))\neval(b.value())")
    assert not _reported(
        tmp_path,
        "a = Box()\nb = Box()\na.raw = input('x')\nb.value()\neval(b.value())")


def test_a_returned_object_keeps_its_tainted_attribute(tmp_path):
    assert _reported(tmp_path, "got = make_tainted()\neval(got.raw)")
    assert _reported(tmp_path, "got = make_tainted()\neval(got.value())")
    assert not _reported(tmp_path, "got = make_tainted()\neval(got.other)")


def test_methods_of_one_object_share_its_attributes(tmp_path):
    prelude = (
        "class View:\n"
        "    def value(self):\n"
        "        return self.raw\n"
        "\n"
        "    def constant(self):\n"
        "        return 'fixed'\n"
        "\n"
        "    def tainted(self):\n"
        "        self.raw = input('x')\n"
        "        eval(self.value())\n"
        "\n"
        "    def clean(self):\n"
        "        self.raw = input('x')\n"
        "        eval(self.constant())\n"
    )
    path = tmp_path / "view.py"
    path.write_text(prelude)
    rule = LintRule(name="flow", find="", message="tainted",
                    flows_from="input($X)", flows_to="eval($CODE)")
    assert sorted(v.line for v in run_lint([rule], [str(path)])) == [10]


def test_a_sanitized_attribute_is_clean_in_the_method_that_reads_it(tmp_path):
    body = "b = Box()\nb.raw = escape(input('x'))\neval(b.value())"
    assert _reported(tmp_path, body)
    assert not _reported(
        tmp_path, body, not_through=[{"pattern": "escape($X)", "effect": "returns"}])


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------

def test_a_constructor_taints_only_the_attributes_it_stores_into(tmp_path):
    assert _reported(tmp_path, "b = Box(input('x'))\neval(b.value())")
    assert _reported(tmp_path, "b = Box(input('x'))\neval(b.raw)")
    assert not _reported(tmp_path, "b = Box(input('x'))\neval(b.constant())")
    assert not _reported(tmp_path, "b = Box(input('x'))\neval(b.other)")


def test_an_opaque_constructor_taints_the_whole_object(tmp_path):
    """Without an ``__init__`` to read, the taint cannot be placed."""
    assert _reported(tmp_path, "b = Elsewhere(input('x'))\neval(b.anything)")
    assert _reported(tmp_path, "b = Elsewhere(input('x'))\neval(b)")


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

def test_an_element_store_taints_the_container_read_whole(tmp_path):
    assert _reported(tmp_path, "d = {}\nd['k'] = input('x')\neval(d)")
    assert _reported(tmp_path, "d = {}\nd['k'] = input('x')\neval(d['k'])")
    assert not _reported(tmp_path, "d = {}\nd['k'] = input('x')\neval(d['j'])")
    assert _reported(tmp_path, "xs = []\nxs.append(input('x'))\neval(xs)")


def test_a_container_held_in_an_attribute(tmp_path):
    assert _reported(
        tmp_path, "b = Box()\nb.items = []\nb.items.append(input('x'))\neval(b.items)")
    assert not _reported(
        tmp_path, "b = Box()\nb.items = []\nb.items.append(input('x'))\neval(b.other)")
    assert _reported(
        tmp_path, "b = Box()\nb.params = {}\nb.params['q'] = input('x')\neval(b.params)")
    assert not _reported(
        tmp_path, "b = Box()\nb.params = {}\nb.params['q'] = input('x')\neval(b.other)")


def test_a_new_object_has_none_of_the_old_ones_stores(tmp_path):
    """Rebinding the name starts a new object; a store into the old one
    kept reaching reads through the new one."""
    assert not _reported(
        tmp_path, "box = make()\nbox.raw = input('x')\nbox = make()\neval(box.raw)")
    assert not _reported(
        tmp_path, "box = make()\nbox.raw = input('x')\nbox = make()\neval(read_raw(box))")
    assert _reported(
        tmp_path, "box = make()\nbox.raw = input('x')\nkept = box\nbox = make()\neval(kept.raw)")
