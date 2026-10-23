from .shared import subprocess, sys
from .core import logger, run_shell
from .lfs import install_git_filter_repo

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
