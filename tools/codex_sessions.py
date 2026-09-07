#!/usr/bin/env python3
"""Housekeeping for the Codex session files *this project* created (SPEC §7).

    uv run python tools/codex_sessions.py list
    uv run python tools/codex_sessions.py archive --older-than 1d --dry-run
    uv run python tools/codex_sessions.py archive --older-than 1d
    uv run python tools/codex_sessions.py clean --older-than 7d --yes

SPEC §7 lists「``~/.codex/sessions`` 堆滿」as a risk: one ``codex exec`` per
decision at 200 decisions an hour leaves 200 rollout files per hour of show,
mixed in with every other thing the user does with Codex. So the hard part is
not deleting — it is being *certain* which files are ours.

Two fingerprints, either of which is enough, both taken from the first
:data:`SNIFF_BYTES` of the file:

* the sandbox cwd — :data:`amv.codex_client.SANDBOX_CWD`, the dedicated empty
  directory ``codex exec -C`` is pointed at. It appears in the session's
  ``session_meta`` record and nothing else on the machine uses that path;
* the first line of :data:`amv.director.SYSTEM_PROMPT`, which is in every
  director prompt and in no one else's.

A file matching neither is never listed, never moved, never deleted — not even
by ``clean``, which additionally refuses to touch anything outside
:data:`ARCHIVE_DIR`.

The three verbs are deliberately unequal in power:

===========  ==================================================================
``list``     Read-only. Count, total size, oldest and newest.
``archive``  **Moves** matches into :data:`ARCHIVE_DIR`, keeping the
             ``YYYY/MM/DD`` layout. Never deletes. Reversible with ``mv``.
``clean``    Deletes, only from :data:`ARCHIVE_DIR`, only matches, and only
             with ``--yes``. Two deliberate steps stand between a session file
             and oblivion, and that is the point.
===========  ==================================================================

``--dry-run`` works on all three and prints exactly what would happen.

Note on secrets: session files are read, but only the first
:data:`SNIFF_BYTES` and only to test for those two substrings. Nothing from a
session's content is printed — the output is paths, sizes and times.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from amv.codex_client import SANDBOX_CWD  # noqa: E402  (needs the sys.path shim)
from amv.director import SYSTEM_PROMPT  # noqa: E402

__all__ = [
    "SESSIONS_DIR",
    "ARCHIVE_DIR",
    "SNIFF_BYTES",
    "FINGERPRINTS",
    "SessionFile",
    "parse_age",
    "matches",
    "discover",
    "summarise",
    "archive",
    "clean",
    "main",
]

SESSIONS_DIR = Path.home() / ".codex" / "sessions"

#: Archive lives beside the sessions directory, not inside it, so codex itself
#: never sees the archived files and this tool cannot re-archive its own work.
ARCHIVE_DIR = Path.home() / ".codex" / "sessions-amv-archive"

#: How much of each file is read to fingerprint it. The ``session_meta`` record
#: is the first line and the prompt follows shortly after, so a few KB is
#: plenty — and it keeps a scan over thousands of files cheap.
SNIFF_BYTES = 16384

#: Either of these in the sniffed head means the file is ours.
FINGERPRINTS: tuple[str, ...] = (
    str(SANDBOX_CWD),
    SYSTEM_PROMPT.splitlines()[0].strip(),
)

_AGE_UNITS: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}

_AGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)


def parse_age(text: str) -> float:
    """``"1d"`` → 86400.0. A bare number is days; ``0`` means "any age"."""
    match = _AGE_RE.match(str(text))
    if not match:
        raise ValueError(f"cannot read an age from {text!r}; try 30m, 12h, 1d, 2w")
    value, unit = float(match.group(1)), (match.group(2) or "d").lower()
    return value * _AGE_UNITS[unit]


@dataclass(frozen=True)
class SessionFile:
    """One matched session file: where it is, how big, how old."""

    path: Path
    size: int
    mtime: float

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.mtime)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size": self.size,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.mtime)),
            "age_days": round(self.age_s / 86400.0, 2),
        }


def matches(path: Path, fingerprints: Sequence[str] = FINGERPRINTS) -> bool:
    """Is this one of ours? Reads at most :data:`SNIFF_BYTES` and decides.

    Unreadable or binary files answer ``False``: "I could not tell" and "not
    ours" have to collapse to the same answer, because the only action taken on
    a ``True`` is a move or a delete.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        return False
    text = head.decode("utf-8", "replace")
    return any(mark and mark in text for mark in fingerprints)


def discover(
    root: str | Path = SESSIONS_DIR,
    *,
    older_than_s: float = 0.0,
    fingerprints: Sequence[str] = FINGERPRINTS,
) -> list[SessionFile]:
    """Every matching file under ``root``, oldest first."""
    base = Path(root).expanduser()
    if not base.exists():
        return []
    now = time.time()
    found: list[SessionFile] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if older_than_s and (now - stat.st_mtime) < older_than_s:
            continue
        if not matches(path, fingerprints):
            continue
        found.append(SessionFile(path, stat.st_size, stat.st_mtime))
    found.sort(key=lambda f: f.mtime)
    return found


def summarise(files: Iterable[SessionFile]) -> dict[str, Any]:
    """Count, total bytes, oldest and newest — the ``list`` payload."""
    items = list(files)
    total = sum(f.size for f in items)
    return {
        "count": len(items),
        "total_bytes": total,
        "total_mb": round(total / (1024.0**2), 2),
        "oldest": items[0].as_dict() if items else None,
        "newest": items[-1].as_dict() if items else None,
    }


def _destination(file: SessionFile, root: Path, archive_dir: Path) -> Path:
    """Same relative layout under the archive, with a suffix on collision."""
    try:
        relative = file.path.relative_to(root)
    except ValueError:  # pragma: no cover - discover() only yields paths under root
        relative = Path(file.path.name)
    target = archive_dir / relative
    if not target.exists():
        return target
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        target = target.with_name(f"{stem}.{n}{suffix}")
        n += 1
    return target


def archive(
    files: Sequence[SessionFile],
    root: str | Path = SESSIONS_DIR,
    archive_dir: str | Path = ARCHIVE_DIR,
    *,
    dry_run: bool = False,
) -> list[tuple[Path, Path]]:
    """Move ``files`` into ``archive_dir``. Returns the ``(from, to)`` pairs."""
    root = Path(root).expanduser()
    archive_dir = Path(archive_dir).expanduser()
    moves: list[tuple[Path, Path]] = []
    for file in files:
        target = _destination(file, root, archive_dir)
        moves.append((file.path, target))
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(file.path), str(target))
    return moves


def clean(
    files: Sequence[SessionFile],
    archive_dir: str | Path = ARCHIVE_DIR,
    *,
    dry_run: bool = False,
) -> list[Path]:
    """Delete ``files``, refusing anything that is not inside ``archive_dir``.

    The containment test is on the resolved path, so a symlink pointing out of
    the archive does not become a way to delete a live session.
    """
    base = Path(archive_dir).expanduser().resolve()
    removed: list[Path] = []
    for file in files:
        resolved = file.path.resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"refusing to delete {file.path}: outside {base}")
        removed.append(file.path)
        if dry_run:
            continue
        try:
            resolved.unlink()
        except OSError as exc:  # pragma: no cover - permissions
            raise ValueError(f"could not delete {file.path}: {exc}") from exc
    return removed


# -- CLI --------------------------------------------------------------------


def _print_files(files: Sequence[SessionFile], limit: int = 10) -> None:
    for file in files[:limit]:
        print(
            f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(file.mtime))}"
            f"  {file.size / 1024.0:8.1f} KB  {file.path}"
        )
    if len(files) > limit:
        print(f"  … 還有 {len(files) - limit} 個")


def cmd_list(args: argparse.Namespace) -> int:
    files = discover(args.sessions_dir, older_than_s=args.older_than_s)
    summary = summarise(files)
    if args.json:
        print(json.dumps({"command": "list", **summary}, ensure_ascii=False, indent=2))
        return 0
    print(f"sessions dir : {Path(args.sessions_dir).expanduser()}")
    print(f"fingerprints : {FINGERPRINTS[0]}")
    print(f"               {FINGERPRINTS[1][:48]}…")
    print(f"matched      : {summary['count']} 個 / {summary['total_mb']:.2f} MB")
    if summary["oldest"]:
        print(f"oldest       : {summary['oldest']['mtime']}  {summary['oldest']['path']}")
        print(f"newest       : {summary['newest']['mtime']}  {summary['newest']['path']}")
        _print_files(files)
    else:
        print("（沒有本專案產生的 session；其他 Codex session 一概不動）")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    root = Path(args.sessions_dir).expanduser()
    files = discover(root, older_than_s=args.older_than_s)
    moves = archive(files, root, args.archive_dir, dry_run=args.dry_run)
    verb = "would move" if args.dry_run else "moved"
    if args.json:
        print(
            json.dumps(
                {
                    "command": "archive",
                    "dry_run": bool(args.dry_run),
                    "count": len(moves),
                    "archive_dir": str(Path(args.archive_dir).expanduser()),
                    "moves": [{"from": str(a), "to": str(b)} for a, b in moves[:200]],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"{verb} {len(moves)} 個 session → {Path(args.archive_dir).expanduser()}")
    _print_files(files)
    if not moves:
        print(f"（{args.older_than} 以前、且由本專案產生的 session 是 0 個）")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    archive_dir = Path(args.archive_dir).expanduser()
    files = discover(archive_dir, older_than_s=args.older_than_s)
    if not args.yes and not args.dry_run:
        print(
            f"clean 會永久刪除 {len(files)} 個檔案（{summarise(files)['total_mb']:.2f} MB）"
            f"，只刪 {archive_dir} 裡面的。\n"
            "確定的話加 --yes；想先看清單就加 --dry-run。",
            file=sys.stderr,
        )
        return 2
    try:
        removed = clean(files, archive_dir, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    verb = "would delete" if args.dry_run else "deleted"
    if args.json:
        print(
            json.dumps(
                {
                    "command": "clean",
                    "dry_run": bool(args.dry_run),
                    "count": len(removed),
                    "archive_dir": str(archive_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"{verb} {len(removed)} 個檔案（{archive_dir}）")
    _print_files(files)
    return 0


def _common_flags() -> argparse.ArgumentParser:
    """Flags accepted on *either* side of the sub-command.

    ``--dry-run`` reading differently depending on where it sits in the command
    line is exactly the kind of surprise a tool that deletes things must not
    have, so the shared flags are declared twice: once on the main parser with
    real defaults, once here with ``SUPPRESS`` so that a sub-command copy only
    sets the attribute when it was actually typed.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--sessions-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--archive-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output"
    )
    common.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print what would happen and change nothing",
    )
    return common


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    common = _common_flags()
    parser = argparse.ArgumentParser(
        prog="tools/codex_sessions.py",
        description="列出／封存／清除本專案產生的 Codex session 檔（SPEC §7）。",
    )
    parser.add_argument("--sessions-dir", default=str(SESSIONS_DIR), help="default ~/.codex/sessions")
    parser.add_argument(
        "--archive-dir", default=str(ARCHIVE_DIR), help="default ~/.codex/sessions-amv-archive"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would happen and change nothing"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser(
        "list", parents=[common], help="count, size, oldest and newest (read-only)"
    )
    listing.add_argument("--older-than", default="0", help="only files this old (default: all)")
    listing.set_defaults(func=cmd_list)

    archiving = subparsers.add_parser(
        "archive",
        parents=[common],
        help="move matching sessions into the archive directory (never deletes)",
    )
    archiving.add_argument("--older-than", default="1d", help="default 1d")
    archiving.set_defaults(func=cmd_archive)

    cleaning = subparsers.add_parser(
        "clean",
        parents=[common],
        help="delete from the archive directory only, and only with --yes",
    )
    cleaning.add_argument("--older-than", default="7d", help="default 7d")
    cleaning.add_argument("--yes", action="store_true", help="required: this one deletes")
    cleaning.set_defaults(func=cmd_clean)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        args.older_than_s = parse_age(args.older_than)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    # clean reads from the archive, so its --sessions-dir is meaningless; the
    # sub-commands share a parser and this is where that shortcut is paid for.
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
