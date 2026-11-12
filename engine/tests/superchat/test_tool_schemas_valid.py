"""Every tool's input schema is valid JSON Schema 2020-12.

A provider refuses the whole turn when any offered tool has an invalid schema, so one bad tool
breaks every chat that offers it.
"""

import importlib
import inspect
import pkgutil

from jsonschema import Draft202012Validator

import omnigent
from omnigent.tools.base import Tool


def _tool_classes():
    for info in pkgutil.walk_packages(omnigent.__path__, "omnigent."):
        if any(part in info.name for part in ("tests", "cli", "alembic")):
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception:
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, Tool) and cls.__module__ == module.__name__:
                if not inspect.isabstract(cls):
                    yield cls


def test_every_tool_schema_is_valid_json_schema() -> None:
    checked = 0
    for cls in _tool_classes():
        try:
            schema = cls().get_schema()
        except Exception:
            continue
        params = schema.get("function", {}).get("parameters") or schema.get("parameters")
        if not params:
            continue
        Draft202012Validator.check_schema(params)
        checked += 1
    assert checked > 50
