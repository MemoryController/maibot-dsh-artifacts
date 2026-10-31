"""Load unchanged pinned source methods for isolated media component checks.

Full ComponentQueryService/MaisakaReasoningEngine imports boot most bot services.
The harness compiles selected method AST nodes without altering their bodies or
decorators, using the real lightweight Host data types. This is not a full Host
module import, dispatcher or Planner test. The caller verifies the checkout SHA.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def source_class(source: Path, class_name: str, methods: tuple[str, ...], namespace: dict[str, Any]) -> type:
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    chosen = [
        node for node in original.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in methods
    ]
    assert {node.name for node in chosen} == set(methods), "Pinned official methods changed"
    isolated = ast.ClassDef(name=class_name, bases=[], keywords=[], body=chosen, decorator_list=[], type_params=[])
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    compiled = ast.fix_missing_locations(ast.Module(body=[future, isolated], type_ignores=[]))
    # Retain line positions and original file name so errors identify real source.
    exec(compile(compiled, str(source), "exec"), namespace)
    loaded = namespace[class_name]
    # No hidden fallback implementation or normalized function signature.
    for method in methods:
        descriptor = loaded.__dict__[method]
        function = descriptor.__func__ if isinstance(descriptor, (staticmethod, classmethod)) else descriptor
        assert function.__code__.co_filename == str(source)
    return loaded
