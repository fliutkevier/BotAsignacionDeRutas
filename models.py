"""Modelos de dominio del BOT de Asignaciones."""
from __future__ import annotations

from dataclasses import dataclass


def ruta_norm(ruta: str, turno: str = "") -> str:
    """Normaliza la ruta con ceros adelante según el turno.
    - Turno 43: 3 dígitos (ej. '920', no '0920').
    - Resto:    4 dígitos (ej. '530' -> '0530').
    Si no es numérica, la devuelve tal cual."""
    r = str(ruta).strip()
    if not r.isdigit():
        return r
    ancho = 3 if str(turno).strip() == "43" else 4
    return r.zfill(ancho)


def clave_rl(ruta: str, localidad: str = "", turno: str = "") -> tuple[str, str]:
    """Clave única de una ruta física: (ruta normalizada, localidad en mayúsculas).
    La localidad solo desempata en el turno 43; en el resto va vacía."""
    return (ruta_norm(ruta, turno), (localidad or "").strip().upper())


# --- Estados que el bot escribe en la columna ESTADO -------------------------
class Estado:
    PENDIENTE = "pendiente"      # valor inicial (o celda vacía) que el bot toma
    PREPARANDO = "preparando"
    ASIGNADA = "asignada"        # estado terminal de este bot

    @staticmethod
    def error(motivo: str) -> str:
        return f"no se pudo asignar: {motivo}"

    @staticmethod
    def parcial(restantes: int) -> str:
        return f"asignada parcial ({restantes} ruta(s) quedaron libres)"

    @staticmethod
    def es_pendiente(valor: str) -> bool:
        return (valor or "").strip().lower() in ("", Estado.PENDIENTE)


@dataclass
class CasoRuta:
    """Fila de la hoja AsignacionPorRuta. `fila` es 1-based para escribir de vuelta."""
    fila: int
    turno: str
    ruta: str
    localidad: str   # obligatoria SOLO en turno 43 (desempate); resto puede ir vacía
    colector: str
    estado: str = ""

    @property
    def clave(self) -> tuple[str, str]:
        return clave_rl(self.ruta, self.localidad, self.turno)


@dataclass
class CasoLocalidad:
    """Fila de la hoja AsignacionPorLocalidad: una localidad entera a un colector."""
    fila: int
    turno: str
    localidad: str
    colector: str
    estado: str = ""
