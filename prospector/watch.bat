@echo off
REM Executa apenas o watcher de cards e produção com Claude Code.
cd /d "%~dp0.."
set PROSPECTOR_FAST=
REM Sem isso, qualquer print com acento/★ etc. quebra o processo quando a
REM saida vai pro log (Task Scheduler usa o codepage do console, nao UTF-8).
set PYTHONUTF8=1
if not exist "%~dp0runs\watcher" mkdir "%~dp0runs\watcher"
python -m prospector.cli watch >> "%~dp0runs\watcher\agendador.log" 2>&1
