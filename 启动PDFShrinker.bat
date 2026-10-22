@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title PDF Shrinker - 启动器

echo ========================================
echo    PDF Shrinker - 自动安装器 v5
echo ========================================
echo.

:: ===== 查找 Ghostscript =====
set "GS_DIR="

:: 检查 PATH
where gs >nul 2>&1
if %errorlevel%==0 (
    echo [OK] Ghostscript 已就绪
    goto :launch
)

:: 检查常见目录
for %%d in (
    "C:\Program Files\gs\gs10.70\bin"
    "C:\Program Files\Ghostscript\gs10.70\bin"
    "C:\Program Files\gs\bin"
    "C:\Program Files\Ghostscript\bin"
    "C:\Program Files (x86)\gs\gs10.70\bin"
) do (
    if exist "%%d\gswin64c.exe" set "GS_DIR=%%d"
    if exist "%%d\gswin64.exe" set "GS_DIR=%%d"
    if exist "%%d\gswin32c.exe" set "GS_DIR=%%d"
)

if defined GS_DIR (
    echo [OK] Ghostscript 已安装（!GS_DIR!）
    set "PATH=%PATH%;!GS_DIR!"
    goto :launch
)

echo [检测] 未找到 Ghostscript，开始安装...
echo.

:: ===== 安装 Ghostscript =====
:: 方法1: winget
echo [步骤1/2] winget 安装 Ghostscript...
winget install --id ArtifexSoftware.Ghostscript -e --silent --accept-package-agreements --accept-source-agreements
if %errorlevel%==0 (
    echo [OK] winget 安装完成，正在扫描安装目录...
    goto :find_gs_dirs
)

:: 方法2: 直接下载
echo.
echo [步骤2/2] winget 失败，下载安装包（约45MB）...
echo [提示] 如果下载太慢，可以手动安装，网址见下方

powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10070/gs10070w64.exe' -OutFile '%TEMP%\gs10070w64.exe' -TimeoutSec 300"

if not exist "%TEMP%\gs10070w64.exe" (
    echo.
    echo [错误] 下载失败！
    echo.
    echo 请手动安装：
    echo 打开浏览器访问以下链接，下载后双击安装：
    echo https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/tag/gs10070
    pause
    exit /b 1
)

echo [安装] 正在安装（约30秒）...
"%TEMP%\gs10070w64.exe" /S
del "%TEMP%\gs10070w64.exe" 2>nul

:find_gs_dirs
:: 扫描常见目录找 gswin*.exe
set "FOUND_GS="
for %%d in (
    "C:\Program Files\gs"
    "C:\Program Files\Ghostscript"
) do (
    if exist "%%d" (
        for /f "delims=" %%f in ('dir /s /b "%%d\gswin*.exe" 2^>nul') do (
            if not defined FOUND_GS (
                set "FOUND_GS=%%f"
            )
        )
    )
)

if defined FOUND_GS (
    echo [成功] Ghostscript 安装完成！
    for %%f in ("!FOUND_GS!") do set "GS_BIN=%%~dpath"
    set "PATH=!PATH;!GS_BIN!"
    echo [路径] !GS_BIN!
) else (
    echo.
    echo [警告] 未找到 Ghostscript 安装目录
    echo 重启电脑后 PATH 会自动更新，届时正常运行
    echo.
    echo 如果重启后仍无法使用，请手动安装：
    echo https://ghostscript.com
)

:launch
echo.
echo [启动] 正在打开 PDF Shrinker...
start "" "%~dp0PDFShrinker.exe"
endlocal
exit
