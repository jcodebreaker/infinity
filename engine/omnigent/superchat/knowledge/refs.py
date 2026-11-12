"""References a passage carries: the files, artifacts, Project and Helpers its work touched.

A journal line carries them in its trailing comment, after the chat id, as ``key:value,value``
pairs (``<!-- conv_1 files:a.xlsx,b.md artifacts:art_7f2@v3 project:spring-launch -->``); a
conversation passage takes them from its own turn's tool calls. The index stores them as passage
metadata so a search can filter on them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote

from omnigent.context.refs import refs_from_items

#: The reference keys, in the order they are written.
REF_KEYS = ("files", "artifacts", "project", "helpers")

_COMMENT = re.compile(r"<!--(.*?)-->\s*$")


def merge_refs(found: Iterable[Mapping[str, Iterable[str]]]) -> dict[str, list[str]]:
    """One map of the references in *found*, each value once, first seen first; empty keys out."""
    out: dict[str, list[str]] = {}
    for refs in found:
        for key in REF_KEYS:
            for value in refs.get(key) or ():
                if isinstance(value, str) and value and value not in out.setdefault(key, []):
                    out[key].append(value)
    return {key: values for key, values in out.items() if values}


def line_refs(line: str) -> dict[str, list[str]]:
    """The references in one journal line's trailing comment (``{}`` when it has none).

    Values are written percent-encoded (``omnigent.context.refs``) and read back decoded.
    """
    match = _COMMENT.search(line)
    if match is None:
        return {}
    pairs = []
    for token in match.group(1).split():
        key, sep, values = token.partition(":")
        if sep and key in REF_KEYS:
            pairs.append({key: [unquote(v) for v in values.split(",") if v]})
    return merge_refs(pairs)


def journal_refs(text: str) -> dict[str, list[str]]:
    """The references of every journal line in *text* (a passage of a journal day file)."""
    return merge_refs(line_refs(line) for line in text.splitlines())


def turn_refs(items: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The references of a turn, from its tool items (``function_call`` and their outputs)."""
    return merge_refs([refs_from_items(items)]) if items else {}
