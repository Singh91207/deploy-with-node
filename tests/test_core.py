"""Tests for deploy_with_node.core.

All tests build a temporary tree on disk, iterate it, and assert on the
records yielded. No network, no sleep, no wall-clock time.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from deploy_with_node.core import EntryRecord, iterate_filesystem


class _TempTree:
    """Context manager that builds a known directory tree and cleans up."""

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="dwn_test_")
        return self

    def __exit__(self, *exc):
        # Best-effort cleanup; tempfile handles the rest on OS temp rotation.
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)
        return False

    def makedirs(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(p, exist_ok=True)
        return p

    def touch(self, rel, content=b"x"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)
        return p

    def symlink(self, target_rel, link_rel):
        link_path = os.path.join(self.root, link_rel)
        target_path = os.path.join(self.root, target_rel)
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        os.symlink(target_path, link_path)
        return link_path


class TestYieldsRootAndChildren(unittest.TestCase):
    def test_root_is_yielded_at_depth_zero(self):
        with _TempTree() as t:
            t.touch("a.txt")
            records = list(iterate_filesystem(t.root))
            root_rec = records[0]
            self.assertEqual(root_rec.depth, 0)
            self.assertTrue(root_rec.is_dir)
            self.assertEqual(os.path.abspath(root_rec.path), os.path.abspath(t.root))

    def test_immediate_children_are_depth_one(self):
        with _TempTree() as t:
            t.touch("a.txt")
            t.touch("b.txt")
            t.makedirs("subdir")
            records = [
                r for r in iterate_filesystem(t.root) if r.depth == 1
            ]
            names = {r.name for r in records}
            self.assertEqual(names, {"a.txt", "b.txt", "subdir"})


class TestPreOrder(unittest.TestCase):
    def test_parent_before_child(self):
        with _TempTree() as t:
            t.makedirs("d/sub/deep")
            t.touch("d/sub/deep/leaf.txt")
            records = list(iterate_filesystem(t.root))
            paths = [r.path for r in records]
            # The root must come first.
            self.assertEqual(paths[0], os.path.abspath(t.root))
            # 'd' must appear before 'd/sub' which must appear before the leaf.
            idx_d = next(i for i, p in enumerate(paths) if p.endswith(os.sep + "d"))
            idx_sub = next(i for i, p in enumerate(paths) if p.endswith(os.sep + "sub"))
            idx_leaf = next(i for i, p in enumerate(paths) if p.endswith("leaf.txt"))
            self.assertLess(idx_d, idx_sub)
            self.assertLess(idx_sub, idx_leaf)


class TestMaxDepth(unittest.TestCase):
    def test_max_depth_zero_yields_only_root(self):
        with _TempTree() as t:
            t.touch("a.txt")
            t.makedirs("d")
            records = list(iterate_filesystem(t.root, max_depth=0))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].depth, 0)

    def test_max_depth_one_yields_root_and_immediate_children(self):
        with _TempTree() as t:
            t.touch("a.txt")
            t.makedirs("d/nested")
            t.touch("d/nested/deep.txt")
            records = list(iterate_filesystem(t.root, max_depth=1))
            depths = {r.depth for r in records}
            self.assertEqual(depths, {0, 1})
            # nested dir is at depth 2, should not appear.
            names = {r.name for r in records}
            self.assertNotIn("nested", names)
            self.assertNotIn("deep.txt", names)


class TestIncludeTypes(unittest.TestCase):
    def test_filter_files_only(self):
        with _TempTree() as t:
            t.touch("a.txt")
            t.makedirs("d")
            t.touch("d/b.txt")
            records = list(
                iterate_filesystem(t.root, include_types=frozenset({"file"}))
            )
            self.assertTrue(all(r.is_file for r in records))
            names = {r.name for r in records}
            self.assertEqual(names, {"a.txt", "b.txt"})

    def test_filter_dirs_only_excludes_symlinks_to_dirs(self):
        with _TempTree() as t:
            t.makedirs("realdir")
            t.symlink("realdir", "linkdir")
            records = list(
                iterate_filesystem(t.root, include_types=frozenset({"dir"}))
            )
            names = {r.name for r in records}
            # root is a dir, realdir is a dir; linkdir is classified as symlink.
            self.assertIn("realdir", names)
            self.assertNotIn("linkdir", names)

    def test_filter_symlinks_only(self):
        with _TempTree() as t:
            t.makedirs("realdir")
            t.touch("realfile.txt")
            t.symlink("realdir", "linkdir")
            t.symlink("realfile.txt", "linkfile.txt")
            records = list(
                iterate_filesystem(t.root, include_types=frozenset({"symlink"}))
            )
            names = {r.name for r in records}
            self.assertEqual(names, {"linkdir", "linkfile.txt"})
            self.assertTrue(all(r.is_symlink for r in records))


class TestSymlinks(unittest.TestCase):
    def test_symlink_not_followed_by_default(self):
        with _TempTree() as t:
            t.makedirs("realdir")
            t.touch("realdir/secret.txt")
            t.symlink("realdir", "linkdir")
            records = list(iterate_filesystem(t.root))
            paths = [r.path for r in records]
            # secret.txt is inside realdir; linkdir is a symlink to realdir.
            # Since we don't follow, secret.txt should appear once (via realdir).
            secret_count = sum(1 for p in paths if p.endswith("secret.txt"))
            self.assertEqual(secret_count, 1)

    def test_symlink_record_has_is_symlink_true(self):
        with _TempTree() as t:
            t.touch("target.txt")
            t.symlink("target.txt", "link.txt")
            records = list(iterate_filesystem(t.root))
            link_rec = next(r for r in records if r.name == "link.txt")
            self.assertTrue(link_rec.is_symlink)
            # Not following: is_file describes the link itself, which is not a
            # regular file in the st_mode sense.
            self.assertFalse(link_rec.is_file)


class TestNonDirectoryRoot(unittest.TestCase):
    def test_file_root_raises_not_a_directory(self):
        with _TempTree() as t:
            f = t.touch("plain.txt")
            with self.assertRaises(NotADirectoryError):
                list(iterate_filesystem(f))

    def test_missing_root_raises(self):
        with self.assertRaises((NotADirectoryError, FileNotFoundError)):
            list(iterate_filesystem("/nonexistent/path/that/should/not/exist/dwn"))


class TestEntryRecordProperties(unittest.TestCase):
    def test_record_is_frozen(self):
        with _TempTree() as t:
            t.touch("a.txt")
            rec = next(iter(iterate_filesystem(t.root)))
            with self.assertRaises(Exception):
                rec.path = "mutated"  # type: ignore

    def test_stat_result_is_real(self):
        with _TempTree() as t:
            t.touch("a.txt")
            records = list(iterate_filesystem(t.root))
            file_rec = next(r for r in records if r.name == "a.txt")
            self.assertIsInstance(file_rec.st, os.stat_result)
            self.assertTrue(stat.S_ISREG(file_rec.st.st_mode))


class TestOneDevice(unittest.TestCase):
    """one_device only has observable behaviour when st_dev changes, which
    we can't force on a single filesystem. We test the *contract*: when
    one_device is True and a subdir has the same dev, traversal proceeds
    normally. The cross-device skip is exercised by checking that the code
    path does not raise and yields the same results as one_device=False on
    a single-device tree."""

    def test_one_device_true_same_as_false_on_single_device(self):
        with _TempTree() as t:
            t.makedirs("d/sub")
            t.touch("d/sub/leaf.txt")
            without = list(iterate_filesystem(t.root, one_device=False))
            with_flag = list(iterate_filesystem(t.root, one_device=True))
            # Same set of paths, same order.
            self.assertEqual(
                [r.path for r in without], [r.path for r in with_flag]
            )


class TestUnreadableDirectory(unittest.TestCase):
    """If a directory is unreadable (permission denied), the iterator skips
    it rather than raising. We can only test this on POSIX where chmod 000
    actually blocks reads for non-root. Skip on Windows or if running as root.
    """

    def setUp(self):
        import sys
        if os.name == "nt":
            self.skipTest("chmod semantics differ on Windows")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses permission checks")

    def test_unreadable_dir_is_skipped(self):
        with _TempTree() as t:
            t.touch("readable.txt")
            locked = t.makedirs("locked")
            t.touch("locked/hidden.txt")
            os.chmod(locked, 0o000)
            try:
                records = list(iterate_filesystem(t.root))
                names = {r.name for r in records}
                self.assertIn("readable.txt", names)
                # 'locked' itself is yielded (it's a dir entry in the parent),
                # but 'hidden.txt' inside it is not because we can't scan it.
                self.assertIn("locked", names)
                self.assertNotIn("hidden.txt", names)
            finally:
                os.chmod(locked, 0o755)


class TestEmptyDirectory(unittest.TestCase):
    def test_empty_root_yields_only_root(self):
        with _TempTree() as t:
            records = list(iterate_filesystem(t.root))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].depth, 0)

    def test_empty_subdir_yielded_but_not_descended_error(self):
        with _TempTree() as t:
            t.makedirs("empty_sub")
            records = list(iterate_filesystem(t.root))
            names = {r.name for r in records}
            self.assertIn("empty_sub", names)
            # No children beyond root and empty_sub.
            self.assertEqual(len(records), 2)


if __name__ == "__main__":
    unittest.main()
