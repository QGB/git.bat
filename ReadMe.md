
#todo ？ 2026年8月3日 05点57分

快速运行本地测试：

```bash
./test.sh

或者
python3 -m unittest discover -s tests -v
```

✅ stderr 分级日志：正常返回码 stderr 输出 DEBUG，仅非 0 返回码 stderr 标记 ERROR
附带优化：realtime=True 模式增加进程组捕获，中断时完整清理子进程 


默认 git push 不显示连接详情   【除非一开始 verbose debug】
但是如果 push第一次 失败，retry时候 自动调高输出级别 显示详情

commit_msg auto模式下 加入  最大size 修改的文件名。

使用 `git.bat clone <远程仓库地址> [目标目录]` 克隆仓库。工具会自动检查并安装 Git LFS，克隆完成后执行 `git lfs pull`，恢复仓库中的 LFS 大文件。

`--max-commit-size` 只根据本次暂存文件的 Blob 大小拆分新提交，不会拆分本地已有但远端缺失的历史。首次推送或推送大量旧历史时，Git 仍可能生成超过 GitHub 单次 2 GiB 限制的 pack；这类历史需要按提交批次推送。遇到 `RPC failed; HTTP 500` 或 `pack exceeds maximum allowed size` 时，工具会停止重试，避免重复上传同一个超限 pack。

## 模块化 Python 工具

`git/` 按核心工具、LFS、网络重试、身份配置、提交推送、历史大文件和命令行入口拆分实现。运行模块化版本：

`--retry` 默认是 `auto`：通过 HTTPS 查询公网 IP 地区，中国地区默认重试 99 次，其他地区 3 次；检测服务不可用时警告并使用 3 次。显式传入正整数时不进行地区查询。

```bash
python3 -m git --help
```

合并为独立的单文件脚本（默认生成 `git_logic.generated.py`，不会覆盖现有的 `git_logic.py`）：

```bash
./generate-single-file
```

可以指定输出位置；如需覆盖已有文件，必须显式添加 `--force`。生成后可用 `--check` 校验文件与当前模块是否一致：

```bash
./generate-single-file --output build/git_logic.py
./generate-single-file --output build/git_logic.py --check
```