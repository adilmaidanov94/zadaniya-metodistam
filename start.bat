@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Задания методистам - сервер

rem ===== PIN замдекана =====
rem Чтобы поставить свой PIN, впишите его вместо 1234 в строке ниже и сохраните файл.
set "ZAM_PIN=1234"

rem Ищем Python: сначала py (ставится с python.org), потом python
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% --version >nul 2>nul || goto nopython

%PY% server.py
goto end

:nopython
echo.
echo  Python не найден.
echo  Установите Python с сайта https://www.python.org/downloads/
echo  При установке обязательно отметьте галочку "Add python.exe to PATH",
echo  затем снова запустите start.bat.
echo.

:end
echo.
pause
