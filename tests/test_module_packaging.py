"""Release packaging must include every repository module imported by the server."""
import ast
import re
from pathlib import Path


def test_server_local_imports_are_listed_in_py_modules():
    root = Path(__file__).resolve().parents[1]
    config = (root / "pyproject.toml").read_text()
    listed = set(ast.literal_eval(re.search(r"(?ms)^py-modules\s*=\s*(\[.*?^\])", config)[1]))
    imports = set()
    for node in ast.walk(ast.parse((root / "server.py").read_text())):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imports.add(node.module.split(".")[0])
    local = {name for name in imports if (root / (name + ".py")).is_file()}
    assert local - listed == set(), f"Missing packaged server imports: {sorted(local - listed)}"
