from .shared import Path, logging, os, platform, shutil, subprocess, sys, time, urlparse, urlunparse
from .core import logger, run_shell

def check_lfs_available(git_bin: str) -> bool:
    return run_shell(git_bin, ["lfs", "version"], realtime=False).returncode == 0


def install_lfs() -> bool:
    system = platform.system()
    logger.info("检测到大文件，但未找到 Git LFS，尝试自动安装...")
    if system == "Linux":
        for cmd in [["sudo", "apt-get", "install", "-y", "git-lfs"],
                    ["sudo", "yum", "install", "-y", "git-lfs"],
                    ["sudo", "dnf", "install", "-y", "git-lfs"],
                    ["sudo", "zypper", "install", "-y", "git-lfs"]]:
            if shutil.which(cmd[0]):
                try:
                    subprocess.run(cmd, check=True)
                    return True
                except subprocess.CalledProcessError:
                    pass
        return False
    elif system == "Darwin" and shutil.which("brew"):
        try:
            subprocess.run(["brew", "install", "git-lfs"], check=True)
            return True
        except subprocess.CalledProcessError:
            return False
    return False


def init_lfs(git_bin: str, repo_root: Path = None) -> bool:
    logger.info("执行 git lfs install 初始化仓库过滤器...")
    lfs_args = ["lfs", "install"]
    if repo_root:
        lfs_args.append("--local")
    if run_shell(git_bin, lfs_args, cwd=repo_root).returncode != 0:
        logger.error("Git LFS 初始化失败！")
        return False
    return True


def remove_stale_index_lock(git_bin: str, repo_root: Path) -> bool:
    """Remove an abandoned index lock, but never remove one held by Git.
remove_stale_index_lock 在 Windows 上必然返回 False 暂不修复    
    """
    result = subprocess.run(
        [git_bin, "rev-parse", "--git-path", "index.lock"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return True
    lock_path = Path(result.stdout.strip())
    if not lock_path.is_absolute():
        lock_path = repo_root / lock_path
    if not lock_path.exists():
        return True

    fuser = shutil.which("fuser")
    lsof = shutil.which("lsof")
    if fuser:
        holder = subprocess.run([fuser, "-s", str(lock_path)], capture_output=True)
    elif lsof:
        holder = subprocess.run([lsof, "-t", "--", str(lock_path)], capture_output=True)
    else:
        logger.error("无法确认 Git 索引锁是否被占用，请先手动检查并清理: " + str(lock_path))
        return False
    if holder.returncode == 0:
        logger.error(f"Git 索引锁正在被其他进程使用: {lock_path}")
        return False
    try:
        lock_path.unlink()
        logger.warning(f"已清理中断后遗留的 Git 索引锁: {lock_path}")
        return True
    except OSError as exc:
        logger.error(f"无法清理 Git 索引锁 {lock_path}: {exc}")
        return False


def renormalize_lfs(git_bin: str, repo_root: Path, paths: set[str] | None = None,
                    cleanup_deleted: bool = True) -> bool:
    """Re-clean tracked files after adding or changing LFS attributes."""
    logger.info("执行 Git LFS 重新规范化，确保已跟踪大文件转换为 LFS 指针...")
    realtime = logger.getEffectiveLevel() <= logging.DEBUG
    if not remove_stale_index_lock(git_bin, repo_root):
        return False
    if cleanup_deleted and run_shell(git_bin, ["add", "-u"], realtime=realtime, cwd=repo_root).returncode != 0:
        logger.error("清理已删除文件的暂存状态失败！")
        return False
    if not paths:
        return True
    add_args = ["add", "--renormalize", "--verbose", "--"] + sorted(paths)
    if run_shell(git_bin, add_args, realtime=realtime, cwd=repo_root).returncode != 0:
        logger.error("Git LFS 重新规范化失败！")
        return False
    return True

def verify_staged_lfs_files(git_bin: str, repo_root: Path) -> bool:
    """Fail before push if a staged LFS-matched file is still a large blob."""
    attrs_result = subprocess.run(
        [git_bin, "diff", "--cached", "--name-only", "-z"],
        cwd=repo_root,
        capture_output=True,
    )
    if attrs_result.returncode != 0:
        return True
    staged_paths = [path for path in attrs_result.stdout.split(b"\0") if path]
    if not staged_paths:
        return True

    attr_result = subprocess.run(
        [git_bin, "check-attr", "-z", "filter", "--stdin"],
        cwd=repo_root,
        input=b"\0".join(staged_paths) + b"\0",
        capture_output=True,
    )
    if attr_result.returncode != 0:
        logger.warning("批量检查 Git 属性失败，跳过 LFS Blob 校验。")
        return True

    # check-attr -z emits path, attribute, and value as NUL-separated fields.
    attr_fields = attr_result.stdout.split(b"\0")
    lfs_paths = [
        attr_fields[index].decode("utf-8", errors="surrogateescape")
        for index in range(0, len(attr_fields) - 2, 3)
        if attr_fields[index + 2] == b"lfs"
    ]
    if not lfs_paths:
        return True

    cat_result = subprocess.run(
        [git_bin, "cat-file", "--batch"],
        cwd=repo_root,
        input=b"\n".join(f":{path}".encode("utf-8", errors="surrogateescape") for path in lfs_paths) + b"\n",
        capture_output=True,
    )
    invalid_files = []
    output = cat_result.stdout
    offset = 0
    pointer_prefix = b"version https://git-lfs.github.com/spec/v1\n"
    for path in lfs_paths:
        header_end = output.find(b"\n", offset)
        if header_end < 0:
            invalid_files.append(path)
            break
        header = output[offset:header_end].split(b" ")
        offset = header_end + 1
        if len(header) != 3 or header[1] != b"blob":
            invalid_files.append(path)
            continue
        try:
            blob_size = int(header[2])
        except ValueError:
            invalid_files.append(path)
            continue
        blob = output[offset:offset + blob_size]
        offset += blob_size
        if offset < len(output) and output[offset:offset + 1] == b"\n":
            offset += 1
        if not blob.startswith(pointer_prefix):
            invalid_files.append(path)
    if invalid_files:
        logger.error("以下暂存文件匹配 LFS 规则，但仍是普通 Git Blob: "
                     f"{invalid_files}")
        logger.error("已阻止提交，请执行 git lfs install --local，"
                     "再执行 git add --renormalize . && git add -A。")
        return False
    return True


def install_git_filter_repo(git_bin: str) -> bool:
    """Install git-filter-repo with the same Python used to run this tool."""
    logger.info("未检测到 git-filter-repo，尝试使用清华源自动安装...")
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-i",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "--trusted-host",
        "pypi.tuna.tsinghua.edu.cn",
        "git-filter-repo",
    ]
    try:
        subprocess.check_call(command)
    except (OSError, subprocess.CalledProcessError) as error:
        logger.error(f"git-filter-repo 自动安装失败: {error}")
        return False
    return run_shell(git_bin, ["filter-repo", "--version"]).returncode == 0


def set_remote(git_bin: str, remote_url: str, repo_root: Path = None):
    if not remote_url:
        return
    check = subprocess.run([git_bin, "remote", "get-url", "origin"], cwd=repo_root,
                           capture_output=True, text=True)
    if check.returncode == 0:
        if check.stdout.strip() == remote_url:
            return
        logger.info("更新远程 origin 地址...")
        run_shell(git_bin, ["remote", "set-url", "origin", remote_url], cwd=repo_root)
    else:
        logger.info("添加远程 origin 地址...")
        run_shell(git_bin, ["remote", "add", "origin", remote_url], cwd=repo_root)


def parse_github_subdirectory_url(remote_url: str) -> tuple[str, str | None, str | None]:
    """Return (repository URL, branch, subdirectory) for a GitHub URL."""
    parsed = urlparse(remote_url)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in ("github.com", "www.github.com"):
        return remote_url, None, None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) == 3:
        owner, repository, branch = parts
        repository = repository.removesuffix(".git")
        repository_url = urlunparse(parsed._replace(
            path=f"/{owner}/{repository}.git", params="", query="", fragment=""
        ))
        return repository_url, branch, None
    if len(parts) < 5 or parts[2] not in ("blob", "tree"):
        return remote_url, None, None
    owner, repository, _, branch, *subdirectory = parts
    repository = repository.removesuffix(".git")
    repository_url = urlunparse(parsed._replace(path=f"/{owner}/{repository}.git", params="", query="", fragment=""))
    return repository_url, branch, "/".join(subdirectory)


def scan_large_files(repo_root: Path, threshold: int) -> set[str]:
    large_files = set()
    skip_dirs = {".git", "dist", "__pycache__"}
    scanned_files = 0
    scanned_dirs = 0
    last_report = time.monotonic()
    for current_root, dirnames, filenames in os.walk(repo_root, topdown=True, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in skip_dirs]
        # A submodule is a separate Git worktree. Its files must not be passed
        # to Git commands running against the containing repository.
        dirnames[:] = [
            name for name in dirnames
            if not (Path(current_root) / name / ".git").exists()
        ]
        scanned_dirs += 1
        for filename in filenames:
            path = Path(current_root) / filename
            if path.is_symlink():
                continue
            scanned_files += 1
            try:
                fsize = path.lstat().st_size
            except OSError:
                continue
            if fsize >= threshold:
                large_files.add(str(path.relative_to(repo_root)).replace("\\", "/"))
            now = time.monotonic()
            if now - last_report >= 1:
                display_path = str(path.relative_to(repo_root)).replace("\\", "/")
                prefix = f"扫描文件: {scanned_files:,} | 目录: {scanned_dirs:,} | 当前: "
                width = shutil.get_terminal_size(fallback=(120, 1)).columns
                available = max(1, width - len(prefix) - 1)
                if len(display_path) > available:
                    display_path = "..." + display_path[-max(1, available - 3):]
                status = prefix + display_path
                sys.stdout.write("\r\033[2K" + status)
                sys.stdout.flush()
                last_report = now
    if scanned_files:
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()
    return large_files


def clean_and_apply_lfs(git_bin: str, repo_root: Path, large_patterns: set[str]):
    attr_path = repo_root / ".gitattributes"
    other_lines, lfs_lines = [], set()
    nested_repo_prefixes = set()
    for current_root, dirnames, _ in os.walk(repo_root, topdown=True, followlinks=False):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in list(dirnames):
            nested_root = Path(current_root) / name
            if (nested_root / ".git").exists():
                nested_repo_prefixes.add(nested_root.relative_to(repo_root).as_posix() + "/")
                dirnames.remove(name)
    if attr_path.exists():
        with open(attr_path, "r", encoding="utf-8") as f:
            for line in f.readlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if "filter=lfs" in stripped:
                    rule_path = stripped.split(None, 1)[0].strip('"')
                    if any(rule_path.startswith(prefix) for prefix in nested_repo_prefixes):
                        continue
                    if (repo_root / rule_path).is_symlink():
                        continue
                    lfs_lines.add(stripped)
                else:
                    other_lines.append(stripped)
    for pat in large_patterns:
        safe_pat = f'"{pat}"' if " " in pat else pat
        lfs_lines.add(f"{safe_pat} filter=lfs diff=lfs merge=lfs -text")
    all_rules = other_lines + sorted(lfs_lines)
    if all_rules:
        with open(attr_path, "w", encoding="utf-8") as f:
            f.write("\n".join(all_rules) + "\n")
    logger.info(f".gitattributes 更新完成，LFS追踪总数: {len(lfs_lines)}")
