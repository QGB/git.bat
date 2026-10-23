from .shared import Path, logging, os, shutil, subprocess, sys, time, urlparse, urlunparse

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
    no_ask_aliases = {"--noask", "-noask", "--no-ask", "-y", "-yes"}
    raw = sys.argv[1:]
    url_indices = {i for i, arg in enumerate(raw) if looks_like_url(arg)}
    new, need_auto_user, missing_value, i = [], False, False, 0
    while i < len(raw):
        arg = raw[i]
        if arg == "--remote":
            new.append(arg)
            if i + 1 < len(raw):
                new.append(raw[i + 1])
                i += 2
            else:
                i += 1
            continue
        if arg in ("-m", "--commit-msg", "--commit_msg"):
            msg_parts = raw[i + 1:]
            if msg_parts:
                new.append("--commit-msg")
                new.append(" ".join(msg_parts))
            else:
                new.append(arg)
                missing_value = True
            i = len(raw)
            continue
        if arg in ("-u", "--user"):
            if i + 1 < len(raw):
                nxt = raw[i + 1]
                if (i + 1) in url_indices:
                    new.extend(["--user", "--remote", nxt])
                    i += 2
                    continue
                elif nxt in no_ask_aliases:
                    new.extend(["--user", "--no-ask"])
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
    if not missing_value and not any(a in valid_modes for a in new):
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


def get_origin_url(git_bin: str, repo_root: Path = None) -> str:
    try:
        res = subprocess.run([git_bin, "remote", "get-url", "origin"], cwd=repo_root,
                             capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except:
        pass
    return ""


def get_current_branch(git_bin: str, repo_root: Path = None) -> str:
    """Return the current branch, including an unborn branch after git init."""
    try:
        result = subprocess.run(
            [git_bin, "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return ""


def is_git_repository(git_bin: str, repo_root: Path) -> bool:
    result = subprocess.run(
        [git_bin, "rev-parse", "--git-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def get_branch_tracking_url(git_bin: str, branch: str, repo_root: Path = None) -> str:
    try:
        remote_name = subprocess.run([git_bin, "config", "--get", f"branch.{branch}.remote"],
                                     cwd=repo_root, capture_output=True, text=True).stdout.strip()
        if not remote_name:
            return ""
        url_res = subprocess.run([git_bin, "remote", "get-url", remote_name], cwd=repo_root,
                     capture_output=True, text=True)
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
