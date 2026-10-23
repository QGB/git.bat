from .shared import Path, logging, os, subprocess, sys, tempfile
from .core import is_git_repository, logger, run_shell, stime
from .lfs import remove_stale_index_lock, renormalize_lfs, verify_staged_lfs_files
from .network import run_network_retry
from .identity import apply_git_user_config

def get_staged_blob_sizes(git_bin: str, repo_root: Path) -> list[tuple[str, int]]:
    """Return staged paths and their indexed blob sizes without reading worktree files."""
    staged_result = subprocess.run(
        [git_bin, "diff", "--cached", "--name-only", "-z"],
        cwd=repo_root,
        capture_output=True,
    )
    if staged_result.returncode != 0:
        return []
    staged_paths = [
        path.decode("utf-8", errors="surrogateescape")
        for path in staged_result.stdout.split(b"\0")
        if path
    ]
    if not staged_paths:
        return []

    index_result = subprocess.run(
        [git_bin, "ls-files", "--stage", "-z"],
        cwd=repo_root,
        capture_output=True,
    )
    if index_result.returncode != 0:
        return []
    index_entries = {}
    object_ids = []
    for record in index_result.stdout.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        metadata, path = record.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) != 3:
            continue
        object_id = fields[1].decode("ascii")
        decoded_path = path.decode("utf-8", errors="surrogateescape")
        index_entries[decoded_path] = object_id
        object_ids.append(object_id)
    if not object_ids:
        return []
    size_result = subprocess.run(
        [git_bin, "cat-file", "--batch-check=%(objectsize)"],
        cwd=repo_root,
        input=("\n".join(object_ids) + "\n").encode("ascii"),
        capture_output=True,
    )
    if size_result.returncode != 0:
        return []
    sizes = size_result.stdout.splitlines()
    object_sizes = {
        object_id: int(size)
        for object_id, size in zip(object_ids, sizes)
        if size.isdigit()
    }
    return [
        (path, object_sizes.get(index_entries.get(path, ""), 0))
        for path in staged_paths
    ]


def commit_staged_changes(git_bin: str, repo_root: Path, commit_msg: str,
                          max_commit_bytes: int) -> list[str] | None:
    """Commit staged files in bounded batches when a single commit is too large."""
    staged = get_staged_blob_sizes(git_bin, repo_root)
    if not staged:
        return None
    total_size = sum(size for _, size in staged)
    if total_size <= max_commit_bytes:
        if run_shell(git_bin, ["commit", "-m", commit_msg], cwd=repo_root).returncode != 0:
            return None
        result = subprocess.run([git_bin, "rev-parse", "HEAD"], cwd=repo_root,
                                capture_output=True, text=True)
        return [result.stdout.strip()] if result.returncode == 0 else None

    batches = []
    current, current_size = [], 0
    for path, size in sorted(staged, key=lambda item: item[0]):
        if current and current_size + size > max_commit_bytes:
            batches.append(current)
            current, current_size = [], 0
        current.append(path)
        current_size += size
    if current:
        batches.append(current)

    logger.warning(f"暂存内容约 {total_size / 1024 / 1024 / 1024:.2f} GiB，"
                   f"超过单提交上限 {max_commit_bytes / 1024 / 1024 / 1024:.2f} GiB，"
                   f"将拆分为 {len(batches)} 个提交。")
    if run_shell(git_bin, ["reset", "--", "."], cwd=repo_root).returncode != 0:
        logger.error("拆分提交时清空暂存区失败！")
        return None
    committed_ids = []
    for index, batch in enumerate(batches, 1):
        try:
            with tempfile.NamedTemporaryFile(prefix="git-pathspec-", mode="wb", delete=False) as path_file:
                path_file.write(b"\0".join(path.encode("utf-8", errors="surrogateescape") for path in batch))
                path_file.write(b"\0")
                pathspec_file = path_file.name
            try:
                add_result = run_shell(
                    git_bin,
                    ["add", "-A", "--pathspec-from-file=" + pathspec_file, "--pathspec-file-nul"],
                    cwd=repo_root,
                )
            finally:
                os.unlink(pathspec_file)
        except OSError as error:
            logger.error(f"第 {index}/{len(batches)} 段生成路径清单失败: {error}")
            return None
        if add_result.returncode != 0:
            logger.error(f"第 {index}/{len(batches)} 段重新暂存失败！")
            return None
        part_msg = f"【{index}/{len(batches)}】文件数：{len(batch)} {commit_msg}"
        if run_shell(git_bin, ["commit", "-m", part_msg], cwd=repo_root).returncode != 0:
            logger.error(f"第 {index}/{len(batches)} 段提交失败！")
            return None
        commit_result = subprocess.run(
            [git_bin, "rev-parse", "HEAD"], cwd=repo_root,
            capture_output=True, text=True,
        )
        if commit_result.returncode != 0 or not commit_result.stdout.strip():
            logger.error(f"第 {index}/{len(batches)} 段提交后无法读取提交 ID！")
            return None
        committed_ids.append(commit_result.stdout.strip())
        logger.info(f"✅ 已提交第 {index}/{len(batches)} 段，文件数: {len(batch)}")
    return committed_ids


def git_push(git_bin: str, branch: str, repo_root: Path, extra_args: list[str],
             commit_msg: str = "", remote_url: str = "",
             user_arg: str = None, retry_count: int = 10, retry_seconds=5,
             connect_timeout: int = 45, low_speed_limit: int = 1000, low_speed_time: int = 30,
             no_ask: bool = False, lfs_paths: set[str] | None = None,
             max_commit_bytes: int = 1900 * 1024 * 1024):
    EmptyAfterPush = False
    logger.info(f"当前工作目录: {repo_root.resolve()}")
    if not is_git_repository(git_bin, repo_root):
        logger.error("当前目录尚未初始化 Git 仓库。")
        sys.exit(1)
    apply_git_user_config(git_bin, remote_url, user_arg, no_ask, repo_root)
    realtime = logger.getEffectiveLevel() <= logging.DEBUG
    if not remove_stale_index_lock(git_bin, repo_root):
        sys.exit(1)
    if run_shell(git_bin, ["add", "-A", "--verbose"], realtime=realtime, cwd=repo_root).returncode != 0:
        logger.error("git add 失败")
        sys.exit(1)
    if lfs_paths and not renormalize_lfs(git_bin, repo_root, lfs_paths, cleanup_deleted=False):
        sys.exit(1)
    if (repo_root / ".gitattributes").is_file():
        if not verify_staged_lfs_files(git_bin, repo_root):
            sys.exit(1)
    status_result = subprocess.run([git_bin, "status", "--porcelain"], capture_output=True, text=True)
    changed_files = []
    if status_result.returncode == 0 and status_result.stdout.strip():
        changed_files = [line[3:].strip() for line in status_result.stdout.strip().split("\n") if line[3:].strip()]
    submodule_result = subprocess.run([git_bin, "ls-files", "--stage"], capture_output=True, text=True)
    submodule_paths = {
        line.split("\t", 1)[1].strip()
        for line in submodule_result.stdout.splitlines()
        if line.startswith("160000 ") and "\t" in line
    }
    staged_result = subprocess.run([git_bin, "diff", "--cached", "--name-only"], capture_output=True, text=True)
    staged_files = []
    if staged_result.returncode == 0 and staged_result.stdout.strip():
        staged_files = [line.strip() for line in staged_result.stdout.splitlines() if line.strip()]
    if not staged_files:
        submodule_changes = [path for path in changed_files
                             if path in submodule_paths or any(path.startswith(f"{item}/") for item in submodule_paths)]
        if submodule_changes:
            logger.warning("检测到子模块内部有未提交修改，但父仓库没有可提交的暂存内容: "
                           f"{submodule_changes}")
            logger.warning("请进入子模块单独提交，或在父仓库提交子模块更新后的 gitlink。")
        changed_files = []
    else:
        changed_files = staged_files
    if not commit_msg:
        max_file, max_size = None, -1
        for rel in changed_files:
            fp = repo_root / rel
            if not fp.is_file():
                continue
            try:
                sz = fp.stat().st_size
            except OSError:
                continue
            if sz > max_size:
                max_size = sz
                max_file = rel.replace("\\", "/")
        commit_msg = (f"[{max_file} {max_size}B] {stime()} {str(SCRIPT_FILE)[-20:]} auto"
                      if max_file else f" auto {stime()}")
    commit_ids = []
    if changed_files:
        logger.info(f"变更文件: {len(changed_files)} 个" + (
            f" (显示前10: {changed_files[:10]})" if len(changed_files) > 10 else f" {changed_files}"))
        for f in changed_files:
            if f == "ReadMe.md":
                with open(repo_root / "ReadMe.md", 'rb') as fh:
                    if b'#EmptyAfterPush' in fh.read():
                        EmptyAfterPush = True
        commit_ids = commit_staged_changes(git_bin, repo_root, commit_msg, max_commit_bytes)
        if not commit_ids:
            logger.error("git commit 失败。")
            logger.error("请查看上方 Git 输出；可执行 git status 和 git diff --cached 进一步确认暂存内容。")
            sys.exit(1)
    else:
        logger.info("暂存区为空")

    is_debug = logger.getEffectiveLevel() <= logging.DEBUG
    # 注入 HTTP 超时配置（临时生效，不修改全局配置）
    git_config_args = [
        "-c", f"http.connectTimeout={connect_timeout}",
        "-c", f"http.lowSpeedLimit={low_speed_limit}",
        "-c", f"http.lowSpeedTime={low_speed_time}"
    ]
    

    push_targets = commit_ids or [None]
    for index, commit_id in enumerate(push_targets, 1):
        push_args = git_config_args + ["push", "-v", "--progress"] + extra_args
        if commit_id:
            push_args.extend([remote_url, f"{commit_id}:refs/heads/{branch}"])
        else:
            push_args.extend([remote_url, branch])
        run_network_retry(git_bin, push_args, f"推送第 {index}/{len(push_targets)} 段"
                          if len(push_targets) > 1 else "推送",
                          remote_url, branch, retry_count, retry_seconds, is_debug)
    if EmptyAfterPush:
        with open(repo_root / 'ReadMe.md', 'wb') as f:
            f.write(b'')
        logger.info(f"EmptyAfterPush 成功 {stime()}")
    logger.info(f"✅ 推送成功 {stime()}")
