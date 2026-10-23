from .shared import Path, subprocess, sys, urlparse
from .core import logger, run_shell

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


def parse_user_identity_input(value: str, current_name: str, current_email: str,
                              default_name: str, default_email: str) -> tuple[str, str]:
    """Parse one interactive name/email answer, including numeric defaults."""
    answer = value.strip()
    if answer in ("", "1"):
        return current_name, current_email
    if answer == "2":
        return default_name, default_email
    normalized = answer.replace("，", ",").replace("、", ",")
    if "," in normalized:
        name, email = (part.strip() for part in normalized.split(",", 1))
    else:
        parts = normalized.split()
        if len(parts) < 2:
            name = answer
            return name, f"{name}@users.noreply.github.com"
        name, email = " ".join(parts[:-1]), parts[-1]
    return name or current_name or default_name, email or current_email or default_email


def apply_git_user_config(git_bin: str, remote_url: str, user_arg: str,
                          no_ask: bool = False, repo_root: Path = None):
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
        run_shell(git_bin, ["config", "user.name", target_user], cwd=repo_root)
        run_shell(git_bin, ["config", "user.email", target_email], cwd=repo_root)
        return
    if not remote_user:
        return
    if no_ask:
        logger.info("非交互模式：保留当前 Git 用户配置。")
        return
    local_name = subprocess.run([git_bin, "config", "user.name"], cwd=repo_root,
                                capture_output=True, text=True).stdout.strip()
    local_email = subprocess.run([git_bin, "config", "user.email"], cwd=repo_root,
                                 capture_output=True, text=True).stdout.strip()
    default_email = f"{remote_user}@users.noreply.github.com"
    if local_name != remote_user or local_email != default_email:
        logger.warning("发现当前 Git 用户配置与远程目标不一致！")
        print(f"\n请输入本次 Commit 的用户名和邮箱：\n"
              f"  [1] 保持当前 ({local_name}, {local_email})\n"
              f"  [2] 使用默认 ({remote_user}, {default_email})\n"
              "  也可直接输入：name mail@example.com 或 name，mail@example.com")
        try:
            identity = input("用户名和邮箱 (默认 1): ")
        except KeyboardInterrupt:
            sys.exit(130)
        target_name, target_email = parse_user_identity_input(
            identity, local_name, local_email, remote_user, default_email
        )
        run_shell(git_bin, ["config", "user.name", target_name], cwd=repo_root)
        run_shell(git_bin, ["config", "user.email", target_email], cwd=repo_root)
        logger.info(f"✅ 应用本次 Commit 配置: user.name={target_name}, user.email={target_email}")
