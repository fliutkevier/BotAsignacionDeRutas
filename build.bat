@echo off
REM ===========================================================================
REM  BOT de Asignaciones - genera el ejecutable para repartir a los supervisores
REM
REM  Uso: doble click en este archivo (o "build.bat" desde la consola).
REM  Resultado: dist\BOT_Asignaciones\  -> esa carpeta es la que se comprime.
REM
REM  Requiere en ESTA maquina: Python 3.11+ e internet (baja Firefox y PyInstaller).
REM  La maquina del supervisor NO necesita Python ni Playwright: Firefox viaja
REM  adentro del paquete.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set NOMBRE=BOT_Asignaciones
set DESTINO=dist\%NOMBRE%

echo.
echo === [1/6] Dependencias =====================================================
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo.
echo === [2/6] Firefox DENTRO del paquete de Playwright =========================
REM PLAYWRIGHT_BROWSERS_PATH=0 lo instala en site-packages\playwright\driver\...
REM en vez del perfil del usuario, que es lo unico que PyInstaller puede empaquetar.
REM config.py vuelve a poner la variable al ejecutar el .exe.
set PLAYWRIGHT_BROWSERS_PATH=0
python -m playwright install firefox
if errorlevel 1 goto :error

echo.
echo === [3/6] Limpiando build anterior =========================================
if exist build rmdir /s /q build
if exist "%DESTINO%" rmdir /s /q "%DESTINO%"
if exist "%NOMBRE%.spec" del /q "%NOMBRE%.spec"

echo.
echo === [4/6] Empaquetando (tarda unos minutos) ================================
REM --onedir: carpeta comprimible. --console: el supervisor ve el avance.
REM --collect-all playwright: arrastra el driver Node + el Firefox del paso 2.
python -m PyInstaller --noconfirm --clean --onedir --console --name "%NOMBRE%" ^
  --collect-all playwright ^
  --collect-all gspread ^
  --collect-all google.auth ^
  --hidden-import google.oauth2.service_account ^
  main.py
if errorlevel 1 goto :error

echo.
echo === [5/6] credentials.json junto al .exe ===================================
REM config.py lo busca al lado del .exe (no adentro): tiene que ir suelto.
if not exist credentials.json (
    echo   ERROR: falta credentials.json en esta carpeta.
    goto :error
)
copy /y credentials.json "%DESTINO%\credentials.json" >nul
if errorlevel 1 goto :error

echo.
echo === [6/6] Lanzador para el supervisor ======================================
> "%DESTINO%\EJECUTAR BOT.bat" echo @echo off
>>"%DESTINO%\EJECUTAR BOT.bat" echo cd /d "%%~dp0"
>>"%DESTINO%\EJECUTAR BOT.bat" echo echo Conectate a la VPN ANTES de seguir.
>>"%DESTINO%\EJECUTAR BOT.bat" echo pause
>>"%DESTINO%\EJECUTAR BOT.bat" echo "%NOMBRE%.exe"
>>"%DESTINO%\EJECUTAR BOT.bat" echo echo.
>>"%DESTINO%\EJECUTAR BOT.bat" echo echo Termino. El detalle queda en logs\bot.log
>>"%DESTINO%\EJECUTAR BOT.bat" echo pause

echo.
echo ===========================================================================
echo  LISTO: %DESTINO%
echo.
echo  Comprimi esa carpeta ENTERA y repartila. El supervisor descomprime y
echo  ejecuta "EJECUTAR BOT.bat" (con la VPN ya conectada).
echo ===========================================================================
echo.
pause
exit /b 0

:error
echo.
echo ===========================================================================
echo  FALLO EL BUILD. Mira el error de mas arriba.
echo ===========================================================================
echo.
pause
exit /b 1
