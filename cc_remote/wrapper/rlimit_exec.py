"""Raise one child-only resource limit before replacing this process."""
from __future__ import annotations

import os
import resource
import sys


_MAX_NOFILE_SOFT = 65_536


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        raise SystemExit("usage: rlimit_exec NOFILE_SOFT PROGRAM [ARG ...]")
    try:
        requested = int(args[0], 10)
    except ValueError as exc:
        raise SystemExit("invalid NOFILE_SOFT") from exc
    if requested < 1 or requested > _MAX_NOFILE_SOFT:
        raise SystemExit("NOFILE_SOFT is outside the safety bound")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < requested:
        raise SystemExit("NOFILE hard limit is below the requested soft limit")
    target = requested
    if soft != resource.RLIM_INFINITY and target > soft:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    verified_soft, _verified_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if verified_soft != resource.RLIM_INFINITY and verified_soft < requested:
        raise SystemExit("NOFILE soft limit could not be raised")
    program = args[1]
    os.execvpe(program, args[1:], os.environ)


if __name__ == "__main__":
    main()
