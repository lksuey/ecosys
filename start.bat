@echo off
REM Liga duas tarefas independentes: watcher e prospecção em lotes.
REM Exige Claude Code CLI instalado/autenticado. Ver playbook-LP.md.

where claude >nul 2>nul
if errorlevel 1 (
  echo Claude Code CLI nao encontrado. Instale/autentique antes de ligar o ecossistema.
  exit /b 1
)
schtasks /Create /SC MINUTE /MO 20 /TN "Ecosys - prospect" ^
  /TR "%~dp0prospector\prospect.bat" /F >nul
if errorlevel 1 (
  echo Falha ao criar/atualizar a tarefa de prospeccao.
  exit /b 1
)
schtasks /Create /SC MINUTE /MO 5 /TN "Ecosys - watcher" ^
  /TR "%~dp0prospector\watch.bat" /F >nul
if errorlevel 1 (
  schtasks /Change /TN "Ecosys - prospect" /DISABLE >nul 2>nul
  echo Falha ao criar/atualizar a tarefa do watcher.
  exit /b 1
)
schtasks /Change /TN "Ecosys - prospect" /ENABLE >nul
if errorlevel 1 (
  schtasks /Change /TN "Ecosys - watcher" /DISABLE >nul 2>nul
  echo Falha ao ativar prospeccao.
  exit /b 1
)
schtasks /Change /TN "Ecosys - watcher" /ENABLE >nul
if errorlevel 1 (
  schtasks /Change /TN "Ecosys - prospect" /DISABLE >nul 2>nul
  echo Falha ao ativar watcher.
  exit /b 1
)

echo Ecossistema LIGADO — watcher a cada 5 min; prospeccao a cada 20 min.
schtasks /Query /TN "Ecosys - watcher" /FO LIST | findstr /C:"Status" /C:"execucao" /C:"execução"
schtasks /Query /TN "Ecosys - prospect" /FO LIST | findstr /C:"Status" /C:"execucao" /C:"execução"
echo Log: prospector\runs\watcher\agendador.log
echo Para desligar: stop.bat
