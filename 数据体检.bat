@echo off
chcp 65001 >nul
REM 数据新鲜度/覆盖面体检（只读，不改任何数据；只走索引，耗时约数秒，盘中盘后任意时刻都可运行）
REM 最有意义的时机：每日盘后同步脚本（日线、财报、股东等）跑完之后；结果会被「市场状态」页读取
cd /d "%~dp0"

REM 使用 PATH 中的 python；若本机多个版本，可改为完整路径
set PYTHON_EXE=python

echo [数据体检] 开始体检，请稍候（约数秒）...
echo.
"%PYTHON_EXE%" -X utf8 "%~dp0data_health_check.py"
set EXITCODE=%ERRORLEVEL%
echo.
if %EXITCODE%==0 (
    echo [数据体检] 完成：没有 FAIL。WARN 只是提醒，可看上面的说明。
) else if %EXITCODE%==130 (
    echo [数据体检] 已被中断。
) else (
    echo [数据体检] 发现 FAIL 或出错，退出码 %EXITCODE%，请看上面的说明。
)
echo 日志文件：%~dp0logs\data_health_check.log
pause
exit /b %EXITCODE%
