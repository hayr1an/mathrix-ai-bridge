"""Dump Claude Desktop's accessibility tree.

    python -m bridge.inspect                    # dump once
    python -m bridge.inspect --grep message     # only matching lines, with context
    python -m bridge.inspect --roles            # raw AX roles, to build ROLE_MAP
    python -m bridge.inspect --watch            # print what changes as you interact
    python -m bridge.inspect --watch --save captures/

Every non-obvious name in detect.py came out of this command. Guessing at
control names is what breaks this kind of driver repeatedly (DRIVERSPEC §6):
capture the shape first, then write the detection.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from . import config
from .core import Snapshot
from .macos import Walker


def _lines(snap: Snapshot, max_name: int) -> list[str]:
    out = []
    for n in snap.nodes:
        name = n.label.replace("\n", "\\n")
        if len(name) > max_name:
            name = name[: max_name - 1] + "…"
        out.append("{:>5} {}{} {!r}".format(n.index, "  " * n.depth, n.type, name))
    return out


def _print(snap: Snapshot, args: argparse.Namespace) -> None:
    lines = _lines(snap, args.max_name)
    if not args.grep:
        print("\n".join(lines))
        return
    needle = args.grep.casefold()
    hits = [i for i, line in enumerate(lines) if needle in line.casefold()]
    if not hits:
        print(f"(no node matched {args.grep!r} in {len(snap)} nodes)")
        return
    shown: set[int] = set()
    for i in hits:
        for j in range(max(0, i - args.context), min(len(lines), i + args.context + 1)):
            shown.add(j)
    last = -2
    for j in sorted(shown):
        if j != last + 1:
            print("      …")
        print(("> " if j in hits else "  ") + lines[j])
        last = j


def _roles(snap: Snapshot) -> None:
    counts = Counter((n.extra.get("role", "?"), n.type) for n in snap.nodes)
    print("{:<28} {:<18} {}".format("raw AX role", "normalised", "count"))
    for (raw, norm), count in counts.most_common():
        print("{:<28} {:<18} {}".format(raw, norm, count))


def _save(snap: Snapshot, where: Path, tag: str = "") -> Path:
    where.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = where / f"tree-{stamp}{('-' + tag) if tag else ''}.json"
    path.write_text(json.dumps(snap.to_dict(), indent=1, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _watch(walker: Walker, args: argparse.Namespace) -> int:
    """Print only what changed since the last read.

    A full tree is thousands of lines; the useful signal while clicking around
    the app is the diff.
    """
    previous: Optional[list[str]] = None
    save_dir = Path(args.save) if args.save else None
    print("watching - interact with Claude Desktop; Ctrl+C to stop", file=sys.stderr)
    try:
        while True:
            snap = walker.wake()
            current = _lines(snap, args.max_name)
            if previous is None:
                print(f"[{time.strftime('%H:%M:%S')}] baseline: {len(snap)} nodes")
            elif current != previous:
                before, after = set(previous), set(current)
                gone = [l for l in previous if l not in after]
                new = [l for l in current if l not in before]
                print(f"\n[{time.strftime('%H:%M:%S')}] {len(snap)} nodes  "
                      f"(-{len(gone)} +{len(new)})")
                for line in gone[: args.limit]:
                    print("  - " + line.strip())
                for line in new[: args.limit]:
                    print("  + " + line.strip())
                if save_dir:
                    print("  saved " + str(_save(snap, save_dir)))
            previous = current
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge.inspect", description=__doc__)
    parser.add_argument("--watch", action="store_true", help="print changes as you interact")
    parser.add_argument("--roles", action="store_true", help="raw AX role histogram")
    parser.add_argument("--grep", help="only lines containing this, with context")
    parser.add_argument("--context", type=int, default=3)
    parser.add_argument("--save", nargs="?", const=str(config.CAPTURES_DIR),
                        help="write the tree as JSON into this directory")
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--limit", type=int, default=40, help="max diff lines shown")
    parser.add_argument("--max-name", type=int, default=110, dest="max_name")
    args = parser.parse_args(argv)

    walker = Walker(config.load())
    if args.watch:
        return _watch(walker, args)

    snap = walker.wake()
    print(f"# {len(snap)} nodes", file=sys.stderr)
    if args.roles:
        _roles(snap)
    else:
        _print(snap, args)
    if args.save:
        print("saved " + str(_save(snap, Path(args.save))), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
