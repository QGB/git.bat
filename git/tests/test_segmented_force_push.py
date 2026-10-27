import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "git_logic.py"

# 优先使用 GIT 环境变量；Windows 上显式查找 git.exe，避免误选本仓库的 git.bat 包装脚本。
GIT = os.environ.get("GIT") or shutil.which("git.exe" if os.name == "nt" else "git")


def run_git(cwd, *args):
    return subprocess.run(
        [GIT, *map(str, args)],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


class SegmentedForcePushTests(unittest.TestCase):
    def setUp(self):
        if not GIT:
            self.skipTest("未找到 git 可执行文件：请将 git 加入 PATH 或设置 GIT 环境变量")

    def test_force_option_pushes_each_chunk_when_remote_has_diverged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote = root / "remote.git"
            seed = root / "seed"
            local = root / "local"
            remote_update = root / "remote-update"

            run_git(root, "init", "--bare", "--initial-branch=main", remote)
            seed.mkdir()
            run_git(seed, "init", "-q", "-b", "main")
            run_git(seed, "config", "user.name", "Test User")
            run_git(seed, "config", "user.email", "test@example.invalid")
            (seed / "base.txt").write_text("base", encoding="utf-8")
            run_git(seed, "add", ".")
            run_git(seed, "commit", "-m", "base")
            run_git(seed, "remote", "add", "origin", remote)
            run_git(seed, "push", "-u", "origin", "main")

            run_git(root, "clone", "-q", remote, local)
            run_git(local, "config", "user.name", "Test User")
            run_git(local, "config", "user.email", "test@example.invalid")
            run_git(root, "clone", "-q", remote, remote_update)
            run_git(remote_update, "config", "user.name", "Test User")
            run_git(remote_update, "config", "user.email", "test@example.invalid")
            (remote_update / "remote-only.txt").write_text("remote", encoding="utf-8")
            run_git(remote_update, "add", ".")
            run_git(remote_update, "commit", "-m", "remote-only")
            run_git(remote_update, "push", "origin", "main")

            for name in ("a.txt", "b.txt", "c.txt"):
                (local / name).write_text("12345678", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--verbose",
                    "0",
                    "--repo-path",
                    str(local),
                    "--branch",
                    "main",
                    "--remote",
                    str(remote),
                    "--max-commit-size",
                    "8",
                    "push",
                    "--no-ask",
                    "-f",
                    "-m",
                    "segmented force regression",
                ],
                cwd=local,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            tree = run_git(
                root,
                "--git-dir",
                remote,
                "ls-tree",
                "-r",
                "--name-only",
                "refs/heads/main",
            ).stdout.splitlines()
            self.assertEqual(set(tree), {"base.txt", "a.txt", "b.txt", "c.txt"})
            commit_count = run_git(
                root,
                "--git-dir",
                remote,
                "rev-list",
                "--count",
                "refs/heads/main",
            ).stdout.strip()
            self.assertEqual(commit_count, "4")


if __name__ == "__main__":
    unittest.main()
