from urllib.request import urlopen

from .shared import Path, argparse, os, sys
from .core import (find_git, get_branch_tracking_url, get_current_branch, get_origin_url,
                   is_git_repository, logger, parse_size_str, preprocess_args, redact_url,
                   run_shell, setup_logging, verify_generated_freshness)
from .lfs import (check_lfs_available, clean_and_apply_lfs, init_lfs, install_lfs,
                  parse_github_subdirectory_url, scan_large_files, set_remote)
from .network import git_clone, git_pull
from .identity import apply_git_user_config
from .commit import git_push
from .history import git_list_big, git_remove_big


def parse_retry_count(value: str) -> int | str:
    if value.lower() == "auto":
        return "auto"
    try:
        retry_count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("重试次数必须是正整数或 auto") from error
    if retry_count < 1:
        raise argparse.ArgumentTypeError("重试次数必须大于 0")
    return retry_count


def can_reach_google(timeout: float = 5) -> bool:
    """尝试与 google.com 建立 HTTPS 连接，判断是否处于可直连 Google 的网络。

    ipapi.co 之类的公网 IP 地理接口不稳定，可能限流或返回异常内容导致地区误判；
    改用连通性探测：大陆网络通常无法直连 google.com，连通即视为海外网络。
    """
    try:
        with urlopen("https://www.google.com/generate_204", timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def resolve_retry_count(value: int | str) -> int:
    if value != "auto":
        return value
    google_reachable = can_reach_google()
    retry_count = 3 if google_reachable else 99
    logger.info(f"自动重试次数: {retry_count}"
                f"（google.com HTTPS 直连{'成功' if google_reachable else '失败'}）")
    return retry_count

def main():
    sys.argv = preprocess_args()
    default_git = os.environ.get("GIT_PATH", "git")
    configured_branch = os.environ.get("BRANCH") or 'main'
    parser = argparse.ArgumentParser(description="Git Auto LFS Tool")
    parser.add_argument("--git", default=default_git, help="git 可执行文件路径")
    parser.add_argument("--repo-path", "--repo", "--path", "-path", "-p", dest="repo_path", default=".",
                        help="指定 Git 仓库路径，默认使用当前目录")
    parser.add_argument("--branch", '-b', default=configured_branch, help="分支名称")
    parser.add_argument("--size", '-s', default="100mb", help="大文件大小限制（默认 100mb）")
    parser.add_argument("--threshold", type=int, default=0, help="字节数阈值（兼容）")
    parser.add_argument("--hashes", "--hash", default="", help="手动指定 Blob Hash，逗号分隔")
    parser.add_argument("--remote", default="", help="完整远程 URL")
    parser.add_argument("--commit-msg", "--commit_msg", '-m', default="", help="自定义 commit 消息")
    parser.add_argument("--user", "-u", nargs="?", const="AUTO", default=None, help="自动配置 Git 用户")
    parser.add_argument("--noask", "-noask", "--no-ask", "-y", "-yes", dest="no_ask", action="store_true",
                        help="非交互模式：自动确认初始化并跳过用户配置询问")
    parser.add_argument("--retry", "-retry", "-r", type=parse_retry_count, default="auto",
                        help="Push/Pull 失败重试次数（默认 auto：google.com 可直连按海外 3 次，不可直连按国内 99 次）")
    parser.add_argument("--verbose", "-v", type=int, default=2,
                        help="日志级别: 0=Error, 1=Warn, 2=Info, 3=Debug")
    # 新增网络超时相关参数
    parser.add_argument("--connect-timeout", type=int, default=45,
                        help="HTTP TCP连接建立超时时间（秒），默认45")
    parser.add_argument("--low-speed-limit", type=int, default=10,
                        help="传输低速阈值（字节/秒），低于该值持续指定时间则断开")
    parser.add_argument("--low-speed-time", type=int, default=60,
                        help="低速持续超时时间（秒）")
    parser.add_argument("--max-commit-size", type=parse_size_str, default=1900 * 1024 * 1024,
                        help="单个提交的最大暂存 Blob 大小（默认 1900mb，超过后自动分段）")
    parser.add_argument("mode", nargs="?", default="push",
                        choices=["push", "pull", "clone", "config", "init", "list-big", "listbig", "remove-big", "undo"])
    args, extra = parser.parse_known_args()

    setup_logging(args.verbose)
    verify_generated_freshness()
    args.retry = resolve_retry_count(args.retry)
    git_exe = find_git(args.git)
    repo_root = Path(args.repo_path).expanduser().resolve()
    if not repo_root.is_dir():
        logger.critical(f"指定的仓库路径不存在或不是目录: {repo_root}")
        sys.exit(1)
    os.chdir(repo_root)
    if args.mode == "push" and not is_git_repository(git_exe, repo_root):
        logger.warning("当前目录尚未初始化 Git 仓库。")
        choice = "yes" if args.no_ask else ""
        if not args.no_ask:
            try:
                choice = input("是否执行 git init 初始化当前目录？[Y/n]: ").strip().lower()
            except KeyboardInterrupt:
                sys.exit(130)
        if choice not in ("", "y", "yes"):
            logger.info("已取消初始化，操作终止。")
            return
        if run_shell(git_exe, ["init"], cwd=repo_root).returncode != 0:
            logger.error("git init 失败")
            sys.exit(1)
    if not args.branch and args.mode in ("push", "pull"):
        args.branch = get_current_branch(git_exe, repo_root) or "main"
    remote_url = (args.remote or get_origin_url(git_exe, repo_root)
                  or get_branch_tracking_url(git_exe, args.branch, repo_root))
    clone_branch = args.branch
    clone_subdirectory = None
    explicit_branch = any(option in sys.argv[1:] for option in ("--branch", "-b"))
    if remote_url and args.mode in ("push", "pull", "clone"):
        remote_url, url_branch, clone_subdirectory = parse_github_subdirectory_url(remote_url)
        if url_branch and not explicit_branch:
            args.branch = url_branch
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
            apply_git_user_config(git_exe, remote_url, "AUTO", args.no_ask, repo_root)
            logger.info("✅ 当前仓库用户配置完成！")
            return
        if args.mode == "clone":
            git_clone(git_exe, clone_branch, remote_url, extra, clone_subdirectory,
                      connect_timeout=args.connect_timeout,
                      low_speed_limit=args.low_speed_limit,
                      low_speed_time=args.low_speed_time,
                      retry_count=args.retry,
                    )
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
                set_remote(git_exe, remote_url, repo_root)
                logger.info("✅ 远程地址已重新绑定。")
            return
        large_files = scan_large_files(repo_root, threshold_bytes)
        has_large = len(large_files) > 0
        logger.info(f"扫描到 {len(large_files)} 个本地大文件")
        if has_large:
            if not check_lfs_available(git_exe):
                if not install_lfs(git_exe, retry_count=args.retry):
                    sys.exit(1)
                if not check_lfs_available(git_exe):
                    logger.critical("Git LFS 安装后仍不可用")
                    sys.exit(1)
            if not init_lfs(git_exe, repo_root):
                sys.exit(1)
            clean_and_apply_lfs(git_exe, repo_root, large_files)
        if remote_url:
            set_remote(git_exe, remote_url, repo_root)
        if args.mode == "pull":
            git_pull(git_exe, args.branch, extra, remote_url,
                     retry_count=args.retry,
                     connect_timeout=args.connect_timeout,
                     low_speed_limit=args.low_speed_limit,
                     low_speed_time=args.low_speed_time,
                     repo_root=repo_root)
        elif args.mode == "push":
            git_push(git_exe, args.branch, repo_root, extra, args.commit_msg, remote_url,
                     args.user, args.retry,
                     connect_timeout=args.connect_timeout,
                     low_speed_limit=args.low_speed_limit,
                     low_speed_time=args.low_speed_time,
                     no_ask=args.no_ask, lfs_paths=large_files,
                     max_commit_bytes=args.max_commit_size)
        logger.info("✅ 操作结束！")
    except KeyboardInterrupt:
        logger.warning("\n[CANCEL] 用户手动终止。")
        sys.exit(130)
