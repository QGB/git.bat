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
    if not logger.handlers: logger.addHandler(handler)

def stime():
    ft = time.time()
    return time.strftime('%Y-%m-%d__%H.%M.%S', time.localtime(ft)) + '__.' + f"{ft:.3f}".split('.')[1]

def kill_process_tree(pid: int, proc: subprocess.Popen = None):
    if sys.platform == "win32":
        try: subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
    else:
        if proc:
            try: proc.kill()
            except: pass

def looks_like_url(s: str) -> bool: return s.startswith("https://") or s.startswith("git@") or "://" in s

def parse_size_str(val: str) -> int:
    if not val: return 104857600
    s = str(val).strip().lower()
    multiplier = 1
    if s.endswith("gb") or s.endswith("g"): multiplier = 1024**3; s = s.rstrip("gb").rstrip("g")
    elif s.endswith("mb") or s.endswith("m"): multiplier = 1024**2; s = s.rstrip("mb").rstrip("m")
    elif s.endswith("kb") or s.endswith("k"): multiplier = 1024; s = s.rstrip("kb").rstrip("k")
    elif s.endswith("b"): s = s.rstrip("b")
    try: return int(float(s) * multiplier)
    except ValueError: return 104857600

def preprocess_args():
    valid_modes = {"push", "pull", "init", "list-big", "listbig", "remove-big", "undo"}
    raw = sys.argv[1:]
    url_indices = {i for i, arg in enumerate(raw) if looks_like_url(arg)}
    new, need_auto_user, i = [], False, 0
    while i < len(raw):
        arg = raw[i]
        if arg in ("-m", "--commit-msg", "--commit_msg"):
            msg_parts = raw[i+1:]
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
                if (i + 1) in url_indices: new.extend(["--user", "--remote", nxt]); i += 2; continue
                elif nxt in valid_modes or nxt.startswith("-"): need_auto_user = True; i += 1; continue
                else: new.extend([arg, nxt]); i += 2; continue
            else: need_auto_user = True; i += 1; continue
        if looks_like_url(arg): new.extend(["--remote", arg]); i += 1; continue
        new.append(arg); i += 1
    if need_auto_user: new.append("--user")
    if not any(a in valid_modes for a in new): new.append("push")
    return [sys.argv[0]] + new

def find_git(user_git: str) -> str:
    if user_git and Path(user_git).is_file(): return user_git
    env_git = os.environ.get("GIT_PATH", "")
    if env_git and Path(env_git).is_file(): return env_git
    sys_git = shutil.which("git")
    if sys_git: return sys_git
    logger.critical("无法找到 git 可执行文件。请设置 GIT_PATH 或确保 git 在 PATH 中。"); sys.exit(1)

def get_origin_url(git_bin: str) -> str:
    try:
        res = subprocess.run([git_bin, "remote", "get-url", "origin"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip(): return res.stdout.strip()
    except: pass
    return ""

def get_branch_tracking_url(git_bin: str, branch: str) -> str:
    try:
        remote_name = subprocess.run([git_bin, "config", "--get", f"branch.{branch}.remote"], capture_output=True, text=True).stdout.strip()
        if not remote_name: return ""
        url_res = subprocess.run([git_bin, "remote", "get-url", remote_name], capture_output=True, text=True)
        if url_res.returncode == 0 and url_res.stdout.strip(): return url_res.stdout.strip()
        if looks_like_url(remote_name): return remote_name
    except: pass
    return ""

def run_shell(git_bin: str, args: list[str], realtime: bool = False, extra_env: dict = None) -> subprocess.CompletedProcess:
    cmd = [git_bin] + args
    git_exe_path = Path(git_bin).resolve()
    git_bin_dir = git_exe_path.parent; git_root = git_bin_dir.parent
    portable_paths = [str(git_root / "cmd"), str(git_bin_dir), str(git_root / "mingw64" / "bin"), str(git_root / "usr" / "bin")]
    env = os.environ.copy()
    env["NoDefaultCurrentDirectoryInExePath"] = "1"; env["GIT_FLUSH"] = "1"; env["PYTHONUNBUFFERED"] = "1"
    valid_paths = [p for p in portable_paths if os.path.exists(p)]
    env["PATH"] = os.pathsep.join(valid_paths) + os.pathsep + env.get("PATH", "")
    if extra_env: env.update(extra_env)  # 局部注入，不污染全局
    logger.info(f"▶ RUN: {' '.join(cmd)}")
    proc = None
    try:
        if realtime:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
            output_chunks = []
            for char in iter(lambda: proc.stdout.read(1), ''):
                sys.stdout.write(char); sys.stdout.flush(); output_chunks.append(char)
            retcode = proc.wait()
            return subprocess.CompletedProcess(cmd, retcode, stdout=''.join(output_chunks), stderr='')
        else:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            stdout, stderr = proc.communicate()
            if stdout and stdout.strip(): logger.debug(f"[STDOUT]\n{stdout.strip()}")
            if stderr and stderr.strip():
                if proc.returncode == 0: logger.debug(f"[STDERR]\n{stderr.strip()}")
                else: logger.error(f"[STDERR]\n{stderr.strip()}")
            if proc.returncode != 0: logger.warning(f"命令执行非 0 返回码: {proc.returncode}")
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except KeyboardInterrupt:
        logger.warning("\n[CANCEL] 收到中断信号，正在清理 Git 进程树...")
        if proc and proc.poll() is None: kill_process_tree(proc.pid, proc)
        logger.warning("[CANCEL] 所有相关进程已终止。"); sys.exit(130)
    except Exception as e:
        logger.critical(f"执行异常: {repr(e)}"); raise

def check_lfs_available(git_bin: str) -> bool: return run_shell(git_bin, ["lfs", "version"], realtime=False).returncode == 0

def is_lfs_initialized(repo_root: Path) -> bool:
    hook_path = repo_root / ".git" / "hooks" / "pre-push"
    if hook_path.exists():
        try:
            if 'git-lfs' in hook_path.read_text(encoding='utf-8', errors='ignore'): return True
        except: pass
    return False

def install_lfs() -> bool:
    system = platform.system()
    logger.info("检测到大文件，但未找到 Git LFS，尝试自动安装...")
    if system == "Linux":
        for cmd in [["sudo", "apt-get", "install", "-y", "git-lfs"], ["sudo", "yum", "install", "-y", "git-lfs"],
                    ["sudo", "dnf", "install", "-y", "git-lfs"], ["sudo", "zypper", "install", "-y", "git-lfs"]]:
            if shutil.which(cmd[0]):
                try: subprocess.run(cmd, check=True); return True
                except subprocess.CalledProcessError: pass
        return False
    elif system == "Darwin" and shutil.which("brew"):
        try: subprocess.run(["brew", "install", "git-lfs"], check=True); return True
        except subprocess.CalledProcessError: return False
    return False

def init_lfs(git_bin: str) -> bool:
    logger.info("执行 git lfs install 初始化...")
    if run_shell(git_bin, ["lfs", "install"]).returncode != 0: logger.error("Git LFS 初始化失败！"); return False
    return True

def set_remote(git_bin: str, remote_url: str):
    if not remote_url: return
    check = subprocess.run([git_bin, "remote", "get-url", "origin"], capture_output=True, text=True)
    if check.returncode == 0:
        if check.stdout.strip() == remote_url: return  # 避免重复设置
        logger.info("更新远程 origin 地址...")
        run_shell(git_bin, ["remote", "set-url", "origin", remote_url])
    else:
        logger.info("添加远程 origin 地址...")
        run_shell(git_bin, ["remote", "add", "origin", remote_url])

def scan_large_files(repo_root: Path, threshold: int) -> set[str]:
    large_files = set()
    skip_dirs = {".git", "build", "dist", "__pycache__"}
    for path in repo_root.rglob("*"):
        if any(part in skip_dirs for part in path.parts) or not path.is_file(): continue
        try: fsize = path.stat().st_size
        except OSError: continue
        if fsize >= threshold: large_files.add(str(path.relative_to(repo_root)).replace("\\", "/"))
    return large_files

def clean_and_apply_lfs(git_bin: str, repo_root: Path, large_patterns: set[str]):
    attr_path = repo_root / ".gitattributes"
    other_lines, lfs_lines = [], set()
    if attr_path.exists():
        with open(attr_path, "r", encoding="utf-8") as f:
            for line in f.readlines():
                stripped = line.strip()
                if not stripped: continue
                if "filter=lfs" in stripped: lfs_lines.add(stripped)
                else: other_lines.append(stripped)
    for pat in large_patterns:
        safe_pat = f'"{pat}"' if " " in pat else pat
        lfs_lines.add(f"{safe_pat} filter=lfs diff=lfs merge=lfs -text")
    all_rules = other_lines + sorted(lfs_lines)
    if all_rules:
        with open(attr_path, "w", encoding="utf-8") as f: f.write("\n".join(all_rules) + "\n")
    logger.info(f".gitattributes 更新完成，LFS追踪总数: {len(lfs_lines)}")

def git_pull(git_bin: str, branch: str, extra_args: list[str], remote_url: str = ""):
    ''' #TODO 网络重试逻辑只在 git_push 内部实现
git pull没有重试循环，网络抖动直接退出。 应该封装通用重试逻辑  '''
    logger.info(f"===== 开始执行 git pull {remote_url} {branch} =====")
    if run_shell(git_bin, ["pull", "--progress"] + extra_args + [remote_url, branch], realtime=True).returncode != 0:
        logger.error("git pull 失败！"); sys.exit(1)
    logger.info("===== 开始执行 git lfs pull =====")
    run_shell(git_bin, ["lfs", "pull"], realtime=True)

def extract_remote_user_from_url(remote_url: str) -> str | None:
    if not remote_url: return None
    parsed = urlparse(remote_url)
    if parsed.scheme and parsed.netloc:
        if parsed.username: return parsed.username
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        if path_parts: return path_parts[0]
    if remote_url.startswith("git@"):
        parts = remote_url.split("@", 1)
        if len(parts) == 2 and ":" in parts[1]:
            path_parts = [p for p in parts[1].split(":", 1)[1].strip("/").split("/") if p]
            if path_parts: return path_parts[0]
    path_parts = [p for p in (parsed.path if parsed.path else remote_url).strip("/").split("/") if p]
    return path_parts[0] if path_parts else None

def apply_git_user_config(git_bin: str, remote_url: str, user_arg: str):
    if not remote_url: return
    remote_user = extract_remote_user_from_url(remote_url)
    if user_arg is not None:
        target_user = remote_user if user_arg == "AUTO" else user_arg
        if not target_user: target_user = "git_user"; logger.warning("无法提取用户名，回退为 'git_user'")
        target_email = f"{target_user}@users.noreply.github.com"
        logger.info(f"强制应用用户配置 (-u): user.name=[{target_user}], user.email=[{target_email}]")
        run_shell(git_bin, ["config", "user.name", target_user]); run_shell(git_bin, ["config", "user.email", target_email])
        return
    if not remote_user: return
    local_name = subprocess.run([git_bin, "config", "user.name"], capture_output=True, text=True).stdout.strip()
    local_email = subprocess.run([git_bin, "config", "user.email"], capture_output=True, text=True).stdout.strip()
    if local_name != remote_user:
        logger.warning("发现当前 Git 用户配置与远程目标不一致！")
        print(f"\n请选择本次 Commit 使用配置:\n  [1] 保持原样 ({local_name})\n  [2] 更新为目标 ({remote_user})")
        try: choice = input("请输入 1 或 2 (默认 1): ").strip()
        except KeyboardInterrupt: sys.exit(130)
        if choice == "2":
            run_shell(git_bin, ["config", "user.name", remote_user])
            default_email = f"{remote_user}@users.noreply.github.com"
            try: new_email = input(f"输入邮箱 (默认: {default_email}): ").strip() or default_email
            except KeyboardInterrupt: sys.exit(130)
            run_shell(git_bin, ["config", "user.email", new_email])
            logger.info(f"✅ 更新仓库配置: user.name={remote_user}, user.email={new_email}")
        else: logger.info("保持原配置不变。")


def git_push(git_bin: str, branch: str, repo_root: Path, extra_args: list[str],
             commit_msg: str = "", remote_url: str = "",
             user_arg: str = None, retry_count: int = 10, retry_seconds=5):
    EmptyAfterPush = False
    logger.info(f"当前工作目录: {repo_root.resolve()}")
    if not (repo_root / ".git").exists():
        logger.info("检测到当前目录尚未初始化 Git 仓库，自动执行 git init...")
        run_shell(git_bin, ["init"])
        if remote_url: run_shell(git_bin, ["remote", "add", "origin", remote_url])
    apply_git_user_config(git_bin, remote_url, user_arg)
    if run_shell(git_bin, ["add", "-A"]).returncode != 0:
        logger.error("git add 失败")
        sys.exit(1)
    result = subprocess.run([git_bin, "status", "--porcelain"], capture_output=True, text=True)
    changed_files = []
    if result.returncode == 0 and result.stdout.strip():
        changed_files = [line[3:].strip() for line in result.stdout.strip().split("\n") if line[3:].strip()]
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

        commit_msg = (f"[{max_file} {max_size}B] {stime()} {__file__[-20:]} auto" if max_file else f" auto {stime()}")
    if changed_files:
        logger.info(f"变更文件: {len(changed_files)} 个" + (f" (显示前10: {changed_files[:10]})" if len(changed_files) > 10 else f" {changed_files}"))
        for f in changed_files:
            if f == "ReadMe.md":
                with open(repo_root / "ReadMe.md", 'rb') as fh:
                    if b'#EmptyAfterPush' in fh.read():
                        EmptyAfterPush = True
        if run_shell(git_bin, ["commit", "-m", commit_msg]).returncode != 0:
            logger.error("git commit 失败")
            sys.exit(1)
    else:
        logger.info("暂存区为空")

    is_debug = logger.getEffectiveLevel() <= logging.DEBUG
    cmd_args = ["push", "-v", "--progress"] + extra_args + [remote_url, branch]
    for attempt in range(1, retry_count + 1):
        logger.info(f"===== 推送 {remote_url} {branch} (尝试 {attempt}/{retry_count}) 间隔 {retry_seconds}s =====")
        extra_env = {}
        if is_debug or attempt > 1:
            extra_env["GIT_CURL_VERBOSE"] = "1"
            extra_env["GIT_TRACE"] = "1"
            if attempt > 1:
                logger.info("🔍 启用详细连接日志")
        try:
            push_res = run_shell(git_bin, cmd_args, realtime=True, extra_env=extra_env)
            if push_res.returncode == 0:
                if EmptyAfterPush:
                    with open(repo_root / 'ReadMe.md', 'wb') as f:
                        f.write(b'')
                    logger.info(f"EmptyAfterPush 成功 {stime()}")
                logger.info(f"✅ 推送成功 {stime()}")
                break
            else:
                eout = push_res.stdout or ""
                eout_l = eout.lower()
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
                    "connected host has failed to respond"
                ]
                auth_kw = [
                    "http 401",
                    "http 403",
                    "fatal: authentication failed",
                    "permission denied (publickey)"
                ]
                is_net = any(k in eout_l for k in net_kw)
                is_auth = any(k in eout_l for k in auth_kw)

                if is_net and not is_auth:
                    logger.warning(f"⚠️ 网络错误，稍后重试 (返回码: {push_res.returncode})")
                else:
                    logger.error(f"❌ 推送失败 (返回码: {push_res.returncode})")
                    sys.exit(1)
        except Exception as e:
            logger.warning(f"⚠️ 异常: {repr(e)}，重试")

        if attempt < retry_count:
            time.sleep(retry_seconds)
        else:
            logger.error(f"❌ 达到最大重试次数 {retry_count}")
            sys.exit(1)
        
def git_list_big(git_bin: str, threshold_bytes: int) -> list[tuple[int, str, str]]:
    logger.info(f"===== 扫描历史大文件 >= {threshold_bytes/1024/1024:.2f} MB =====")
    try:
        p1 = subprocess.Popen([git_bin, "rev-list", "--objects", "--all"], stdout=subprocess.PIPE, text=True)
        p2 = subprocess.Popen([git_bin, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize) %(rest)"],
                              stdin=p1.stdout, stdout=subprocess.PIPE, text=True)
        p1.stdout.close()
        large_files = []
        count = 0
        for line in p2.stdout:  # 流式读取，不一次性加载到内存
            count += 1
            if count % 10000 == 0: logger.info(f"已扫描 {count} 个对象...")
            parts = line.split(" ", 3)
            if len(parts) >= 4 and parts[1] == "blob":
                size = int(parts[2])
                if size >= threshold_bytes: large_files.append((size, parts[3], parts[0]))
        p2.wait()
        large_files.sort(key=lambda x: x[0], reverse=True)
        if not large_files: logger.info("🎉 未发现超过阈值的大文件。")
        else:
            print(f"\n{'大小 (MB)':<12} | {'Blob Hash':<40} | {'文件路径'}")
            print("-" * 85)
            for size, path, blob_hash in large_files: print(f"{size/1024/1024:<12.2f} | {blob_hash:<40} | {path}")
        return large_files
    except Exception as e:
        logger.error(f"扫描失败: {e}"); return []

def git_remove_big(git_bin: str, threshold_bytes: int, target_hashes: list[str] = None):
    logger.info("===== 准备清理历史大文件 =====")
    logger.info("🛡️ 仅移除指定 Blob 及其关联 Commit，更早的历史哈希保持不变。\n")
    if run_shell(git_bin, ["filter-repo", "--version"]).returncode != 0:
        logger.error("未检测到 git-filter-repo，请先安装：pip install git-filter-repo"); sys.exit(1)
    hashes_to_remove = set()
    if target_hashes:
        for h in target_hashes:
            h = h.strip()
            if h: hashes_to_remove.add(h)
        logger.info(f"使用指定 {len(hashes_to_remove)} 个 Blob Hash 进行精准删除。")
    else:
        large_files = git_list_big(git_bin, threshold_bytes)
        if not large_files: logger.info("没有符合条件的大文件。"); return
        for _, _, blob_hash in large_files: hashes_to_remove.add(blob_hash)
    if not hashes_to_remove: return
    logger.info(f"即将擦除 {len(hashes_to_remove)} 个 Blob:")
    for h in sorted(hashes_to_remove): logger.info(f"  - {h}")
    # 核心修复：在 callback 中使用 .decode() 将 bytes 转成字符串，与集合中的字符串对比
    hash_list_code = ", ".join([f'"{h}"' for h in hashes_to_remove])
    callback_code = (
        f"target_hashes = {{{hash_list_code}}}\n"
        f"if blob.original_id.decode('ascii') in target_hashes:\n"
        f"    blob.skip()"
    )
    res = run_shell(git_bin, ["filter-repo", "--blob-callback", callback_code, "--force"], realtime=True)
    if res.returncode == 0:
        msg=rf'''
filter‑repo 重写历史后，旧对象还在本地 git 库，磁盘空间不会立刻释放，需要手动：
git reflog expire --expire=now --all
git gc --prune=now --aggressive
✅ 历史大文件 Blob 已擦除 （ 之前 Commit 保留未动）
'''
        logger.info(msg)
        logger.warning("⚠️ 历史已重写，推送需使用 --force")
    else: logger.error("❌ 清理失败"); sys.exit(1)

def main():
    sys.argv = preprocess_args()
    default_git = os.environ.get("GIT_PATH", "git")
    default_branch = os.environ.get("BRANCH", "master")
    parser = argparse.ArgumentParser(description="Git Auto LFS Tool")
    parser.add_argument("--git", default=default_git, help="git 可执行文件路径")
    parser.add_argument("--branch", '-b', default=default_branch, help="分支名称")
    parser.add_argument("--size", '-s', default="100mb", help="大文件大小限制（默认 100mb）")
    parser.add_argument("--threshold", type=int, default=0, help="字节数阈值（兼容）")
    parser.add_argument("--hashes", "--hash", default="", help="手动指定 Blob Hash，逗号分隔")
    parser.add_argument("--remote", default="", help="完整远程 URL")
    # parser.add_argument("--auth", help="认证信息（已废弃）")
    parser.add_argument("--commit-msg", "--commit_msg", '-m', default="", help="自定义 commit 消息")
    parser.add_argument("--user", "-u", nargs="?", const="AUTO", default=None, help="自动配置 Git 用户")
    parser.add_argument("--retry", "-r", type=int, default=10, help="Push 失败重试次数")
    parser.add_argument("--verbose", "-v", type=int, default=2, help="日志级别: 0=Error, 1=Warn, 2=Info, 3=Debug")
    parser.add_argument("mode", choices=["push", "pull", "init", "list-big", "listbig", "remove-big", "undo"])
    args, extra = parser.parse_known_args()
    setup_logging(args.verbose)
    git_exe = find_git(args.git)
    repo_root = Path.cwd()
    remote_url = args.remote or get_origin_url(git_exe) or get_branch_tracking_url(git_exe, args.branch)
    if not remote_url and args.mode not in ("list-big", "listbig", "remove-big", "undo"):
        logger.critical("未提供远程仓库地址，且未找到 origin/tracking 配置。"); sys.exit(1)
    threshold_bytes = args.threshold if args.threshold > 0 else parse_size_str(args.size)
    logger.info(f"仓库路径: {repo_root.absolute()}"); logger.info(f"Git程序: {git_exe}")
    if args.mode != "init": logger.info(f"文件限制: {threshold_bytes/1024/1024:.2f} MB ({threshold_bytes} 字节)")
    if remote_url: logger.info(f"远程地址: {remote_url}")
    logger.info(f"分支: {args.branch}")
    try:
        if args.mode == "undo":
            logger.info("===== 撤销上一次提交 =====")
            run_shell(git_exe, ["reset", "--soft", "HEAD~1"])
            run_shell(git_exe, ["reset", "HEAD", "."])
            logger.info("✅ 撤销完成，工作区文件未改动。")
            return
        if args.mode == "init":
            logger.info("===== 执行 git init =====")
            run_shell(git_exe, ["init"]); run_shell(git_exe, ["remote", "remove", "origin"])
            run_shell(git_exe, ["remote", "add", "origin", remote_url])
            logger.info("✅ 初始化完成！"); return
        if args.mode in ("list-big", "listbig"):
            git_list_big(git_exe, threshold_bytes); return
        if args.mode == "remove-big":
            target_hashes = [h.strip() for h in args.hashes.split(",") if h.strip()] if args.hashes else None
            git_remove_big(git_exe, threshold_bytes, target_hashes)
            if remote_url: set_remote(git_exe, remote_url); logger.info("✅ 远程地址已重新绑定。")
            return
        large_files = scan_large_files(repo_root, threshold_bytes)
        has_large = len(large_files) > 0
        logger.info(f"扫描到 {len(large_files)} 个本地大文件")
        if has_large:
            if not check_lfs_available(git_exe):
                if not install_lfs(): sys.exit(1)
                if not check_lfs_available(git_exe): logger.critical("Git LFS 安装后仍不可用"); sys.exit(1)
            if not is_lfs_initialized(repo_root):
                if not init_lfs(git_exe):sys.exit(1)
            else: logger.info("LFS hooks 已初始化")
            clean_and_apply_lfs(git_exe, repo_root, large_files)
        if remote_url: set_remote(git_exe, remote_url)
        if args.mode == "pull": git_pull(git_exe, args.branch, extra, remote_url)
        elif args.mode == "push": git_push(git_exe, args.branch, repo_root, extra, args.commit_msg, remote_url, args.user, args.retry)
        logger.info("✅ 操作结束！")
    except KeyboardInterrupt:
        logger.warning("\n[CANCEL] 用户手动终止。"); sys.exit(130)

if __name__ == "__main__":
    main()