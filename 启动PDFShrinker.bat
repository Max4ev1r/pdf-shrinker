@echo off
chcp 65001 >nul
title PDF Shrinker - 启动器

echo ========================================
echo    PDF Shrinker - 自动安装器
echo ========================================
echo.

:: ===== Ghostscript 检测 =====
set "GS_READY="
where gs >nul 2>&1
if %errorlevel%==0 set "GS_READY=1"

:: 检查常见安装目录
if not defined GS_READY (
    if exist "C:\Program Files\gs\gs10.70\bin\gswin64c.exe" set "GS_READY=1"
    if exist "C:\Program Files\gs\gs10.70\bin\gswin64.exe" set "GS_READY=1"
    if exist "C:\Program Files (x86)\gs\gs10.70\bin\gswin32c.exe" set "GS_READY=1"
    if exist "C:\Program Files\Ghostscript\gs10.70\bin\gswin64c.exe" set "GS_READY=1"
    if exist "C:\Program Files\Ghostscript\gs10.70\bin\gswin64.exe" set "GS_READY=1"
)

if defined GS_READY (
    echo [OK] Ghostscript 已安装
    goto :launch
)

echo [检测] 未找到 Ghostscript，开始安装...
echo.

:: ===== 方法1: winget =====
echo [步骤1] 尝试 winget 安装...
winget install --id ArtifexSoftware.Ghostscript -e --silent --accept-package-agreements --accept-source-agreements
if %errorlevel%==0 (
    echo [OK] winget 安装完成，等待 PATH 更新...
    timeout /t 8 /nobreak >nul
    :: 刷新 PATH 后再检测
    where gs >nul 2>&1
    if %errorlevel%==0 (
        echo [验证] Ghostscript 就绪
        goto :launch
    )
    echo [提示] winget 安装完成但检测未刷新，将检查安装目录...
    if exist "C:\Program Files\gs\gs10.70\bin\gswin64c.exe" goto :launch
)

:: ===== 方法2: 直接下载 =====
echo.
echo [步骤2] 尝试直接下载安装包（约45MB）...
echo [提示] 如果下载太慢，可以手动安装：https://ghostscript.com

powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10070/gs10070w64.exe' -OutFile '%TEMP%\gs_install.exe'" 2>&1

if not exist "%TEMP%\gs_install.exe" (
    echo.
    echo [错误] 下载失败！可能原因：
    echo   1. 网络连接问题
    echo   2. 防火墙拦截
    echo.
    echo   手动下载安装：
    echo   打开浏览器访问以下链接，下载后双击安装：
    echo   https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10070/gs10070w64.exe
    echo.
    echo 按回车键启动程序（压缩功能可能受限）...
    pause >nul
    goto :launch
)

echo [下载完成] 正在安装（请稍候，约30秒）...
"%TEMP%\gs_install.exe" /S
del "%TEMP%\gs_install.exe" 2>nul

timeout /t 5 /nobreak >nul

:: 验证
where gs >nul 2>&1
if %errorlevel%==0 (
    echo [验证] Ghostscript 安装成功！
    goto :launch
)

if exist "C:\Program Files\gs\gs10.70\bin\gswin64c.exe" (
    echo [验证] Ghostscript 安装成功（安装目录）！
    goto :launch
)

if exist "C:\Program Files\Ghostscript\gs10.70\bin\gswin64c.exe" (
    echo [验证] Ghostscript 安装成功（Ghostscript目录）！
    goto :launch
)

echo.
echo [警告] 未能自动验证安装，但仍尝试启动程序...
echo   如果启动后提示 Ghostscript 未找到，请重启电脑后重试。
pause >nul

:launch
echo.
echo [启动] 正在打开 PDF Shrinker...
start "" "%~dp0PDFShrinker.exe"
exit
