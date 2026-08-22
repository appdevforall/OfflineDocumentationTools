#!/usr/bin/env python3
"""
renumber_misnumbered_fragments.py

One-time, idempotent repair for ADFA-5171: some chunked Content rows number
their continuation fragments "<path>-2", "<path>-3", ... with no "<path>-1"
at all. WebServer.kt's reassembly loop always probes "<path>-1" first, so
for these rows it finds nothing and stops after the base CHUNK_SIZE-byte
row - silently truncating (ContentTypes.compression = 'none') or failing to
decompress (compression = 'brotli', since the truncated stream is missing
its tail).

Every writer in this tool (insert_chunked_content, used by populate_db.py,
insert_optimized_media.py, and migrate_content_to_dictionary_brotli.py) has
always numbered fragments starting at "-1" - none of them produced this, so
it predates this pipeline: inherited data, not something today's code
writes. This script repairs existing databases that still carry it.

Detects every base row whose content is exactly CHUNK_SIZE bytes, that
isn't itself a fragment of some other chain, and whose own fragment chain
(found by LIKE-querying "<path>-%" and sorting on the numeric suffix, not
by constructed path) doesn't start at 1. A chain with a gap in its
suffixes (a real missing chunk, a different failure than this one) is left
alone and reported rather than guessed at. Renumbers matching chains to a
contiguous "-1", "-2", ... run, lowest original suffix first, so each
rename's target path is always the one just vacated by the previous rename
in the same chain (see renumber_chain). Content bytes are never touched -
only paths move - so this is safe regardless of a row's compression.

A base row that's exactly CHUNK_SIZE with no continuation fragments at all
is left alone: that's a file that is genuinely exactly 1,048,576 bytes, not
a truncated chain, and WebServer.kt already serves it correctly.

Idempotent: a chain renumbered by this script starts at -1 afterward, so a
second run finds nothing left to fix.

Usage:
    python3 renumber_misnumbered_fragments.py <db_path>
"""
import re
import sqlite3
import sys
from pathlib import Path

from populate_db import CHUNK_SIZE, backup_database, fragment_chain

FRAGMENT_SUFFIX_RE = re.compile(r"^(.*)-(\d+)$")


def find_fragment_paths(conn) -> set:
    """Every Content.path that is itself a "<base>-<N>" continuation
    fragment of some other row in this table - lets the scan below skip a
    fragment that would otherwise also look like a candidate base of its
    own (fragments are never themselves further chunked)."""
    all_paths = {row[0] for row in conn.execute("SELECT path FROM Content")}
    fragments = set()
    for path in all_paths:
        m = FRAGMENT_SUFFIX_RE.match(path)
        if m and m.group(1) in all_paths:
            fragments.add(path)
    return fragments


def chain_fragments(conn, base_path: str) -> list:
    """Every "<base_path>-<N>" row present, as (n, path) sorted by n. Delegates
    to populate_db.fragment_chain so this script and the migration cannot drift
    apart on how a chain is found - two conventions is what let ADFA-5171's
    -2-based chains be silently skipped by the migration."""
    return fragment_chain(conn, base_path)


def is_contiguous_from_one(fragments: list) -> bool:
    return [n for n, _path in fragments] == list(range(1, len(fragments) + 1))


def renumber_chain(conn, base_path: str, fragments: list) -> None:
    """Renumbers `fragments` (n, path), sorted ascending by n, to a contiguous
    "-1", "-2", ... run.

    Two passes, via a parking name no Content row can already hold. A single
    ascending pass is collision-free only when the chain shifts *down* (each
    target was just vacated by the previous rename); a chain numbered from 0
    shifts up, where ascending order renames onto a slot still occupied and
    trips UNIQUE(path) - which rolls back the whole repair run, so one such
    chain would block every other fix in the same pass. Parking first is
    correct regardless of direction."""
    parked = []
    for _n, path in fragments:
        parking_path = f"{path}.renumbering"
        conn.execute("UPDATE Content SET path = ? WHERE path = ?", (parking_path, path))
        parked.append(parking_path)
    for i, parking_path in enumerate(parked, start=1):
        conn.execute("UPDATE Content SET path = ? WHERE path = ?", (f"{base_path}-{i}", parking_path))


def find_chains(conn, fragment_paths: set) -> tuple:
    """Returns (misnumbered, gapped): base paths whose content is exactly
    CHUNK_SIZE bytes and aren't themselves a fragment of another chain,
    split by whether their fragment chain (if any) is a contiguous run not
    starting at 1 (misnumbered - safe to repair) or has an actual gap
    (gapped - a real missing chunk, left alone and reported instead of
    guessed at)."""
    candidates = conn.execute("SELECT path FROM Content WHERE length(content) = ?", (CHUNK_SIZE,)).fetchall()
    misnumbered = []
    gapped = []
    for (path,) in candidates:
        if path in fragment_paths:
            continue
        fragments = chain_fragments(conn, path)
        if not fragments or fragments[0][0] == 1:
            continue
        # A 0-based chain is repaired, not skipped: WebServer probes "-1" first,
        # finds it, and serves the chain with "-0" silently dropped. Shifting it
        # up is what renumber_chain's parking pass exists to make safe.
        suffixes = [n for n, _path in fragments]
        if suffixes == list(range(suffixes[0], suffixes[0] + len(suffixes))):
            misnumbered.append((path, fragments))
        else:
            gapped.append((path, fragments))
    return misnumbered, gapped


def repair(conn) -> dict:
    fragment_paths = find_fragment_paths(conn)
    misnumbered, gapped = find_chains(conn, fragment_paths)
    for base_path, fragments in gapped:
        suffixes = [n for n, _path in fragments]
        print(f"warning: {base_path!r} has a gapped fragment chain (suffixes {suffixes}); left untouched",
              file=sys.stderr)
    for base_path, fragments in misnumbered:
        renumber_chain(conn, base_path, fragments)
    return {
        "chains_renumbered": len(misnumbered),
        "fragments_moved": sum(len(f) for _, f in misnumbered),
        "chains_gapped": len(gapped),
    }


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <db_path>", file=sys.stderr)
        sys.exit(1)
    db_path = Path(sys.argv[1])
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"Backing up {db_path}...", file=sys.stderr)
    backup_path = backup_database(db_path)
    print(f"Backup written to {backup_path}", file=sys.stderr)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN")
        stats = repair(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("Vacuuming database to reclaim freed space...", file=sys.stderr)
    vacuum_conn = sqlite3.connect(db_path)
    try:
        vacuum_conn.execute("VACUUM")
    finally:
        vacuum_conn.close()

    print(
        f"Renumbered {stats['chains_renumbered']} chain(s), moved {stats['fragments_moved']} fragment row(s). "
        f"{stats['chains_gapped']} chain(s) had a real gap and were left untouched."
    )


if __name__ == "__main__":
    main()
