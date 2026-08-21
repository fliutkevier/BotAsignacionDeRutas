# BOT de Asignaciones de Rutas

Automatiza la asignación de rutas a colectores en el portal de lecturas
(GeneXus + K2BTools sobre ASP.NET), tomando el trabajo pendiente de una planilla
de Google Sheets y devolviendo el resultado a la misma planilla.

El supervisor carga qué rutas o localidades van a qué colector, ejecuta el bot y
mira la columna `ESTADO` de la planilla: cada fila termina en `asignada` o con el
motivo por el que no se pudo.

---

## Cómo funciona

El bot lee las filas pendientes de **dos hojas** del mismo archivo de Sheets y
las procesa en este orden:

1. **`AsignacionPorRuta`** — rutas puntuales. Se agrupan por turno y colector, se
   tildan todas las rutas del grupo y se hace **una sola** asignación por
   colector.
2. **`AsignacionPorLocalidad`** — localidades enteras. Se filtra la grilla por
   localidad, se tilda todo con el CHECKALL y se asigna.

El orden importa: si las localidades fueran primero, el CHECKALL se llevaría
rutas que estaban destinadas puntualmente a otro colector. Yendo por ruta
primero, la localidad se lleva solo las que quedaron libres.

Todo ocurre en **una sola navegación**. El turno se re-selecciona únicamente
cuando cambia (y eso además sirve de reset: destilda y limpia el filtro).

### Verificación

El bot no da por asignada una ruta porque el click haya salido bien:

- **Por ruta**: tras asignar, re-lee la grilla de libres. Solo las rutas que
  **desaparecieron** cuentan como asignadas; las que siguen ahí se marcan con
  error y se destildan.
- **Por localidad**: con el filtro todavía puesto, la grilla de libres tiene que
  quedar en cero. Lo que quedó visible no se asignó, y la fila queda como
  `asignada parcial`.

---

## La planilla

Los headers se leen normalizados (MAYÚSCULAS, sin espacios de más), así que
podés tener **columnas extra o en otro orden** sin romper nada. Lo único que
rompe es renombrar una columna requerida.

### Hoja `AsignacionPorRuta`

| Columna | Obligatoria | Notas |
|---|---|---|
| `TURNO` | sí | |
| `RUTA` | sí | |
| `LOCALIDAD` | depende | **Obligatoria en turno 43**, donde desempata rutas con el mismo número. **En los demás turnos se ignora**: la ruta ya es única, así que lo que digas acá no afecta la búsqueda (podés dejarla vacía o usarla como referencia tuya). |
| `COLECTOR` | sí | Se busca en el desplegable por nombre: exacto primero, si no "contiene". |
| `ESTADO` | sí | El bot lee y escribe acá. |
| `CANTIDAD` | no | Si la columna no existe, se avisa y sigue. |

### Hoja `AsignacionPorLocalidad`

| Columna | Obligatoria | Notas |
|---|---|---|
| `TURNO` | sí | |
| `LOCALIDAD` | sí | Es el texto que se escribe en el filtro de la grilla. |
| `COLECTOR` | sí | Igual que arriba. |
| `ESTADO` | sí | |
| `CANTIDAD` | no | |

### Normalización de rutas

El bot completa con ceros a la izquierda según el turno:

- **Turno 43** → 3 dígitos (`920`)
- **Resto** → 4 dígitos (`530` → `0530`)

O sea que podés escribir `530` en la planilla sin preocuparte por el formato.

### Estados

| Valor | Significado |
|---|---|
| *(vacío)* o `pendiente` | El bot toma la fila. **Cualquier otro valor la ignora.** |
| `preparando` | Se escribe antes de empezar con esa fila. Si quedó así, la corrida se cortó a la mitad. |
| `asignada` | Terminal. Verificado contra la grilla. |
| `asignada parcial (N ruta(s) quedaron libres)` | La localidad se asignó pero N rutas quedaron sin asignar. |
| `no se pudo asignar: <motivo>` | El motivo dice qué pasó (falta colector, ruta no encontrada, colector ambiguo, página bloqueada, etc.). |

Para reprocesar una fila, borrá su `ESTADO` o ponelo en `pendiente`.

### La columna `CANTIDAD`

Es opcional y sale de dos lugares distintos según la hoja:

- **Por ruta**: la columna **"Total Leer"** de esa fila de la grilla, leída
  antes de tildarla.
- **Por localidad**: el campo **"Suministros Seleccionados"** después del
  CHECKALL, o sea el total del lote.

Si no se pudo leer, la celda **queda como estaba**: no se escribe un cero, que
en la planilla se leería como "esta ruta no tiene suministros".

---

## Requisitos

- **Windows** con la **VPN conectada** antes de ejecutar. Es precondición: el
  bot no la levanta ni la verifica.
- **Python 3.11+** (solo para correr desde el código; el `.exe` no lo necesita).
- **`credentials.json`** — clave de una service account de Google con la API de
  Sheets habilitada, ubicada junto a `main.py` (o junto al `.exe`).
- La planilla tiene que estar **compartida con el email de la service account**,
  con permiso de edición.

```bash
pip install -r requirements.txt
python -m playwright install firefox
```

---

## Ejecución

```bash
python main.py
```

| Flag | Para qué |
|---|---|
| *(sin flags)* | Ventana de Firefox visible. Podés minimizarla y seguir usando la máquina: el bot no toca tu mouse ni tu teclado. |
| `--headless` | Sin ventana. Para lanzarlo y olvidarse; el avance se sigue por consola y por `logs/bot.log`. |
| `--mock` | Sin portal ni VPN. Valida la orquestación y la escritura en Sheets. **Escribe en la planilla real.** |

Mientras corre: **no cierres la ventana de Firefox** que abre el bot y no le
toques nada a mano (cambiar el turno o escribir en el filtro lo descoloca). Que
la máquina no se suspenda, o se cae la VPN.

Convenciones del modo `--mock`: localidad `VACIA` simula filtro sin resultados,
un colector que empieza con `X` simula colector no encontrado, y la ruta `9999`
simula una ruta que quedó sin asignar.

---

## Distribución a los supervisores

```bash
build.bat
```

El supervisor descomprime y ejecuta `EJECUTAR BOT.bat`. No necesita
Python ni Playwright, pero **sí la VPN**.

Dos detalles que el `.bat` resuelve y no son obvios:

- **Firefox viaja adentro del paquete.** Playwright normalmente busca el
  navegador en el perfil del usuario, que en la máquina del supervisor no
  existe. El build lo instala con `PLAYWRIGHT_BROWSERS_PATH=0` (queda dentro de
  `site-packages\playwright\`) y `config.py` repone esa variable al ejecutar el
  `.exe`. Hacen falta las dos mitades.
- **`credentials.json` va suelto al lado del `.exe`**, no adentro, porque es ahí
  donde `config.py` lo busca.

> ⚠️ El zip incluye la clave privada de la service account. Quien tenga la
> carpeta puede escribir en la planilla. Si eso no es aceptable, hay que mover
> el `credentials.json` a una ruta de red y apuntar `config.GOOGLE_CREDENTIALS`
> ahí.

---

## Detalles de implementación

Tres problemas concretos que el bot resuelve y conviene conocer antes de tocarlo:

### El mask

Mientras hay un overlay de carga (`div.gx-mask`), el portal **descarta todos los
clicks**. Por eso hay un `_sin_mask()` antes de cada interacción.

Si el mask no se va, la página quedó clavada y seguir sería clickear al vacío
(marcando como asignado algo que nunca se tildó). En ese caso el bot **recarga
la página, re-selecciona el turno y rehace el bloque entero** — entero y no
desde donde quedó, porque la recarga pierde todos los tildes. Si tras las
recargas sigue bloqueada, marca esas filas y sigue con el resto en vez de
abortar la corrida. Se configura con `RECARGAS_POR_MASK`.

### La cuota de Google Sheets

Sheets permite **60 escrituras por minuto por usuario**, y escribir celda por
celda agotaba la cuota a mitad de corrida (error 429). Las escrituras se
**encolan y se mandan en lote**: un `batch_update` es una sola escritura para N
celdas. Se vuelcan antes de cada tramo largo en el portal (para que el avance se
vea) y al terminarlo (para no perder resultados). Además, todo pedido reintenta
con espera creciente ante 429 o errores transitorios.

### Los re-renders de la grilla

Tildar una fila dispara un re-render que puede **correr los índices o destildar
filas ya tildadas**. Por eso cada tilde se verifica, hay una pasada final de
verificación antes de accionar, y los datos de la fila (como "Total Leer") se
leen **antes** de tildar, mientras el índice todavía es válido.

Otras reglas heredadas del portal: nunca usar `networkidle` (los websockets de
GeneXus no drenan nunca) — el fin del ASIGNAR se detecta por el cartel flotante
`K2BT_MessageText`.

---

## Estructura

```
main.py            Entry point: flags, logging, lee pendientes y arranca.
config.py          Toda la configuración: URLs, timeouts, nombres de columnas.
models.py          CasoRuta, CasoLocalidad, Estado y normalización de rutas.
orchestrator.py    Orden de procesamiento, validaciones y recuperación por mask.
portal.py          Playwright contra el portal. Portal (real) y MockPortal.
sheets_client.py   Lectura/escritura de las dos hojas, en lote y con reintentos.
build.bat          Genera dist\BOT_Asignaciones\ para repartir.
```

`config.py` no tiene nada hardcodeado en el resto del código: URLs, timeouts,
nombres de hojas y de columnas se cambian todos desde ahí.

---

## Limitaciones conocidas

- Las filas pendientes se leen **una sola vez al arrancar**. Lo que agregues a la
  planilla mientras corre no entra en esa corrida.
- **Fase B (liberar rutas)** en `wpliberaruta.aspx` está como stub sin
  implementar.
- Si la misma ruta física aparece con **dos colectores distintos**, el bot no
  elige: marca ambas filas como conflicto y no las procesa.
