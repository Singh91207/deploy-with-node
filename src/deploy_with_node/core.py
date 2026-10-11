"""Lazy filesystem iterator with stat annotations and cross-device boundary.

Design decisions (stated plainly so the tests and README agree):

- The iterator yields `EntryRecord` instances in a deterministic, pre-order
  traversal: a directory is yielded *before* its children. Sibling order is the
  natural order returned by `os.scandir`, which on POSIX is filesystem entry
  order and is *not* guaranteed to be sorted. We do not sort because sorting
  defeats laziness — the whole point of the library is to stream entries.

- Depth is zero-based: the root itself is depth 0, its immediate children are
  depth 1, and so on.

- `one_device=True` stops recursion at any subdirectory whose `st_dev` differs
  from the root's `st_dev`. The differing directory itself is still yielded (so
  the caller can see the boundary), but its children are not descended into.

- `follow_symlinks=False` by default. We never follow symlinks for *stat*; when
  `follow_symlinks` is False, `EntryRecord.is_symlink` reflects the link itself
  and `is_dir` / `is_file` describe the link, not the target. This avoids the
  classic infinite-loop trap where a symlink points at an ancestor.

- If a directory cannot be read (permission denied, or it vanished between the
  scandir call and the read), we yield nothing for its children and do not raise.
  A `PermissionError` during `scandir` is caught and silently skipped. This is a
  deliberate trade-off: a streaming iterator that raises mid-traversal is hard
  to resume, and most callers want "best effort, skip what you can't read".
"""

from __future__ import annotations

import os
import stat as _stat
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True)
class EntryRecord:
    """A single filesystem entry with its stat metadata.

    `path` is the full path as a string. `name` is the final path component.
    `depth` is zero-based from the iteration root. `st` is the raw
    `os.stat_result` (via `os.scandir`'s entry, so no extra syscall is made on
    platforms where scandir caches it). `is_symlink` is True if the entry itself
    is a symbolic link, independent of `follow_symlinks`.
    """

    path: str
    name: str
    depth: int
    st: os.stat_result
    is_symlink: bool

    @property
    def is_dir(self) -> bool:
        """True if the entry is a directory (respecting follow_symlinks).

        When the entry is a symlink and `follow_symlinks` was False, this
        describes the *link*, not the target — i.e. a symlink is not a dir.
        """
        return _stat.S_ISDIR(self.st.st_mode)

    @property
    def is_file(self) -> bool:
        """True if the entry is a regular file (respecting follow_symlinks)."""
        return _stat.S_ISREG(self.st.st_mode)


def _entry_to_record(entry: os.DirEntry, depth: int, follow_symlinks: bool) -> EntryRecord:
    """Convert a scandir DirEntry into an EntryRecord.

    `os.DirEntry.stat(follow_symlinks=...)` uses the cached stat on platforms
    that provide it (Linux, macOS), avoiding a redundant syscall. We rely on
    that here rather than calling `os.stat` separately.
    """
    st = entry.stat(follow_symlinks=follow_symlinks)
    return EntryRecord(
        path=entry.path,
        name=entry.name,
        depth=depth,
        st=st,
        is_symlink=entry.is_symlink(),
    )


def iterate_filesystem(
    root: str,
    *,
    include_types: Optional[frozenset] = None,
    max_depth: Optional[int] = None,
    one_device: bool = False,
    follow_symlinks: bool = False,
) -> Iterator[EntryRecord]:
    """Lazily yield every entry under `root` as an `EntryRecord`.

    Parameters
    ----------
    root:
        Directory to traverse. Must exist and be a directory.
    include_types:
        Optional frozenset of strings from {"dir", "file", "symlink", "other"}.
        If given, only entries whose primary classification matches are yielded.
        A symlink is classified as "symlink" regardless of what it points at,
        so symlinked directories are *not* counted as "dir" when filtering —
        this keeps the filter predictable.
    max_depth:
        If set, do not descend into directories deeper than this (0-based).
        `max_depth=0` yields only the root. `max_depth=1` yields the root and
        its immediate children. None means unlimited.
    one_device:
        If True, do not recurse into subdirectories on a different `st_dev`
        than the root. The boundary directory itself is still yielded.
    follow_symlinks:
        Passed to `DirEntry.stat`. When False (the default), symlinks are
        stat'd as links, not their targets, preventing loops.

    Yields
    ------
    EntryRecord
        Entries in pre-order (parent before children).

    Raises
    ------
    NotADirectoryError
        If `root` does not exist or is not a directory.
    """
    root_stat = os.stat(root, follow_symlinks=False)
    if not _stat.S_ISDIR(root_stat.st_mode):
        raise NotADirectoryError(f"not a directory: {root!r}")
    root_dev = root_stat.st_dev

    # We treat the root specially so that it is always yielded at depth 0,
    # even though it is not produced by a scandir call.
    root_entry_info = EntryRecord(
        path=os.path.abspath(root),
        name=os.path.basename(os.path.abspath(root)) or root,
        depth=0,
        st=root_stat,
        is_symlink=os.path.islink(root),
    )

    def _classify(rec: EntryRecord) -> str:
        if rec.is_symlink:
            return "symlink"
        if rec.is_dir:
            return "dir"
        if rec.is_file:
            return "file"
        return "other"

    def _passes_filter(rec: EntryRecord) -> bool:
        if include_types is None:
            return True
        return _classify(rec) in include_types

    # Explicit stack so traversal is genuinely lazy and we never build a full
    # list in memory. Each item is (dir_path, depth).
    stack = [(root, 0)]

    # Yield the root first if it passes the filter.
    if _passes_filter(root_entry_info):
        yield root_entry_info

    while stack:
        current_dir, depth = stack.pop(0)

        # Children are at depth+1. If we've hit max_depth, don't even scan.
        if max_depth is not None and depth >= max_depth:
            continue

        try:
            scandir_iter = os.scandir(current_dir)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            # Vanished or unreadable: skip silently. See module docstring.
            continue

        # We collect child dirs to recurse into, but yield all entries as we go.
        # Using a list for deferred-push keeps ordering stable (FIFO via pop(0)).
        child_dirs = []

        try:
            for entry in scandir_iter:
                try:
                    rec = _entry_to_record(entry, depth + 1, follow_symlinks)
                except OSError:
                    # Entry vanished or became unreadable between scandir and
                    # stat. Skip it rather than aborting the whole traversal.
                    continue

                if _passes_filter(rec):
                    yield rec

                # Decide whether to recurse. We only recurse into real
                # directories (not symlinks, unless follow_symlinks is True
                # and the symlink target is a dir — handled by st being the
                # target's stat in that case).
                if rec.is_dir:
                    if one_device and rec.st.st_dev != root_dev:
                        # Cross-device boundary: yield it (already done above
                        # if it passed the filter) but don't descend.
                        continue
                    child_dirs.append((entry.path, depth + 1))
        finally:
            scandir_iter.close()

        # Push child dirs to the front-preserving queue. We use pop(0) from the
        # stack, so to keep sibling order correct we must push them in order
        # at the *front*. Inserting reversed at index 0 achieves FIFO.
        for i, item in enumerate(reversed(child_dirs)):
            stack.insert(0, item)
