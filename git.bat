@echo on
chcp 65001 >nul 2>&1
cd /d "%~dp0"

:: ===== 基础配置（可按需修改） =====
set "PY_PATH=D:\test\github\CQ-editor\python.exe"
set "GIT_PATH=C:\QGB\PortableGit\bin\git.exe"
set "REPO_PATH=775cpu/energetic.git"
set "BRANCH=master"
set "SIZE_THRESHOLD=104857600"
:: ==================================

"%PY_PATH%" "%~dp0git_logic.py" %*
