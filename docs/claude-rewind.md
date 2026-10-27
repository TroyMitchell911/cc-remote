# Claude Code rewind

The user chooses a completed historical message, a restore scope, and then confirms.
Both the message entry and `/rewind` open the same dialog. This feature is limited to
Claude Code; Codex, DSH and Work do not expose these controls.

## Native operations

- Files use the pinned Agent SDK's public `rewind_files(user_message_id)` method.
  A native `rewind_files` control with `dry_run: true` checks availability before
  offering or executing a file restore. Old sessions may lack file checkpoints.
- Conversation restore uses Claude Code's native `rewind_conversation` control.
  This is **not a public Python SDK method**. The handle probes support with an
  impossible target UUID, rejects unrecognized responses and never sends a model
  prompt. Unsupported runtimes report unavailability.
- The request includes the selected native user UUID and
  `last_seen_user_message_uuid`. The latter guards against a changed conversation
  and permits an older target without repeatedly removing the newest turn.
  Public `resume_session_at` alone does not persist this operation immediately.
- Claude writes an explicit, rewound `last-prompt` record. The history index follows
  its `leafUuid`, including null for a rewind before the first message. It never
  rewrites the native JSONL or treats abandoned rows as the active conversation.

## Execution and synchronization

The Wrapper serializes confirmation with normal queries, rejects foreground,
background and queued work, and checks native ownership. It verifies the target
again after preparing the runtime. A stale confirmation does not mutate files.
The durable rollback journal prevents reconnects or duplicate commands from
executing the same operation again.

For a combined restore, conversation acceptance comes first, followed by files,
then a runtime reconnect that flushes and reloads native state. A file failure
after successful conversation restore is reported as partial success. This is
not an atomic filesystem transaction or a substitute for Git. Native file
checkpoints cover tracked edits, not arbitrary shell effects; later manual edits
to checkpointed files can be overwritten.

Conversation changes invalidate the shared history projection and publish a fresh
snapshot; file changes invalidate file previews. Only the requesting client's
matching command receipt can prefill its original session's empty draft. Newer
drafts, another session's composer and other clients are preserved.

## Verification

Regression coverage includes native anchor indexing and cache migration, rollback
command idempotency, stale/busy rejection, partial results, and mobile dialog
layout and draft preservation in Chromium and WebKit. Native CLI behavior was
also checked in an isolated temporary config/project using synthetic transcript
and checkpoint fixtures with model traffic disabled. These checks do not execute
existing user sessions or submit model messages.
