"""Read Claude's persisted rewind anchors without rewriting native transcripts."""
from __future__ import annotations

from typing import Any
from uuid import UUID


REWIND_ANCHOR = "cc_remote_rewind_anchor"


def rewind_anchor_row(row: dict[str, Any], offset: int) -> dict[str, Any]:
    """Project the native metadata row as an ancestry edge for our read index.

    A null leaf is an intentional empty conversation. Keep a synthetic *index*
    identity so it cannot fall back to the last abandoned assistant. Subsequent
    native message rows become the leaf normally; nothing is written to JSONL.
    """
    if (row.get("type") != "last-prompt" or row.get("rewound") is not True
            or row.get("explicit") is not True or "leafUuid" not in row):
        return row
    leaf = row["leafUuid"]
    if leaf is not None:
        try:
            if not isinstance(leaf, str) or str(UUID(leaf)) != leaf:
                return row
        except ValueError:
            return row
    return {"type": "system", "subtype": REWIND_ANCHOR,
            "uuid": f"rewind-anchor-{offset}", "parentUuid": leaf,
            "isSidechain": False}
