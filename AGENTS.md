# AGENTS.md

本仓库是 `git.bat` 的 Python 版 git 包装工具：`git.bat` → 调用 `git_logic.py`。

## 最重要：git_logic.py 是生成文件，不要直接改
`git_logic.py` 由 `git/` 下的模块按顺序拼接生成：
`shared.py → core.py → lfs.py → network.py → identity.py → commit.py → history.py → cli.py`

正确改法：
1. 只改 `git/*.py` 源码（例如网络/拉取逻辑在 `git/network.py`，LFS 在 `git/lfs.py`）；
2. 重新生成单文件：
   ```bash
   ./generate-single-file --force          # 或 python -m git.generate_single_file --force
   ./generate-single-file --check          # 校验 git_logic.py 与源码一致
   ```
3. **直接编辑 `git_logic.py` 会在下次生成时被覆盖。**

## 验证
```bash
./test.sh                                  # unittest discover -s tests + py_compile
python -m unittest discover -s git/tests   # 另一处测试目录
```
- 测试分两处：`tests/` 和 `git/tests/`，**改测试要两边同步**。
- 测试依赖真正的 `git`：找不到时相关用例自动 skip；可用 `GIT` 环境变量指定 git 路径（Windows 上显式查找 `git.exe`，避免误选本仓库的 `git.bat` 包装脚本）。
- 符号链接用例在无权限平台（如未开开发者模式的 Windows）会自动 skip。

## 约定
- `git pull` 成功后执行 `git lfs pull`：LFS 缺失/失败只记 `error` 日志，**不中断**已成功的 pull。
- 参数 `--git` 可指定 git 可执行文件；`--size/-s` 为大文件阈值（默认 100mb）。