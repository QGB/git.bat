from .shared import Path, logging, shutil, subprocess, sys, time
from .core import logger, redact_url, run_shell
from .lfs import check_lfs_available, install_lfs

# =====================================================================
# 网络重试相关关键字（run_network_retry 
# =====================================================================
RETRY_NET_KEYWORDS = [
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
    "empty reply from server",
]
RETRY_AUTH_KEYWORDS = [
    "http 401",
    "http 403",
    "error: 401",
    "error: 403",
    "fatal: authentication failed",
    "permission to ",
    "permission denied (publickey)",
]
RETRY_LARGE_FILE_KEYWORDS = [
    "gh001: large files detected",
    "exceeds github's file size limit",
    "exceeds github's file size limit of 100.00 mb",
]
NON_RETRYABLE_PUSH_KEYWORDS = [
    "rpc failed; http 500",
    "pack exceeds maximum allowed size",
]

def run_network_retry(git_bin: str, cmd_args: list[str], operation: str, remote_url: str,
                      branch: str, retry_count: int = 10, retry_seconds: int = 5,
                      is_debug: bool = False, cwd: Path = None,
                      before_attempt=None, base_env: dict | None = None):
    """
    通用网络重试：适用于 pull / push / fetch 等“幂等、失败不留残留”的命令。

    可选钩子：
    - before_attempt(attempt): 在每次尝试（含第 1 次）前调用，参数是当前尝试编号 1..N。
                               pull / push 不传，行为与之前完全一致；
                               clone 传它来清理上一次失败留下的残留目录。
    - base_env:                每次尝试都叠加到 run_shell 的环境变量上的基础值。
                               clone 用它注入 GIT_LFS_SKIP_SMUDGE=1。

    - 成功：返回 run_shell 的结果
    - 历史大文件 / 认证失败：立即 sys.exit(1)，不重试
    - 网络错误 / 未知错误：重试，直到 retry_count 用尽
    """
    for attempt in range(1, retry_count + 1):
        logger.info(f"===== {operation} {redact_url(remote_url)} {branch} "
                    f"(尝试 {attempt}/{retry_count}) 间隔 {retry_seconds}s =====")

        # 钩子：clone 用它清理上一次失败的残留目录；pull/push 不传则为空操作
        if before_attempt is not None:
            try:
                before_attempt(attempt)
            except Exception as exc:
                logger.warning(f"⚠️ 第 {attempt} 次尝试前的钩子异常: {exc!r}")

        extra_env = dict(base_env) if base_env else {}
        if is_debug or attempt > 1:
            extra_env.update({"GIT_CURL_VERBOSE": "1", "GIT_TRACE": "1"})
            if attempt > 1:
                logger.info("🔍 启用详细连接日志")
        try:
            result = run_shell(git_bin, cmd_args, realtime=True, extra_env=extra_env, cwd=cwd)
            if result.returncode == 0:
                return result
            output = (result.stdout or "").lower()

            # 1) 历史大文件：不可重试
            if any(keyword in output for keyword in RETRY_LARGE_FILE_KEYWORDS):
                logger.error("❌ GitHub 拒绝了历史中的大文件，当前工作区扫描不到并不代表历史对象已清除。")
                logger.error("请先执行 ./git.py list-big，确认 Blob；再执行 ./git.py remove-big，"
                             "完成历史改写后使用 git push --force 推送。")
                logger.error("如果希望保留这些文件，请先配置 Git LFS 并迁移历史，而不是只新增 .gitattributes。")
                sys.exit(1)

            if any(keyword in output for keyword in NON_RETRYABLE_PUSH_KEYWORDS):
                logger.error(f"❌ {operation}遇到 HTTP 500 或 pack 大小限制，停止自动重试。")
                logger.error("如果本次上传 pack 超过 GitHub 的 2 GiB 单次 push 限制，"
                             "需要把缺失的提交历史拆成更小批次推送；重复发送同一 pack 不会解决问题。")
                sys.exit(1)

            # 2) 网络 vs 认证：只有“网络错误且非认证错误”才重试
            is_net = any(keyword in output for keyword in RETRY_NET_KEYWORDS)
            is_auth = any(keyword in output for keyword in RETRY_AUTH_KEYWORDS)
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

def git_clone(git_bin: str, branch: str, remote_url: str, extra_args: list[str],
              sparse_path: str | None = None,
              connect_timeout: int = 45, low_speed_limit: int = 1000,
              low_speed_time: int = 30,
              retry_count: int = 10, retry_seconds: int = 5):
    """
    Clone or resume a repository and download its LFS objects.

    clone 的重试复用 run_network_retry：
      - before_attempt 钩子：每次尝试前清理目标目录残留
      - base_env：每次尝试都注入 GIT_LFS_SKIP_SMUDGE=1

    为什么 clone 需要钩子而 pull/push 不需要：
      - pull / push / fetch 作用在已存在的仓库上，失败不留残留，重试时原地重跑即可；
      - git clone 失败会留下半个目标目录（含 .git、部分 objects 等），不清理就重试会
        立刻报：
            fatal: destination path 'xxx' already exists and is not an empty directory

    恢复路径为什么先做健康检查：
      - destination 存在且含 .git，不代表这个 .git 是完好的。
      - 上一次中断的 clone 可能留下一个残破的 .git（HEAD 指向不存在的对象、
        refs 缺失等），此时 fetch 会立即失败且不会重试，也不会退回全新 clone。
      - 所以恢复前先用 git rev-parse 验证 .git 是否可用；不可用则清掉目录，
        退回走带重试的全新 clone 路径。
    """
    if not remote_url:
        logger.error("clone 缺少远程仓库地址。")
        sys.exit(1)

    # 所有相对路径都以进程启动时的 CWD 为基准，并显式传给 run_shell，避免隐式依赖。
    base_dir = Path.cwd()
    branch_display = branch if branch else "<default>"

    def _detect_default_branch() -> str | None:
        """探测远端默认分支，避免 branch=None 时拼出 origin/None。"""
        try:
            result = subprocess.run(
                [git_bin, "ls-remote", "--symref", remote_url, "HEAD"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("ref:"):
                ref = line.split("\t", 1)[0][len("ref:"):].strip()
                if ref.startswith("refs/heads/"):
                    return ref[len("refs/heads/"):]
        return None

    def _is_healthy_repo(path: Path) -> bool:
        """判断 path 里的 .git 是否可用：能定位 git-dir，且 HEAD 能解析出有效对象。"""
        try:
            r1 = subprocess.run(
                [git_bin, "rev-parse", "--git-dir"],
                cwd=path, capture_output=True, text=True,
            )
            if r1.returncode != 0:
                return False
            r2 = subprocess.run(
                [git_bin, "rev-parse", "--verify", "HEAD"],
                cwd=path, capture_output=True, text=True,
            )
            return r2.returncode == 0
        except OSError:
            return False

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
    # 统一规范为绝对路径后再写回 clone_args，clone 命令不再依赖 CWD。
    if not destination.is_absolute():
        destination = base_dir / destination
    clone_args.append(str(destination))

    # 抽出“全新 clone”分支，恢复路径发现 .git 损坏时可以复用。
    def _do_fresh_clone():
        def _cleanup_destination(_attempt: int):
            if destination.exists():
                logger.info(f"清理克隆残留目录: {destination}")
                if destination.is_dir():
                    shutil.rmtree(destination, ignore_errors=True)
                else:
                    try:
                        destination.unlink()
                    except OSError:
                        pass

        run_network_retry(
            git_bin,
            clone_args,
            "克隆",
            remote_url,
            branch_display,
            retry_count,
            retry_seconds,
            logger.getEffectiveLevel() <= logging.DEBUG,
            cwd=base_dir,
            before_attempt=_cleanup_destination,
            base_env={"GIT_LFS_SKIP_SMUDGE": "1"},
        )

        if sparse_path and run_shell(
            git_bin,
            ["sparse-checkout", "set", "--no-cone", f"/{sparse_path.strip('/')}/"],
            cwd=destination,
        ).returncode != 0:
            logger.error("设置 sparse-checkout 子目录失败！")
            sys.exit(1)

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
        if run_shell(git_bin, ["sparse-checkout", "set", "--no-cone", f"/{sparse_path.strip('/')}/"], cwd=destination).returncode != 0:
            logger.error("设置 sparse-checkout 子目录失败！")
            sys.exit(1)
    elif destination.exists() and any(destination.iterdir()):
        if not (destination / ".git").exists():
            logger.error(f"目标目录已存在且不是 Git 仓库，拒绝覆盖: {destination}")
            sys.exit(1)

        # 关键：先验证 .git 是否真的可用，再决定“恢复”还是“清掉重 clone”。
        if not _is_healthy_repo(destination):
            logger.warning(f"检测到不完整或损坏的 .git，清理后重新 clone: {destination}")
            shutil.rmtree(destination, ignore_errors=True)
            _do_fresh_clone()
        else:
            logger.info(f"===== 检测到未完成的仓库，开始从远程恢复: {destination} =====")
            # branch 为 None 时先探测远端默认分支，避免拼出 origin/None。
            effective_branch = branch
            if not effective_branch:
                effective_branch = _detect_default_branch()
                if not effective_branch:
                    logger.error("未指定分支，且无法探测远端默认分支（ls-remote --symref 失败）。"
                                 "请通过 --branch/-b 显式指定分支后重试。")
                    sys.exit(1)
                logger.info(f"未指定分支，使用远端默认分支: {effective_branch}")
            if run_shell(git_bin, ["remote", "set-url", "origin", remote_url], cwd=destination).returncode != 0:
                logger.error("更新 origin 地址失败！")
                sys.exit(1)
            fetch_args = git_config_args + ["fetch", "--prune", "origin", effective_branch]
            if run_shell(git_bin, fetch_args, realtime=True, cwd=destination).returncode != 0:
                logger.error("获取远程最新提交失败！")
                sys.exit(1)
            if run_shell(git_bin, ["checkout", "-B", effective_branch, f"origin/{effective_branch}"], realtime=True,
                         extra_env={"GIT_LFS_SKIP_SMUDGE": "1"}, cwd=destination).returncode != 0:
                logger.error("切换到远程分支失败！")
                sys.exit(1)
            if run_shell(git_bin, ["reset", "--hard", f"origin/{effective_branch}"],
                         extra_env={"GIT_LFS_SKIP_SMUDGE": "1"}, cwd=destination).returncode != 0:
                logger.error("同步工作树到远程最新版本失败！")
                sys.exit(1)
            if run_shell(git_bin, ["clean", "-fdx"], cwd=destination).returncode != 0:
                logger.error("清理未完成克隆残留失败！")
                sys.exit(1)
    else:
        _do_fresh_clone()

    if sparse_path:
        selected_path = destination.joinpath(*sparse_path.strip("/").split("/"))
        if not selected_path.is_dir():
            logger.error(f"远程仓库中不存在子目录: {sparse_path}")
            sys.exit(1)
        for child in selected_path.iterdir():
            target = destination / child.name
            if target.exists():
                logger.error(f"子目录内容与目标目录冲突: {target}")
                sys.exit(1)
            shutil.move(str(child), str(target))
        shutil.rmtree(selected_path)
        for parent in selected_path.parents:
            if parent == destination:
                break
            try:
                parent.rmdir()
            except OSError:
                break

    attr_path = destination / ".gitattributes"
    has_lfs_rules = sparse_path or (attr_path.is_file() and "filter=lfs" in attr_path.read_text(encoding="utf-8", errors="ignore"))
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

def git_pull(git_bin: str, branch: str, extra_args: list[str], remote_url: str = "",
             connect_timeout: int = 45, low_speed_limit: int = 1000, low_speed_time: int = 30,
             retry_count: int = 10, retry_seconds: int = 5, repo_root: Path = None):
    git_config_args = [
        "-c", f"http.connectTimeout={connect_timeout}",
        "-c", f"http.lowSpeedLimit={low_speed_limit}",
        "-c", f"http.lowSpeedTime={low_speed_time}"
    ]
    pull_args = git_config_args + ["pull", "--progress"] + extra_args + [remote_url, branch]
    run_network_retry(git_bin, pull_args, "拉取", remote_url, branch, retry_count, retry_seconds,
                      logger.getEffectiveLevel() <= logging.DEBUG, cwd=repo_root)
    logger.info("===== 开始执行 git lfs pull =====")
    lfs_env = {}
    if logger.getEffectiveLevel() <= logging.DEBUG:
        lfs_env.update({"GIT_CURL_VERBOSE": "1", "GIT_TRACE": "1", "GIT_TRANSFER_TRACE": "1"})
    run_shell(git_bin, ["lfs", "pull"], realtime=True, extra_env=lfs_env, cwd=repo_root)
