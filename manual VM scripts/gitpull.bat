@echo off
cd /d "C:\Users\Administrator\fxjournal"
git pull
python -m pip install -r requirements.txt
pause
