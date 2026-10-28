# Claude CLI and Remote controls

Remote follows Claude Code's native persistence rules rather than defining a
second set of per-conversation defaults:

- An explicit model choice saves `model` in the selected account's
  `settings.json`. An explicit persistent effort saves
  `modelSettings[resolvedModel].effortLevel`; `[1m]` is not part of that key.
  `max` remains a runtime-only choice. Work does not write these user defaults.
- Both keys are merged from the latest file contents and published together.
  Mixing a local model write with native `update_settings` can restore a stale
  model from the CLI's settings cache, so Remote does not mix those writers.
  Malformed, oversized, foreign-owned or symlinked settings files are not
  replaced. A failed save leaves the runtime choice intact and shows a notice.
- Reading history, restoring a session and observing a provider response never
  save user defaults. The full settings object is not copied to Remote state,
  logged or sent to the Relay.
- A cold resume uses the native conversation model and omits an effort override
  unless the current command explicitly supplied one. Old Remote model/effort
  caches must not override newer native choices. Native `get_settings.applied`
  supplies the effective effort for display and the resolved model for saving;
  its private `effective` and `sources` objects are discarded.
- An attachment to the same persistent service worker is not a cold resume.
  It retains that worker's running controls and a pending Remote effort choice,
  without interrupting its accepted input. Reconnect within a live Remote
  session likewise retains explicit controls.

These rules intentionally share effort defaults between sessions of the same
account and model. They do not promise a separate persistent effort per
conversation. Remote also does not rewrite a native transcript to repair the
CLI's model restoration when a model was selected without a subsequent reply.

For an unmanaged terminal, Remote observes
`<account-config>/sessions/<pid>.json` when available. Claude Code 2.1.311 updates
its `sessionId` after an in-process `/resume`. The reader validates the account,
same-user process, OS start identity and native UTC `procStart`; it excludes the
Wrapper/service process trees. A valid record wins over stale launch arguments,
including when the current session is outside Remote's watched subset. Missing
records retain the older argv/cwd fallback; malformed records are incomplete
observations rather than permission to write. Registry PIDs also provide argv
scan hints for directly launched version-named binaries on macOS.

The registry does not report live model/effort. An unmanaged terminal therefore
shows one compact “CLI 配置未同步” label instead of stale Remote choices. Takeover
retains its explicit ownership checks, then reloads native configuration. No
statusline or shell hooks are installed.

Private BTW reconnects resume a confirmed child identity. An unqueried fork
with no confirmed identity receives a fresh reservation after confirmed shutdown;
the old reservation remains private even if cleanup fails. Once a query may
have been accepted, identity uncertainty fails the reconnect rather than
resubmitting it or silently discarding the child conversation.

References: [native model and effort configuration](https://code.claude.com/docs/en/model-config),
[SDK configuration](https://code.claude.com/docs/en/agent-sdk/configuration).

Zero-model regression coverage lives in `test_claude_native_defaults.py`,
`test_claude_registry.py`, `test_claude_btw_reconnect.py`, and the existing
permission, ownership, service lifetime and command replay suites.
