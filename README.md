# deploy-with-node

Lazily yields every filesystem entry under a root path as a stat-annotated record, with optional filtering by type, depth, and device boundary.

## Usage

```python
from deploy_with_node import iterate_filesystem, EntryRecord

for entry in iterate_filesystem("./src", max_depth=3, one_device=True):
    print(entry.depth, entry.path, entry.is_file)
```

`iterate_filesystem(root, *, include_types=None, max_depth=None, one_device=False, follow_symlinks=False)` returns an iterator of `EntryRecord` instances. Each record carries `path`, `name`, `depth`, `st` (an `os.stat_result`), `is_symlink`, and the derived `is_dir` / `is_file` properties.

`include_types` accepts a `frozenset` of strings: `"dir"`, `"file"`, `"symlink"`, `"other"`. A symlink is classified as `"symlink"` regardless of what it points at, so a symlinked directory is not counted as `"dir"` when filtering.

## Why this exists

Walking a filesystem with `os.walk` buffers directory names per-level and gives you `(dirpath, dirnames, filenames)` tuples rather than a uniform stream of stat-annotated entries. When you need to make decisions per-entry — filter by type, cap depth, stop at a mount boundary — `os.walk` forces you to re-implement the same wrapper every time. This library does that once.

The trade-off: traversal is pre-order and **not sorted**. Sibling order is whatever `os.scandir` returns, which on POSIX is filesystem entry order. If you need sorted output, sort the results yourself — the iterator stays lazy so you can stop early without paying for the full walk.

## The awkward edge

Symlinked directories. By default `follow_symlinks=False`, which means a symlink is stat'd as a link, not its target. This prevents the classic infinite loop where a symlink points at an ancestor. If you turn on `follow_symlinks=True`, you are responsible for the topology of the tree you point this at.

`one_device=True` stops recursion at mount boundaries: a subdirectory on a different `st_dev` than the root is yielded (so you can see the boundary) but its children are not visited.

Unreadable directories (permission denied, or vanished between the `scandir` call and the read) are skipped silently. A streaming iterator that raises mid-traversal is hard to resume, and most callers want best-effort traversal. If you need strict error propagation, this is not the right tool.
