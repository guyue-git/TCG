@echo off
rem ============================================================
rem 交易确认书运维台 —— PyInstaller 一键打包（方案 B 三入口 onedir）
rem 前置：项目 .venv 已安装 pyinstaller
rem 产物：packaging\dist\alert\{monitor.exe, service_a.exe, tc_ops_ui.exe, _internal\}
rem ============================================================
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 未找到 .venv\Scripts\python.exe，请先在项目根创建虚拟环境并安装依赖
    exit /b 1
)

.venv\Scripts\python.exe -m PyInstaller packaging\alert.spec ^
    --noconfirm ^
    --distpath packaging\dist ^
    --workpath packaging\build ^
    --specpath packaging
if errorlevel 1 (
    echo [ERROR] 打包失败，请查看上方 PyInstaller 输出
    exit /b 1
)

echo.
echo [OK] 打包完成：packaging\dist\alert\
echo 分发 = 整个 alert 目录（含 _internal）+ 两份 ini 拷贝到同目录（见部署手册）
endlocal
