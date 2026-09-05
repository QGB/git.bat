@echo on
chcp 65001 >nul 2>&1
cd /d "%~dp0"

:: ===== 基础配置（可按需修改） =====
set "PY_PATH=C:\QGB\miniforge3\python.exe"
set "GIT_PATH=C:\QGB\PortableGit\bin\git.exe"
rem Leave BRANCH unset so git_logic.py follows the repository's current branch.
set "SIZE_THRESHOLD=104857600"
:: ==================================

"%PY_PATH%" "%~dp0git_logic.py" %*
