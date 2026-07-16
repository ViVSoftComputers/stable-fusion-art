@echo off
cd /d C:\herman\crowpanel-p4\example\V1.2\idf-code\7_9_10.1_P4_HMI_AI\7inch_9inch_10inch_P4_HMI_AI
call C:\Espressif\frameworks\esp-idf-v5.4.4\export.bat
if errorlevel 1 (
    echo EXPORT FAILED
    exit /b 1
)
idf.py build
exit /b %errorlevel%
