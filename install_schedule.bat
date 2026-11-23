@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ---- Settings ----
set "REPO=BarnardF/slack-extractor"
set "TASK_NAME=SlackCacheSync"
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
set "VENV=%DIR%\.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "LOG=%DIR%\activity.log"
set "VERSION_FILE=%DIR%\VERSION"

>> "%LOG%" echo Slack-to-Sheet run log - %DATE% %TIME% (Mode: %1)

rem =======================================================================
rem SILENT BACKGROUND RUN (Triggered by Windows Task Scheduler)
rem =======================================================================
if /i "%~1"=="--silent" goto :run_silent

rem =======================================================================
rem INTERACTIVE SETUP / MANUAL RUN (When double-clicked by client)
rem =======================================================================

rem ---- 0. Check for updates from GitHub Releases ----
call :check_updates

rem ---- 1. Check project files ----
call :step "[1/5] Checking project files"
if not exist "%DIR%\config.json" set "ERR=config.json must be in the same folder as this installer." & goto :fail
if not exist "%DIR%\main.py" set "ERR=main.py must be in the same folder as this installer." & goto :fail
if not exist "%DIR%\slack_cache.py" set "ERR=slack_cache.py must be in the same folder as this installer." & goto :fail
if not exist "%DIR%\requirements.txt" set "ERR=requirements.txt must be in the same folder as this installer." & goto :fail

findstr /C:"PASTE_" "%DIR%\config.json" >nul 2>&1
if not errorlevel 1 set "ERR=Please set valid WEB_APP_URL and SECRET in config.json." & goto :fail

rem ---- 2. Find (or install) Python ----
call :step "[2/5] Looking for compatible Python"
call :find_python
if not defined PY (
    echo     No compatible Python found. Trying automatic installation...
    call :install_python
    if not defined PY set "ERR=Python may have just been installed, but this window can't see it yet. Close this window and run again." & goto :fail
)
if not defined PY set "ERR=Could not find or install 64-bit Python 3.11-3.14." & goto :fail
echo     Using: "%PY%"
>> "%LOG%" echo Using Python: "%PY%"

rem ---- 3. Create .venv and install packages ----
call :step "[3/5] Setting up .venv and installing packages (this may take a few minutes)"
if not exist "%VENV_PY%" "%PY%" -m venv "%VENV%" >> "%LOG%" 2>&1
if not exist "%VENV_PY%" set "ERR=Could not create .venv. Check folder permissions." & goto :fail

"%VENV_PY%" -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (3,15) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if errorlevel 1 set "ERR=Existing .venv does not use 64-bit Python 3.11-3.14. Delete the .venv folder and run this installer again." & goto :fail

rem Install packages if not already done
if not exist "%VENV%\.installed" (
    "%VENV_PY%" -m pip install --disable-pip-version-check setuptools==80.9.0 wheel==0.45.1 >> "%LOG%" 2>&1
    if errorlevel 1 set "ERR=Package installation failed. Check your internet access." & goto :fail
    "%VENV_PY%" -m pip install --disable-pip-version-check --no-deps --no-build-isolation -r "%DIR%\requirements.txt" >> "%LOG%" 2>&1
    if errorlevel 1 set "ERR=Package installation failed. Check your internet access." & goto :fail
    echo installed > "%VENV%\.installed"
)

rem ---- 4. Run First Sync ----
call :step "[4/5] Running first sync"
"%VENV_PY%" "%DIR%\main.py"
if errorlevel 1 set "ERR=Slack sync execution failed. Check activity.log for details." & goto :fail

rem ---- 5. Schedule the sync ----
call :step "[5/5] Scheduling 12:30 / 17:30"
set "SCHEDULED_EXE=%DIR%\install_schedule.bat"
set "SLACKSYNC_DIR=%DIR%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $action=New-ScheduledTaskAction -Execute $env:SCHEDULED_EXE -Argument '--silent' -WorkingDirectory $env:SLACKSYNC_DIR; $triggers=@((New-ScheduledTaskTrigger -Daily -At 12:30),(New-ScheduledTaskTrigger -Daily -At 17:30)); $settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries; Register-ScheduledTask -TaskName '%TASK_NAME%' -Action $action -Trigger $triggers -Settings $settings -Force | Out-Null } catch { Write-Error $_; exit 1 }" >> "%LOG%" 2>&1
if errorlevel 1 set "ERR=Could not create the scheduled task. Check Task Scheduler permissions." & goto :fail

echo.
echo ============================================================
echo Setup complete. The sync is scheduled twice a day (12:30 / 17:30).
echo Activity log: activity.log
echo To stop automatic runs, double-click stop_schedule.bat.
echo ============================================================
>> "%LOG%" echo Setup completed successfully.
pause
exit /b 0


rem =======================================================================
rem SILENT ROUTINE FOR TASK SCHEDULER
rem =======================================================================
:run_silent
rem 1. Check & apply updates silently
call :check_updates_silent

rem 2. Check if packages need update after a release
if not exist "%VENV%\.installed" (
    if exist "%DIR%\requirements.txt" (
        "%VENV_PY%" -m pip install --disable-pip-version-check --no-deps --no-build-isolation -r "%DIR%\requirements.txt" >> "%LOG%" 2>&1
        echo installed > "%VENV%\.installed"
    )
)

rem 3. Run sync silently via pythonw
set "PYW=%VENV%\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%VENV_PY%"
if exist "%DIR%\main.py" (
    "%PYW%" "%DIR%\main.py" >> "%LOG%" 2>&1
)
exit /b 0


rem ======== Helper routines ========

:step
echo.
echo %~1
>> "%LOG%" echo %~1
exit /b 0

:fail
echo.
echo ERROR: %ERR%
echo See activity.log for details.
>> "%LOG%" echo ERROR: %ERR%
echo.
pause
exit /b 1

:check_updates
echo [0/5] Checking for updates...
set "LOCAL_VER=none"
if exist "%VERSION_FILE%" set /p LOCAL_VER=<"%VERSION_FILE%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$repo = '%REPO%'; $local = '%LOCAL_VER%'; $dir = '%DIR%';" ^
    "try {" ^
    "  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;" ^
    "  $api = 'https://api.github.com/repos/' + $repo + '/releases/latest';" ^
    "  $rel = Invoke-RestMethod -Uri $api -Headers @{'User-Agent'='SlackExtractorUpdater'} -TimeoutSec 15;" ^
    "  $remote = $rel.tag_name;" ^
    "  if ($remote -and $remote -ne $local) {" ^
    "    Write-Host ('    New version found: ' + $remote + ' (Current: ' + $local + ')');" ^
    "    Write-Host '    Downloading update...';" ^
    "    $reqsChanged = $false;" ^
    "    foreach ($asset in $rel.assets) {" ^
    "      $name = $asset.name;" ^
    "      if ($name -in @('main.py', 'slack_cache.py', 'requirements.txt', 'stop_schedule.bat', 'install_schedule.bat')) {" ^
    "        $dest = Join-Path $dir $name;" ^
    "        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $dest;" ^
    "        if ($name -eq 'requirements.txt') { $reqsChanged = $true }" ^
    "      }" ^
    "    }" ^
    "    Set-Content -Path (Join-Path $dir 'VERSION') -Value $remote;" ^
    "    if ($reqsChanged -and (Test-Path (Join-Path $dir '.venv\.installed'))) { Remove-Item (Join-Path $dir '.venv\.installed') -Force };" ^
    "    Write-Host '    Update applied successfully!';" ^
    "  } else {" ^
    "    Write-Host ('    Up to date (' + $local + ')');" ^
    "  }" ^
    "} catch {" ^
    "  Write-Host ('    Update check skipped (offline or repo release not yet published)');" ^
    "}" >> "%LOG%" 2>&1
exit /b 0

:check_updates_silent
set "LOCAL_VER=none"
if exist "%VERSION_FILE%" set /p LOCAL_VER=<"%VERSION_FILE%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$repo = '%REPO%'; $local = '%LOCAL_VER%'; $dir = '%DIR%';" ^
    "try {" ^
    "  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;" ^
    "  $api = 'https://api.github.com/repos/' + $repo + '/releases/latest';" ^
    "  $rel = Invoke-RestMethod -Uri $api -Headers @{'User-Agent'='SlackExtractorUpdater'} -TimeoutSec 15;" ^
    "  $remote = $rel.tag_name;" ^
    "  if ($remote -and $remote -ne $local) {" ^
    "    $reqsChanged = $false;" ^
    "    foreach ($asset in $rel.assets) {" ^
    "      $name = $asset.name;" ^
    "      if ($name -in @('main.py', 'slack_cache.py', 'requirements.txt', 'stop_schedule.bat', 'install_schedule.bat')) {" ^
    "        $dest = Join-Path $dir $name;" ^
    "        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $dest;" ^
    "        if ($name -eq 'requirements.txt') { $reqsChanged = $true }" ^
    "      }" ^
    "    }" ^
    "    Set-Content -Path (Join-Path $dir 'VERSION') -Value $remote;" ^
    "    if ($reqsChanged -and (Test-Path (Join-Path $dir '.venv\.installed'))) { Remove-Item (Join-Path $dir '.venv\.installed') -Force };" ^
    "  }" ^
    "} catch {}" >> "%LOG%" 2>&1
exit /b 0

:find_python
set "PY="
for %%V in (3.14 3.13 3.12 3.11) do if not defined PY call :try_python %%V
exit /b 0

:try_python
py -%1 -c "import sys; raise SystemExit(0 if sys.maxsize > 2**32 else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
for /f "delims=" %%P in ('py -%1 -c "import sys; print(sys.executable)"') do set "PY=%%P"
exit /b 0

:install_python
where winget >nul 2>&1
if not errorlevel 1 (
    echo     Trying Windows Package Manager...
    winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1
    call :find_python
)
if defined PY exit /b 0

echo     Downloading Python from python.org...
set "INSTALLER=%TEMP%\python-3.13.17-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe' -OutFile '%INSTALLER%' -TimeoutSec 120 } catch { Write-Error $_; if (Test-Path '%INSTALLER%') { Remove-Item '%INSTALLER%' -Force } }" >> "%LOG%" 2>&1
if not exist "%INSTALLER%" exit /b 0
start /wait "" "%INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1
del "%INSTALLER%" >nul 2>&1
call :find_python
exit /b 0
