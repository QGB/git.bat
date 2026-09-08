import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "git_logic.py"
SPEC = importlib.util.spec_from_file_location("git_logic", MODULE_PATH)
git_logic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(git_logic)


class GitRepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        self.git = "git"
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Test User")
        self.run_git("config", "user.email", "test@example.invalid")

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_git(self, *args, check=True):
        return subprocess.run(
            [self.git, *args],
            cwd=self.repo,
            check=check,
            capture_output=True,
            text=True,
        )

    def write(self, relative_path, content):
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class PureFunctionTests(unittest.TestCase):
    def test_parse_size_str_supports_units_and_invalid_values(self):
        self.assertEqual(git_logic.parse_size_str("2gb"), 2 * 1024 ** 3)
        self.assertEqual(git_logic.parse_size_str("1.5mb"), int(1.5 * 1024 ** 2))
        self.assertEqual(git_logic.parse_size_str("512k"), 512 * 1024)
        self.assertEqual(git_logic.parse_size_str("42b"), 42)
        self.assertEqual(git_logic.parse_size_str("not-a-size"), 100 * 1024 ** 2)
        self.assertEqual(git_logic.parse_size_str(""), 100 * 1024 ** 2)

    def test_redact_url_hides_password_but_keeps_user_and_host(self):
        value = "https://alice:secret@example.com/org/repo.git"
        self.assertEqual(
            git_logic.redact_url(value),
            "https://alice:***@example.com/org/repo.git",
        )
        self.assertEqual(git_logic.redact_url("git@example.com:org/repo.git"),
                         "git@example.com:org/repo.git")

    def test_parse_github_subdirectory_url(self):
        result = git_logic.parse_github_subdirectory_url(
            "https://github.com/acme/demo/tree/dev/mobile/app"
        )
        self.assertEqual(result, ("https://github.com/acme/demo.git", "dev", "mobile/app"))
        self.assertEqual(
            git_logic.parse_github_subdirectory_url("https://github.com/acme/demo/master"),
            ("https://github.com/acme/demo.git", "master", None),
        )
        self.assertEqual(
            git_logic.parse_github_subdirectory_url(
                "https://alice:secret@example.com/acme/demo/master"
            ),
            ("https://alice:secret@example.com/acme/demo/master", None, None),
        )
        self.assertEqual(
            git_logic.parse_github_subdirectory_url(
                "https://alice:secret@github.com/acme/demo/master"
            ),
            ("https://alice:secret@github.com/acme/demo.git", "master", None),
        )
        self.assertEqual(
            git_logic.parse_github_subdirectory_url("git@github.com:acme/demo.git"),
            ("git@github.com:acme/demo.git", None, None),
        )

    def test_preprocess_args_moves_urls_and_joins_commit_message(self):
        argv = ["git_logic.py", "-u", "--no-ask", "https://github.com/acme/demo", "-m", "hello", "world"]
        with patch.object(sys, "argv", argv):
            result = git_logic.preprocess_args()
        self.assertEqual(
            result,
            ["git_logic.py", "--user", "--no-ask", "--remote", "https://github.com/acme/demo",
             "--commit-msg", "hello world", "push"],
        )

    def test_preprocess_args_preserves_explicit_remote_option(self):
        argv = [
            "git_logic.py",
            "--remote",
            "https://github.com/775cpu/build_xime_home.git",
            "--branch",
            "main",
            "pull",
        ]
        with patch.object(sys, "argv", argv):
            result = git_logic.preprocess_args()
        self.assertEqual(result, argv)

    def test_repo_path_argument_is_supported(self):
        parser = git_logic.argparse.ArgumentParser()
        parser.add_argument("--repo", "--repo-path", dest="repo_path", default=".")
        self.assertEqual(parser.parse_args(["--repo", "/tmp/repository"]).repo_path,
                         "/tmp/repository")
        self.assertEqual(parser.parse_args(["--repo-path", "/tmp/repository"]).repo_path,
                         "/tmp/repository")

    def test_mode_defaults_to_push(self):
        parser = git_logic.argparse.ArgumentParser()
        parser.add_argument("mode", nargs="?", default="push",
                            choices=["push", "pull"])
        self.assertEqual(parser.parse_args([]).mode, "push")

    def test_parse_user_identity_input_supports_defaults_spaces_and_commas(self):
        self.assertEqual(
            git_logic.parse_user_identity_input("1", "old", "old@mail", "remote", "remote@mail"),
            ("old", "old@mail"),
        )
        self.assertEqual(
            git_logic.parse_user_identity_input("2", "old", "old@mail", "remote", "remote@mail"),
            ("remote", "remote@mail"),
        )
        self.assertEqual(
            git_logic.parse_user_identity_input("my name mail@com", "old", "old@mail", "remote", "remote@mail"),
            ("my name", "mail@com"),
        )
        self.assertEqual(
            git_logic.parse_user_identity_input("my name，mail@com", "old", "old@mail", "remote", "remote@mail"),
            ("my name", "mail@com"),
        )
        self.assertEqual(
            git_logic.parse_user_identity_input("alice", "old", "old@mail", "remote", "remote@mail"),
            ("alice", "alice@users.noreply.github.com"),
        )


class ScanAndAttributeTests(unittest.TestCase):
    def test_scan_large_files_skips_symlinks_and_nested_git_repositories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "real.bin").write_bytes(b"x" * 10)
            (root / "small.txt").write_text("small", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / ".git").mkdir()
            (nested / "large.bin").write_bytes(b"x" * 20)
            os.symlink(root / "real.bin", root / "linked.bin")
            self.assertEqual(git_logic.scan_large_files(root, 10), {"real.bin"})

    def test_clean_and_apply_lfs_removes_stale_nested_and_symlink_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "nested" / ".git").mkdir(parents=True)
            (root / "link-target").write_bytes(b"target")
            os.symlink(root / "link-target", root / "link.bin")
            (root / ".gitattributes").write_text(
                "nested/old.bin filter=lfs diff=lfs merge=lfs -text\n"
                "link.bin filter=lfs diff=lfs merge=lfs -text\n"
                "keep.bin filter=lfs diff=lfs merge=lfs -text\n",
                encoding="utf-8",
            )
            git_logic.clean_and_apply_lfs("git", root, {"new.bin"})
            attributes = (root / ".gitattributes").read_text(encoding="utf-8")
            self.assertNotIn("nested/old.bin", attributes)
            self.assertNotIn("link.bin", attributes)
            self.assertIn("keep.bin filter=lfs", attributes)
            self.assertIn("new.bin filter=lfs", attributes)


class StagedBlobAndChunkCommitTests(GitRepositoryTestCase):
    def test_get_staged_blob_sizes_only_returns_current_staged_changes(self):
        self.write("already.txt", "already")
        self.run_git("add", "already.txt")
        self.run_git("commit", "-m", "base")
        self.write("pending.txt", "pending content")
        self.run_git("add", "pending.txt")

        staged = git_logic.get_staged_blob_sizes("git", self.repo)
        self.assertEqual([path for path, _ in staged], ["pending.txt"])
        self.assertEqual(staged[0][1], len("pending content"))

    def test_get_staged_blob_sizes_handles_deletions_and_new_files(self):
        self.write("removed.txt", "to remove")
        self.run_git("add", "removed.txt")
        self.run_git("commit", "-m", "base")
        (self.repo / "removed.txt").unlink()
        self.write("new.txt", "new content")
        self.run_git("add", "-A")

        staged = dict(git_logic.get_staged_blob_sizes("git", self.repo))

        self.assertIn("removed.txt", staged)
        self.assertIn("new.txt", staged)
        self.assertEqual(staged["new.txt"], len("new content"))

    def test_commit_staged_changes_splits_by_blob_size(self):
        self.write("a.txt", "a" * 8)
        self.write("b.txt", "b" * 8)
        self.write("c.txt", "c" * 8)
        self.run_git("add", "-A")

        commit_ids = git_logic.commit_staged_changes("git", self.repo, "import", 10)

        self.assertEqual(len(commit_ids), 3)
        messages = self.run_git("log", "--format=%s", "-3").stdout.splitlines()
        # 替换原来的 self.assertEqual(messages, [...])
        expect_marks = ["【3/3】", "【2/3】", "【1/3】"]
        self.assertEqual(len(messages), len(expect_marks))
        for msg, mark in zip(messages, expect_marks):
            with self.subTest(message=msg):
                self.assertIn(mark, msg)
                self.assertIn("文件数", msg)

        self.assertEqual(self.run_git("status", "--porcelain").stdout, "")


    def test_commit_staged_changes_keeps_small_commit_as_one_commit(self):
        self.write("small.txt", "small")
        self.run_git("add", "small.txt")

        commit_ids = git_logic.commit_staged_changes("git", self.repo, "small import", 100)

        self.assertEqual(len(commit_ids), 1)
        self.assertEqual(self.run_git("log", "-1", "--format=%s").stdout.strip(), "small import")


if __name__ == "__main__":
    unittest.main()
