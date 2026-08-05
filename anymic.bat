@echo off
rem ===========================================================================
rem  AnyMic launcher for Windows.
rem
rem  Checks the two things Windows needs that Linux does not -- a real Python,
rem  and a virtual audio cable -- offers to install whichever is missing, and
rem  then starts the server. Any arguments are passed straight through, so
rem  "anymic.bat --port 9000" works like "python anymic.py --port 9000".
rem ===========================================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo   A N Y M I C   -   Windows launcher
echo.

rem --------------------------------------------------------------------------
rem  1. Find a Python that actually runs.
rem --------------------------------------------------------------------------
rem  The catch on Windows is that "python" is very often the Microsoft Store
rem  stub in WindowsApps: it exists, it is on the PATH, and running it opens
rem  the Store instead of executing anything. So every candidate has to be
rem  proved by running it, not by finding it.

set "PY="

rem The py launcher is the most reliable when it is present.
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PY=py -3"
  goto :got_python
)

for %%C in (
  "python"
  "python3"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
) do (
  %%C -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
  if not errorlevel 1 (
    rem Keep the quotes: "C:\Program Files\...\python.exe" must survive as one
    rem argument when it is used to launch anything below.
    set "PY=%%C"
    goto :got_python
  )
)

rem --- no Python -------------------------------------------------------------
echo   [--] Python 3.8 or newer was not found.
echo.
echo        Note that a "python" command may still exist without Python being
echo        installed -- Windows ships a placeholder that just opens the
echo        Microsoft Store. This check ignores it.
echo.

where winget >nul 2>&1
if errorlevel 1 goto :python_manual

rem set /p eats the leading spaces of its prompt, so the question is echoed
rem separately and the prompt itself kept short.
echo   Install Python now with winget?
set /p "ANS=  [Y/n] "
if /i "!ANS!"=="n" goto :python_manual

echo.
echo   Installing Python 3.12...
winget install --id Python.Python.3.12 --exact --silent ^
  --accept-package-agreements --accept-source-agreements --scope user
echo.

for %%C in (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "python"
) do (
  %%C -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
  if not errorlevel 1 (
    rem Keep the quotes: "C:\Program Files\...\python.exe" must survive as one
    rem argument when it is used to launch anything below.
    set "PY=%%C"
    goto :got_python
  )
)

echo   [--] Python still is not runnable from here.
echo        It may only need a new terminal to appear on the PATH.
echo        Close this window, open a new one, and run anymic.bat again.
goto :fail

:python_manual
echo.
echo        Install it from https://www.python.org/downloads/windows/
echo        and tick "Add python.exe to PATH" in the installer.
goto :fail

:got_python
for /f "tokens=2" %%V in ('%PY% --version 2^>^&1') do set "PYVER=%%V"
echo   [ok] Python !PYVER!

rem --------------------------------------------------------------------------
rem  2. Ask AnyMic itself whether the audio side is ready.
rem --------------------------------------------------------------------------
rem  Rather than second-guessing the device list from batch, run the same
rem  detection the server uses. --check exits 0 when a cable is present.

%PY% anymic.py --check >nul 2>&1
if not errorlevel 1 (
  echo   [ok] Virtual audio cable found
  goto :run
)

echo   [--] No virtual audio cable is installed.
echo.
echo        Windows cannot present a microphone without a driver, so AnyMic
echo        plays into a virtual cable and your apps record the other end.
echo        VB-CABLE is free, about 1 MB, and the usual choice.
echo.
echo        Installing it needs administrator rights and its installer will
echo        ask for them. Nothing else on your system is changed.
echo.

echo   Download and install VB-CABLE from vb-audio.com now?
set /p "ANS=  [y/N] "
if /i not "!ANS!"=="y" goto :cable_manual

set "TMPDIR=%TEMP%\anymic-vbcable"
if exist "%TMPDIR%" rd /s /q "%TMPDIR%" >nul 2>&1
mkdir "%TMPDIR%" >nul 2>&1

echo.
echo   Downloading...
curl -L --fail --silent --show-error ^
  -o "%TMPDIR%\vbcable.zip" ^
  "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
if errorlevel 1 (
  echo   [--] Download failed.
  goto :cable_manual
)

echo   Extracting...
tar -xf "%TMPDIR%\vbcable.zip" -C "%TMPDIR%"
if errorlevel 1 (
  echo   [--] Could not unpack the download.
  goto :cable_manual
)

rem Refuse to run it if Windows does not vouch for the signature. The file came
rem off the network a second ago; "it downloaded without error" is not a reason
rem to trust it with a driver install.
echo   Verifying the signature...
powershell -NoProfile -Command ^
  "$s = Get-AuthenticodeSignature '%TMPDIR%\VBCABLE_Setup_x64.exe';" ^
  "if ($s.Status -ne 'Valid') { Write-Host ('   signature: ' + $s.Status); exit 1 };" ^
  "Write-Host ('   signed by: ' + $s.SignerCertificate.Subject.Split(',')[0])"
if errorlevel 1 (
  echo   [--] The installer is not validly signed. Refusing to run it.
  goto :cable_manual
)

echo.
echo   Launching the installer -- approve the administrator prompt, then
echo   click Install in the window that appears.
echo.
powershell -NoProfile -Command ^
  "Start-Process -FilePath '%TMPDIR%\VBCABLE_Setup_x64.exe' -Verb RunAs -Wait"

echo.
echo   Re-checking...
%PY% anymic.py --check >nul 2>&1
if not errorlevel 1 (
  echo   [ok] Virtual audio cable found
  goto :run
)

echo   [--] Still not detected.
echo        The driver usually works immediately, but some machines need a
echo        reboot to finish. Reboot and run anymic.bat again.
goto :fail

:cable_manual
echo.
echo        To install it by hand:
echo          1. Download https://vb-audio.com/Cable/
echo          2. Unzip it anywhere
echo          3. Right-click VBCABLE_Setup_x64.exe, "Run as administrator"
echo          4. Reboot if prompted, then run anymic.bat again
echo.
echo        VoiceMeeter and Virtual Audio Cable also work if you have one.
goto :fail

rem --------------------------------------------------------------------------
rem  3. Start the server.
rem --------------------------------------------------------------------------
:run
echo.
echo   Starting AnyMic. Press Ctrl-C to stop.
echo.
echo   If your phone cannot reach the page, allow Python through the
echo   firewall on Private networks -- Windows asks only once.
echo.
%PY% anymic.py %*
set "RC=!errorlevel!"

rem A double-click gets a window that would otherwise vanish with the error in
rem it. A run from an existing prompt should just return.
if not "!RC!"=="0" (
  echo.
  echo   AnyMic exited with code !RC!.
  echo %CMDCMDLINE% | find /i "/c" >nul && pause
)
endlocal & exit /b %RC%

:fail
echo.
echo %CMDCMDLINE% | find /i "/c" >nul && pause
endlocal & exit /b 1
