import argparse
import os
import subprocess
import sys
from pathlib import Path

def kill_process_tree(pid: int):
    """
    在 Windows 上递归强制杀死进程及其所有派生子进程树 (如 git-lfs, git-remote-https)
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass

def run_shell(git_bin: str, args: list[str], realtime: bool = False) -> subprocess.CompletedProcess:
    """
    执行 git 命令
    :param realtime: 是否开启实时控制台输出（耗时命令如 push/pull 建议开启）
    """
    cmd = [git_bin] + args
    git_exe_path = Path(git_bin).resolve()
    git_bin_dir = git_exe_path.parent
    git_root = git_bin_dir.parent

    # 环境变量补全
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

    print(f"\n[RUN] {' '.join(cmd)}")

    proc = None
    try:
        if realtime:
            # ===== 实时控制台输出模式 =====
            proc = subprocess.Popen(cmd, env=env)
            retcode = proc.wait()
            return subprocess.CompletedProcess(cmd, retcode)
        else:
            # ===== 静默捕获输出模式 =====
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8"
            )
            stdout, stderr = proc.communicate()
            if stdout and stdout.strip():
                print(f"[STDOUT]\n{stdout}")
            if stderr and stderr.strip():
                print(f"[STDERR]\n{stderr}")
            print(f"[RETURN CODE] {proc.returncode}")
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    except KeyboardInterrupt:
        print("\n\n[CANCEL] ⚠️ 收到 Ctrl+C 中断信号，正在递归清理 Git 进程树...")
        if proc and proc.poll() is None:
            kill_process_tree(proc.pid)  # 强杀整个进程树（包含 git-lfs 等所有子进程）
        print("[CANCEL] 所有相关进程已终止。")
        sys.exit(130)
    except Exception as e:
        print(f"[FATAL] 执行异常: {repr(e)}")
        raise

def init_lfs(git_bin: str) -> bool:
    """初始化Git LFS，增强日志"""
    print("\n[INFO] 执行 git lfs install")
    res = run_shell(git_bin, ["lfs", "install"])
    if res.returncode != 0:
        print("[FATAL] Git LFS 初始化失败！")
        return False
    print("[INFO] Git LFS 就绪")
    return True

def set_remote(git_bin: str, remote_url: str):
    """设置origin远程地址"""
    run_shell(git_bin, ["remote", "set-url", "origin", remote_url])

def scan_large_files(repo_root: Path, threshold: int) -> set[str]:
    """扫描仓库内超过阈值的文件，返回相对路径集合（unix分隔）"""
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
    """读取.gitattributes，保留原有规则并更新大文件LFS追踪"""
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
        rule = f'{pat} filter=lfs diff=lfs merge=lfs -text' #Git attributes 语法不需要引号；引号会被当成文件名一部分，规则永久失效？
        lfs_lines.add(rule)

    all_rules = other_lines + sorted(lfs_lines)
    if all_rules:
        with open(attr_path, "w", encoding="utf-8") as f:
            f.write("\n".join(all_rules) + "\n")
    print(f"\n[INFO] .gitattributes 更新完成，LFS追踪总数: {len(lfs_lines)}")

def git_pull(git_bin: str, branch: str):
    """执行pull + lfs pull（带实时进度与进程树管理）"""
    print("\n===== 执行 git pull origin " + branch + " =====")
    res = run_shell(git_bin, ["pull", "--progress", "origin", branch], realtime=True)
    if res.returncode != 0:
        print("[ERROR] git pull 失败！")
        sys.exit(1)
    print("\n===== 执行 git lfs pull =====")
    run_shell(git_bin, ["lfs", "pull"], realtime=True)

def git_push(git_bin: str, branch: str, repo_root: Path):
    """暂存、提交、推送（带实时进度与进程树管理）"""
    run_shell(git_bin, ["add", "-A"])
    diff_check = run_shell(git_bin, ["diff", "--cached", "--quiet"])
    if diff_check.returncode == 0:
        print("\n[INFO] 无变更，跳过commit，直接推送")
    else:
        run_shell(git_bin, ["commit", "-m", "auto update"])

    print(f"\n===== 推送 origin {branch} =====")

    os.environ["GIT_TRACE"] = "1"
    os.environ["GIT_CURL_VERBOSE"] = "1"
    os.environ["GIT_TRANSFER_TRACE"] = "1"
    push_res = run_shell(git_bin, ["push",'-v', "--progress", "origin", branch], realtime=True)
    if push_res.returncode != 0:
        print("[ERROR] git push 失败！")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--git", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--threshold", type=int, required=True)
    parser.add_argument("--mode", choices=["pull", "push"], required=True)
    args = parser.parse_args()

    repo_root = Path.cwd()
    git_exe = Path(args.git)
    print("================================================")
    print(f"   CQ-editor Git {args.mode.upper()} (Auto LFS)")
    print("================================================")
    print(f"仓库路径: {repo_root.absolute()}")
    print(f"Git程序: {git_exe.absolute()}")
    print(f"文件阈值: {args.threshold / 1024 / 1024:.2f} MB")
    print(f"远程地址: {args.remote}")
    print(f"分支: {args.branch}")

    # 前置校验：git.exe 是否存在
    if not git_exe.is_file():
        print(f"\n[FATAL] 指定Git程序不存在: {args.git}")
        sys.exit(1)

    try:
        # 公共前置步骤
        set_remote(args.git, args.remote)
        if not init_lfs(args.git):
            sys.exit(1)

        # 扫描大文件并更新lfs规则
        large_file_set = scan_large_files(repo_root, args.threshold)
        print(f"\n[INFO] 扫描到 {len(large_file_set)} 个超过阈值的大文件")
        clean_and_apply_lfs(args.git, repo_root, large_file_set)

        # 分支逻辑分发
        if args.mode == "pull":
            git_pull(args.git, args.branch)
        elif args.mode == "push":
            git_push(args.git, args.branch, repo_root)

        print("\n✅ 操作完成！")
    except KeyboardInterrupt:
        print("\n[CANCEL] 用户手动终止程序。")
        sys.exit(130)

if __name__ == "__main__":
    main()