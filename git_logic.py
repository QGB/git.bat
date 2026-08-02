import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

# 全局日志记录器
logger = logging.getLogger("GitAutoLFS")

def setup_logging(verbosity: int):
    """
    配置日志级别：
    0 = ERROR (仅致命错误)
    1 = WARNING (警告与错误)
    2 = INFO (默认，常规执行流程信息)
    3 = DEBUG (包含所有子进程详细 stdout/stderr 输出)
    """
    levels = {
        0: logging.ERROR,
        1: logging.WARNING,
        2: logging.INFO,
        3: logging.DEBUG
    }
    level = levels.get(verbosity, logging.DEBUG if verbosity > 3 else logging.ERROR)
    
    # 结构化输出格式
    formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-7s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    
    logger.setLevel(level)
    # 避免重复添加 handler
    if not logger.handlers:
        logger.addHandler(handler)

def stime():
    ft = time.time()
    # 更加健壮的时间格式化，避免 .0 导致的 IndexError
    sf = f"{ft:.3f}" 
    tail = sf.split('.')[1]
    return time.strftime('%Y-%m-%d__%H.%M.%S', time.localtime(ft)) + '__.' + tail

def kill_process_tree(pid: int, proc: subprocess.Popen = None):
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    else:
        # Linux/Mac 备用终止方案
        if proc:
            try:
                proc.kill()
            except Exception:
                pass

def looks_like_url(s: str) -> bool:
    return s.startswith("https://") or s.startswith("git@") or "://" in s

def parse_url_info(url: str):
    parsed = urlparse(url)
    auth = None
    if "@" in parsed.netloc:
        auth, host = parsed.netloc.split("@", 1)
        remote_url = urlunparse(parsed._replace(netloc=f"{auth}@{host}"))
    else:
        remote_url = url
    repo_path = parsed.path.lstrip("/")
    return remote_url, auth, repo_path

def parse_size_str(val: str) -> int:
    if not val:
        return 104857600
    s = str(val).strip().lower()
    multiplier = 1
    if s.endswith("gb") or s.endswith("g"):
        multiplier = 1024 * 1024 * 1024
        s = s.rstrip("gb").rstrip("g")
    elif s.endswith("mb") or s.endswith("m"):
        multiplier = 1024 * 1024
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
    valid_modes = {"push", "pull", "init", "list-big", "listbig", "remove-big", "filter-repo"}
    raw = sys.argv[1:]

    url_indices = []
    url_values = []
    for i, arg in enumerate(raw):
        if looks_like_url(arg):
            url_indices.append(i)
            url_values.append(arg)

    new = []
    i = 0
    while i < len(raw):
        arg = raw[i]
        if arg in ("-u", "--user"):
            if i + 1 < len(raw) and (i + 1) in url_indices:
                new.append("--user")
                new.append("--remote")
                new.append(raw[i + 1])
                i += 2
                continue
            else:
                if i + 1 < len(raw):
                    nxt = raw[i + 1]
                    if nxt in valid_modes or nxt.startswith("-"):
                        new.append(arg)
                        i += 1
                        continue
                    else:
                        new.append(arg)
                        new.append(nxt)
                        i += 2
                        continue
                else:
                    new.append(arg)
                    i += 1
                    continue

        if looks_like_url(arg):
            if i > 0 and raw[i - 1] in ("-u", "--user"):
                i += 1
                continue
            new.append("--remote")
            new.append(arg)
            i += 1
            continue

        new.append(arg)
        i += 1

    has_mode = any(a in valid_modes for a in new)
    if not has_mode:
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
    except Exception:
        pass
    return ""

def get_branch_tracking_url(git_bin: str, branch: str) -> str:
    try:
        remote_name = subprocess.run(
            [git_bin, "config", "--get", f"branch.{branch}.remote"],
            capture_output=True, text=True
        ).stdout.strip()
        if not remote_name:
            return ""
        url_res = subprocess.run(
            [git_bin, "remote", "get-url", remote_name],
            capture_output=True, text=True
        )
        if url_res.returncode == 0 and url_res.stdout.strip():
            return url_res.stdout.strip()
        if looks_like_url(remote_name):
            return remote_name
    except Exception:
        pass
    return ""

def run_shell(git_bin: str, args: list[str], realtime: bool = False) -> subprocess.CompletedProcess:
    cmd = [git_bin] + args
    git_exe_path = Path(git_bin).resolve()
    git_bin_dir = git_exe_path.parent
    git_root = git_bin_dir.parent
    portable_paths = [
        str(git_root / "cmd"),
        str(git_bin_dir),
        str(git_root / "mingw64" / "bin"),
        str(git_root / "usr" / "bin"),
    ]
    env = os.environ.copy()
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    valid_paths = [p for p in portable_paths if os.path.exists(p)]
    env["PATH"] = os.pathsep.join(valid_paths) + os.pathsep + env.get("PATH", "")
    
    logger.info(f"▶ RUN: {' '.join(cmd)}")
    proc = None
    try:
        if realtime:
            proc = subprocess.Popen(cmd, env=env)
            retcode = proc.wait()
            return subprocess.CompletedProcess(cmd, retcode)
        else:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8")
            stdout, stderr = proc.communicate()
            
            if stdout and stdout.strip():
                logger.debug(f"[STDOUT]\n{stdout.strip()}")
            if stderr and stderr.strip():
                # 很多时候 Git 正常信息也会走向 stderr，因此根据 returncode 区分级别
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
    res = run_shell(git_bin, ["lfs", "version"], realtime=False)
    return res.returncode == 0

def is_lfs_initialized(repo_root: Path) -> bool:
    hook_path = repo_root / ".git" / "hooks" / "pre-push"
    if hook_path.exists():
        try:
            content = hook_path.read_text(encoding='utf-8', errors='ignore')
            if 'git-lfs' in content:
                return True
        except Exception:
            pass
    return False

def install_lfs() -> bool:
    system = platform.system()
    logger.info("检测到大文件，但未找到 Git LFS，尝试自动安装...")
    if system == "Linux":
        for cmd in [
            ["sudo", "apt-get", "install", "-y", "git-lfs"],
            ["sudo", "yum", "install", "-y", "git-lfs"],
            ["sudo", "dnf", "install", "-y", "git-lfs"],
            ["sudo", "zypper", "install", "-y", "git-lfs"],
        ]:
            if shutil.which(cmd[0]):
                try:
                    subprocess.run(cmd, check=True)
                    return True
                except subprocess.CalledProcessError:
                    pass
        return False
    elif system == "Darwin":
        if shutil.which("brew"):
            try:
                subprocess.run(["brew", "install", "git-lfs"], check=True)
                return True
            except subprocess.CalledProcessError:
                return False
    return False

def init_lfs(git_bin: str) -> bool:
    logger.info("执行 git lfs install 初始化...")
    res = run_shell(git_bin, ["lfs", "install"])
    if res.returncode != 0:
        logger.error("Git LFS 初始化失败！")
        return False
    return True

def set_remote(git_bin: str, remote_url: str):
    if not remote_url:
        return
    check = subprocess.run([git_bin, "remote", "get-url", "origin"],
                           capture_output=True, text=True)
    if check.returncode == 0:
        logger.info("更新远程 origin 地址...")
        run_shell(git_bin, ["remote", "set-url", "origin", remote_url])
    else:
        logger.info("添加远程 origin 地址...")
        run_shell(git_bin, ["remote", "add", "origin", remote_url])

def scan_large_files(repo_root: Path, threshold: int) -> set[str]:
    large_files = set()
    skip_dirs = {".git", "build", "dist", "__pycache__"}
    for path in repo_root.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            fsize = path.stat().st_size
        except OSError:
            continue
        if fsize >= threshold:
            rel = str(path.relative_to(repo_root)).replace("\\", "/")
            large_files.add(rel)
    return large_files

def clean_and_apply_lfs(git_bin: str, repo_root: Path, large_patterns: set[str]):
    attr_path = repo_root / ".gitattributes"
    other_lines = []
    lfs_lines = set()
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
        # 如果路径包含空格，用双引号包裹避免解析错误
        safe_pat = f'"{pat}"' if " " in pat else pat
        rule = f"{safe_pat} filter=lfs diff=lfs merge=lfs -text"
        lfs_lines.add(rule)
        
    all_rules = other_lines + sorted(lfs_lines)
    if all_rules:
        with open(attr_path, "w", encoding="utf-8") as f:
            f.write("\n".join(all_rules) + "\n")
    logger.info(f".gitattributes 更新完成，LFS追踪总数: {len(lfs_lines)}")

def git_pull(git_bin: str, branch: str, extra_args: list[str], remote_url: str = ""):
    logger.info(f"===== 开始执行 git pull {remote_url} {branch} =====")
    cmd_args = ["pull", "--progress"] + extra_args + [remote_url, branch]
    res = run_shell(git_bin, cmd_args, realtime=True)
    if res.returncode != 0:
        logger.error("git pull 失败！")
        sys.exit(1)
    logger.info("===== 开始执行 git lfs pull =====")
    run_shell(git_bin, ["lfs", "pull"], realtime=True)

def extract_remote_user_from_url(remote_url: str) -> str | None:
    if not remote_url:
        return None
    parsed = urlparse(remote_url)
    if parsed.scheme and parsed.netloc:
        username = parsed.username
        if username:
            return username
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        if path_parts:
            return path_parts[0]
    if remote_url.startswith("git@"):
        parts = remote_url.split("@", 1)
        if len(parts) == 2:
            host_path = parts[1]
            if ":" in host_path:
                _, path = host_path.split(":", 1)
                path_parts = [p for p in path.strip("/").split("/") if p]
                if path_parts:
                    return path_parts[0]
    path = parsed.path if parsed.path else remote_url
    path_parts = [p for p in path.strip("/").split("/") if p]
    return path_parts[0] if path_parts else None

def apply_git_user_config(git_bin: str, remote_url: str, user_arg: str):
    if not remote_url:
        return
    remote_user = extract_remote_user_from_url(remote_url)
    
    if user_arg is not None:
        if user_arg == "AUTO":
            target_user = remote_user
            if not target_user:
                logger.warning("无法从远程 URL 中提取到用户名，回退为 'git_user'")
                target_user = "git_user"
        else:
            target_user = user_arg
        target_email = f"{target_user}@users.noreply.github.com"
        run_shell(git_bin, ["config", "user.name", target_user])
        run_shell(git_bin, ["config", "user.email", target_email])
        logger.info(f"强制应用用户配置 (-u): user.name=[{target_user}], user.email=[{target_email}]")
        return
        
    if not remote_user:
        return
        
    res_name = subprocess.run([git_bin, "config", "user.name"], capture_output=True, text=True)
    local_name = res_name.stdout.strip()
    res_email = subprocess.run([git_bin, "config", "user.email"], capture_output=True, text=True)
    local_email = res_email.stdout.strip()
    
    if local_name != remote_user:
        logger.warning("发现当前 Git 用户配置与远程目标不一致！")
        logger.info(f"当前 Git 本地配置: user.name=[{local_name or '未设置'}], user.email=[{local_email or '未设置'}]")
        logger.info(f"远程目标 URL 用户: [{remote_user}]")
        print("\n请选择本次 Commit 要使用的配置 (命令行带入 -u 参数即可跳过此交互询问):")
        print(f"  [1] 保持当前 Git 本地配置不变")
        print(f"  [2] 更新为远程目标用户名 ({remote_user})")
        try:
            choice = input("请输入 1 或 2 (默认 1): ").strip()
        except KeyboardInterrupt:
            logger.info("用户交互操作取消。")
            sys.exit(130)
            
        if choice == "2":
            run_shell(git_bin, ["config", "user.name", remote_user])
            default_email = f"{remote_user}@users.noreply.github.com"
            try:
                new_email = input(f"请输入对应的邮箱 (直接回车默认使用: {default_email}): ").strip()
            except KeyboardInterrupt:
                logger.info("用户交互操作取消。")
                sys.exit(130)
            if not new_email:
                new_email = default_email
            run_shell(git_bin, ["config", "user.email", new_email])
            logger.info(f"✅ 已成功更新仓库配置: user.name={remote_user}, user.email={new_email}")
        else:
            logger.info("保持原配置不变。")

def git_push(git_bin: str, branch: str, repo_root: Path, extra_args: list[str],
             commit_msg: str = "", remote_url: str = "",
             user_arg: str = None, retry_count: int = 10, retry_seconds=5):
    
    if not commit_msg:
        commit_msg = f'{__file__[-20:]} auto {stime()}'

    logger.info(f"当前工作目录: {repo_root.resolve()}")
    if not (repo_root / ".git").exists():
        logger.info("检测到当前目录尚未初始化 Git 仓库，自动执行 git init...")
        run_shell(git_bin, ["init"])
        if remote_url:
            run_shell(git_bin, ["remote", "add", "origin", remote_url])
            
    apply_git_user_config(git_bin, remote_url, user_arg)
    run_shell(git_bin, ["add", "-A"])
    
    result = subprocess.run([git_bin, "status", "--porcelain"], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        files = result.stdout.strip().split("\n")
        logger.info(f"本次涉及变更的文件列表 (共 {len(files)} 个):")
        if len(files) > 10:
            logger.info(f"  ... 以及其他 {len(files) - 10} 个文件")
        else:    
            logger.info(f"  {files}")
            
        for f in files:
            if ('M  ReadMe.md' in f) or ('A  ReadMe.md' in f):
                b_ReadMe=''
                with open(repo_root/'ReadMe.md','rb') as f:
                    b_ReadMe=f.read(-1)
                if b'#EmptyAfterPush' in b_ReadMe:
                    globals()['EmptyAfterPush']=True
                    # print(stime(),'EmptyAfterPush',b_ReadMe[-99:])
            # logger.info(f"  {f}")
            
        run_shell(git_bin, ["commit", "-m", commit_msg])    
    else:
        logger.info("暂存区为空（可能没有新增或修改）")
        
    cmd_args = ["push", "-v", "--progress"] + extra_args + [remote_url, branch]
    # 在 git_push 函数中，推送循环部分修改如下：

    for attempt in range(1, retry_count + 1):
        logger.info(f"===== 推送 {remote_url} {branch} (尝试 {attempt}/{retry_count}) 间隔 {retry_seconds}s =====")
        try:
            push_res = run_shell(git_bin, cmd_args, realtime=True)
            if push_res.returncode == 0:
                if 'EmptyAfterPush' in globals() and globals()['EmptyAfterPush']:
                    with open(repo_root/'ReadMe.md','wb') as f:
                        f.write(b'')
                    logger.info(f"{b_ReadMe} #EmptyAfterPush ReadMe.md 成功！ {stime()}")
                logger.info(f"✅ 推送成功！ {stime()}")
                break
            else:
                # 判断是否为网络错误
                error_output = (push_res.stderr or "") + (push_res.stdout or "")
                is_network_error = any(keyword in error_output for keyword in [
                    "Could not read from remote repository",
                    "ssh: connect to host",
                    "Connection timed out",
                    "The remote end hung up unexpectedly",
                    "fatal: unable to access",
                    "Failed to connect to",
                    "Network is unreachable",
                    "remote: fatal:",
                    "HTTP 401",   # 401 是认证失败，通常不是临时网络问题，但有时 token 过期也算
                    "HTTP 403",   # 403 权限不足，不应重试
                    "HTTP 500",   # 服务器内部错误，可能临时，也可能不是
                    # 更精确地，只有网络超时、连接重置等才重试，401/403 不重试
                ])
                # 排除认证/权限错误（401/403）不重试
                if any(keyword in error_output for keyword in ["HTTP 401", "HTTP 403", "fatal: Authentication failed", "Permission denied (publickey)"]):
                    is_network_error = False  # 这类错误不应重试
                    
                if is_network_error:
                    logger.warning(f"⚠️ 网络错误，稍后重试 (返回码: {push_res.returncode})")
                else:
                    logger.error(f"❌ 推送失败，非网络错误，退出。返回码: {push_res.returncode}")
                    sys.exit(1)
                    
        except Exception as e:
            # 异常可能也是网络问题，但无法确定，按网络错误处理并重试
            logger.warning(f"⚠️ 推送进程发生异常: {repr(e)}，可能为网络问题，重试")
            # 但仍需检查是否为底层异常如权限拒绝？通常异常多为超时等，暂且重试
        
        if attempt < retry_count:
            time.sleep(retry_seconds)
        else:
            logger.error(f"❌ 已达到最大重试次数 {retry_count}，网络持续失败，终止操作。")
            sys.exit(1)

def git_list_big(git_bin: str, threshold_bytes: int) -> list[tuple[int, str, str]]:
    logger.info(f"===== 扫描历史记录中 >= {threshold_bytes/1024/1024:.2f} MB ({threshold_bytes} 字节) 的大文件 =====")
    try:
        p1 = subprocess.Popen([git_bin, "rev-list", "--objects", "--all"], stdout=subprocess.PIPE, text=True)
        p2 = subprocess.Popen([git_bin, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize) %(rest)"],
                              stdin=p1.stdout, stdout=subprocess.PIPE, text=True)
        p1.stdout.close()
        output, _ = p2.communicate()
        large_files = []
        for line in output.splitlines():
            parts = line.split(" ", 3)
            if len(parts) >= 4 and parts[1] == "blob":
                size = int(parts[2])
                if size >= threshold_bytes:
                    large_files.append((size, parts[3], parts[0]))
        large_files.sort(key=lambda x: x[0], reverse=True)
        if not large_files:
            logger.info("🎉 历史记录中未发现超过设定的文件。")
        else:
            print(f"\n{'大小 (MB)':<12} | {'Blob Hash':<40} | {'文件路径'}")
            print("-" * 85)
            for size, path, blob_hash in large_files:
                print(f"{size/1024/1024:<12.2f} | {blob_hash:<40} | {path}")
        return large_files
    except Exception as e:
        logger.error(f"获取历史记录大文件失败: {e}")
        return []

def git_remove_big(git_bin: str, threshold_bytes: int, target_hashes: list[str] = None):
    logger.info("===== 准备清理历史大文件 =====")
    logger.info("🛡️ 【安全保证】Git 的底层机制确切保证：")
    logger.info("   1. 工具只会跳过该大文件的 Blob 对象及其直接关联。")
    logger.info("   2. 彻底移除仅影响引入该大文件的 Commit 及其后续子 Commit。")
    logger.info("   3. ⚠️ 引入该大文件之前的历史 Commit Hash 绝对不会受到任何影响，100% 保持原样！\n")
    
    check_cmd = run_shell(git_bin, ["filter-repo", "--version"], realtime=False)
    if check_cmd.returncode != 0:
        logger.error("未检测到 git-filter-repo 工具。")
        logger.info("💡 请先在终端运行安装：pip install git-filter-repo")
        sys.exit(1)
        
    hashes_to_remove = set()
    if target_hashes:
        for h in target_hashes:
            cleaned = h.strip()
            if cleaned:
                hashes_to_remove.add(cleaned)
        logger.info(f"使用指定的 {len(hashes_to_remove)} 个 Blob Hash 进行精准删除。")
    else:
        large_files = git_list_big(git_bin, threshold_bytes)
        if not large_files:
            logger.info("没有找到符合条件的大文件，无需清理。")
            return
        for _, _, blob_hash in large_files:
            hashes_to_remove.add(blob_hash)
            
    if not hashes_to_remove:
        logger.info("没有待清理的 Blob Hash，操作已取消。")
        return
        
    logger.info(f"即将按 Blob Hash 精准擦除以下 {len(hashes_to_remove)} 个数据节点:")
    for h in sorted(hashes_to_remove):
        logger.info(f"  - Blob Hash: {h}")
        
    hash_list_code = ", ".join([f'b"{h}"' for h in hashes_to_remove])
    callback_code = (
        f"target_hashes = {{{hash_list_code}}}\n"
        f"if blob.original_id in target_hashes:\n"
        f"    blob.skip()"
    )
    cmd_args = ["filter-repo", "--blob-callback", callback_code, "--force"]
    res = run_shell(git_bin, cmd_args, realtime=True)
    if res.returncode == 0:
        logger.info("✅ 历史大文件 Blob 已成功擦除！(之前的 Commit 完全保留未动)")
        logger.warning("⚠️  注意：关联历史记录已被重写，推送时需使用强制推送 (例如: --force)。")
    else:
        logger.error("❌ 清理失败！")
        sys.exit(1)

def main():
    sys.argv = preprocess_args()

    default_git = os.environ.get("GIT_PATH", "git")
    default_branch = os.environ.get("BRANCH", "master")
    parser = argparse.ArgumentParser(description="Git Auto LFS Tool")
    parser.add_argument("--git", default=default_git, help="git 可执行文件路径")
    parser.add_argument("--branch", '-b', default=default_branch, help="分支名称")
    parser.add_argument("--size", '-s', default="100mb", help="大文件大小限制（默认 100mb）")
    parser.add_argument("--threshold", type=int, default=0, help="（兼容项）字节数阈值")
    parser.add_argument("--hashes", "--hash", default="", help="手动要清理的 Blob Hash（多个用逗号隔开）")
    parser.add_argument("--remote", default="", help="完整远程 URL")
    parser.add_argument("--auth", help="认证信息 user:token（已废弃，认证信息已包含在 remote 中）")
    parser.add_argument("--commit-msg", "--commit_msg", '-m', default="", help="自定义 commit 消息")
    parser.add_argument("--user", "-u", nargs="?", const="AUTO", default=None, help="自动配置 Git 用户")
    parser.add_argument("--retry", "-r", type=int, default=10, help="网络断开或 Push 失败时的重试次数 (默认 10)")
    
    # 新增 --verbose 参数，默认值为 2 (即INFO)
    parser.add_argument("--verbose", "-v", type=int, default=2, 
                        help="日志输出级别: 0=Error, 1=Warn, 2=Info(默认), 3=Debug")
    parser.add_argument("mode", choices=["push", "pull", "init", "list-big", "listbig", "remove-big", "filter-repo"], help="操作模式")
    
    args, extra = parser.parse_known_args()

    # 初始化配置 logging 系统
    setup_logging(args.verbose)

    git_exe = find_git(args.git)
    repo_root = Path.cwd()

    remote_url = args.remote or get_origin_url(git_exe) or get_branch_tracking_url(git_exe, args.branch)
    if not remote_url and args.mode not in ("list-big", "listbig", "remove-big", "filter-repo"):
        logger.critical("未提供远程仓库地址，且未找到 origin 或当前分支的 tracking 远程配置。")
        logger.info("💡 请通过参数传递 URL，或先配置远程仓库：git remote add origin <url>")
        sys.exit(1)

    if args.threshold > 0:
        threshold_bytes = args.threshold
    else:
        threshold_bytes = parse_size_str(args.size)

    logger.info(f"仓库路径: {repo_root.absolute()}")
    logger.info(f"Git程序 : {git_exe}")
    if args.mode != "init":
        logger.info(f"文件限制: {threshold_bytes / 1024 / 1024:.2f} MB ({threshold_bytes} 字节)")
    if remote_url:
        logger.info(f"远程地址: {remote_url}")
    logger.info(f"分支    : {args.branch}")

    try:
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

        if args.mode in ("remove-big", "filter-repo"):
            target_hashes = [h.strip() for h in args.hashes.split(",") if h.strip()] if args.hashes else None
            git_remove_big(git_exe, threshold_bytes, target_hashes=target_hashes)
            if remote_url:
                set_remote(git_exe, remote_url)
                logger.info("✅ 远程地址已重新绑定。")
            return

        large_files = scan_large_files(repo_root, threshold_bytes)
        has_large = len(large_files) > 0
        logger.info(f"扫描到 {len(large_files)} 个本地超过阈值的文件")
        
        lfs_needed = has_large
        lfs_available = check_lfs_available(git_exe)
        if lfs_needed and not lfs_available:
            if not install_lfs():
                sys.exit(1)
            if not check_lfs_available(git_exe):
                logger.critical("Git LFS 安装后仍然不可用，请检查环境。")
                sys.exit(1)
            lfs_available = True
            
        if lfs_needed or lfs_available:
            if is_lfs_initialized(repo_root):
                logger.info("Git LFS hooks 已初始化，跳过 git lfs install")
            else:
                if not init_lfs(git_exe):
                    sys.exit(1)
                    
        if lfs_needed:
            clean_and_apply_lfs(git_exe, repo_root, large_files)
        if remote_url:
            set_remote(git_exe, remote_url)

        if args.mode == "pull":
            git_pull(git_exe, args.branch, extra, remote_url)
        elif args.mode == "push":
            git_push(git_exe, args.branch, repo_root, extra, args.commit_msg, remote_url, args.user, args.retry)

        logger.info("✅ 操作结束！")
    except KeyboardInterrupt:
        logger.warning("\n[CANCEL] 用户手动终止程序。")
        sys.exit(130)

if __name__ == "__main__":
    main()