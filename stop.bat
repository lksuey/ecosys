@echo off

schtasks /Change /TN "Ecosys - watcher" /DISABLE >nul 2>&1
schtasks /Change /TN "Ecosys - prospect" /DISABLE >nul 2>&1

echo Ecossistema DESLIGADO (watcher e prospeccao desativados).
schtasks /Query /TN "Ecosys - watcher" /FO LIST | findstr /C:"Status"
schtasks /Query /TN "Ecosys - prospect" /FO LIST | findstr /C:"Status"
echo Para religar: start.bat
