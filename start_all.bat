@echo off
REM ===========================================================================
REM Start hele SSB-agent-stacken: MCP-server, ADK API-server, ADK Web og klient.
REM Hver tjeneste starter i sitt eget cmd-vindu slik at du ser loggene separat.
REM
REM Forutsetninger (sjekkes ikke automatisk):
REM   - Ollama kjorer (`ollama serve` i et eget vindu)
REM   - Modellen er lastet ned (`ollama pull qwen2.5`)
REM   - .venv er opprettet og avhengigheter er installert
REM ===========================================================================

setlocal
set "REPO_ROOT=%~dp0"
set "VENV=%REPO_ROOT%.venv\Scripts"

REM Sjekk at venv finnes — uten den vil ingenting starte.
if not exist "%VENV%\python.exe" (
    echo [FEIL] Fant ikke .venv i %REPO_ROOT%
    echo Kjor: py -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)

echo.
echo === Starter MCP-server (port 8000) ...
start "MCP Server SSB" cmd /k ""%VENV%\python.exe" "%REPO_ROOT%MCP_server_SSB\src\__init__.py" http"

REM Gi MCP-serveren et par sekunder paa aa binde porten for ADK kobler til.
timeout /t 3 /nobreak >nul

echo === Starter ADK API-server (port 8001) ...
REM AGENTS_DIR maa peke til foreldermappen som inneholder SSB_agent_api/, ikke selve agent-mappa.
start "ADK API Server" cmd /k ""%VENV%\adk.exe" api_server "%REPO_ROOT%Agent_API\api" --port 8001 --session_service_uri memory:// --artifact_service_uri memory://"

timeout /t 3 /nobreak >nul

echo === Starter ADK Web (port 8080) ...
start "ADK Web" cmd /k ""%VENV%\adk.exe" web "%REPO_ROOT%Agent_API\api" --port 8080"

REM Gi ADK API-serveren litt ekstra tid for klienten kobler til /run.
timeout /t 3 /nobreak >nul

echo === Starter agent-klient ...
start "Agent Klient" cmd /k "echo Bruk dette vinduet for aa snakke med agenten eller apne opp Google ADK Web som kjorer i eget vindu, default skal vaere http://127.0.0.1:8080 && echo. && "%VENV%\python.exe" "%REPO_ROOT%Agent_API\api\client.py""

echo.
echo Alle fire vinduene er startet:
echo   - MCP-server   : http://127.0.0.1:8000/mcp
echo   - ADK API      : http://127.0.0.1:8001
echo   - ADK Web UI   : http://127.0.0.1:8080
echo   - Agent-klient : prompt i eget vindu
echo.
echo For aa stoppe: lukk de fire cmd-vinduene som ble apnet.
echo.
endlocal
