"""Configuración central del BOT de Asignaciones. Nada hardcodeado en el resto."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_FROZEN = getattr(sys, "frozen", False)

# Base del ejecutable: junto al .exe (PyInstaller) o junto al .py.
_BASE = Path(sys.executable).parent if _FROZEN else Path(__file__).parent

# En el .exe, Firefox viaja DENTRO del paquete de Playwright (build.bat lo
# instala con PLAYWRIGHT_BROWSERS_PATH=0). Hay que repetir la variable al
# ejecutar o Playwright lo busca en el perfil del usuario, que en la máquina
# del supervisor no existe. Este módulo se importa antes que portal.py.
if _FROZEN:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

# --- Portales (PRECONDICIÓN: VPN ya conectada) -------------------------------
# Stack: GeneXus + K2BTools sobre ASP.NET.
BASE_URL = "https://lecturasbanprod.gasnor.com:7091"
URL_LIBERACION = f"{BASE_URL}/wpliberaruta.aspx"        # FASE B (futura)
URL_ASIGNACION = f"{BASE_URL}/wpasignacionrutas.aspx"   # FASE C: asignar a colector

# --- Navegador ----------------------------------------------------------------
HEADLESS = False          # v1 SIEMPRE visible para validar contra el proceso manual
NAVEGADOR = "firefox"
TIMEOUT_NAVEGACION_MS = 60_000
TIMEOUT_ACCION_MS = 45_000
TIMEOUT_CARGA_RUTAS_MS = 60_000  # carga AJAX de la grilla tras elegir turno (~20s)
# Si el mask (overlay de carga) no se va, la página está clavada y descarta los
# clicks: cuántas veces recargar + re-seleccionar turno y rehacer el bloque.
RECARGAS_POR_MASK = 1

# --- Google Sheets ------------------------------------------------------------
SHEET_ID = "18p_Yjv6DlXaUuhyIaTZ-dbngv5src2WSof_t-qirD5s"
WORKSHEET_POR_RUTA = "AsignacionPorRuta"
WORKSHEET_POR_LOCALIDAD = "AsignacionPorLocalidad"
GOOGLE_CREDENTIALS = _BASE / "credentials.json"

# Headers requeridos (se leen normalizados a MAYÚSCULAS; columnas extra no rompen).
COL_TURNO = "TURNO"
COL_RUTA = "RUTA"
COL_LOCALIDAD = "LOCALIDAD"
COL_COLECTOR = "COLECTOR"
COL_ESTADO = "ESTADO"
COL_CANTIDAD = "CANTIDAD"   # opcional: suministros seleccionados (se escribe)

# Único turno con rutas de 3 dígitos, que SE REPITEN entre localidades: ahí la
# LOCALIDAD de la planilla desempata. En los demás turnos la ruta ya es única y
# la localidad es informativa: usarla para filtrar solo genera falsos negativos.
TURNO_DESEMPATE_LOCALIDAD = "43"

# --- Logging ------------------------------------------------------------------
LOG_DIR = _BASE / "logs"
