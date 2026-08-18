"""Orquestación del BOT de Asignaciones.

Orden de procesamiento (decisión de Fran):
  1. POR RUTA:      asignaciones puntuales primero (lotes grandes por colector).
  2. POR LOCALIDAD: después, el CHECKALL solo se lleva las libres RESTANTES
                    (así una localidad no 'roba' rutas destinadas a otro colector).

Una sola navegación por corrida. El turno se re-selecciona solo cuando cambia
(re-seleccionar también sirve de reset: destilda y limpia el filtro heredado).

CANTIDAD (columna opcional en ambas hojas):
  - POR RUTA: 'Total Leer' de ESA fila de la grilla.
  - POR LOCALIDAD: total del contador de suministros tras el CHECKALL.
Si no se pudo leer, la celda queda como estaba: no se inventa un número.

PÁGINA CLAVADA (mask): si el overlay de carga no se va, el portal descarta los
clicks y todo lo que siga es humo. En vez de continuar, se recarga la página, se
re-selecciona el turno y se REHACE el bloque entero (la recarga pierde los
tildes, así que continuar a la mitad no es opción).

Estados por fila:
  - 'asignada' si quedó asignada (verificado contra la grilla, no solo el click).
  - 'asignada parcial (...)' si tras asignar una localidad quedaron rutas libres.
  - 'no se pudo asignar: <motivo>' ante datos inválidos o falla del portal.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

import config
from models import CasoLocalidad, CasoRuta, Estado, clave_rl
from portal import MaskAtascado, PortalInterface, ResultadoAsignacion
from sheets_client import SheetsClient

log = logging.getLogger(__name__)


def procesar(casos_ruta: list[CasoRuta], casos_loc: list[CasoLocalidad],
             portal: PortalInterface, sheets: SheetsClient) -> None:
    validos_r = _validar_ruta(casos_ruta, sheets)
    validos_l = _validar_localidad(casos_loc, sheets)
    sheets.flush()  # los rechazos de validación viajan en un solo lote
    if not validos_r and not validos_l:
        log.info("No hay casos válidos para procesar.")
        return

    portal.ir_a_asignacion()
    turno_actual = ""

    def _turno(turno: str) -> None:
        nonlocal turno_actual
        if turno != turno_actual:
            portal.seleccionar_turno(turno)
            turno_actual = turno

    # ---- 1) POR RUTA: por turno -> por colector, tildado en lote ------------
    por_turno_r: dict[str, dict[str, list[CasoRuta]]] = defaultdict(lambda: defaultdict(list))
    for c in validos_r:
        por_turno_r[c.turno][c.colector].append(c)

    for turno, por_colector in por_turno_r.items():
        _turno(turno)
        for colector, grupo in por_colector.items():
            try:
                ok = _con_recuperacion(
                    portal, turno, f"colector {colector} (turno {turno})",
                    lambda tras_recarga, cl=colector, g=grupo:
                        _asignar_grupo_ruta(turno, cl, g, portal, sheets, tras_recarga))
                if not ok:
                    for c in grupo:
                        sheets.actualizar_estado(c, Estado.error(_MOTIVO_BLOQUEO))
                    turno_actual = ""  # página en estado incierto: forzar el turno
            finally:
                sheets.flush()  # los resultados del grupo, en un solo lote

    # ---- 2) POR LOCALIDAD: por turno, una localidad por vez -----------------
    por_turno_l: dict[str, list[CasoLocalidad]] = defaultdict(list)
    for c in validos_l:
        por_turno_l[c.turno].append(c)

    for turno, grupo in por_turno_l.items():
        _turno(turno)
        for c in grupo:
            try:
                ok = _con_recuperacion(
                    portal, turno, f"localidad {c.localidad} (turno {turno})",
                    lambda tras_recarga, caso=c:
                        _asignar_localidad(turno, caso, portal, sheets, tras_recarga))
                if not ok:
                    sheets.actualizar_estado(c, Estado.error(_MOTIVO_BLOQUEO))
                    turno_actual = ""
            finally:
                sheets.flush()


_MOTIVO_BLOQUEO = "la página quedó bloqueada (mask) y no se recuperó"


def _con_recuperacion(portal: PortalInterface, turno: str, descripcion: str,
                      accion: Callable[[bool], None]) -> bool:
    """Corre `accion`; si la página se clava con el mask, RECARGA, re-selecciona
    el turno y la rehace desde cero (la recarga pierde los tildes, así que
    retomar a la mitad daría por asignado lo que nunca se tildó). `accion`
    recibe si viene de una recarga, para poder matizar los motivos de error.
    False si siguió bloqueada tras agotar las recargas."""
    for intento in range(config.RECARGAS_POR_MASK + 1):
        try:
            if intento:
                portal.recuperar(turno)
            accion(intento > 0)
            return True
        except MaskAtascado as e:
            log.warning("%s: %s", descripcion, e)
    log.error("%s: sigue bloqueada tras %d recarga(s); se marca y se sigue con "
              "el resto", descripcion, config.RECARGAS_POR_MASK)
    return False


def _asignar_grupo_ruta(turno: str, colector: str, grupo: list[CasoRuta],
                        portal: PortalInterface, sheets: SheetsClient,
                        tras_recarga: bool = False) -> None:
    """Todas las rutas de un colector en un turno: tildado en lote y una sola
    asignación. El llamador vuelca a la hoja al terminar. Es re-ejecutable de
    punta a punta: no arrastra estado entre corridas."""
    for c in grupo:
        sheets.actualizar_estado(c, Estado.PREPARANDO)
    sheets.flush()  # antes del portal: el 'preparando' tiene que verse en vivo

    pares = [(c.ruta, c.localidad) for c in grupo]
    tildadas, cantidades = portal.tildar_rutas(pares)

    presentes = [c for c in grupo if c.clave in tildadas]
    # Tras una recarga por bloqueo, "no está entre las libres" es ambiguo: pudo
    # haberse asignado justo antes de que la página se clavara. Se avisa para
    # que se revise a mano en vez de darlo por no asignado sin más.
    faltante = ("la ruta no apareció en las libres tras recargar "
                "(revisar a mano: puede haberse asignado antes del bloqueo)"
                if tras_recarga else "la ruta no apareció en las libres")
    for c in grupo:
        if c.clave not in tildadas:
            sheets.actualizar_estado(c, Estado.error(faltante))
    if not presentes:
        return

    res, no_asignadas = portal.asignar_colector(
        colector, [(c.ruta, c.localidad) for c in presentes])
    if res is ResultadoAsignacion.OK:
        # Verificación POR RUTA: 'asignada' SOLO si desapareció de la
        # grilla de libres; el click exitoso no alcanza como prueba.
        for c in presentes:
            if c.clave in no_asignadas:
                sheets.actualizar_estado(
                    c, Estado.error("la ruta no se asignó al colector (verificación)"))
            else:
                sheets.actualizar_estado(c, Estado.ASIGNADA)
                # CANTIDAD (delta por ruta) SOLO en filas asignadas de
                # verdad: escribirla antes dejaba cantidades en filas
                # cuya asignación después falló.
                if c.clave in cantidades:
                    sheets.actualizar_cantidad(c, cantidades[c.clave])
    else:
        motivo = ("colector no encontrado"
                  if res is ResultadoAsignacion.COLECTOR_NO_ENCONTRADO
                  else "falla al asignar")
        for c in presentes:
            sheets.actualizar_estado(c, Estado.error(motivo))
        # Reset: re-seleccionar el turno destilda y no arrastra al siguiente.
        portal.seleccionar_turno(turno)


def _asignar_localidad(turno: str, c: CasoLocalidad, portal: PortalInterface,
                       sheets: SheetsClient, tras_recarga: bool = False) -> None:
    """Una localidad entera vía CHECKALL. El llamador vuelca a la hoja al
    terminar. Es re-ejecutable de punta a punta."""
    sheets.actualizar_estado(c, Estado.PREPARANDO)
    sheets.flush()  # antes del portal: el 'preparando' tiene que verse en vivo

    n = portal.filtrar_localidad(c.localidad)
    if n == 0:
        # Tras una recarga por bloqueo, 0 libres puede ser que ya se asignaron
        # justo antes de que la página se clavara: se pide revisión manual.
        sheets.actualizar_estado(c, Estado.error(
            "sin rutas libres tras recargar (revisar a mano: pueden haberse "
            "asignado antes del bloqueo)" if tras_recarga
            else "sin rutas libres para la localidad"))
        return
    cantidad = portal.tildar_todas()
    if cantidad is None:
        sheets.actualizar_estado(c, Estado.error("no se pudieron tildar las rutas"))
        return
    res, _ = portal.asignar_colector(c.colector)
    if res is ResultadoAsignacion.OK:
        # CANTIDAD: total del contador tras el CHECKALL. En 'parcial'
        # también se escribe (el estado ya avisa que quedaron libres).
        sheets.actualizar_cantidad(c, cantidad)
        # Verificación: con el filtro aún aplicado, la grilla de libres
        # debe quedar en 0; lo que quedó visible NO se asignó.
        restantes = portal.filas_libres_visibles()
        if restantes:
            log.warning("Localidad %s: %d ruta(s) quedaron sin asignar",
                        c.localidad, restantes)
            # Siguen TILDADAS: si no se destildan, la próxima localidad las
            # hereda en su CHECKALL y se las lleva su colector.
            portal.destildar_visibles()
            sheets.actualizar_estado(c, Estado.parcial(restantes))
        else:
            sheets.actualizar_estado(c, Estado.ASIGNADA)
    else:
        motivo = ("colector no encontrado"
                  if res is ResultadoAsignacion.COLECTOR_NO_ENCONTRADO
                  else "falla al asignar")
        sheets.actualizar_estado(c, Estado.error(motivo))
        # Reset del tildado heredado antes de la próxima localidad.
        portal.seleccionar_turno(turno)


def _validar_ruta(casos: list[CasoRuta], sheets: SheetsClient) -> list[CasoRuta]:
    """Reglas: turno y ruta obligatorios; colector obligatorio; en turno 43 la
    localidad es obligatoria (desempate); en el resto puede ir vacía."""
    validos: list[CasoRuta] = []
    for c in casos:
        if not c.turno or not c.ruta:
            sheets.actualizar_estado(c, Estado.error("falta turno o ruta"))
        elif not c.colector:
            sheets.actualizar_estado(c, Estado.error("falta colector"))
        elif c.turno == "43" and not c.localidad.strip():
            sheets.actualizar_estado(
                c, Estado.error("turno 43 requiere localidad (desempate de ruta)"))
        else:
            validos.append(c)
    _detectar_duplicados(validos, sheets)
    return validos


def _detectar_duplicados(casos: list[CasoRuta], sheets: SheetsClient) -> None:
    """Misma ruta física (turno, ruta, localidad) con DOS colectores distintos
    es un conflicto de datos: se marcan esas filas y no se procesan."""
    por_clave: dict[tuple, set[str]] = defaultdict(set)
    for c in casos:
        por_clave[(c.turno, *clave_rl(c.ruta, c.localidad, c.turno))].add(c.colector)
    conflictivas = {k for k, cols in por_clave.items() if len(cols) > 1}
    if not conflictivas:
        return
    for c in casos[:]:
        if (c.turno, *clave_rl(c.ruta, c.localidad, c.turno)) in conflictivas:
            sheets.actualizar_estado(c, Estado.error("conflicto de colector en la ruta"))
            casos.remove(c)


def _validar_localidad(casos: list[CasoLocalidad], sheets: SheetsClient) -> list[CasoLocalidad]:
    validos: list[CasoLocalidad] = []
    for c in casos:
        if not c.turno or not c.localidad.strip():
            sheets.actualizar_estado(c, Estado.error("falta turno o localidad"))
        elif not c.colector:
            sheets.actualizar_estado(c, Estado.error("falta colector"))
        else:
            validos.append(c)
    return validos
