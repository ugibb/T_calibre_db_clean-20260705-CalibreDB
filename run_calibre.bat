@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

REM Calibre 增量预处理流水线执行脚本 (Windows)
REM 详见 06-src/knowledge_assets/README.md

REM ── 基础路径 ──

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not defined PYTHON set "PYTHON=python"
set "CLI=06-src/knowledge_assets/preprocess/cli.py"
set "CONFIG=06-src/template/pipeline_config.json"
set "HELPER=%SCRIPT_DIR%_run_helper.py"

if not exist "%CLI%" (
    echo 错误： 找不到 CLI： %CLI%
    exit /b 1
)
if not exist "%HELPER%" (
    echo 错误： 找不到辅助脚本： %HELPER%
    exit /b 1
)

REM ── 读取配置 ──

for /f "delims=" %%i in ('%PYTHON% %HELPER% _cfg defaults input_dir') do set "INPUT_DIR=%%i"
for /f "delims=" %%i in ('%PYTHON% %HELPER% _cfg defaults output_root') do set "OUTPUT_ROOT=%%i"
for /f "delims=" %%i in ('%PYTHON% %HELPER% _cfg defaults full_db_dir') do set "FULL_DB_DIR=%%i"

REM 检测批次
set "BATCH="
for /f "delims=" %%i in ('%PYTHON% %HELPER% _detect_batch "!INPUT_DIR!"') do set "BATCH=%%i"
if "!BATCH!"=="" (
    for /f "delims=" %%i in ('%PYTHON% %HELPER% _cfg defaults batch') do set "BATCH=%%i"
)

REM ── 命令分发 ──

if "%~1"=="" goto :cmd_auto
if "%~1"=="status"  goto :cmd_status
if "%~1"=="step1"   goto :cmd_step1
if "%~1"=="1"       goto :cmd_step1
if "%~1"=="step2"   goto :cmd_step2
if "%~1"=="2"       goto :cmd_step2_full
if "%~1"=="step3"   goto :cmd_step3
if "%~1"=="3"       goto :cmd_step3
if "%~1"=="step4"   goto :cmd_step4
if "%~1"=="4"       goto :cmd_step4
if "%~1"=="step5"   goto :cmd_step5
if "%~1"=="5"       goto :cmd_step5_full
if "%~1"=="auto"    goto :cmd_auto
if "%~1"=="help"    goto :usage
if "%~1"=="-h"      goto :usage
if "%~1"=="--help"  goto :usage

echo 未知命令： %~1（可用 help 查看用法）
exit /b 1

REM ── 辅助子程序 ──

:_run
set "_STEP=%~1"
set "_PHASE=%~2"
set "_ARGS="
shift
shift
:build_args
if "%~1"=="" goto :run_cli
set "_ARGS=!_ARGS! "%~1""
shift
goto :build_args
:run_cli
"%PYTHON%" "%HELPER%" _run "!_STEP!" "!_PHASE!" "!BATCH!" !_ARGS!
exit /b %errorlevel%

:_gate_notice
echo.
echo ────────────────────────────────────────────────────────────────
echo 人工闸口：
echo   闸口 1：确认 step2/ 异常处理 CSV
echo   闸口 2：确认 step3/ 回归验证 CSV
echo   闸口 3：填写 step5/ 待人工确认 CSV 的 human_decision 列
echo 详见 06-src/knowledge_assets/README.md
echo ────────────────────────────────────────────────────────────────
exit /b 0

:_has_help_flag
set "_HAS_HELP=0"
:has_help_loop
if "%~1"=="" exit /b 0
if "%~1"=="-h" set "_HAS_HELP=1"& exit /b 0
if "%~1"=="--help" set "_HAS_HELP=1"& exit /b 0
shift /1
goto :has_help_loop

REM ── 命令实现 ──

:cmd_status
set "OUT_DIR=!OUTPUT_ROOT!\!BATCH!"
set "BATCH_COMPACT=!BATCH:-=!"

set "INPUT_DB="
for /f "delims=" %%i in ('%PYTHON% %HELPER% _latest_db "!INPUT_DIR!" "metadata-*.db"') do set "INPUT_DB=%%i"

set "LATEST_FULL="
for /f "delims=" %%i in ('%PYTHON% %HELPER% _latest_db "!FULL_DB_DIR!" "metadata-full-*.db"') do set "LATEST_FULL=%%i"

echo 批次          !BATCH!
if defined INPUT_DB (
    echo 输入增量库    !INPUT_DB!
) else (
    echo 输入增量库    无
)
echo 输出目录      !OUT_DIR!
if defined LATEST_FULL (
    echo 全量库基准    !LATEST_FULL!
) else (
    echo 全量库基准    无
)

echo.
echo 进度：

if exist "!OUT_DIR!\step1\metadata-!BATCH_COMPACT!.deduped.db" (
    echo   [OK] Step1
) else (
    echo   [  ] Step1
)

if exist "!OUT_DIR!\step2\metadata-!BATCH_COMPACT!.encoded.db" (
    echo   [OK] Step2 anomaly
) else (
    echo   [  ] Step2 anomaly
)

if exist "!OUT_DIR!\step3\step3_scan_snapshot.json" (
    echo   [OK] Step3 regression
) else (
    echo   [  ] Step3 regression
)

if exist "!OUT_DIR!\step4\step4_cleaning_instructions.sql" (
    echo   [OK] Step4 sql
) else (
    echo   [  ] Step4 sql
)

if exist "!OUT_DIR!\step3\metadata.cleaned.db" (
    echo   [OK] Step4 apply
) else (
    echo   [  ] Step4 apply
)

if exist "!OUT_DIR!\step5\5-1-1：系统确认-全量库不存在可新增.csv" (
    echo   [OK] Step5 scan
) else (
    echo   [  ] Step5 scan
)

if exist "!OUT_DIR!\step5\step5_merge_instructions.sql" (
    echo   [OK] Step5 plan
) else (
    echo   [  ] Step5 plan
)

echo.
if not exist "!OUT_DIR!\step1\metadata-!BATCH_COMPACT!.deduped.db" (
    echo 下一步： run_calibre.bat 1
) else if not exist "!OUT_DIR!\step2\metadata-!BATCH_COMPACT!.encoded.db" (
    echo 下一步： run_calibre.bat step2
) else if not exist "!OUT_DIR!\step3\step3_scan_snapshot.json" (
    echo 下一步： run_calibre.bat 3
) else if not exist "!OUT_DIR!\step4\step4_cleaning_instructions.sql" (
    echo 下一步： run_calibre.bat step4 sql
) else if not exist "!OUT_DIR!\step3\metadata.cleaned.db" (
    echo 下一步： run_calibre.bat step4 apply
) else if not exist "!OUT_DIR!\step5\step5_merge_instructions.sql" (
    echo 下一步： run_calibre.bat step5 plan
) else (
    echo 下一步： run_calibre.bat step5 apply -y
)
exit /b 0

:cmd_step1
call :_build_extra_skip1 %*
call :_run step1 "" !_EXTRA!
exit /b %errorlevel%

:_build_extra_skip1
REM 跳过第一个参数（命令名），构建剩余参数
set "_EXTRA="
if "%~1"=="" exit /b 0
shift /1
goto :_build_extra_continue

:_build_extra
set "_EXTRA="
if "%~1"=="" exit /b 0
:_build_extra_continue
if "%~1"=="" exit /b 0
set "_EXTRA=!_EXTRA! "%~1""
shift /1
goto :_build_extra_continue

:cmd_step2
call :_parse_step2 %*
if "%_STEP2_PHASE%"=="" goto :step2_run
if "%_STEP2_PHASE:~0,1%"=="-" goto :step2_run
if "%_STEP2_PHASE%"=="scan" (
    call :_run step2 scan !_EXTRA!
    exit /b %errorlevel%
)
if "%_STEP2_PHASE%"=="apply" (
    call :_run step2 apply !_EXTRA!
    exit /b %errorlevel%
)
echo 错误： step2 需要子阶段： scan / apply
exit /b 1
:step2_run
call :_run step2 "" !_EXTRA!
exit /b %errorlevel%

:_parse_step2
REM 第一个参数是命令名，第二个是 phase，其余是额外参数
set "_STEP2_PHASE="
set "_EXTRA="
if "%~1"=="" exit /b 0
shift /1
if "%~1"=="" exit /b 0
set "_STEP2_PHASE=%~1"
shift /1
goto :_build_extra_continue

:cmd_step3
call :_build_extra_skip1 %*
call :_run step3 "" !_EXTRA!
exit /b %errorlevel%

:cmd_step4
call :_parse_step4 %*
if "%_STEP4_PHASE%"=="" goto :step4_default
if "%_STEP4_PHASE:~0,1%"=="-" goto :step4_default
if "%_STEP4_PHASE%"=="sql" (
    call :_run step4 sql !_EXTRA!
    exit /b %errorlevel%
)
if "%_STEP4_PHASE%"=="apply" (
    call :_run step4 apply !_EXTRA!
    exit /b %errorlevel%
)
echo 错误： step4 需要子阶段： sql / apply
exit /b 1
:step4_default
call :_run step4 sql !_EXTRA!
if errorlevel 1 exit /b 1
call :_run step4 apply !_EXTRA!
exit /b %errorlevel%

:_parse_step4
set "_STEP4_PHASE="
set "_EXTRA="
if "%~1"=="" exit /b 0
shift /1
if "%~1"=="" exit /b 0
set "_STEP4_PHASE=%~1"
shift /1
goto :_build_extra_continue

:cmd_step5
call :_parse_step5 %*
if "%_STEP5_PHASE%"=="" goto :step5_scan
if "%_STEP5_PHASE:~0,1%"=="-" goto :step5_scan
if "%_STEP5_PHASE%"=="scan" goto :step5_scan
if "%_STEP5_PHASE%"=="validate" (
    call :_run step5 validate !_EXTRA!
    exit /b %errorlevel%
)
if "%_STEP5_PHASE%"=="plan" (
    call :_run step5 plan !_EXTRA!
    exit /b %errorlevel%
)
if "%_STEP5_PHASE%"=="apply" (
    echo.
    echo [!] Step5 apply 会直接修改全量预处理库
    echo     全量库目录： !FULL_DB_DIR!
    echo.
    set /p "_ANSWER=确认继续请输入 yes 并回车： "
    if not "!_ANSWER!"=="yes" (
        echo 已取消。
        exit /b 1
    )
    call :_run step5 apply !_EXTRA!
    exit /b %errorlevel%
)
echo 错误： step5 需要子阶段： scan / validate / plan / apply
exit /b 1
:step5_scan
call :_run step5 scan !_EXTRA!
exit /b %errorlevel%

:_parse_step5
set "_STEP5_PHASE="
set "_EXTRA="
if "%~1"=="" exit /b 0
shift /1
if "%~1"=="" exit /b 0
set "_STEP5_PHASE=%~1"
shift /1
goto :_build_extra_continue

:cmd_step2_full
call :_build_extra_skip1 %*
call :_has_help_flag !_EXTRA!
if "%_HAS_HELP%"=="1" (
    call :_run step2 scan !_EXTRA!
    exit /b %errorlevel%
)
echo ---- Step2 完整流程：scan -^> 确认 -^> apply ----
call :_run step2 scan !_EXTRA!
if errorlevel 1 exit /b 1
call :_gate_notice
echo.
set /p "_S2_ANSWER=请确认 step2/ CSV 后输入 yes 执行修复： "
if not "%_S2_ANSWER%"=="yes" (
    echo 已取消。
    exit /b 1
)
call :_run step2 apply !_EXTRA!
exit /b %errorlevel%

:cmd_step5_full
call :_build_extra_skip1 %*
call :_has_help_flag !_EXTRA!
if "%_HAS_HELP%"=="1" (
    call :_run step5 scan !_EXTRA!
    exit /b %errorlevel%
)
echo ---- Step5 完整流程：scan -^> validate -^> plan -^> 确认 -^> apply ----
set "BATCH_COMPACT=%BATCH:-=%"
if not exist "%OUTPUT_ROOT%\%BATCH%\step3\metadata.cleaned.db" (
    echo 错误： 缺少 metadata.cleaned.db，先执行： run_calibre.bat step4 apply
    exit /b 1
)

call :_run step5 scan !_EXTRA!
if errorlevel 1 exit /b 1

set "STEP5_DIR=%OUTPUT_ROOT%\%BATCH%\step5"
set "_CSV_COUNT=0"
if exist "!STEP5_DIR!" (
    for %%f in ("!STEP5_DIR!\*.csv") do set /a "_CSV_COUNT+=1"
)

if "!_CSV_COUNT!"=="0" (
    echo.
    echo ---- Step5 scan 完成（无比对目标全量库） ----
    echo 未找到历史全量库，所有记录将在 apply 时直接初始化全量库。
    echo.
    echo [!] Step5 apply 会直接修改全量预处理库。
    set /p "_S5_ANSWER=确认继续请输入 yes 并回车： "
    if not "!_S5_ANSWER!"=="yes" (
        echo 已取消。
        exit /b 1
    )
    call :_run step5 apply
    exit /b %errorlevel%
)

call :_run step5 validate
if errorlevel 1 exit /b 1
call :_run step5 plan
if errorlevel 1 exit /b 1
call :_gate_notice

echo.
echo [!] Step5 apply 会直接修改全量预处理库。
set /p "_S5_ANSWER2=确认继续请输入 yes 并回车： "
if not "!_S5_ANSWER2!"=="yes" (
    echo 已取消。
    exit /b 1
)
call :_run step5 apply
exit /b %errorlevel%

:cmd_auto
echo 批次 %BATCH%：执行 Step1 + Step2 scan + Step3
call :_run step1 ""
if errorlevel 1 exit /b 1
call :_run step2 scan
if errorlevel 1 exit /b 1
call :_run step3 ""
if errorlevel 1 exit /b 1
call :_gate_notice
exit /b 0

:usage
echo.
echo Calibre 增量预处理流水线执行脚本 (Windows)
echo.
echo   run_calibre.bat                           auto 连跑非破坏性步骤
echo   run_calibre.bat status                    查看当前批次进度
echo   run_calibre.bat 1                         Step1 前置预处理
echo   run_calibre.bat step2 [scan^|apply]       Step2 异常处理
echo   run_calibre.bat 3                         Step3 回归验证
echo   run_calibre.bat step4 [sql^|apply]        Step4 自清洗
echo   run_calibre.bat 5                         Step5 比对合并完整流程
echo   run_calibre.bat step5 [scan^|validate^|plan^|apply]
echo   run_calibre.bat auto                      Step1+Step2 scan+Step3
echo.
exit /b 0
