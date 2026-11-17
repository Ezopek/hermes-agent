#!/usr/bin/env python3
"""Print the base the fork gate's diff-based guards (code health, blob size) should judge against.

The guards flag what a change adds or grows relative to a base. A file whose mode and content at
HEAD equal the same path in the upstream mirror is upstream's code, not the fork's, so it must not
count as a change. The base printed here is a commit whose tree is BASE's tree, except that every
such file holds its HEAD content: those files then show no diff, and every other file is still
judged against BASE.

    python3 .github/fork-gate-base.py BASE UPSTREAM [HEAD]

BASE and UPSTREAM are revisions (HEAD defaults to ``HEAD``). Nothing is moved or pushed: the
commit is a loose, unreferenced object in the local repository.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ZERO_MODE = "000000"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "fork-gate", "GIT_AUTHOR_EMAIL": "fork-gate@localhost",
    "GIT_COMMITTER_NAME": "fork-gate", "GIT_COMMITTER_EMAIL": "fork-gate@localhost",
}


def _git(*args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="surrogateescape", input=stdin, env=env, timeout=300, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"fork-gate-base: git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _commit(rev: str) -> str:
    return _git("rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def _upstream_paths(upstream: str, head: str) -> set[str]:
    """Paths whose mode or content differ between UPSTREAM and HEAD (added, changed or removed)."""
    out = _git("diff", "--name-only", "-z", "--no-renames", upstream, head)
    return {path for path in out.split("\0") if path}


def _shared_entries(base: str, upstream: str, head: str) -> list[str]:
    """``update-index --index-info`` records for HEAD files that BASE lacks or holds differently
    and that UPSTREAM holds exactly as HEAD does."""
    fork_owned = _upstream_paths(upstream, head)
    fields = _git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, head).split("\0")
    entries = []
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        new_mode, new_sha = meta.split()[1:4:2]
        if new_mode != _ZERO_MODE and path not in fork_owned:
            entries.append(f"{new_mode} {new_sha}\t{path}\0")
    return entries


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    # A missing upstream ref must stop the gate: judging against the plain base would change what
    # the guards mean without anyone noticing.
    base, upstream, head = _commit(argv[0]), _commit(argv[1]), _commit(argv[2] if len(argv) == 3 else "HEAD")
    entries = _shared_entries(base, upstream, head)
    with tempfile.TemporaryDirectory(prefix="fork-gate-base-") as tmp:
        env = {**os.environ, **_IDENTITY, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git("read-tree", base, env=env)
        if entries:
            _git("update-index", "-z", "--index-info", env=env, stdin="".join(entries))
        tree = _git("write-tree", env=env).strip()
    message = f"fork gate base: {base[:12]} with {len(entries)} upstream-identical files at {head[:12]}"
    commit = _git("commit-tree", tree, "-p", base, "-m", message, env={**os.environ, **_IDENTITY}).strip()
    print(f"fork-gate-base: {len(entries)} upstream-identical changed files taken from {head[:12]}",
          file=sys.stderr)
    print(commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
