"""Node dataclass — the in-memory representation of a WhatsApp XML element."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Attrs = dict[str, Any]


@dataclass
class Node:
    """A WhatsApp binary XML element.

    Content is either None, a list of Nodes, or raw bytes. Attribute values
    are strings, ints, bools, or JIDs; encoders coerce non-string numerics
    to their decimal string representation.
    """

    tag: str
    attrs: Attrs = field(default_factory=dict)
    content: "None | list[Node] | bytes" = None

    def get_children(self) -> list["Node"]:
        if isinstance(self.content, list):
            return self.content
        return []

    def get_child_by_tag(self, *tags: str) -> "Node | None":
        cur: Node | None = self
        for tag in tags:
            if cur is None:
                return None
            found: Node | None = None
            for child in cur.get_children():
                if child.tag == tag:
                    found = child
                    break
            cur = found
        return cur
