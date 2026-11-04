@echo off
cd /d "%~dp0.."
set PROSPECTOR_FAST=
set PYTHONUTF8=1
if not exist "%~dp0runs\prospeccao" mkdir "%~dp0runs\prospeccao"
python -m prospector.cli prospect >> "%~dp0runs\prospeccao\agendador.log" 2>&1
