
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

## 模块化 Python 工具

`git/` 按核心工具、LFS、网络重试、身份配置、提交推送、历史大文件和命令行入口拆分实现。运行模块化版本：

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