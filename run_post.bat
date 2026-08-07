@echo off
cd /d "%~dp0"
python post_to_instagram.py >> task_log.txt 2>&1
