from emend.editor_search import EditorSearchEngine
from emend.transform import warm_caches


def _make_engine(tmp_path, filename, code):
    """Helper: write code to file, warm caches, return (engine, file_path)."""
    file_path = tmp_path / filename
    file_path.write_text(code.strip())
    (tmp_path / ".emend/cache").mkdir(parents=True, exist_ok=True)
    warm_caches(str(tmp_path))
    engine = EditorSearchEngine(str(tmp_path))
    return engine, file_path


def _assert_goto(engine, file_path, line, col, expected_name, expected_line):
    """Helper: assert goto_local returns expected result."""
    res = engine.goto_local(str(file_path), line=line, col=col)
    assert len(res.items) >= 1, (
        f"goto_local({line}, {col}) returned no results, "
        f"expected {expected_name} at line {expected_line}"
    )
    assert res.items[0]["name"] == expected_name, (
        f"goto_local({line}, {col}) returned name={res.items[0]['name']!r}, "
        f"expected {expected_name!r}"
    )
    assert res.items[0]["line"] == expected_line, (
        f"goto_local({line}, {col}) returned line={res.items[0]['line']}, "
        f"expected {expected_line}"
    )


# -- Basic tests --


def test_goto_local_python(tmp_path):
    """Basic goto: local variable and parameter."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def foo(x):
    y = x + 1
    return y

z = foo(10)
""")
    # 'y' in return -> definition at line 2
    _assert_goto(engine, fp, line=3, col=12, expected_name="y", expected_line=2)
    # 'x' usage at line 2 -> parameter at line 1
    _assert_goto(engine, fp, line=2, col=9, expected_name="x", expected_line=1)
    engine.close()


def test_goto_local_ts(tmp_path):
    """Basic goto in TypeScript."""
    engine, fp = _make_engine(tmp_path, "test.ts", """
function greet(name: string) {
    console.log(name);
}
""")
    # 'name' usage at line 2 -> parameter at line 1
    _assert_goto(engine, fp, line=2, col=17, expected_name="name", expected_line=1)
    engine.close()


# -- Previously skipped tests (line numbers fixed) --


def test_goto_chained_assignment(tmp_path):
    """Goto through chained assignment (y = x = 10)."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def func():
    y = x = 10
    return y
""")
    _assert_goto(engine, fp, line=3, col=12, expected_name="y", expected_line=2)
    engine.close()


def test_goto_multiple_statements_on_line(tmp_path):
    """Goto with semicolon-separated statements on one line."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def func():
    x = 1; y = x + 1
    return y
""")
    _assert_goto(engine, fp, line=3, col=12, expected_name="y", expected_line=2)
    engine.close()


def test_goto_whitespace_before_identifier(tmp_path):
    """Cursor on whitespace just before an identifier should resolve it."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def foo(x):
    y = x + 1
    return y
""")
    # '    return y' - col 11 is space before 'y', col 12 is on 'y', col 13 is past 'y'
    _assert_goto(engine, fp, line=3, col=11, expected_name="y", expected_line=2)
    _assert_goto(engine, fp, line=3, col=12, expected_name="y", expected_line=2)
    _assert_goto(engine, fp, line=3, col=13, expected_name="y", expected_line=2)
    engine.close()


def test_goto_local_shadows_import(tmp_path):
    """Local variable shadowing an import should resolve to local definition."""
    engine, fp = _make_engine(tmp_path, "test.py", """
import math

def compute():
    math = 42
    return math
""")
    # 'math' in return -> local assignment at line 4, not import at line 1
    _assert_goto(engine, fp, line=5, col=12, expected_name="math", expected_line=4)
    engine.close()


# -- Edge case stress tests --


def test_goto_for_loop_variable(tmp_path):
    """Goto for-loop iteration variable."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def process(items):
    for item in items:
        print(item)
""")
    # 'item' at line 3 -> for-loop binding at line 2
    _assert_goto(engine, fp, line=3, col=15, expected_name="item", expected_line=2)
    engine.close()


def test_goto_comprehension_result(tmp_path):
    """Goto variable assigned from a list comprehension."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def transform(data):
    result = [x * 2 for x in data]
    return result
""")
    _assert_goto(engine, fp, line=3, col=12, expected_name="result", expected_line=2)
    engine.close()


def test_goto_walrus_operator(tmp_path):
    """Goto variable defined via walrus operator (:=)."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def check(data):
    if (n := len(data)) > 10:
        return n
""")
    _assert_goto(engine, fp, line=3, col=16, expected_name="n", expected_line=2)
    engine.close()


def test_goto_closure_variable(tmp_path):
    """Goto variable from enclosing (closure) scope."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def outer():
    x = 10
    def inner():
        return x
    return inner()
""")
    # 'x' in inner() at line 4 -> definition at line 2
    _assert_goto(engine, fp, line=4, col=16, expected_name="x", expected_line=2)
    engine.close()


def test_goto_self_parameter(tmp_path):
    """Goto 'self' parameter in method."""
    engine, fp = _make_engine(tmp_path, "test.py", """
class MyClass:
    def method(self, value):
        self.data = value
        return self.data
""")
    # 'self' at line 3 -> parameter at line 2
    _assert_goto(engine, fp, line=3, col=9, expected_name="self", expected_line=2)
    # 'value' at line 3 -> parameter at line 2
    _assert_goto(engine, fp, line=3, col=21, expected_name="value", expected_line=2)
    engine.close()


def test_goto_unpacking_assignment(tmp_path):
    """Goto variables from tuple unpacking."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def unpack():
    a, b = 1, 2
    return a + b
""")
    # 'a' at line 3 col 12 -> definition at line 2
    _assert_goto(engine, fp, line=3, col=12, expected_name="a", expected_line=2)
    # 'b' at line 3 col 16 -> definition at line 2
    _assert_goto(engine, fp, line=3, col=16, expected_name="b", expected_line=2)
    engine.close()


def test_goto_decorator_parameter(tmp_path):
    """Goto parameter used inside decorated inner function."""
    engine, fp = _make_engine(tmp_path, "test.py", """
import functools
def my_decorator(func):
    @functools.wraps(func)
    def wrapper(*args):
        return func(*args)
    return wrapper
""")
    # 'func' at line 5 -> parameter at line 2
    _assert_goto(engine, fp, line=5, col=16, expected_name="func", expected_line=2)
    engine.close()


def test_goto_try_except_variable(tmp_path):
    """Goto variable defined inside try block, used after."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def safe_call():
    try:
        x = risky()
    except ValueError as e:
        print(e)
    return x
""")
    # 'x' at line 6 -> definition at line 3
    _assert_goto(engine, fp, line=6, col=12, expected_name="x", expected_line=3)
    engine.close()


def test_goto_global_scope(tmp_path):
    """Goto variable at module level."""
    engine, fp = _make_engine(tmp_path, "test.py", """
x = 10
y = x + 1
""")
    _assert_goto(engine, fp, line=2, col=5, expected_name="x", expected_line=1)
    engine.close()


def test_goto_type_annotated_variable(tmp_path):
    """Goto variable with type annotation."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def foo(x: int) -> str:
    result: str = str(x)
    return result
""")
    _assert_goto(engine, fp, line=3, col=12, expected_name="result", expected_line=2)
    engine.close()


def test_goto_default_parameter(tmp_path):
    """Goto parameter with default value."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def greet(name='world'):
    return name
""")
    _assert_goto(engine, fp, line=2, col=12, expected_name="name", expected_line=1)
    engine.close()


def test_goto_args_kwargs(tmp_path):
    """Goto *args and **kwargs parameters."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def func(*args, **kwargs):
    print(args)
    print(kwargs)
""")
    _assert_goto(engine, fp, line=2, col=11, expected_name="args", expected_line=1)
    _assert_goto(engine, fp, line=3, col=11, expected_name="kwargs", expected_line=1)
    engine.close()


def test_goto_lambda_variable(tmp_path):
    """Goto variable assigned from lambda."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def outer():
    fn = lambda x: x + 1
    return fn(5)
""")
    _assert_goto(engine, fp, line=3, col=12, expected_name="fn", expected_line=2)
    engine.close()


def test_goto_multiline_expression(tmp_path):
    """Goto variable assigned via multiline expression."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def compute():
    result = (
        1 + 2 +
        3 + 4
    )
    return result
""")
    _assert_goto(engine, fp, line=6, col=12, expected_name="result", expected_line=2)
    engine.close()


def test_goto_augmented_assignment(tmp_path):
    """Goto variable that was augmented (+=)."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def count():
    total = 0
    total += 1
    return total
""")
    # 'total' in return -> first assignment at line 2
    _assert_goto(engine, fp, line=4, col=12, expected_name="total", expected_line=2)
    engine.close()


def test_goto_multiple_functions(tmp_path):
    """Goto distinguishes same-named variables in different functions."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def foo():
    x = 1
    return x

def bar():
    x = 2
    return x
""")
    # 'x' in foo's return -> line 2
    _assert_goto(engine, fp, line=3, col=12, expected_name="x", expected_line=2)
    # 'x' in bar's return -> line 6
    _assert_goto(engine, fp, line=7, col=12, expected_name="x", expected_line=6)
    engine.close()


def test_goto_fstring_variable(tmp_path):
    """Goto variable used in f-string, resolved via the outer assignment."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def greet(name):
    msg = f'Hello {name}!'
    return msg
""")
    _assert_goto(engine, fp, line=3, col=12, expected_name="msg", expected_line=2)
    engine.close()


def test_goto_nested_class_method(tmp_path):
    """Goto method parameter in nested class."""
    engine, fp = _make_engine(tmp_path, "test.py", """
class Outer:
    class Inner:
        def method(self, val):
            return val
""")
    _assert_goto(engine, fp, line=4, col=20, expected_name="val", expected_line=3)
    engine.close()


def test_goto_star_unpacking(tmp_path):
    """Goto starred variable in unpacking."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def func():
    first, *rest = [1, 2, 3, 4]
    return rest
""")
    _assert_goto(engine, fp, line=3, col=12, expected_name="rest", expected_line=2)
    engine.close()


def test_goto_conditional_assignment(tmp_path):
    """Goto variable defined in both branches of if/else."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def func(flag):
    if flag:
        val = 1
    else:
        val = 2
    return val
""")
    # 'val' in return -> first definition at line 3
    _assert_goto(engine, fp, line=6, col=12, expected_name="val", expected_line=3)
    engine.close()


def test_goto_generator_expression(tmp_path):
    """Goto variable assigned from generator expression."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def func(data):
    total = sum(x for x in data)
    return total
""")
    _assert_goto(engine, fp, line=3, col=12, expected_name="total", expected_line=2)
    engine.close()


def test_goto_multiline_function_params(tmp_path):
    """Goto parameter in function with multiline signature."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def long_func(
    alpha,
    beta,
    gamma,
):
    return alpha + beta + gamma
""")
    # 'alpha' at line 6 -> definition at line 2
    _assert_goto(engine, fp, line=6, col=12, expected_name="alpha", expected_line=2)
    # 'gamma' at line 6 -> definition at line 4
    _assert_goto(engine, fp, line=6, col=28, expected_name="gamma", expected_line=4)
    engine.close()


def test_goto_with_as_variable(tmp_path):
    """Goto context manager variable from 'with ... as fh:'."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def read_file():
    with open('f') as fh:
        data = fh.read()
    return data
""")
    # 'fh' at line 3 -> 'with ... as fh' at line 2
    _assert_goto(engine, fp, line=3, col=16, expected_name="fh", expected_line=2)
    engine.close()


def test_goto_except_as_variable(tmp_path):
    """Goto exception variable from 'except ... as e:'."""
    engine, fp = _make_engine(tmp_path, "test.py", """
def handler():
    try:
        pass
    except ValueError as e:
        print(e)
""")
    # 'e' at line 5 -> 'except ... as e' at line 4
    _assert_goto(engine, fp, line=5, col=15, expected_name="e", expected_line=4)
    engine.close()
