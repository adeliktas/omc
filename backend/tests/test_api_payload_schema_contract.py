"""Every `payload.<field>` an API handler reads must exist on its request schema.

Pydantic raises AttributeError on undeclared attribute access, so a handler that
reads a field its schema lacks 500s on *every* call. This has now happened twice
in `provider_capacity.create_provider`: `priority` (fixed 2026-08-25) and `tier`
(added to ProviderRead/ProviderUpdate/the model on 2026-09-01 but not to
ProviderCreate — POST /provider-capacity/providers 500'd until 2026-09-22).

Static check rather than a live POST: it needs no DB, and it covers every handler
in app/api at once instead of the one endpoint a regression test would pin.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from pydantic import BaseModel

API_DIR = Path(__file__).resolve().parents[1] / "app" / "api"


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    module = importlib.import_module(f"app.api.{path.stem}")
    bad: list[str] = []

    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # param name -> pydantic model, for params annotated with a bare class
        # name that resolves to a BaseModel in this module.
        models: dict[str, type[BaseModel]] = {}
        for arg in [*func.args.args, *func.args.posonlyargs, *func.args.kwonlyargs]:
            if not isinstance(arg.annotation, ast.Name):
                continue
            obj = getattr(module, arg.annotation.id, None)
            if isinstance(obj, type) and issubclass(obj, BaseModel):
                models[arg.arg] = obj
        if not models:
            continue

        for node in ast.walk(func):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            model = models.get(node.value.id)
            if model is None or node.attr.startswith(("model_", "_")):
                continue
            if node.attr not in model.model_fields and not hasattr(model, node.attr):
                bad.append(
                    f"{path.name}:{node.lineno} {func.name}() reads "
                    f"{node.value.id}.{node.attr}, missing from {model.__name__}",
                )
    return bad


@pytest.mark.parametrize(
    "path",
    sorted(p for p in API_DIR.glob("*.py") if p.name != "__init__.py"),
    ids=lambda p: p.stem,
)
def test_handlers_only_read_declared_schema_fields(path: Path) -> None:
    assert _violations(path) == []
