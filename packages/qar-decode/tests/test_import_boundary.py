"""The architectural rule, enforced.

qar_decode must never import a cloud SDK. The moment it does, the decoder
stops being portable and stops being testable without an AWS account -- and
the portability argument that justified every compute choice evaporates.

This test is cheap. Deleting it is not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BANNED = {
    "boto3",
    "botocore",
    "pyiceberg",
    "dagster",
    "awswrangler",
    "s3fs",
    "duckdb",
}

SRC = Path(__file__).resolve().parents[1] / "src" / "qar_decode"


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: p.name)
def test_no_cloud_imports(path: Path) -> None:
    offending = _imported_roots(path) & BANNED
    assert not offending, (
        f"{path.relative_to(SRC)} imports {sorted(offending)}. "
        "qar_decode must stay free of cloud dependencies."
    )
