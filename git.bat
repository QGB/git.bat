@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

:: 提前获取脚本名，防止变量污染
set "SCRIPT_NAME=%~nx0"

:: ===================== 基础配置区 =====================
set "GIT_PATH=C:\QGB\PortableGit\bin\git.exe"
set "PY_PATH=D:\test\github\CQ-editor\python.exe"
set "REPO_PATH=775cpu/energetic.git"
set "BRANCH=master"
set "SIZE_THRESHOLD=104857600"
:: ==================================================

cd /d "%~dp0"
set "SCRIPT=%~dp0git_logic.py"

if not exist "%GIT_PATH%" (
    echo [ERROR] Git 不存在: %GIT_PATH%
    goto end
)

:: 参数初始化
set "CMD_MODE="
set "AUTH_PART="

:parse_arg
if "%~1"=="" goto arg_end
if "%~1"=="--auth" (
    set "AUTH_PART=%~2"
    shift
    shift
    goto parse_arg
)
if not defined CMD_MODE (
    set "CMD_MODE=%~1"
)
shift
goto parse_arg
:arg_end

:: 无模式，打印帮助
if "%CMD_MODE%"=="" (
    echo Usage:
    echo   !SCRIPT_NAME! init --auth user:ghp_xxxxxxxx
    echo   !SCRIPT_NAME! push --auth user:ghp_xxxxxxxx
    echo   !SCRIPT_NAME! pull --auth user:ghp_xxxxxxxx
    echo.
    goto end
)

:: 校验是否存在auth参数
if not defined AUTH_PART (
    echo [ERROR] 必须传入 --auth 参数，格式：用户名:ghp_token
    goto end
)

:: 拼接完整远程地址
set "FULL_REMOTE=https://!AUTH_PART!@github.com/!REPO_PATH!"

:: init 模式（纯bat，不走python）
if "%CMD_MODE%"=="init" (
    echo ================================================
    echo   Git Init + Set Remote Origin
    echo ================================================
    "%GIT_PATH%" init
    "%GIT_PATH%" remote remove origin 2>nul
    "%GIT_PATH%" remote add origin "!FULL_REMOTE!"
    echo.
    echo [DONE] 初始化完成
    echo Remote: !FULL_REMOTE!
    goto end
)

:: 校验模式合法性
if not "%CMD_MODE%"=="push" if not "%CMD_MODE%"=="pull" (
    echo [ERROR] 模式仅支持 init / push / pull
    goto end
)

:: push / pull 调用Python脚本
@echo on
"%PY_PATH%" "%SCRIPT%" ^
--git "%GIT_PATH%" ^
--remote "!FULL_REMOTE!" ^
--branch "%BRANCH%" ^
--threshold "%SIZE_THRESHOLD%" ^
--mode "%CMD_MODE%"

:end
@endlocal