"""Check built distributions without importing emend from the checkout."""

import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile

LANGUAGES = {"css", "datalog", "html", "jinja2", "python", "rust", "sql", "typescript"}
SMOKE = '''
from pathlib import Path
import sys
import emend
from emend.language_registry import load_config
from emend.duplicate import query_duplicates

assert Path(emend.__file__).resolve().parent == Path(sys.argv[1]) / "emend"
for language in sys.argv[2:]:
    assert load_config(language)["language"]["name"] == language
source = "def compute(value):\\n    a = value + 17\\n    b = a * 23\\n    c = b - 41\\n    d = c / 7\\n    return d\\n"
for name in ("one.py", "two.py"):
    Path(name).write_text(source)
for mode in ("exact", "sequence"):
    assert query_duplicates(".", mode=mode)
'''


def main(wheel, sdist):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        package, project = root / "wheel", root / "project"
        project.mkdir()
        with zipfile.ZipFile(wheel) as archive:
            expected = {f"emend/languages/{lang}/config.toml" for lang in LANGUAGES}
            assert expected <= set(archive.namelist()), "wheel missing language configs"
            archive.extractall(package)
        subprocess.run(
            [sys.executable, "-c", SMOKE, str(package), *sorted(LANGUAGES)],
            cwd=project, env={**os.environ, "PYTHONPATH": str(package)}, check=True,
        )
        with tarfile.open(sdist) as archive:
            names = archive.getnames()
            prefix = names[0].split("/")[0]
            expected = {f"{prefix}/src/emend/languages/{lang}/config.toml" for lang in LANGUAGES}
            assert expected <= set(names), "sdist missing language configs"
            archive.extractall(root / "sdist", filter="data")
        source = root / "sdist" / prefix
        # Reused Cargo targets can hide missing include_str! inputs when tarball
        # timestamps are older than existing build artifacts.
        (source / "rust/src/scope.rs").touch()
        subprocess.run(
            [sys.executable, "-m", "maturin", "build", "--interpreter", sys.executable,
             "--out", str(root / "rebuilt")], cwd=source, check=True,
        )


if __name__ == "__main__":
    main(*sys.argv[1:])
