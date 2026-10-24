# Sanitized README screenshots

`serve.py` serves the production `web/dist` UI with a small synthetic protocol
fixture on **127.0.0.1 only**. It does not start an engine, execute terminal
commands, read private transcripts, load credentials or connect to a Relay.
It is a screenshot fixture, not a general-purpose Relay or an integration test.
Unsupported operations are not implemented.

```bash
# Use Node 24 and the repository development Python dependencies.
npm --prefix web run build
.venv/bin/python scripts/readme/serve.py
```

In cc-remote Desktop, use the app menu's site selector to open
`http://127.0.0.1:4192`. Use computer-use controls to open the actual panels and
capture the app window. Keep all demo interactions on this loopback origin.
Restore the operator's original site afterward and stop the fixture server.
Do not save a live conversation screenshot to the repository.

The v5 gallery was captured through computer use on 2026-10-24. It includes:

- Codex and Claude workspaces and activity.
- DSH chat, mode selection and session tool categories.
- Subagent details, terminal, automation form and Goal controls.
- Artifacts, docked/centered Markdown and Mermaid previews.
- Per-engine appearance settings.

All content, identifiers, paths, device labels, quotas and task outputs are
invented demonstration data. Captures are unretouched PNGs under
`assets/readme-v5-*.png`, shared by both README languages. Review each image
visually before publishing; metadata and fixture source must contain no operator
inventory or secrets. Only synthetic static responses are shown, so the gallery
must not be described as proof of live engine execution or all-feature acceptance.
