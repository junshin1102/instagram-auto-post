@echo off
rem 毎朝1回、Windowsタスクスケジューラから実行する。
rem タスク登録(管理者PowerShellで1回だけ実行):
rem   schtasks /create /tn "JunshinHealthCheck" /tr "\"%~f0\"" /sc daily /st 07:30
cd /d "%~dp0"
python health_check.py >> health_check_log.txt 2>&1
