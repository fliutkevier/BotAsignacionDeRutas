"""Entry point del BOT de Asignaciones.

  python main.py            # procesa las filas pendientes de las DOS hojas
  python main.py --headless # sin ventana de navegador (para dejarlo corriendo)
  python main.py --mock     # sin portal ni VPN (valida orquestación + Sheets)

Hojas: AsignacionPorRuta (rutas puntuales) y AsignacionPorLocalidad (localidades
enteras vía CHECKALL). Se procesa POR RUTA primero.
"""
from __future__ import annotations

import argparse
import logging

import config
from models import Estado
from orchestrator import procesar
from portal import MockPortal, Portal
from sheets_client import SheetsClient


def _setup_logging() -> None:
    config.LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(config.LOG_DIR / "bot.log", encoding="utf-8")])


def main() -> None:
    parser = argparse.ArgumentParser(description="BOT de Asignaciones de rutas")
    parser.add_argument("--mock", action="store_true", help="usar MockPortal (sin navegador)")
    parser.add_argument("--headless", action="store_true",
                        help="correr sin ventana de navegador; el avance se sigue "
                             "por consola y por logs/bot.log")
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("main")

    sheets = SheetsClient()
    casos_ruta = [c for c in sheets.leer_casos_ruta() if Estado.es_pendiente(c.estado)]
    casos_loc = [c for c in sheets.leer_casos_localidad() if Estado.es_pendiente(c.estado)]
    if not casos_ruta and not casos_loc:
        log.info("No hay casos pendientes en ninguna hoja.")
        return
    log.info("Pendientes: %d por ruta, %d por localidad.", len(casos_ruta), len(casos_loc))

    # El flag solo puede OCULTAR: si config.HEADLESS ya es True, manda la config.
    portal = MockPortal() if args.mock else Portal(headless=args.headless or config.HEADLESS)
    try:
        procesar(casos_ruta, casos_loc, portal, sheets)
    finally:
        try:
            sheets.flush()  # nada encolado se pierde, ni si la corrida abortó
        finally:
            portal.cerrar()
    log.info("Proceso finalizado.")


if __name__ == "__main__":
    main()
