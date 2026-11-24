@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo        越南礼包码自动化配置工作台 - 一键推送到 GitHub
echo ========================================================
echo.
set /p REPO_URL="请输入你的 GitHub 仓库地址 (例如 https://github.com/yourname/giftcode.git): "
if "%REPO_URL%"=="" (
    echo [错误] 仓库地址不能为空！
    pause
    exit /b
)

git remote remove origin >nul 2>&1
git remote add origin %REPO_URL%
git branch -M main
echo.
echo 正在推送到 GitHub...
git push -u origin main
if %ERRORLEVEL% equ 0 (
    echo.
    echo ========================================================
    echo [成功] 代码已成功推送到 GitHub！
    echo 接下来你可以在 Render (https://render.com) 或 Zeabur 
    echo 关联该仓库一键部署，生成免登录、永久可用的外网访问链接！
    echo ========================================================
) else (
    echo.
    echo [提示] 推送未成功，请检查仓库权限或网络连接。
)
pause
