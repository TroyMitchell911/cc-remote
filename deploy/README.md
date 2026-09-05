# deploy/

Reference files for the production deploy (public VPS relay + wrapper on your
machine). The **full step-by-step guide is in the main [README](../README.md#生产部署公网-vps-中继--你机器上的-wrapper)**
([English](../README_en.md#production-deploy-public-vps-relay--wrapper-on-your-machine)).

## Deployment contract for automation

This directory is the deployment source of truth for humans and automation.
Machine inventory is deliberately external: host aliases, usernames, domains,
addresses, home directories, and credentials belong to the operator's
environment, not this repository. Replace documented placeholders only with
values the operator supplied or that were read from the existing installation;
never guess them.

Before changing a live service:

1. Inspect the source worktree, target installation, current release, service
   manager, and health. Preserve unrelated changes; do not normalize a dirty
   worktree or silently replace a custom installation layout.
2. Select the matching supported path. Use `install.sh` for a published release.
   Use the main README's source-staging/manual production path for the current
   source tree. An existing nonstandard installation must retain its established
   service ownership and configuration boundaries rather than being overwritten
   with a first-install template.
3. Run the complete gate in `AGENTS.md`, build `web/dist`, and validate the
   Python/Web protocol pair with `validate_protocol_bundle.py`.
4. Freeze those tested bytes once. Every Relay, Web client, and Wrapper in the
   maintenance window must come from that same snapshot or coordinated artifact
   set. Do not rebuild independently on different hosts.
5. Stage and validate every target before activation. Keep `.env`, device
   authority, profile configuration, private databases, and other runtime state
   outside immutable release trees. Never upload secrets as part of a source
   snapshot.

Activate a coordinated protocol change in the order documented by the current
protocol note below: stop incompatible old Wrappers, activate Relay + Web as one
transaction, then activate/start every Wrapper and hard-refresh clients. Use the
repository installers' immutable `releases/` plus atomic `current` switch; never
overlay the live tree with `rsync --delete`. If Wrapper state requires a schema
snapshot, create it while the Wrapper is stopped and keep it with the previous
release.

A command that loses SSH, terminal, or cc-remote connectivity has an **unknown
result**, not a failed result. Inspect the exact service/job, `current` target,
logs, health endpoint, PID, and restart count before retrying. Never start a
second installer merely because the first caller stopped receiving output.
A Wrapper must not be its own only deployment controller: activate it from an
independent terminal/SSH connection or from exactly one OS-owned one-shot job
that can finish after the old Wrapper exits.

Success requires all of the following: the expected immutable releases are
active, Python and served Web build metadata report the same protocol/product,
services have stable PIDs without restart loops, the public health endpoint is
healthy, expected Wrappers reconnect, and recent logs contain no new fatal
errors. On failure, use the installer-owned rollback or the retained previous
release and matching state snapshot; do not delete old releases during the
deployment.

- `install.sh` — versioned GitHub Release bootstrap. It requires an explicit
  `relay` or `wrapper` role, detects OS/CPU, downloads that one role archive,
  verifies its `SHA256SUMS` entry before extraction, rejects unsafe archive
  paths, and then invokes the in-bundle installer. It never pipes a network
  response into a shell.
- `build_release.py` / `release_manifest.py` — reproducible role-bundle builder
  and fail-closed manifest validator. Relay artifacts contain `web/dist` and
  `requirements-relay.lock`; Wrapper artifacts contain no Web tree and use
  `requirements-wrapper.lock`. Each artifact carries the product version,
  protocol, full Git SHA, OS, architecture, and Python runtime contract.
- `install-relay.sh` — first-install/upgrade entry for a published Relay
  bundle. It creates secrets only when `/opt/cc-remote/.env` does not exist,
  then delegates to the existing transactional VPS installer. The explicit
  `--allow-private-origins` first-install option binds IPv4 `0.0.0.0:8765`
  for simultaneous LAN/Tailscale access and requires firewall restriction;
  the default remains loopback-only behind Caddy.
- `install-wrapper.sh` — first-install/upgrade entry for published macOS and
  Linux Wrapper bundles. It builds the immutable release before pairing and
  activation, stores device authority outside the release, atomically switches
  `current`, installs a per-user LaunchAgent or root-managed systemd unit, and
  restores the previous release/service definition on failure. The installer
  requires and explicitly selects the service user's daily
  `~/.local/bin/claude`; it never silently falls back to the SDK-bundled CLI.
- `prepare_wrapper_stage.py` — unprivileged preflight for an existing manual
  immutable-Wrapper topology. It reuses an active venv only when the dependency
  lock and Python pin are identical; otherwise it builds a platform-local venv
  with the pinned uv/Python, hashed binary wheels, and copy link mode. It
  validates imports plus the Python/Web protocol pair and writes a bound stage
  manifest for the separate privileged activation step. It never switches
  `current` or restarts a service.
- `setup-vps.sh` — atomic VPS release installer. It validates a user-owned
  upload, copies it to a new root-owned
  `/opt/cc-remote/releases/release-*` directory, builds that release's own
  venv, validates the Python/web protocol pair, then switches the
  `/opt/cc-remote/current` symlink in one rename. The running tree is never
  overlaid with `rsync --delete`. If relay restart/readiness fails, `current`,
  Caddyfile, and the relay unit roll back together and the previous release is
  health-checked. The previous full code + web + venv directory is retained.
  Run `sudo bash ~/cc-remote-upload/deploy/setup-vps.sh your-domain.com \
  ~/cc-remote-upload`; the optional second argument defaults to the repository
  containing the invoked script. Shared secrets stay only in
  `/opt/cc-remote/.env`, whose `WEB_STATIC_DIR` must point to
  `/opt/cc-remote/current/web/dist`.
- `Caddyfile` — reverse proxy + auto Let's Encrypt TLS (`wss://domain/ws` →
  `127.0.0.1:8765`) plus an early 4 KiB login-body limit. Replace
  `cc-remote.example.com` with your domain.
- `Caddyfile.insecure` — explicit plain-HTTP public-IP template selected only
  when `ALLOW_INSECURE_HTTP=1`, the setup target is a public IPv4 address, and
  `PUBLIC_ORIGIN` exactly matches `http://that-address`. It omits HSTS and
  permits `ws://` in CSP; login credentials, cookies, wrapper tokens, and all
  session traffic are unencrypted in this mode. Pass the IP and source
  directory to the same immutable `setup-vps.sh` flow used for TLS.
- `cc-remote-relay.service` — systemd unit for the relay on the VPS.
- `cc-remote-wrapper.service` — systemd unit for the wrapper on your machine
  (edit `User` + paths first). It reads root-only
  `/etc/cc-remote/wrapper.env`, hides that file and any legacy repository
  `.env` from model descendants, and disables core dumps.
- `env.relay.example` / `env.wrapper.example` — environment templates for each
  side. Install the wrapper template as root:root mode 0600 at the path above.
- `com.muggle.cc-remote.wrapper.plist.in` — secret-free macOS LaunchAgent
  template. The runtime reads the current user's mode-0600 device JSON instead
  of embedding control credentials in the plist.
- `Dockerfile` / `docker-compose.yml` / `env.relay.docker.example` — the same
  relay release as a container (build `web/dist` in a Node stage, install the
  hash-locked wheels, run as the `ccremote` user). See the container section
  below; this is an alternative to the systemd + Caddy path, not a fork of it.
- `nginx-reverse-proxy.conf.example` — a WebSocket reverse-proxy front for
  hosts that already run nginx instead of the managed Caddy. Loopback-only
  requirement is documented in the file header.
- `work_registry_snapshot.py` — snapshots provider-local Work SQLite databases
  through SQLite's backup API plus the bounded private Claude control store,
  restores the matching pre-release data before an older wrapper is restarted,
  and verifies the v34 Codex ownership backfill.

Protocol v52 is a coordinated upgrade: publish freshly built Relay/Web and
Wrapper artifacts from the same tagged commit. The strict protocol gate is
intentional and mixed protocol versions will not communicate. `setup-vps.sh`
rejects a missing or mismatched web build manifest. Stop the wrapper first;
activate the v52 relay/web release; then start the v52 wrapper.

The wrapper installer treats local Work data and versioned private control state
as part of the release
transaction. It stops the existing service, writes a private snapshot below
the install root's `rollback-data/`, starts the new release, and refuses the
activation unless the Claude and Codex Work schemas and all legacy profile
ownership rows are ready. On failure it stops the new process, restores both SQLite images, then
restores and starts the previous code. If data restoration fails, it leaves the
wrapper stopped instead of running old code against a new schema. A manual or
legacy-layout deployment must use the same order: stop the wrapper, run
`work_registry_snapshot.py snapshot` from the new staging tree, activate and
verify v52, and retain that snapshot with the previous release. To roll back,
stop v52, run `work_registry_snapshot.py restore`, then switch and start the old
release. Never copy only `registry.sqlite3` while the wrapper is live because
committed state may still be in its WAL file. Restoring a pre-release snapshot
also restores pre-release Work metadata: sessions, projects, or schedule state
created after activation will no longer be registered (their private files are
not deleted). Use this for immediate failed activation; after normal use,
prefer a roll-forward fix unless that metadata rollback is explicitly accepted.

## Container deploy (Docker) and the nginx alternative

The official relay install is a systemd venv staged by `setup-vps.sh` behind a
managed Caddy. Two alternative topologies are supported for hosts that already
manage their own services or TLS:

**Docker container.** `Dockerfile` builds the same relay release as a
multi-stage image: the Node stage compiles `web/dist` from source, the Python
stage installs the same hash-locked wheels `setup-vps.sh` pins and runs
`python -m cc_remote.relay` as a non-root `ccremote` user. From the `deploy/`
directory:

```bash
cp env.relay.docker.example env.relay   # then fill in the secrets
docker compose up -d --build
curl https://your-domain/healthz        # -> {"ok":true,...}
```

The compose file publishes the relay only to the host loopback
(`127.0.0.1:8765`) and mounts a named volume for the SQLite device/Web Push
state. Public TLS + WebSocket termination stays with your existing front.

**nginx instead of Caddy.** `nginx-reverse-proxy.conf.example` terminates TLS
and proxies the `/ws` WebSocket to `127.0.0.1:8765`. Keep it loopback-only:
the relay trusts forwarded transport metadata only from loopback peers.

**Mainland-China mirrors.** The Docker build defaults to PyPI.org. Behind the
GFW, build with Aliyun as the primary index and TUNA as the fallback (both
carry the sdist-only `http-ece` wheel):

```bash
docker build -f deploy/Dockerfile \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  --build-arg PIP_EXTRA_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t cc-remote-relay .
```

## Native terminal coordination

- **Claude Code:** run `claude` directly for the untouched official process;
  Remote treats direct CLI, Desktop, and Agent View ownership as read-only.
  Explicit takeover may gracefully terminate the exact same-user Claude process
  with SIGTERM and then resume through the SDK, but it never kills the terminal
  shell, escalates to SIGKILL, or silently adopts a process.
- **Codex Code:** `CC_REMOTE_CODEX_DAEMON=auto` prefers Codex's official shared
  app-server daemon. Set it to `off` only to force the legacy private stdio path.
  Optional multi-account installs provide either inline
  `CC_REMOTE_CODEX_PROFILES_JSON` or a private
  `CC_REMOTE_CODEX_PROFILES_FILE`; the macOS LaunchAgent defaults the latter to
  `~/.cc-remote/codex-profiles.json`. Each unique `CODEX_HOME` owns a daemon.
  When a sibling home has no duplicate standalone payload, first bootstrap
  safely reuses the verified primary managed CLI through a profile-local
  `current` link; account data and daemon sockets remain isolated.
  Leaving both empty preserves the exact single-account path and UI.
- **Work:** both engines stay on private per-process control planes regardless
  of the Code settings. Codex Work sessions and schedules may select any
  configured profile; the local registry freezes that ownership across retries
  and default-profile changes.

## Security (short version)

The relay is exposed publicly; `LOGIN_PASSWORD` or `LOGIN_USERS_JSON`,
`SESSION_SECRET`, and `WRAPPER_TOKEN` or `WRAPPER_TOKENS_JSON` are the
authentication secrets. Claude defaults to
`bypassPermissions`; Codex inherits its local sandbox and defaults to approval
policy `never`. Treat every logged-in client as holding remote agent/shell
authority on the wrapper machine. Use strong secrets, keep relay `.env` out of
git, never store the production wrapper token in a model-readable repository
file. Always prefer TLS at Caddy; the public-IP escape hatch sends the login
password, browser cookie, wrapper token, and session traffic unencrypted.
See the [security section](../README.md#安全须知务必读) of the main README.

The relay itself limits unfinished login bodies to 32 concurrent reads and 10
seconds each. The managed Caddy global block additionally sets 10-second header,
15-second body, 30-second write, 2-minute idle, and 64 KiB header limits before
requests reach the relay. Other global options and sites are preserved. If a
shared Caddyfile already contains an unmanaged `servers` block, setup fails
closed and asks the administrator to reconcile it instead of silently creating
ambiguous global behavior.
