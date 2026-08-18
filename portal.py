"""Capa de interacción con el portal de asignación (Playwright). GeneXus + K2BTools.

BOT de ASIGNACIONES — trabaja SOLO sobre wpasignacionrutas.aspx (Fase C):
  - Modo POR RUTA: tilda rutas puntuales en lote (muchas rutas por colector).
  - Modo POR LOCALIDAD: filtra por localidad, tilda TODAS con el CHECKALL y asigna.
La Fase B (liberar rutas) queda como stub para implementación futura.

Reglas duras del portal (heredadas y validadas en el bot de correcciones):
  - MIENTRAS hay mask (div.gx-mask), la página DESCARTA los clicks: _sin_mask
    antes de CADA interacción. Si el mask NO se va, la página quedó clavada:
    _sin_mask corta con MaskAtascado y el orquestador recarga y rehace el bloque
    (seguir era clickear al vacío y dar por asignado lo que nunca se tildó).
  - El mask se inyecta DENTRO del contenedor de cada grilla al iniciar el
    request: la aparición se espera con tolerancia CORTA (si en <1s no vino,
    no viene) y por wait_for de Playwright (detecta masks de vida muy corta).
  - Tildar dispara re-renders que corren índices o DESTILDAN filas ya tildadas:
    verificar cada tilde + pasada final de verificación antes de accionar.
  - Fin del ASIGNAR: cartel flotante K2BT_MessageText. PROHIBIDO networkidle
    (los websockets de GX no drenan nunca).
  - Verificación real post-asignar: solo las rutas que DESAPARECIERON de la
    grilla de libres se asignaron; las residuales se destildan.
  - Turno 43: ruta de 3 dígitos, desempatada por localidad. Resto: 4 dígitos.

CANTIDAD:
  - POR RUTA: columna 'Total Leer' de ESA fila (span_vCTD_SERVICIO_NNNN), leída
    ANTES de tildar. Es el dato propio de la ruta: no depende de que el contador
    global haya alcanzado a actualizarse ni de que un re-render lo descuadre.
  - POR LOCALIDAD: #vTOTALSUMINISTROSSELECCIONADOS (total acumulado) tras el
    CHECKALL, que es justo lo que se quiere del lote entero.
"""
from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Protocol

from playwright.sync_api import Page, sync_playwright

import config
from models import clave_rl as _clave_rl, ruta_norm as _ruta_norm

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


class MaskAtascado(RuntimeError):
    """La página quedó bloqueada por el overlay de carga (div.gx-mask).

    No es un error recuperable en el lugar: mientras el mask está puesto la
    página descarta TODO click, así que reintentar la acción no sirve. La única
    salida es recargar (Portal.recuperar) y rehacer el bloque desde cero."""


class ResultadoAsignacion(Enum):
    OK = "ok"
    COLECTOR_NO_ENCONTRADO = "colector_no_encontrado"
    ERROR = "error"


class PortalInterface(Protocol):
    def ir_a_asignacion(self) -> None: ...
    def seleccionar_turno(self, turno: str) -> None: ...
    def recuperar(self, turno: str) -> None: ...
    def tildar_rutas(self, pares: list) -> tuple[set, dict]: ...
    def filtrar_localidad(self, localidad: str) -> int: ...
    def tildar_todas(self) -> int | None: ...
    def destildar_visibles(self) -> int: ...
    def asignar_colector(self, colector: str, pares: list | None = None) -> tuple["ResultadoAsignacion", set]: ...
    def filas_libres_visibles(self) -> int: ...
    def liberar_rutas_turno(self, turno: str) -> None: ...
    def cerrar(self) -> None: ...


class Portal:
    # Overlay de carga: token exacto `div.gx-mask`; NUNCA subcadena, porque
    # `gx-masked-relative`/`gx-masked` son clases PERMANENTES del contenedor.
    MASK = "div.gx-mask"
    MASK_LIBRES = "#GridrutasContainerDiv div.gx-mask"
    MASK_ASIGNADA = "#GridrutasasignadaContainerDiv div.gx-mask"

    FILTRO_RUTAS = "#vGENERICFILTER_GRIDRUTAS"      # filtro genérico (grilla de libres)
    CHECKALL = "#vCHECKALL_GRIDRUTAS"               # tilda todas las filas visibles
    CONTADOR = "#vTOTALSUMINISTROSSELECCIONADOS"    # acumulado; se usa POR LOCALIDAD
    # 'Total Leer' de cada fila: #span_vCTD_SERVICIO_NNNN, mismo sufijo NNNN que
    # span_vRUTASRUTA_ y vMULTIROWITEMSELECTED_GRIDRUTAS_. Se usa POR RUTA.
    SCOPE_LIBRES = "#GridrutasContainerTbl"         # para no tocar la grilla de asignadas

    # Cartel flotante de K2BTools con el resultado de la acción, p.ej.
    # "Se Asignarón Rutas al colector 600". Señal REAL de fin del ASIGNAR.
    CARTEL_RESULTADO = "div.K2BT_MessageText"

    def __init__(self, headless: bool | None = None) -> None:
        """headless=None usa config.HEADLESS. Se decide acá y para toda la
        corrida: no se puede mostrar/ocultar el navegador una vez arrancado."""
        oculto = config.HEADLESS if headless is None else headless
        log.info("Navegador %s (%s)", config.NAVEGADOR,
                 "sin ventana" if oculto else "con ventana visible")
        self._pw = sync_playwright().start()
        browser_type = getattr(self._pw, config.NAVEGADOR)
        self._browser = browser_type.launch(headless=oculto)
        self._ctx = self._browser.new_context(ignore_https_errors=True)
        self._ctx.set_default_timeout(config.TIMEOUT_ACCION_MS)
        self._ctx.set_default_navigation_timeout(config.TIMEOUT_NAVEGACION_MS)
        self._page: Page = self._ctx.new_page()
        self._turno_actual: str = ""

    # ---------------------------------------------------------------- helpers
    def _sin_mask(self, timeout_ms: int | None = None, critico: bool = True) -> None:
        """Espera a que NO haya mask visible. SIEMPRE antes de cada interacción.
        Caso común (sin mask): resuelve en la PRIMERA lectura. Poll corto
        (100ms): los masks suelen durar <1s.
        Si se agota el plazo la página está clavada: con critico=True se corta
        con MaskAtascado para que el llamador recargue. critico=False queda solo
        para los chequeos oportunistas de tolerancia corta, donde seguir de
        largo es legítimo (todavía puede haber una carga normal en curso)."""
        deadline = timeout_ms or config.TIMEOUT_CARGA_RUTAS_MS
        transcurrido = 0
        while transcurrido < deadline:
            try:
                if self._page.locator(f"{self.MASK}:visible").count() == 0:
                    return
            except Exception:  # noqa: BLE001
                return
            self._page.wait_for_timeout(100)
            transcurrido += 100
        if critico:
            raise MaskAtascado(f"el mask no desapareció en {deadline}ms")
        log.warning("El mask sigue puesto tras %dms; se continúa", deadline)

    def _ciclo_mask(self, aparicion_ms: int = 800) -> bool:
        """Espera aparición + desaparición del mask tras una acción que carga.
        Aparición por wait_for (mutaciones, ~16ms): detecta masks de vida corta.
        Tolerancia CORTA: GX inyecta el mask al iniciar el request; si en
        ~800ms no apareció, no va a aparecer (esperas largas acá eran la causa
        principal de lentitud). True si vio el ciclo completo."""
        mask = self._page.locator(self.MASK).first
        try:
            mask.wait_for(state="visible", timeout=aparicion_ms)
        except Exception:  # noqa: BLE001 - nunca apareció (carga sin mask o ya terminó)
            return False
        try:
            mask.wait_for(state="hidden", timeout=config.TIMEOUT_CARGA_RUTAS_MS)
        except Exception:  # noqa: BLE001
            log.warning("El mask no desapareció a tiempo; se continúa")
        # Chequeo oportunista por si hay otro mask simultáneo: no es crítico,
        # el _sin_mask() de la próxima interacción es el que corta de verdad.
        self._sin_mask(timeout_ms=2_000, critico=False)
        self._page.wait_for_timeout(150)  # margen a que pinte el resultado
        return True

    def _esperar_mask(self, selector: str) -> bool:
        """Ciclo aparecer->desaparecer de un mask ESPECÍFICO de grilla, con
        aparición corta (1.5s). False si nunca apareció (el llamador cae a
        estabilización de conteo)."""
        try:
            self._page.wait_for_selector(selector, state="visible", timeout=1_500)
        except Exception:  # noqa: BLE001
            return False
        try:
            self._page.wait_for_selector(selector, state="hidden",
                                         timeout=config.TIMEOUT_CARGA_RUTAS_MS)
            self._page.wait_for_timeout(200)
            return True
        except Exception:  # noqa: BLE001
            log.warning("El overlay de carga no desapareció a tiempo")
            return False

    def filas_libres_visibles(self) -> int:
        """Filas actualmente visibles en la grilla de libres (respeta el filtro)."""
        return self._page.locator(
            f"{self.SCOPE_LIBRES} span[id^='span_vRUTASRUTA_']").count()

    def _esperar_grilla_rutas(self) -> None:
        """Fin de carga de la grilla de LIBRES: por mask o, de respaldo, por
        estabilización de conteo. Con filas: 2 lecturas iguales; con 0 filas se
        exige MÁS sostén (0 también se lee mientras carga)."""
        if self._esperar_mask(self.MASK_LIBRES):
            log.info("Grilla de rutas cargada (mask): %d filas",
                     self.filas_libres_visibles())
            return
        previo, estable, transcurrido = -1, 0, 0
        while transcurrido < config.TIMEOUT_CARGA_RUTAS_MS:
            n = self.filas_libres_visibles()
            if n == previo:
                estable += 1
                if (n > 0 and estable >= 2) or (n == 0 and estable >= 6):
                    log.info("Grilla de rutas cargada: %d filas", n)
                    return
            else:
                estable = 0
            previo = n
            self._page.wait_for_timeout(400)
            transcurrido += 400
        log.warning("La grilla no se estabilizó en %dms (filas=%d)",
                    config.TIMEOUT_CARGA_RUTAS_MS, previo)

    def _esperar_rutas_del_colector(self) -> None:
        """Tras elegir colector cargan SUS rutas. Acá 0 SÍ es resultado válido."""
        filas = "tr[id^='GridrutasasignadaContainerRow_']"
        if self._esperar_mask(self.MASK_ASIGNADA):
            log.info("Rutas del colector cargadas (mask): %d",
                     self._page.locator(filas).count())
            return
        self._page.wait_for_timeout(800)
        previo, estable, transcurrido = -1, 0, 0
        while transcurrido < 20_000:
            n = self._page.locator(filas).count()
            if n == previo:
                estable += 1
                if estable >= 3:
                    log.info("Rutas del colector cargadas: %d", n)
                    return
            else:
                estable = 0
            previo = n
            self._page.wait_for_timeout(400)
            transcurrido += 400

    def _leer_contador(self) -> int:
        """Valor de 'Suministros Seleccionados'. GX puede formatear con puntos
        de miles: se quitan los no-dígitos."""
        try:
            crudo = self._page.input_value(self.CONTADOR) or "0"
            digitos = re.sub(r"\D", "", crudo)
            return int(digitos) if digitos else 0
        except Exception:  # noqa: BLE001
            log.warning("No se pudo leer el contador de suministros")
            return 0

    def _total_leer(self, idx: str) -> int | None:
        """'Total Leer' de la fila NNNN de la grilla de libres. None si no se
        pudo leer: mejor dejar la CANTIDAD vacía que escribir un número inventado.
        Se lee ANTES de tildar, con el índice recién sacado de la grilla."""
        try:
            txt = self._page.locator(
                f"{self.SCOPE_LIBRES} #span_vCTD_SERVICIO_{idx}").inner_text(
                timeout=3_000)
        except Exception:  # noqa: BLE001
            log.warning("No se pudo leer 'Total Leer' de la fila %s", idx)
            return None
        digitos = re.sub(r"\D", "", txt or "")
        if not digitos:
            log.warning("'Total Leer' de la fila %s no es numérico (%r)", idx, txt)
            return None
        return int(digitos)

    # ------------------------------------------------------------ navegación
    def ir_a_asignacion(self) -> None:
        self._page.goto(config.URL_ASIGNACION, wait_until="domcontentloaded")
        self._turno_actual = ""

    def seleccionar_turno(self, turno: str) -> None:
        """Elige el turno; la grilla carga sola (sin botón Actualizar).
        La espera la hace _esperar_grilla_rutas DIRECTO (aparición + fin del
        mask de la grilla): no se consume antes el ciclo con _ciclo_mask (la
        doble espera hacía que la segunda quemara su tolerancia completa).
        Anti doble-carga: verificación SONDEADA del valor (una lectura puntual
        durante el re-render se lee vacía y provocaba re-selección + recarga)."""
        self._turno_actual = str(turno).strip()
        self._sin_mask()
        self._page.select_option("#vTURNO", value=self._turno_actual)
        self._esperar_grilla_rutas()
        if not self._turno_estable():
            log.warning("El turno quedó reseteado tras el postback; re-seleccionando (una vez)")
            self._page.select_option("#vTURNO", value=self._turno_actual)
            self._esperar_grilla_rutas()

    def recuperar(self, turno: str) -> None:
        """Saca a la página del bloqueo: recarga limpia y vuelve a dejar el turno
        seleccionado. La recarga PIERDE todo lo tildado, así que el llamador
        tiene que rehacer el bloque entero, no continuarlo."""
        log.warning("Recuperando la página (recarga + turno %s)", turno)
        self.ir_a_asignacion()
        if str(turno).strip():
            self.seleccionar_turno(turno)

    def _turno_estable(self, sondeo_ms: int = 5_000) -> bool:
        """True si #vTURNO termina mostrando el turno esperado (sondeado)."""
        transcurrido = 0
        while transcurrido < sondeo_ms:
            try:
                if self._page.input_value("#vTURNO").strip() == self._turno_actual:
                    return True
            except Exception:  # noqa: BLE001 - select re-renderizando
                pass
            self._page.wait_for_timeout(200)
            transcurrido += 200
        return False

    # ------------------------------------------------------- modo POR RUTA
    def _fetch_rutas(self, con_localidad: bool) -> tuple[list, dict]:
        """(id, texto) de las rutas de la grilla de libres y, si aplica, mapa
        NNNN -> localidad. Una sola lectura del DOM por llamada."""
        datos_r = self._page.locator(
            f"{self.SCOPE_LIBRES} span[id^='span_vRUTASRUTA_']").evaluate_all(
            "els => els.map(e => [e.id, (e.textContent || '').trim()])")
        loc_map: dict[str, str] = {}
        if con_localidad:
            pares = self._page.locator(
                f"{self.SCOPE_LIBRES} span[id^='span_vRUTASLOCALIDAD_RUTA_']").evaluate_all(
                "els => els.map(e => [e.id, (e.textContent || '').trim()])")
            for el_id, txt in pares:
                m = re.search(r"_(\d{4})$", el_id or "")
                if m:
                    loc_map[m.group(1)] = _norm(txt)
        return datos_r, loc_map

    def _idxs_ruta(self, datos_r: list, loc_map: dict, ruta: str, localidad: str) -> list[str]:
        """Sufijos NNNN de TODAS las filas que matchean la ruta (contiene) y,
        si hay localidad, también la localidad (igual)."""
        objetivo = _norm(_ruta_norm(ruta, self._turno_actual))
        loc = _norm(localidad)
        idxs = []
        for el_id, txt in datos_r:
            m = re.search(r"_(\d{4})$", el_id or "")
            if not m or not objetivo or objetivo not in _norm(txt):
                continue
            idx = m.group(1)
            if loc and loc_map.get(idx, "") != loc:
                continue  # desempate por localidad (turno 43)
            idxs.append(idx)
        return idxs

    def _match_idx(self, datos_r: list, loc_map: dict, ruta: str, localidad: str) -> str | None:
        """Sufijo NNNN de la fila que matchea. None si 0 o >1 coincidencias."""
        idxs = self._idxs_ruta(datos_r, loc_map, ruta, localidad)
        if len(idxs) != 1:
            log.error("Ruta %s (localidad %r): %d coincidencias",
                      _ruta_norm(ruta, self._turno_actual), localidad, len(idxs))
            return None
        return idxs[0]

    def tildar_rutas(self, pares: list[tuple[str, str]]) -> tuple[set, dict]:
        """Tilda rutas puntuales (best-effort) registrando el 'Total Leer' de
        cada fila. Devuelve (claves tildadas, {clave: cantidad}).
        Por cada ruta: _sin_mask -> re-leer grilla -> leer 'Total Leer' ->
        tildar -> VERIFICAR."""
        con_loc = any((loc or "").strip() for _, loc in pares)
        tildadas: set[tuple[str, str]] = set()
        cantidades: dict[tuple[str, str], int] = {}
        contador_inicial = self._leer_contador()
        if contador_inicial:
            log.warning("El contador arranca en %d (selección residual)", contador_inicial)
        for ruta, localidad in pares:
            for intento in range(3):
                self._sin_mask()  # nunca tildar con la página bloqueada
                datos_r, loc_map = self._fetch_rutas(con_localidad=con_loc)
                idx = self._match_idx(datos_r, loc_map, ruta, localidad)
                if idx is None:
                    break  # no está en la grilla (best-effort)
                # ANTES de tildar: el re-render posterior corre los índices y
                # ese mismo NNNN podría ya ser otra fila.
                cantidad = self._total_leer(idx)
                sel = f"#vMULTIROWITEMSELECTED_GRIDRUTAS_{idx}"
                try:
                    self._check_gx(sel)
                    self._ciclo_mask(aparicion_ms=300)  # mask corto entre tildes
                    if not self._page.locator(sel).is_checked():
                        raise RuntimeError("el tilde no quedó aplicado tras el re-render")
                    clave = _clave_rl(ruta, localidad, self._turno_actual)
                    tildadas.add(clave)
                    if cantidad is not None:
                        cantidades[clave] = cantidad
                    break
                except MaskAtascado:
                    raise  # con la página clavada reintentar no tilda nada
                except Exception:  # noqa: BLE001 - re-render: reasentar y reintentar
                    log.warning("Reintentando tildado de ruta %s (intento %d)", ruta, intento + 1)
                    self._page.wait_for_timeout(600)
        if tildadas:
            self._verificar_tildes(pares, tildadas, con_loc)
            # Control cruzado: la suma de los 'Total Leer' leídos debería ser lo
            # que sumó el contador del portal. Si no coincide, algo se tildó o
            # destildó de más: se avisa, no se corrige (best-effort).
            sumado = sum(cantidades.values())
            delta = self._leer_contador() - contador_inicial
            if len(cantidades) == len(tildadas) and delta != sumado:
                log.warning("Suma de 'Total Leer' (%d) != lo que sumó el contador "
                            "(%d); revisar CANTIDAD en el sheet", sumado, delta)
        return tildadas, cantidades

    def _verificar_tildes(self, pares: list, tildadas: set, con_loc: bool) -> None:
        """Pasada final ANTES de accionar: un re-render tardío puede haber
        destildado filas ya tildadas. Re-lee la grilla y re-tilda lo que falte
        (máximo 2 pasadas)."""
        for _ in range(2):
            self._sin_mask()
            datos_r, loc_map = self._fetch_rutas(con_localidad=con_loc)
            pendientes: list[tuple[str, str]] = []
            for ruta, localidad in pares:
                if _clave_rl(ruta, localidad, self._turno_actual) not in tildadas:
                    continue
                idx = self._match_idx(datos_r, loc_map, ruta, localidad)
                if idx is None:
                    continue
                try:
                    if not self._page.locator(
                            f"#vMULTIROWITEMSELECTED_GRIDRUTAS_{idx}").is_checked():
                        pendientes.append((ruta, idx))
                except Exception:  # noqa: BLE001
                    pass
            if not pendientes:
                return
            log.warning("Verificación de tildes: %d perdidos por re-render; re-tildando",
                        len(pendientes))
            for ruta, idx in pendientes:
                try:
                    self._sin_mask()
                    self._check_gx(f"#vMULTIROWITEMSELECTED_GRIDRUTAS_{idx}")
                    self._ciclo_mask(aparicion_ms=300)
                except MaskAtascado:
                    raise
                except Exception:  # noqa: BLE001
                    log.warning("No se pudo re-tildar la ruta %s en la verificación", ruta)

    # -------------------------------------------------- modo POR LOCALIDAD
    def filtrar_localidad(self, localidad: str) -> int:
        """Filtra la grilla de libres por localidad; devuelve las filas visibles.
        Proceso calcado del manual: 1) vaciar el filtro -> esperar la recarga
        con TODAS las rutas; 2) escribir la localidad entera de una (equivale a
        pegarla: el textchanged dispara UNA buscada) -> esperar la carga.
        El filtro HEREDA lo buscado antes: limpiar SIEMPRE."""
        filtro = self._page.locator(self.FILTRO_RUTAS)
        filtro.wait_for(state="visible", timeout=config.TIMEOUT_ACCION_MS)
        self._sin_mask()

        tenia_texto = bool((filtro.input_value() or "").strip())
        filtro.click()
        filtro.press("Control+a")
        filtro.press("Delete")
        try:
            filtro.evaluate(
                "el => { el.value=''; "
                "el.dispatchEvent(new Event('input', {bubbles:true})); "
                "el.dispatchEvent(new Event('keyup', {bubbles:true})); }")
        except Exception:  # noqa: BLE001
            pass
        if tenia_texto:
            # Borrar dispara la recarga con todas las rutas: esperarla ANTES de
            # tipear, o el tipeo pisa una grilla a medio cargar.
            self._esperar_grilla_rutas()

        self._sin_mask()
        filtro.type(str(localidad).strip(), delay=30)
        self._esperar_grilla_rutas()
        n = self.filas_libres_visibles()
        log.info("Filtro localidad %r: %d rutas libres", localidad, n)
        return n

    def tildar_todas(self) -> int | None:
        """Tilda el CHECKALL de la grilla de libres, VERIFICA que quedó tildado
        y devuelve la CANTIDAD total de suministros seleccionados (contador).
        None = no se pudo tildar. El checkall dispara su propio mask.

        El contador es ACUMULATIVO: si arranca en algo distinto de 0 hay una
        selección residual y el total va a venir inflado (además de que esas
        rutas se asignarían a este colector). Se avisa fuerte."""
        residual = self._leer_contador()
        if residual:
            log.warning("El contador arranca en %d ANTES del CHECKALL: hay una "
                        "selección residual y la CANTIDAD va a venir inflada", residual)
        for intento in range(3):
            self._sin_mask()
            chk = self._page.locator(self.CHECKALL)
            try:
                self._check_gx(self.CHECKALL)
                self._ciclo_mask(aparicion_ms=800)
                if chk.is_checked():
                    cantidad = self._leer_contador()
                    log.info("CHECKALL tildado: %d suministros seleccionados", cantidad)
                    return cantidad
            except MaskAtascado:
                raise
            except Exception:  # noqa: BLE001
                pass
            log.warning("Reintentando CHECKALL (intento %d)", intento + 1)
            self._page.wait_for_timeout(600)
        log.error("No se pudo tildar el CHECKALL")
        return None

    def destildar_visibles(self) -> int:
        """Destilda lo que siga seleccionado en la grilla de libres; devuelve
        cuántas filas destildó.

        Tras una asignación PARCIAL las rutas que NO se asignaron quedan
        tildadas y el contador sigue contándolas. Sin esto, la localidad
        siguiente arranca con esa selección puesta: su CANTIDAD sale inflada y
        —peor— esas rutas se le asignan al colector equivocado."""
        self._sin_mask()
        chk = self._page.locator(self.CHECKALL)
        try:
            if chk.is_checked():  # un click destilda todas las visibles
                chk.uncheck(force=True, timeout=8_000)
                self._ciclo_mask(aparicion_ms=800)
        except MaskAtascado:
            raise
        except Exception:  # noqa: BLE001
            pass
        # Barrido de respaldo: el CHECKALL puede haberse reseteado solo dejando
        # filas tildadas por debajo.
        destildadas = 0
        casillas = self._page.locator(
            "input[id^='vMULTIROWITEMSELECTED_GRIDRUTAS_']")
        for i in range(casillas.count()):
            try:
                casilla = casillas.nth(i)
                if casilla.is_checked():
                    casilla.uncheck(force=True, timeout=4_000)
                    destildadas += 1
            except Exception:  # noqa: BLE001
                pass
        if destildadas:
            log.warning("Se destildaron %d ruta(s) residuales de la grilla de libres",
                        destildadas)
        return destildadas

    # ------------------------------------------------------------ asignación
    def _esperar_fin_asignacion(self) -> None:
        """Fin del ASIGNAR por el cartel flotante K2BTools y asentado de la
        grilla. PROHIBIDO networkidle (websockets de GX no drenan nunca). Si el
        cartel no se ve (se desvanece solo), respaldo: mask + estabilización;
        la verificación posterior contra la grilla es la que decide."""
        visto = False
        transcurrido = 0
        while transcurrido < 30_000:
            try:
                if self._page.locator(f"{self.CARTEL_RESULTADO}:visible").count() > 0:
                    visto = True
                    try:
                        txt = self._page.locator(
                            self.CARTEL_RESULTADO).first.inner_text().strip()
                        log.info("Cartel de resultado: %r", txt)
                    except Exception:  # noqa: BLE001
                        pass
                    break
            except Exception:  # noqa: BLE001
                pass
            self._page.wait_for_timeout(200)
            transcurrido += 200
        if not visto:
            log.warning("No se vio el cartel de resultado del ASIGNAR; "
                        "se sigue por mask + grilla")
        self._sin_mask()
        self._esperar_grilla_rutas()  # grilla ya actualizada (asignadas fuera)

    def _rutas_aun_libres(self, pares: list) -> set:
        """Claves de las rutas que SIGUEN en la grilla de libres tras asignar
        (= NO se asignaron; >1 fila ambigua cuenta como no asignada). Además
        las DESTILDA (best-effort) para no arrastrar tildes residuales."""
        con_loc = any((loc or "").strip() for _, loc in pares)
        self._sin_mask()
        datos_r, loc_map = self._fetch_rutas(con_localidad=con_loc)
        quedan: set = set()
        idxs_residuales: list[str] = []
        for ruta, localidad in pares:
            idxs = self._idxs_ruta(datos_r, loc_map, ruta, localidad)
            if not idxs:
                continue  # ya no está entre las libres: se asignó
            quedan.add(_clave_rl(ruta, localidad, self._turno_actual))
            idxs_residuales.extend(idxs)
        for idx in idxs_residuales:
            try:
                loc = self._page.locator(f"#vMULTIROWITEMSELECTED_GRIDRUTAS_{idx}")
                if loc.is_checked():
                    loc.uncheck(force=True, timeout=4_000)
            except Exception:  # noqa: BLE001
                pass
        return quedan

    def asignar_colector(self, colector: str,
                         pares: list | None = None) -> tuple[ResultadoAsignacion, set]:
        """Con las rutas ya tildadas: elige colector (carga SUS rutas), clickea
        Asignar y espera el fin por el cartel. Devuelve (resultado, claves NO
        asignadas). `pares` habilita la verificación por ruta (modo POR RUTA);
        en POR LOCALIDAD el llamador verifica con filas_libres_visibles()."""
        claves_todas = ({_clave_rl(r, loc, self._turno_actual) for r, loc in pares}
                        if pares else set())
        try:
            self._sin_mask()  # elegir colector dispara su propia carga: no pisarla
            if not self._seleccionar_colector(colector):
                return ResultadoAsignacion.COLECTOR_NO_ENCONTRADO, claves_todas
            self._esperar_rutas_del_colector()
            self._sin_mask()
            self._click_gx("#ACTION")  # value: "Asignar"
            self._aceptar_confirm()
            self._esperar_fin_asignacion()
            no_asignadas = self._rutas_aun_libres(pares) if pares else set()
            if no_asignadas:
                log.warning("ASIGNAR a %s: %d ruta(s) NO se asignaron: %s",
                            colector, len(no_asignadas), sorted(no_asignadas))
            return ResultadoAsignacion.OK, no_asignadas
        except MaskAtascado:
            raise  # lo resuelve el orquestador recargando, no un estado de error
        except Exception:  # noqa: BLE001
            log.exception("Error asignando al colector %s", colector)
            return ResultadoAsignacion.ERROR, claves_todas

    def _seleccionar_colector(self, colector: str) -> bool:
        """Selecciona en #vCOLECTOR por NOMBRE (exacto primero, luego contiene)."""
        objetivo = _norm(colector)
        opciones = self._page.locator("#vCOLECTOR option")
        exactos, contiene = [], []
        for i in range(opciones.count()):
            op = opciones.nth(i)
            txt = _norm(op.inner_text())
            val = op.get_attribute("value") or ""
            if not txt:
                continue
            if txt == objetivo:
                exactos.append(val)
            elif objetivo and objetivo in txt:
                contiene.append(val)
        candidatos = exactos or contiene
        if len(candidatos) != 1:
            log.error("Colector %r: %d coincidencias en el desplegable",
                      colector, len(candidatos))
            return False
        self._page.select_option("#vCOLECTOR", value=candidatos[0])
        return True

    # ------------------------------------------------------- FASE B (futura)
    def liberar_rutas_turno(self, turno: str) -> None:
        """FUTURO: en wpliberaruta.aspx, elegir turno, CHECKALL y 'Liberar Rutas'.
        No se implementa todavía a pedido de Fran (no se necesita por ahora)."""
        raise NotImplementedError("Fase B (liberar rutas) aún no implementada")

    # ------------------------------------------------------------- genéricos
    def _check_gx(self, selector: str) -> None:
        loc = self._page.locator(selector)
        loc.wait_for(state="attached", timeout=config.TIMEOUT_ACCION_MS)
        try:
            loc.scroll_into_view_if_needed(timeout=5_000)
        except Exception:  # noqa: BLE001
            pass
        try:
            loc.check(timeout=8_000)
        except Exception:  # noqa: BLE001
            loc.check(force=True, timeout=8_000)
        if not loc.is_checked():  # con force el tilde puede no aplicarse
            raise RuntimeError(f"checkbox {selector} no quedó tildado")

    def _click_gx(self, selector: str) -> None:
        loc = self._page.locator(selector)
        loc.wait_for(state="attached", timeout=config.TIMEOUT_ACCION_MS)
        try:
            loc.scroll_into_view_if_needed(timeout=3_000)
        except Exception:  # noqa: BLE001
            pass
        try:
            loc.click(timeout=6_000)
        except Exception:  # noqa: BLE001
            loc.click(force=True, timeout=6_000)

    def _aceptar_confirm(self) -> None:
        """Acepta el modal de confirmación de K2BTools ('¿Está seguro?')."""
        try:
            self._page.locator("input.K2BT_ConfirmDialogOk").click(timeout=10_000)
        except Exception:  # noqa: BLE001
            log.debug("No apareció el confirm K2BTools (puede no aplicar).")

    def cerrar(self) -> None:
        """Cierre forzado secuencial: los websockets de GX cuelgan el cierre."""
        for accion in (lambda: self._ctx.close(),
                       lambda: self._browser.close(),
                       lambda: self._pw.stop()):
            try:
                accion()
            except Exception:  # noqa: BLE001
                pass


class MockPortal:
    """Simula el portal para validar orquestación y flujo con Sheets.
    Convenciones de prueba:
      - localidad 'VACIA' simula filtro sin resultados (0 rutas).
      - colector que empieza con 'X' simula colector no encontrado.
      - ruta '9999' simula que quedó sin asignar (verificación post-asignar).
      - 'Total Leer' de cada ruta: 25; contador de una localidad entera: 175."""

    def __init__(self) -> None:
        self._turno: str = ""
        self._restantes: int = 0

    def ir_a_asignacion(self) -> None:
        log.info("[MOCK] abrir página de asignación")

    def seleccionar_turno(self, turno: str) -> None:
        self._turno = str(turno).strip()
        log.info("[MOCK] turno %s seleccionado", turno)

    def recuperar(self, turno: str) -> None:
        log.info("[MOCK] recarga + turno %s", turno)
        self.seleccionar_turno(turno)

    def tildar_rutas(self, pares: list) -> tuple[set, dict]:
        tildadas, cantidades = set(), {}
        for r, loc in pares:
            clave = _clave_rl(r, loc, self._turno)
            tildadas.add(clave)
            cantidades[clave] = 25
        log.info("[MOCK] tildadas: %s", sorted(tildadas))
        return tildadas, cantidades

    def filtrar_localidad(self, localidad: str) -> int:
        n = 0 if _norm(localidad) == "vacia" else 7
        self._restantes = 0
        log.info("[MOCK] filtro localidad %r: %d rutas", localidad, n)
        return n

    def tildar_todas(self) -> int | None:
        log.info("[MOCK] CHECKALL tildado: 175 suministros")
        return 175

    def destildar_visibles(self) -> int:
        log.info("[MOCK] destildar residuales: %d", self._restantes)
        return self._restantes

    def asignar_colector(self, colector: str,
                         pares: list | None = None) -> tuple[ResultadoAsignacion, set]:
        if _norm(colector).startswith("x"):
            return (ResultadoAsignacion.COLECTOR_NO_ENCONTRADO,
                    {_clave_rl(r, loc, self._turno) for r, loc in pares} if pares else set())
        no_asignadas = ({_clave_rl(r, loc, self._turno) for r, loc in (pares or [])
                         if str(r).strip() == "9999"})
        log.info("[MOCK] asignadas al colector %s (no asignadas: %s)",
                 colector, sorted(no_asignadas))
        return ResultadoAsignacion.OK, no_asignadas

    def filas_libres_visibles(self) -> int:
        return self._restantes

    def liberar_rutas_turno(self, turno: str) -> None:
        log.info("[MOCK] Fase B no implementada")

    def cerrar(self) -> None:
        pass
