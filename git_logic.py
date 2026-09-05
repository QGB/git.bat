#!/usr/bin/env python3
import argparse, logging, os, platform, shutil, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("GitAutoLFS")


def setup_logging(verbosity: int):
    levels = {0: logging.ERROR, 1: logging.WARNING, 2: logging.INFO, 3: logging.DEBUG}
    level = levels.get(verbosity, logging.DEBUG if verbosity > 3 else logging.ERROR)
    formatter = logging.Formatter(fmt='%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.setLevel(level)
    if not logger.handlers:
        logger.addHandler(handler)


def stime():
    ft = time.time()
    return time.strftime('%Y-%m-%d__%H.%M.%S', time.localtime(ft)) + '__.' + f"{ft:.3f}".split('.')[1]


def kill_process_tree(pid: int, proc: subprocess.Popen = None):
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass
    else:
        if proc:
            try:
                proc.kill()
            except:
                pass


def looks_like_url(s: str) -> bool:
    return s.startswith("https://") or s.startswith("git@") or "://" in s


def redact_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc and "@" in parsed.netloc:
        userinfo, host = parsed.netloc.rsplit("@", 1)
        username = userinfo.split(":", 1)[0]
        return urlunparse(parsed._replace(netloc=f"{username}:***@{host}"))
    return value


def parse_size_str(val: str) -> int:
    if not val:
        return 104857600
    s = str(val).strip().lower()
    multiplier = 1
    if s.endswith("gb") or s.endswith("g"):
        multiplier = 1024 ** 3
        s = s.rstrip("gb").rstrip("g")
    elif s.endswith("mb") or s.endswith("m"):
        multiplier = 1024 ** 2
        s = s.rstrip("mb").rstrip("m")
    elif s.endswith("kb") or s.endswith("k"):
        multiplier = 1024
        s = s.rstrip("kb").rstrip("k")
    elif s.endswith("b"):
        s = s.rstrip("b")
    try:
        return int(float(s) * multiplier)
    except ValueError:
        return 104857600


def preprocess_args():
    valid_modes = {"push", "pull", "clone", "config", "init", "list-big", "listbig", "remove-big", "undo"}
    raw = sys.argv[1:]
    url_indices = {i for i, arg in enumerate(raw) if looks_like_url(arg)}
    new, need_auto_user, i = [], False, 0
    while i < len(raw):
        arg = raw[i]
        if arg in ("-m", "--commit-msg", "--commit_msg"):
            msg_parts = raw[i + 1:]
            if msg_parts:
                new.append("--commit-msg")
                new.append(" ".join(msg_parts))
            else:
                new.append(arg)
            i = len(raw)
            continue
        if arg in ("-u", "--user"):
            if i + 1 < len(raw):
                nxt = raw[i + 1]
                if (i + 1) in url_indices:
                    new.extend(["--user", "--remote", nxt])
                    i += 2
                    continue
                elif nxt in valid_modes or nxt.startswith("-"):
                    need_auto_user = True
                    i += 1
                    continue
                else:
                    new.extend([arg, nxt])
                    i += 2
                    continue
            else:
                need_auto_user = True
                i += 1
                continue
        if looks_like_url(arg):
            new.extend(["--remote", arg])
            i += 1
            continue
        new.append(arg)
        i += 1
    if need_auto_user:
        new.append("--user")
    if not any(a in valid_modes for a in new):
        new.append("push")
    return [sys.argv[0]] + new


def find_git(user_git: str) -> str:
    if user_git and Path(user_git).is_file():
        return user_git
    env_git = os.environ.get("GIT_PATH", "")
    if env_git and Path(env_git).is_file():
        return env_git
    sys_git = shutil.which("git")
    if sys_git:
        return sys_git
    logger.critical("无法找到 git 可执行文件。请设置 GIT_PATH 或确保 git 在 PATH 中。")
    sys.exit(1)


def get_origin_url(git_bin: str) -> str:
    try:
        res = subprocess.run([git_bin, "remote", "get-url", "origin"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except:
        pass
    return ""


def get_current_branch(git_bin: str) -> str:
    """Return the current branch, including an unborn branch after git init."""
    try:
        result = subprocess.run(
            [git_bin, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return ""


def get_branch_tracking_url(git_bin: str, branch: str) -> str:
    try:
        remote_name = subprocess.run([git_bin, "config", "--get", f"branch.{branch}.remote"],
                                     capture_output=True, text=True).stdout.strip()
        if not remote_name:
            return ""
        url_res = subprocess.run([git_bin, "remote", "get-url", remote_name], capture_output=True, text=True)
        if url_res.returncode == 0 and url_res.stdout.strip():
            return url_res.stdout.strip()
        if looks_like_url(remote_name):
            return remote_name
    except:
        pass
    return ""


def run_shell(git_bin: str, args: list[str], realtime: bool = False, extra_env: dict = None,
              cwd: Path = None) -> subprocess.CompletedProcess:
    cmd = [git_bin] + args
    git_exe_path = Path(git_bin).resolve()
    git_bin_dir = git_exe_path.parent
    git_root = git_bin_dir.parent
    portable_paths = [str(git_root / "cmd"), str(git_bin_dir), str(git_root / "mingw64" / "bin"),
                      str(git_root / "usr" / "bin")]
    env = os.environ.copy()
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    env["GIT_FLUSH"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    valid_paths = [p for p in portable_paths if os.path.exists(p)]
    env["PATH"] = os.pathsep.join(valid_paths) + os.pathsep + env.get("PATH", "")
    if extra_env:
        env.update(extra_env)

    logger.info(f"▶ RUN: {' '.join(redact_url(arg) for arg in cmd)}")
    proc = None
    try:
        if realtime:
            proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1)
            output_chunks = []
            for char in iter(lambda: proc.stdout.read(1), ''):
                sys.stdout.write(char)
                sys.stdout.flush()
                output_chunks.append(char)
            retcode = proc.wait()
            return subprocess.CompletedProcess(cmd, retcode, stdout=''.join(output_chunks), stderr='')
        else:
            proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace")
            stdout, stderr = proc.communicate()
            if stdout and stdout.strip():
                logger.debug(f"[STDOUT]\n{stdout.strip()}")
            if stderr and stderr.strip():
                if proc.returncode == 0:
                    logger.debug(f"[STDERR]\n{stderr.strip()}")
                else:
                    logger.error(f"[STDERR]\n{stderr.strip()}")
            if proc.returncode != 0:
                logger.warning(f"命令执行非 0 返回码: {proc.returncode}")
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except KeyboardInterrupt:
        logger.warning("\n[CANCEL] 收到中断信号，正在清理 Git 进程树...")
        if proc and proc.poll() is None:
            kill_process_tree(proc.pid, proc)
        logger.warning("[CANCEL] 所有相关进程已终止。")
        sys.exit(130)
    except Exception as e:
        logger.critical(f"执行异常: {repr(e)}")
        raise


def check_lfs_available(git_bin: str) -> bool:
    return run_shell(git_bin, ["lfs", "version"], realtime=False).returncode == 0


def is_lfs_initialized(repo_root: Path) -> bool:
    hook_path = repo_root / ".git" / "hooks" / "pre-push"
    if hook_path.exists():
        try:
            if 'git-lfs' in hook_path.read_text(encoding='utf-8', errors='ignore'):
                return True
        except:
            pass
    return False


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


def renormalize_lfs(git_bin: str, repo_root: Path) -> bool:
    """Re-clean tracked files after adding or changing LFS attributes."""
    logger.info("执行 Git LFS 重新规范化，确保已跟踪大文件转换为 LFS 指针...")
    if run_shell(git_bin, ["add", "-A"], cwd=repo_root).returncode != 0:
        logger.error("清理已删除文件的暂存状态失败！")
        return False
    if run_shell(git_bin, ["add", "--renormalize", "."], cwd=repo_root).returncode != 0:
        logger.error("Git LFS 重新规范化失败！")
        return False
    return True

def verify_staged_lfs_files(git_bin: str, repo_root: Path) -> bool:
    """Fail before push if a staged LFS-matched file is still a large blob."""
    attrs_result = subprocess.run(
        [git_bin, "diff", "--cached", "--name-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if attrs_result.returncode != 0:
        return True
    invalid_files = []
    for path in (line.strip() for line in attrs_result.stdout.splitlines() if line.strip()):
        attr_result = subprocess.run(
            [git_bin, "check-attr", "filter", "--", path],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if not attr_result.stdout.rstrip().endswith(": lfs"):
            continue
        blob_result = subprocess.run(
            [git_bin, "cat-file", "blob", f":{path}"],
            cwd=repo_root,
            capture_output=True,
        )
        if blob_result.returncode == 0 and not blob_result.stdout.startswith(
                b"version https://git-lfs.github.com/spec/v1\n"):
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


def set_remote(git_bin: str, remote_url: str):
    if not remote_url:
        return
    check = subprocess.run([git_bin, "remote", "get-url", "origin"], capture_output=True, text=True)
    if check.returncode == 0:
        if check.stdout.strip() == remote_url:
            return
        logger.info("更新远程 origin 地址...")
        run_shell(git_bin, ["remote", "set-url", "origin", remote_url])
    else:
        logger.info("添加远程 origin 地址...")
        run_shell(git_bin, ["remote", "add", "origin", remote_url])


def parse_github_subdirectory_url(remote_url: str) -> tuple[str, str | None, str | None]:
    """Return (repository URL, branch, subdirectory) for a GitHub web URL."""
    parsed = urlparse(remote_url)
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return remote_url, None, None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 5 or parts[2] not in ("blob", "tree"):
        return remote_url, None, None
    owner, repository, _, branch, *subdirectory = parts
    repository = repository.removesuffix(".git")
    repository_url = urlunparse(parsed._replace(path=f"/{owner}/{repository}.git", params="", query="", fragment=""))
    return repository_url, branch, "/".join(subdirectory)


def scan_large_files(repo_root: Path, threshold: int) -> set[str]:
    large_files = set()
    skip_dirs = {".git", "dist", "__pycache__"}
    for path in repo_root.rglob("*"):
        if any(part in skip_dirs for part in path.parts) or not path.is_file():
            continue
        try:
            fsize = path.stat().st_size
        except OSError:
            continue
        if fsize >= threshold:
            large_files.add(str(path.relative_to(repo_root)).replace("\\", "/"))
    return large_files


def clean_and_apply_lfs(git_bin: str, repo_root: Path, large_patterns: set[str]):
    attr_path = repo_root / ".gitattributes"
    other_lines, lfs_lines = [], set()
    if attr_path.exists():
        with open(attr_path, "r", encoding="utf-8") as f:
            for line in f.readlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if "filter=lfs" in stripped:
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


def run_network_retry(git_bin: str, cmd_args: list[str], operation: str, remote_url: str,
                      branch: str, retry_count: int = 10, retry_seconds: int = 5,
                      is_debug: bool = False, cwd: Path = None):
    net_kw = [
        "could not read from remote repository",
        "ssh: connect to host",
        "connection timed out",
        "the remote end hung up unexpectedly",
        "fatal: unable to access",
        "failed to connect to",
        "network is unreachable",
        "remote: fatal:",
        "dial tcp",
        "connectex",
        "a connection attempt failed",
        "connected party did not properly respond",
        "connected host has failed to respond",
        "curl 28",
        "rpc failed",
        "expected flush after ref listing",
        "connection was reset",
    ]
    auth_kw = [
        "http 401",
        "http 403",
        "fatal: authentication failed",
        "permission denied (publickey)",
    ]
    history_large_file_kw = [
        "gh001: large files detected",
        "exceeds github's file size limit",
        "exceeds github's file size limit of 100.00 mb",
    ]
    for attempt in range(1, retry_count + 1):
        logger.info(f"===== {operation} {redact_url(remote_url)} {branch} "
                    f"(尝试 {attempt}/{retry_count}) 间隔 {retry_seconds}s =====")
        extra_env = {}
        if is_debug or attempt > 1:
            extra_env.update({"GIT_CURL_VERBOSE": "1", "GIT_TRACE": "1"})
            if attempt > 1:
                logger.info("🔍 启用详细连接日志")
        try:
            result = run_shell(git_bin, cmd_args, realtime=True, extra_env=extra_env, cwd=cwd)
            if result.returncode == 0:
                return result
            output = (result.stdout or "").lower()
            if any(keyword in output for keyword in history_large_file_kw):
                logger.error("❌ GitHub 拒绝了历史中的大文件，当前工作区扫描不到并不代表历史对象已清除。")
                logger.error("请先执行 ./git.py list-big，确认 Blob；再执行 ./git.py remove-big，"
                             "完成历史改写后使用 git push --force 推送。")
                logger.error("如果希望保留这些文件，请先配置 Git LFS 并迁移历史，而不是只新增 .gitattributes。")
                sys.exit(1)
            is_net = any(keyword in output for keyword in net_kw)
            is_auth = any(keyword in output for keyword in auth_kw)
            if is_net and not is_auth:
                logger.warning(f"⚠️ 网络错误，稍后重试 (返回码: {result.returncode})")
            else:
                logger.error(f"❌ {operation}失败 (返回码: {result.returncode})")
                sys.exit(1)
        except Exception as exc:
            logger.warning(f"⚠️ 异常: {repr(exc)}，重试")
        if attempt < retry_count:
            time.sleep(retry_seconds)
        else:
            logger.error(f"❌ {operation}达到最大重试次数 {retry_count}")
            sys.exit(1)


def git_pull(git_bin: str, branch: str, extra_args: list[str], remote_url: str = "",
             connect_timeout: int = 45, low_speed_limit: int = 1000, low_speed_time: int = 30,
             retry_count: int = 10, retry_seconds: int = 5):
    git_config_args = [
        "-c", f"http.connectTimeout={connect_timeout}",
        "-c", f"http.lowSpeedLimit={low_speed_limit}",
        "-c", f"http.lowSpeedTime={low_speed_time}"
    ]
    pull_args = git_config_args + ["pull", "--progress"] + extra_args + [remote_url, branch]
    run_network_retry(git_bin, pull_args, "拉取", remote_url, branch, retry_count, retry_seconds,
                      logger.getEffectiveLevel() <= logging.DEBUG)
    logger.info("===== 开始执行 git lfs pull =====")
    run_shell(git_bin, ["lfs", "pull"], realtime=True)


def git_clone(git_bin: str, branch: str, remote_url: str, extra_args: list[str],
              sparse_path: str | None = None,
              connect_timeout: int = 45, low_speed_limit: int = 1000,
              low_speed_time: int = 30):
    """Clone or resume a repository and download its LFS objects."""
    if not remote_url:
        logger.error("clone 缺少远程仓库地址。")
        sys.exit(1)

    git_config_args = [
        "-c", f"http.connectTimeout={connect_timeout}",
        "-c", f"http.lowSpeedLimit={low_speed_limit}",
        "-c", f"http.lowSpeedTime={low_speed_time}"
    ]
    clone_args = git_config_args + ["clone", "--progress"]
    if sparse_path:
        clone_args.extend(["--filter=blob:none", "--sparse"])
    if branch and "--branch" not in extra_args and "-b" not in extra_args:
        clone_args.extend(["--branch", branch])
    clone_options = list(extra_args)
    value_options = {
        "-b", "--branch", "-o", "--origin", "-c", "--config", "--depth",
        "--shallow-since", "--shallow-exclude", "--reference", "--reference-if-able",
        "--dissociate", "--separate-git-dir", "--template", "--upload-pack"
    }
    positional = [
        arg for index, arg in enumerate(clone_options)
        if not arg.startswith("-") and (index == 0 or clone_options[index - 1] not in value_options)
    ]
    destination = None
    if positional:
        destination = Path(positional[-1])
        for index in range(len(clone_options) - 1, -1, -1):
            if clone_options[index] == str(destination):
                clone_options.pop(index)
                break
    clone_args.extend(clone_options + [remote_url])
    if destination is None:
        if sparse_path:
            destination = Path(sparse_path.rstrip("/").rsplit("/", 1)[-1])
        else:
            repo_name = remote_url.rstrip("/").rsplit("/", 1)[-1]
            if ":" in repo_name and not remote_url.startswith(("http://", "https://")):
                repo_name = repo_name.rsplit(":", 1)[-1]
            destination = Path(repo_name.removesuffix(".git"))
    clone_args.append(str(destination))
    if not destination.is_absolute():
        destination = Path.cwd() / destination

    if destination.exists() and sparse_path and (destination / ".git").exists():
        existing_remote = subprocess.run(
            [git_bin, "remote", "get-url", "origin"],
            cwd=destination,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if existing_remote.rstrip("/").removesuffix(".git") != remote_url.rstrip("/").removesuffix(".git"):
            logger.error(f"目标目录已是其他 Git 仓库，无法复用: {destination}")
            sys.exit(1)
        logger.info(f"===== 复用已有仓库，仅检出项目子目录: {destination / sparse_path} =====")
        if run_shell(git_bin, ["sparse-checkout", "set", "--cone", sparse_path], cwd=destination).returncode != 0:
            logger.error("设置 sparse-checkout 子目录失败！")
            sys.exit(1)
    elif destination.exists() and any(destination.iterdir()):
        if not (destination / ".git").exists():
            logger.error(f"目标目录已存在且不是 Git 仓库，拒绝覆盖: {destination}")
            sys.exit(1)
        logger.info(f"===== 检测到未完成的仓库，开始从远程恢复: {destination} =====")
        if run_shell(git_bin, ["remote", "set-url", "origin", remote_url], cwd=destination).returncode != 0:
            logger.error("更新 origin 地址失败！")
            sys.exit(1)
        fetch_args = git_config_args + ["fetch", "--prune", "origin", branch]
        if run_shell(git_bin, fetch_args, realtime=True, cwd=destination).returncode != 0:
            logger.error("获取远程最新提交失败！")
            sys.exit(1)
        if run_shell(git_bin, ["checkout", "-B", branch, f"origin/{branch}"], realtime=True,
                     extra_env={"GIT_LFS_SKIP_SMUDGE": "1"}, cwd=destination).returncode != 0:
            logger.error("切换到远程分支失败！")
            sys.exit(1)
        if run_shell(git_bin, ["reset", "--hard", f"origin/{branch}"],
                     extra_env={"GIT_LFS_SKIP_SMUDGE": "1"}, cwd=destination).returncode != 0:
            logger.error("同步工作树到远程最新版本失败！")
            sys.exit(1)
        if run_shell(git_bin, ["clean", "-fdx"], cwd=destination).returncode != 0:
            logger.error("清理未完成克隆残留失败！")
            sys.exit(1)
    else:
        logger.info(f"===== 开始执行 git clone {redact_url(remote_url)} =====")
        if run_shell(git_bin, clone_args, realtime=True, extra_env={"GIT_LFS_SKIP_SMUDGE": "1"}).returncode != 0:
            logger.error("git clone 失败！")
            sys.exit(1)

        if sparse_path and run_shell(git_bin, ["sparse-checkout", "set", "--cone", sparse_path], cwd=destination).returncode != 0:
            logger.error("设置 sparse-checkout 子目录失败！")
            sys.exit(1)

    attr_path = destination / ".gitattributes"
    has_lfs_rules = attr_path.is_file() and "filter=lfs" in attr_path.read_text(encoding="utf-8", errors="ignore")
    if not has_lfs_rules:
        logger.info("未发现 Git LFS 追踪规则，跳过大文件恢复。")
        return
    if not check_lfs_available(git_bin):
        if not install_lfs() or not check_lfs_available(git_bin):
            logger.error("克隆 LFS 仓库需要 Git LFS，但自动安装失败。")
            sys.exit(1)
    logger.info(f"===== 开始恢复 Git LFS 大文件: {destination} =====")
    show_lfs_progress = logger.getEffectiveLevel() <= logging.INFO
    lfs_env = {}
    if show_lfs_progress:
        lfs_env["GIT_LFS_FORCE_PROGRESS"] = "1"
    lfs_args = ["lfs", "pull"]
    if logger.getEffectiveLevel() <= logging.DEBUG:
        lfs_env.update({"GIT_CURL_VERBOSE": "1", "GIT_TRACE": "1", "GIT_TRANSFER_TRACE": "1"})
    if run_shell(git_bin, git_config_args + lfs_args, realtime=show_lfs_progress,
                 extra_env=lfs_env, cwd=destination).returncode != 0:
        logger.error("Git LFS 大文件恢复失败！")
        sys.exit(1)


def extract_remote_user_from_url(remote_url: str) -> str | None:
    if not remote_url:
        return None
    parsed = urlparse(remote_url)
    if parsed.scheme and parsed.netloc:
        if parsed.username:
            return parsed.username
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        if path_parts:
            return path_parts[0]
    if remote_url.startswith("git@"):
        parts = remote_url.split("@", 1)
        if len(parts) == 2 and ":" in parts[1]:
            path_parts = [p for p in parts[1].split(":", 1)[1].strip("/").split("/") if p]
            if path_parts:
                return path_parts[0]
    path_parts = [p for p in (parsed.path if parsed.path else remote_url).strip("/").split("/") if p]
    return path_parts[0] if path_parts else None


def apply_git_user_config(git_bin: str, remote_url: str, user_arg: str):
    if not remote_url:
        return
    remote_user = extract_remote_user_from_url(remote_url)
    if user_arg is not None:
        target_user = remote_user if user_arg == "AUTO" else user_arg
        if not target_user:
            target_user = "git_user"
            logger.warning("无法提取用户名，回退为 'git_user'")
        target_email = f"{target_user}@users.noreply.github.com"
        logger.info(f"强制应用用户配置 (-u): user.name=[{target_user}], user.email=[{target_email}]")
        run_shell(git_bin, ["config", "user.name", target_user])
        run_shell(git_bin, ["config", "user.email", target_email])
        return
    if not remote_user:
        return
    local_name = subprocess.run([git_bin, "config", "user.name"], capture_output=True, text=True).stdout.strip()
    local_email = subprocess.run([git_bin, "config", "user.email"], capture_output=True, text=True).stdout.strip()
    if local_name != remote_user:
        logger.warning("发现当前 Git 用户配置与远程目标不一致！")
        print(f"\n请选择本次 Commit 使用配置:\n  [1] 保持原样 ({local_name})\n  [2] 更新为目标 ({remote_user})")
        try:
            choice = input("请输入 1 或 2 (默认 1): ").strip()
        except KeyboardInterrupt:
            sys.exit(130)
        if choice == "2":
            run_shell(git_bin, ["config", "user.name", remote_user])
            default_email = f"{remote_user}@users.noreply.github.com"
            try:
                new_email = input(f"输入邮箱 (默认: {default_email}): ").strip() or default_email
            except KeyboardInterrupt:
                sys.exit(130)
            run_shell(git_bin, ["config", "user.email", new_email])
            logger.info(f"✅ 更新仓库配置: user.name={remote_user}, user.email={new_email}")
        else:
            logger.info("保持原配置不变。")


def git_push(git_bin: str, branch: str, repo_root: Path, extra_args: list[str],
             commit_msg: str = "", remote_url: str = "",
             user_arg: str = None, retry_count: int = 10, retry_seconds=5,
             connect_timeout: int = 45, low_speed_limit: int = 1000, low_speed_time: int = 30):
    EmptyAfterPush = False
    logger.info(f"当前工作目录: {repo_root.resolve()}")
    if not (repo_root / ".git").exists():
        logger.info("检测到当前目录尚未初始化 Git 仓库，自动执行 git init...")
        run_shell(git_bin, ["init"])
        if remote_url:
            run_shell(git_bin, ["remote", "add", "origin", remote_url])
    apply_git_user_config(git_bin, remote_url, user_arg)
    if run_shell(git_bin, ["add", "-A"], cwd=repo_root).returncode != 0:
        logger.error("git add 失败")
        sys.exit(1)
    if (repo_root / ".gitattributes").is_file():
        if not renormalize_lfs(git_bin, repo_root):
            sys.exit(1)
        if run_shell(git_bin, ["add", "-A"]).returncode != 0:
            logger.error("重新暂存 LFS 文件失败")
            sys.exit(1)
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
        commit_msg = (f"[{max_file} {max_size}B] {stime()} {__file__[-20:]} auto"
                      if max_file else f" auto {stime()}")
    if changed_files:
        logger.info(f"变更文件: {len(changed_files)} 个" + (
            f" (显示前10: {changed_files[:10]})" if len(changed_files) > 10 else f" {changed_files}"))
        for f in changed_files:
            if f == "ReadMe.md":
                with open(repo_root / "ReadMe.md", 'rb') as fh:
                    if b'#EmptyAfterPush' in fh.read():
                        EmptyAfterPush = True
        commit_result = run_shell(git_bin, ["commit", "-m", commit_msg])
        if commit_result.returncode != 0:
            logger.error(f"git commit 失败，返回码: {commit_result.returncode}")
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
    cmd_args = git_config_args + ["push", "-v", "--progress"] + extra_args + [remote_url, branch]

    run_network_retry(git_bin, cmd_args, "推送", remote_url, branch,
                      retry_count, retry_seconds, is_debug)
    if EmptyAfterPush:
        with open(repo_root / 'ReadMe.md', 'wb') as f:
            f.write(b'')
        logger.info(f"EmptyAfterPush 成功 {stime()}")
    logger.info(f"✅ 推送成功 {stime()}")


def git_list_big(git_bin: str, threshold_bytes: int) -> list[tuple[int, str, str]]:
    logger.info(f"===== 扫描历史大文件 >= {threshold_bytes / 1024 / 1024:.2f} MB =====")
    try:
        p1 = subprocess.Popen([git_bin, "rev-list", "--objects", "--all"], stdout=subprocess.PIPE, text=True)
        p2 = subprocess.Popen([git_bin, "cat-file",
                               "--batch-check=%(objectname) %(objecttype) %(objectsize) %(rest)"],
                              stdin=p1.stdout, stdout=subprocess.PIPE, text=True)
        p1.stdout.close()
        large_files = []
        count = 0
        for line in p2.stdout:
            count += 1
            if count % 10000 == 0:
                logger.info(f"已扫描 {count} 个对象...")
            parts = line.split(" ", 3)
            if len(parts) >= 4 and parts[1] == "blob":
                size = int(parts[2])
                if size >= threshold_bytes:
                    large_files.append((size, parts[3], parts[0]))
        p2.wait()
        large_files.sort(key=lambda x: x[0], reverse=True)
        if not large_files:
            logger.info("🎉 未发现超过阈值的大文件。")
        else:
            print(f"\n{'大小 (MB)':<12} | {'Blob Hash':<40} | {'文件路径'}")
            print("-" * 85)
            for size, path, blob_hash in large_files:
                print(f"{size / 1024 / 1024:<12.2f} | {blob_hash:<40} | {path}")
        return large_files
    except Exception as e:
        logger.error(f"扫描失败: {e}")
        return []


def git_remove_big(git_bin: str, threshold_bytes: int, target_hashes: list[str] = None):
    logger.info("===== 准备清理历史大文件 =====")
    logger.info("🛡️ 仅移除指定 Blob 及其关联 Commit，更早的历史哈希保持不变。\n")
    if run_shell(git_bin, ["filter-repo", "--version"]).returncode != 0:
        if not install_git_filter_repo(git_bin):
            logger.error("请手动安装：pip install -i https://pypi.tuna.tsinghua.edu.cn/simple git-filter-repo")
            sys.exit(1)
    hashes_to_remove = set()
    if target_hashes:
        for h in target_hashes:
            h = h.strip()
            if h:
                hashes_to_remove.add(h)
        logger.info(f"使用指定 {len(hashes_to_remove)} 个 Blob Hash 进行精准删除。")
    else:
        large_files = git_list_big(git_bin, threshold_bytes)
        if not large_files:
            logger.info("没有符合条件的大文件。")
            return
        for _, _, blob_hash in large_files:
            hashes_to_remove.add(blob_hash)
    if not hashes_to_remove:
        return
    logger.info(f"即将擦除 {len(hashes_to_remove)} 个 Blob:")
    for h in sorted(hashes_to_remove):
        logger.info(f"  - {h}")
    hash_list_code = ", ".join([f'"{h}"' for h in hashes_to_remove])
    callback_code = (
        f"target_hashes = {{{hash_list_code}}}\n"
        f"if blob.original_id.decode('ascii') in target_hashes:\n"
        f"    blob.skip()"
    )
    res = run_shell(git_bin, ["filter-repo", "--blob-callback", callback_code, "--force"], realtime=True)
    if res.returncode == 0:
        msg = '''
filter‑repo 重写历史后，旧对象还在本地 git 库，磁盘空间不会立刻释放，需要手动：
git reflog expire --expire=now --all
git gc --prune=now --aggressive
✅ 历史大文件 Blob 已擦除 （ 之前 Commit 保留未动）
'''
        logger.info(msg)
        logger.warning("⚠️ 历史已重写，推送需使用 --force")
    else:
        logger.error("❌ 清理失败")
        sys.exit(1)


def main():
    sys.argv = preprocess_args()
    default_git = os.environ.get("GIT_PATH", "git")
    configured_branch = os.environ.get("BRANCH")
    parser = argparse.ArgumentParser(description="Git Auto LFS Tool")
    parser.add_argument("--git", default=default_git, help="git 可执行文件路径")
    parser.add_argument("--branch", '-b', default=configured_branch, help="分支名称")
    parser.add_argument("--size", '-s', default="100mb", help="大文件大小限制（默认 100mb）")
    parser.add_argument("--threshold", type=int, default=0, help="字节数阈值（兼容）")
    parser.add_argument("--hashes", "--hash", default="", help="手动指定 Blob Hash，逗号分隔")
    parser.add_argument("--remote", default="", help="完整远程 URL")
    parser.add_argument("--commit-msg", "--commit_msg", '-m', default="", help="自定义 commit 消息")
    parser.add_argument("--user", "-u", nargs="?", const="AUTO", default=None, help="自动配置 Git 用户")
    parser.add_argument("--retry", "-r", type=int, default=10, help="Push 失败重试次数")
    parser.add_argument("--verbose", "-v", type=int, default=2,
                        help="日志级别: 0=Error, 1=Warn, 2=Info, 3=Debug")
    # 新增网络超时相关参数
    parser.add_argument("--connect-timeout", type=int, default=45,
                        help="HTTP TCP连接建立超时时间（秒），默认45")
    parser.add_argument("--low-speed-limit", type=int, default=10,
                        help="传输低速阈值（字节/秒），低于该值持续指定时间则断开")
    parser.add_argument("--low-speed-time", type=int, default=60,
                        help="低速持续超时时间（秒）")
    parser.add_argument("mode", choices=["push", "pull", "clone", "config", "init", "list-big", "listbig", "remove-big", "undo"])
    args, extra = parser.parse_known_args()

    setup_logging(args.verbose)
    git_exe = find_git(args.git)
    repo_root = Path.cwd()
    if not args.branch and args.mode in ("push", "pull"):
        args.branch = get_current_branch(git_exe) or "main"
    remote_url = args.remote or get_origin_url(git_exe) or get_branch_tracking_url(git_exe, args.branch)
    clone_branch = args.branch
    clone_subdirectory = None
    if args.mode == "clone" and remote_url:
        remote_url, url_branch, clone_subdirectory = parse_github_subdirectory_url(remote_url)
        if url_branch:
            clone_branch = url_branch
    if not remote_url and args.mode not in ("list-big", "listbig", "remove-big", "undo"):
        logger.critical("未提供远程仓库地址，且未找到 origin/tracking 配置。")
        sys.exit(1)
    threshold_bytes = args.threshold if args.threshold > 0 else parse_size_str(args.size)
    logger.info(f"仓库路径: {repo_root.absolute()}")
    logger.info(f"Git程序: {git_exe}")
    if args.mode != "init":
        logger.info(f"文件限制: {threshold_bytes / 1024 / 1024:.2f} MB ({threshold_bytes} 字节)")
    if remote_url:
        logger.info(f"远程地址: {redact_url(remote_url)}")
    logger.info(f"分支: {clone_branch if args.mode == 'clone' else args.branch}")
    logger.info(f"连接超时: {args.connect_timeout}s | 低速阈值: {args.low_speed_limit}B/s | 低速超时: {args.low_speed_time}s")

    try:
        if args.mode == "config":
            logger.info("===== 根据远程 URL 配置当前仓库用户 =====")
            apply_git_user_config(git_exe, remote_url, "AUTO")
            logger.info("✅ 当前仓库用户配置完成！")
            return
        if args.mode == "clone":
            git_clone(git_exe, clone_branch, remote_url, extra, clone_subdirectory,
                      connect_timeout=args.connect_timeout,
                      low_speed_limit=args.low_speed_limit,
                      low_speed_time=args.low_speed_time)
            logger.info("✅ clone 及 LFS 大文件恢复完成！")
            return
        if args.mode == "undo":
            logger.info("===== 撤销上一次提交 =====")
            run_shell(git_exe, ["reset", "--soft", "HEAD~1"])
            run_shell(git_exe, ["reset", "HEAD", "."])
            logger.info("✅ 撤销完成，工作区文件未改动。")
            return
        if args.mode == "init":
            logger.info("===== 执行 git init =====")
            run_shell(git_exe, ["init"])
            run_shell(git_exe, ["remote", "remove", "origin"])
            run_shell(git_exe, ["remote", "add", "origin", remote_url])
            logger.info("✅ 初始化完成！")
            return
        if args.mode in ("list-big", "listbig"):
            git_list_big(git_exe, threshold_bytes)
            return
        if args.mode == "remove-big":
            target_hashes = [h.strip() for h in args.hashes.split(",") if h.strip()] if args.hashes else None
            git_remove_big(git_exe, threshold_bytes, target_hashes)
            if remote_url:
                set_remote(git_exe, remote_url)
                logger.info("✅ 远程地址已重新绑定。")
            return
        large_files = scan_large_files(repo_root, threshold_bytes)
        has_large = len(large_files) > 0
        logger.info(f"扫描到 {len(large_files)} 个本地大文件")
        if has_large:
            if not check_lfs_available(git_exe):
                if not install_lfs():
                    sys.exit(1)
                if not check_lfs_available(git_exe):
                    logger.critical("Git LFS 安装后仍不可用")
                    sys.exit(1)
            if not init_lfs(git_exe, repo_root):
                sys.exit(1)
            clean_and_apply_lfs(git_exe, repo_root, large_files)
            if not renormalize_lfs(git_exe, repo_root):
                sys.exit(1)
        if remote_url:
            set_remote(git_exe, remote_url)
        if args.mode == "pull":
            git_pull(git_exe, args.branch, extra, remote_url,
                     retry_count=args.retry,
                     connect_timeout=args.connect_timeout,
                     low_speed_limit=args.low_speed_limit,
                     low_speed_time=args.low_speed_time)
        elif args.mode == "push":
            git_push(git_exe, args.branch, repo_root, extra, args.commit_msg, remote_url,
                     args.user, args.retry,
                     connect_timeout=args.connect_timeout,
                     low_speed_limit=args.low_speed_limit,
                     low_speed_time=args.low_speed_time)
        logger.info("✅ 操作结束！")
    except KeyboardInterrupt:
        logger.warning("\n[CANCEL] 用户手动终止。")
        sys.exit(130)


if __name__ == "__main__":
    main()
