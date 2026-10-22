"""Compact native return receipts, separate from a child's mutable activity."""
import hashlib

from cc_remote.protocol import ProcessEvent


def return_receipt(event: ProcessEvent, source_id: str, run_id: str | None = None):
    """Keep exact attribution without copying the child's output into history."""
    key = hashlib.sha256(source_id.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return ProcessEvent(
        item_id=f"return-{key}", kind=event.kind, phase="end", status=event.status,
        turn_id=event.turn_id, parent_id=event.parent_id, title=event.title, ts=event.ts,
        server="cc_remote", tool="agent_return" if event.kind == "agent" else "task_return",
        input={"agent_run_id": run_id} if event.kind == "agent" else None,
        background=True,
    )
