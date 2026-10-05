#!/usr/bin/env python3
"""The Content-table chunking protocol, in one place.

Every tool in this repo that reads or rewrites a `Content` row has to agree
with what `WebServer.kt` actually serves. Its request handler reassembles a
page like this:

    read `path`; if that blob is exactly CHUNK_SIZE bytes, keep appending
    `path-1`, `path-2`, ... in suffix order, and stop at the first fragment
    shorter than CHUNK_SIZE (or the first one missing).

Two rules fall out of that, and both have been got wrong independently:

  1. **A "<base>-<N>" path is a continuation only if the base row is exactly
     CHUNK_SIZE bytes.** The base merely existing proves nothing:
     `guide.html` and `guide.html-1` are perfectly legal as two unrelated
     pages. Treating the name alone as sufficient has caused an unrelated
     page to be deleted as a "surplus fragment", and a genuinely misnumbered
     chain to be skipped by the tool written to repair it.

  2. **A short fragment terminates the chain.** Whatever else is sitting in
     the table past it is not part of the page the server serves. Ignoring
     this reassembles a blob the server never had - which then gets
     re-compressed and stored, or fails to decode with a message pointing
     nowhere.

Those two rules answer different questions, so this module exposes them
separately rather than as one "get the chain" call:

  * `owned_fragment_paths()` - every fragment belonging to a base row.
    This is the *ownership* question, and it is the right one when
    deleting or replacing a row: a gapped chain's orphaned tail still
    belongs to that base and must not be left behind.
  * `served_fragment_paths()` - the prefix of those the server would
    actually concatenate. This is the *reassembly* question, and it is the
    right one when reading content back.

Suffix discovery is deliberately suffix-agnostic (it does not assume the
chain starts at -1), because ADFA-5171 left real chains numbered from -2 in
the production database and `renumber_misnumbered_fragments.py` exists to
repair them. Probing constructed "-1", "-2", ... paths silently truncates
those.
"""
import re

# Must match WebServer.kt's "contentChunkSize" exactly. The server decides a
# row is fragmented purely by its content being this many bytes, so this
# cannot be "about 1MB" - it has to be the identical constant on both sides.
CHUNK_SIZE = 1024 * 1024

FRAGMENT_SUFFIX_RE = re.compile(r"^(.*)-(\d+)$")


def split_fragment_path(path: str):
    """("k/html/a.html", 2) for "k/html/a.html-2", or None if `path` doesn't
    have a numeric "-<N>" suffix at all. Purely syntactic - says nothing
    about whether the base is really chunked (see is_chunked_base)."""
    match = FRAGMENT_SUFFIX_RE.match(path)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def is_chunked_base(lengths: dict, base_path: str) -> bool:
    """Rule 1: whether `base_path` is the head of a chunked item, given a
    {path: content length} map. Anything shorter than CHUNK_SIZE fits in one
    row, so it owns no continuations no matter what is named after it."""
    return lengths.get(base_path) == CHUNK_SIZE


def is_continuation_path(lengths: dict, path: str) -> bool:
    """Whether `path` is a continuation row of some chunked base, rather than
    a page of its own. Rule 1 applied from the fragment's side - this is what
    a scan over every row needs in order to skip fragments without also
    skipping ordinary pages that merely look like one."""
    split = split_fragment_path(path)
    if split is None:
        return False
    base, _number = split
    return is_chunked_base(lengths, base)


def _discover(conn, base_path: str):
    """[(n, path, length)] for every existing "<base_path>-<N>" row, ordered
    by N.

    The LIKE pattern deliberately over-matches - "_" and "%" are wildcards in
    LIKE, and "-%" does not constrain the tail to digits - and the regex
    re-check is what makes the result exact. Never build a DELETE or UPDATE
    straight off that pattern."""
    found = []
    rows = conn.execute(
        "SELECT path, LENGTH(content) FROM Content WHERE path LIKE ?", (f"{base_path}-%",)
    ).fetchall()
    for path, length in rows:
        split = split_fragment_path(path)
        if split is not None and split[0] == base_path:
            found.append((split[1], path, length))
    found.sort(key=lambda item: item[0])
    return found


def owned_fragment_paths(conn, base_path: str, base_length: int = None) -> list:
    """Every continuation row belonging to `base_path`, in suffix order.

    Empty unless the base is genuinely chunked (rule 1), which is also what
    makes this cheap: the LIKE scan below is unindexed (SQLite's default LIKE
    is case-insensitive, so UNIQUE(path) can't serve it), and for the
    overwhelming majority of rows - anything under CHUNK_SIZE - it cannot
    return a continuation anyway, so it is skipped entirely.

    Pass `base_length` when the caller already knows it to avoid a second
    lookup. This is the *ownership* answer: it includes fragments past a
    short one, because a delete or replace has to take the whole tail with
    it rather than orphaning it."""
    if base_length is None:
        row = conn.execute("SELECT LENGTH(content) FROM Content WHERE path = ?", (base_path,)).fetchone()
        if row is None:
            return []
        base_length = row[0]
    if not is_chunked_base({base_path: base_length}, base_path):
        return []
    return [path for _n, path, _length in _discover(conn, base_path)]


def served_fragment_paths(conn, base_path: str, base_length: int = None) -> list:
    """The continuation rows WebServer.kt would actually concatenate: rule 1
    to decide there is a chain at all, then rule 2 - stop at the first
    fragment shorter than CHUNK_SIZE, *and* at the first gap in the
    numbering, since the server probes consecutive suffixes and stops the
    moment one is missing.

    Both halves of rule 2 matter. Without the short-fragment stop this runs
    past the end of the page; without the gap stop it jumps the hole and
    appends a tail the server never reaches. For a chain p-1 (full), p-2
    (full), p-3 missing, p-4 (short), the server serves base+p-1+p-2 and this
    must agree - otherwise insert_optimized_media reassembles bytes the
    database never served, and either stores them back or dies decompressing
    them. Gapped chains are not hypothetical: renumber_misnumbered_fragments
    reports them as a category it deliberately leaves alone.

    The one deliberate divergence from the server is where the chain is
    allowed to *start*: an ADFA-5171 chain numbered from -2 is contiguous but
    misnumbered, and the migration and repair tooling has to be able to read
    it whole in order to fix it, where the server would just serve the base.
    Contiguity is therefore enforced from whatever suffix the chain begins at,
    not from 1.

    This is the *reassembly* answer. Use it when reading a page's bytes back;
    use owned_fragment_paths when deleting or replacing them."""
    if base_length is None:
        row = conn.execute("SELECT LENGTH(content) FROM Content WHERE path = ?", (base_path,)).fetchone()
        if row is None:
            return []
        base_length = row[0]
    if not is_chunked_base({base_path: base_length}, base_path):
        return []
    served = []
    expected = None
    for number, path, length in _discover(conn, base_path):
        if expected is not None and number != expected:
            break  # a gap: the server would have stopped at the missing suffix
        served.append(path)
        if length < CHUNK_SIZE:
            break
        expected = number + 1
    return served


def reassemble(conn, base_path: str, first_content: bytes) -> bytes:
    """The full bytes the server would serve for `base_path`, given its
    already-read base blob."""
    if len(first_content) < CHUNK_SIZE:
        return first_content
    parts = [first_content]
    for path in served_fragment_paths(conn, base_path, len(first_content)):
        row = conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()
        if row is None:  # raced with a concurrent delete; serve what we have
            break
        parts.append(row[0])
    return b"".join(parts)
