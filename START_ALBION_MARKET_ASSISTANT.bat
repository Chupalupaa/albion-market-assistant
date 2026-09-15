@echo off
cd /d "%~dp0"
title Albion Market Assistant
echo Checking optional live-order-depth support...
py -c "import nats" >nul 2>&1
if errorlevel 1 (
    echo Installing nats-py for the live AODP order feed...
    py -m pip install --user nats-py
)
echo Starting Albion Market Assistant...
py albion_market_assistant.py
if errorlevel 1 python albion_market_assistant.py
pause
