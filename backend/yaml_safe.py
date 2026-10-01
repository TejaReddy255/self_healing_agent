"""Hardened YAML parsing shared by version-controlled content and config (S04, S06).

On top of ``yaml.SafeLoader`` (no arbitrary objects), this rejects:
- anchors and aliases (no "billion laughs" expansion);
- duplicate or non-scalar mapping keys (no silently ignored values);
- nesting deeper than ``MAX_NESTING_DEPTH`` (no stack exhaustion).

Every failure is a ``StrictYamlError`` with a stable code: ``UNSAFE_YAML`` or ``YAML_INVALID``.
This is the only module that calls ``yaml.load`` outside ``backend/metadata`` (boundary test).
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node

MAX_NESTING_DEPTH = 64


class StrictYamlError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML without aliases, duplicate or unhashable keys, or excessive nesting."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: Node | None, index: int) -> Node | None:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        line = event.start_mark.line + 1
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise StrictYamlError(
                "UNSAFE_YAML", f"anchors and aliases are not allowed (line {line})"
            )
        self._depth += 1
        if self._depth > MAX_NESTING_DEPTH:
            raise StrictYamlError(
                "YAML_INVALID", f"nesting deeper than {MAX_NESTING_DEPTH} levels (line {line})"
            )
        try:
            node: Node | None = super().compose_node(parent, index)
        finally:
            self._depth -= 1
        return node

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            line = key_node.start_mark.line + 1
            if isinstance(key, (list, dict, set)) or not isinstance(key, Hashable):
                raise StrictYamlError("YAML_INVALID", f"mapping keys must be scalars (line {line})")
            if key in seen:
                raise StrictYamlError("YAML_INVALID", f"duplicate key {key!r} (line {line})")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_strict(text: str) -> Any:
    """Parse YAML text strictly; raise ``StrictYamlError`` on anything unsafe or malformed."""
    try:
        return yaml.load(text, Loader=_StrictSafeLoader)  # noqa: S506 - strict SafeLoader subclass
    except StrictYamlError:
        raise
    except yaml.constructor.ConstructorError as exc:
        problem = exc.problem or "cannot construct a value"
        code = "UNSAFE_YAML" if "tag" in problem else "YAML_INVALID"
        raise StrictYamlError(code, f"unsupported YAML: {problem}") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise StrictYamlError("YAML_INVALID", f"invalid YAML{where}") from exc
    except RecursionError as exc:
        raise StrictYamlError(
            "YAML_INVALID", f"nesting deeper than {MAX_NESTING_DEPTH} levels"
        ) from exc
