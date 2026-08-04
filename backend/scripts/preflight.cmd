@echo off
REM Lanceur pour cmd.exe :  scripts\preflight
REM
REM Evite deux pieges Windows :
REM   - `bash scripts\preflight.sh` depuis cmd resout vers C:\Windows\System32\bash.exe
REM     (le lanceur WSL), qui tente d'executer le script DANS la distribution
REM     `docker-desktop` — laquelle n'a pas /bin/bash.
REM   - la politique d'execution PowerShell bloque les .ps1 par defaut.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0preflight.ps1" %*
