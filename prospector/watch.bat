@echo off
cd /d "%~dp0.."
set PROSPECTOR_FAST=
set PYTHONUTF8=1
if not exist "%~dp0runs\watcher" mkdir "%~dp0runs\watcher"
python -m prospector.cli watch >> "%~dp0runs\watcher\agendador.log" 2>&1
