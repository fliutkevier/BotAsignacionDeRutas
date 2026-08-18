"""Lectura/escritura de las dos hojas del bot vía service account.

Hojas del MISMO archivo:
  - AsignacionPorRuta:      TURNO | RUTA | LOCALIDAD | COLECTOR | ESTADO
  - AsignacionPorLocalidad: TURNO | LOCALIDAD | COLECTOR | ESTADO
Los headers se leen normalizados (MAYÚSCULAS, sin espacios extra): columnas
adicionales o reordenadas no rompen; solo renombrar las requeridas rompería.

CUOTA: Sheets permite 60 escrituras/minuto por usuario. Escribir celda por celda
agotaba la cuota y abortaba la corrida (APIError 429), así que las escrituras se
ENCOLAN y se mandan en lote con flush(): un batch_update = 1 sola escritura para
N celdas. Como red de seguridad, todo pedido reintenta con espera creciente.
"""
from __future__ import annotations

import logging
import time

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

import config
from models import CasoLocalidad, CasoRuta

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Reintentos: 429 (cuota) y errores transitorios del servidor. Las esperas
# acumuladas (5+10+20+40+60s) superan la ventana de 1 minuto de la cuota.
_MAX_INTENTOS = 6
_ESPERA_INICIAL_S = 5.0
_ESPERA_MAXIMA_S = 60.0
_CODIGOS_REINTENTABLES = {429, 500, 502, 503}


def _norm_header(nombre: str) -> str:
    return str(nombre or "").strip().upper()


def _reintentar(descripcion: str, fn, *args, **kwargs):
    """Ejecuta una llamada a la API tolerando cuota agotada y fallas pasajeras."""
    espera = _ESPERA_INICIAL_S
    for intento in range(1, _MAX_INTENTOS + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if e.code not in _CODIGOS_REINTENTABLES or intento == _MAX_INTENTOS:
                raise
            log.warning("Sheets (%s) devolvió %s; reintento %d/%d en %.0fs.",
                        descripcion, e.code, intento, _MAX_INTENTOS - 1, espera)
            time.sleep(espera)
            espera = min(espera * 2, _ESPERA_MAXIMA_S)


class _Hoja:
    """Worksheet con mapeo header->columna y cola de escrituras pendientes."""

    def __init__(self, ws, requeridas: list[str]) -> None:
        self._ws = ws
        header = _reintentar(f"header de {ws.title}", ws.row_values, 1)
        self._col = {_norm_header(h): i + 1 for i, h in enumerate(header)}
        faltantes = [c for c in requeridas if c not in self._col]
        if faltantes:
            raise ValueError(f"Faltan columnas en '{ws.title}': {faltantes}")
        # (fila, columna) -> valor. Reescribir la misma celda antes del volcado
        # pisa el valor anterior: solo viaja el último.
        self._pendientes: dict[tuple[int, int], object] = {}

    @property
    def titulo(self) -> str:
        return self._ws.title

    def filas(self) -> list[dict]:
        """Filas como dicts con claves normalizadas. offset 0 = fila 2 del sheet."""
        registros = _reintentar(f"lectura de {self.titulo}", self._ws.get_all_records)
        return [{_norm_header(k): str(v).strip() for k, v in row.items()}
                for row in registros]

    def encolar_estado(self, fila: int, estado: str) -> None:
        self._pendientes[(fila, self._col[config.COL_ESTADO])] = estado

    def encolar_cantidad(self, fila: int, cantidad: int) -> bool:
        """Encola CANTIDAD si la columna existe. Es OPCIONAL: si no está en la
        hoja se avisa una vez y no rompe."""
        col = self._col.get(config.COL_CANTIDAD)
        if col is None:
            return False
        self._pendientes[(fila, col)] = cantidad
        return True

    def volcar(self) -> int:
        """Manda todo lo encolado en UNA llamada. Si falla, la cola se conserva
        para el próximo intento. Devuelve la cantidad de celdas escritas."""
        if not self._pendientes:
            return 0
        datos = [{"range": rowcol_to_a1(fila, col), "values": [[valor]]}
                 for (fila, col), valor in self._pendientes.items()]
        # raw=False => USER_ENTERED, igual que el update_cell que reemplaza.
        _reintentar(f"escritura de {self.titulo}",
                    self._ws.batch_update, datos, raw=False)
        self._pendientes.clear()
        return len(datos)


class SheetsClient:
    def __init__(self) -> None:
        creds = Credentials.from_service_account_file(
            str(config.GOOGLE_CREDENTIALS), scopes=_SCOPES)
        gc = gspread.authorize(creds)
        libro = gc.open_by_key(config.SHEET_ID)
        self._por_ruta = _Hoja(
            libro.worksheet(config.WORKSHEET_POR_RUTA),
            [config.COL_TURNO, config.COL_RUTA, config.COL_LOCALIDAD,
             config.COL_COLECTOR, config.COL_ESTADO])
        self._por_localidad = _Hoja(
            libro.worksheet(config.WORKSHEET_POR_LOCALIDAD),
            [config.COL_TURNO, config.COL_LOCALIDAD,
             config.COL_COLECTOR, config.COL_ESTADO])

    def leer_casos_ruta(self) -> list[CasoRuta]:
        return [CasoRuta(
            fila=i + 2,  # +2: header + base 0
            turno=row.get(config.COL_TURNO, ""),
            ruta=row.get(config.COL_RUTA, ""),
            localidad=row.get(config.COL_LOCALIDAD, ""),
            colector=row.get(config.COL_COLECTOR, ""),
            estado=row.get(config.COL_ESTADO, ""),
        ) for i, row in enumerate(self._por_ruta.filas())]

    def leer_casos_localidad(self) -> list[CasoLocalidad]:
        return [CasoLocalidad(
            fila=i + 2,
            turno=row.get(config.COL_TURNO, ""),
            localidad=row.get(config.COL_LOCALIDAD, ""),
            colector=row.get(config.COL_COLECTOR, ""),
            estado=row.get(config.COL_ESTADO, ""),
        ) for i, row in enumerate(self._por_localidad.filas())]

    def _hoja_de(self, caso: CasoRuta | CasoLocalidad) -> _Hoja:
        return self._por_ruta if isinstance(caso, CasoRuta) else self._por_localidad

    def actualizar_estado(self, caso: CasoRuta | CasoLocalidad, estado: str) -> None:
        """Encola el estado; llega a la hoja en el próximo flush(). La hoja se
        resuelve por el tipo del caso."""
        caso.estado = estado
        self._hoja_de(caso).encolar_estado(caso.fila, estado)
        log.info("Fila %s -> %s", caso.fila, estado)

    def actualizar_cantidad(self, caso: CasoRuta | CasoLocalidad, cantidad: int) -> None:
        """Encola los suministros seleccionados (contador del portal) para la
        columna CANTIDAD de la hoja del caso, si existe."""
        if self._hoja_de(caso).encolar_cantidad(caso.fila, cantidad):
            log.info("Fila %s -> cantidad %d", caso.fila, cantidad)
        else:
            log.warning("No existe la columna %r en la hoja; se omite la cantidad",
                        config.COL_CANTIDAD)

    def flush(self) -> None:
        """Vuelca lo encolado de ambas hojas. El orquestador lo llama antes de
        cada tramo largo en el portal (para que el avance se vea) y al terminarlo
        (para no perder resultados), no por cada celda."""
        for hoja in (self._por_ruta, self._por_localidad):
            escritas = hoja.volcar()
            if escritas:
                log.info("Sheets: %d celda(s) escritas en %s", escritas, hoja.titulo)
