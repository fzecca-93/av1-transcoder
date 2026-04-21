# Documentación del Proyecto: Automatización AV1 (TranscoderApp)

Esta es una documentación técnica generada por Inteligencia Artificial estructurada para resumir la lógica, la arquitectura interna del sistema de transcodificación y registro de versiones actualizadas. 

---

## 1. Misión General del Sistema
**AV1 Ultra Transcoder & Library Manager** tiene como objetivo escanear, catalogar y transformar de manera masiva carpetas completas de contenido multimedia (típicamente desde un Servidor NAS) para reducir de forma drástica el espacio en disco mediante la codificación acelerada por GPU (`nvenc_av1_10bit`). 

Utiliza la potencia de **HandBrakeCLI** para cambiar de formato mientras mantiene al máximo la resolución, audio y subtítulos originales; al mismo tiempo provee una interfaz gráfica amigable (usando `customtkinter`) que rastrea inteligentemente progresos, listas de colas y estadísticas de MB/GB ahorrados.

---

## 2. Decisiones de Arquitectura Principal

### A. Base de Datos Persistente
Para no recargar los discos del disco de red (NAS) escaneando las propiedades de video en cada inicio, el programa utiliza una caché de JSON (ej: `biblioteca_vistos.json`). Allí dentro de un mapa de rutas, se almacenan los estados de cada archivo:
- `mtime`: Marca de tiempo de su última modificación.
- `is_av1`: Controla su destino final (`True`, `False`, `None` e incluso literales de descarte como `"NO_TRANSCODIFICAR"`).
- `seen`: Indica que fue revisado por el transcodificador principal.
- `identified_by`: Demuestra cómo el sistema determinó su estado (Por Nombre, MediaInfo, Reporte Jellyfin).

### B. Sistema Asíncrono e Interfaz (Threading & Queues)
Toda la lógica "pesada" (Handbrake, escaneo masivo, lectura MediaInfo) es enviada a un subproceso (`threading.Thread`) o paralelizada globalmente a través de `ThreadPoolExecutor(max_workers=4)`.
Para proteger la integridad de `Tkinter` (CustomTkinter) que por naturaleza debe actualizarse en el hilo principal, se utiliza el patrón concurrente moderno `Task-Queue`: Todas las métricas, barras de progreso y descripciones emiten sus "señales de actualización" introduciéndolas dentro de un hilo global `self.update_queue`, el cual es vaciado (Polling) por el hilo principal cíclicamente cada `100ms` usando el loop base `self.after(100, self.process_queue)`.

### C. Múltiples Fuentes de Escaneo Inteligente
El programa evita la lectura brutal de todos los metadatos dando tres opciones:
1. **Búsqueda Incremental:** Recorre el NAS (`os.walk`) sumando archivos nuevos pero confiando ciegamente a nivel inicial en sí el nombre dice "AV1". No descarga headers de archivos remotos, lo cual le es instantáneo.
2. **Revisión Profunda (MediaInfo/ffprobe):** Verifica desde cabeceras multimedia qué códec real tiene cada archivo dudoso o importado y lo establece en la base de datos de manera explícita.
3. **Importación Externa:** Se vale de un `*.xlsx` renderizado externamente por plataformas como Jellyfin para nutrir la base de datos velozmente emparejando por nombre de archivo.

---

## 3. Lógica Incorporada y Actualizaciones Recientes (Realizadas por la IA)

A lo largo del código agregué flujos críticos de calidad de vida técnica (QoL) basándome en casos extremos descritos:

### 3.1. Prevención de Suspensión Nativa en Windows (Ctypes)
Dado que transformar videos requiere horas incontables de procesamiento silencioso; implementé `ctypes.windll.kernel32.SetThreadExecutionState`. 
- **¿Qué logra?** Al arrancar el procesamiento se inyectan en Windows los flags en memoria `ES_CONTINUOUS | ES_SYSTEM_REQUIRED` notificando explícitamente a Windows de no bloquearse, no apagar los discos ni irse a dormir en la noche. Al terminar de procesar su cola o detenerse manualmente, se liberan y restauran sus estados permitiendo de nuevo la hibernación programada de energía. Todo ocurre encapsulado en operaciones controladas `try...finally`.

### 3.2. Regla de Impacto Mínimo (Límite del 90%)
Se incluyó un filtro heurístico en `process_single_file` para evitar perder calidad cuando el proceso no trae buenos beneficios de tamaño en disco.
- **La Regla:** Tras culminar de re-codificar por medio del motor de NVIDIA `HandBrakeCLI`, el peso exacto final local se compara contra el original en el servidor. Si el archivo local no logró recudir sus bits un **Mínimo del 10%** (es decir, el MB de resultado es el 90% o más que el tamaño inicial), el sistema marca el archivo silenciosamente como `"NO_TRANSCODIFICAR"` en la base de datos y se revoca, auto-destruyéndose el resultado `.AV1` fallido del almacenamiento local. Pasará a verse etiquetado de tono purpura **"OMITIDO"** y sin volver a entrometerse la cola diaria.

### 3.3. Vista Árbol con Colores AV1 por Carpeta (Claude, sesión 7)
Se agregó un toggle **"Vista: Lista / Vista: Árbol"** en la barra de filtros.
- **Árbol**: Organiza los archivos de la librería en jerarquía de carpetas relativa al `input_dir`. Cada carpeta muestra estadísticas recursivas (`av1/total AV1`) y se colorea:
  - 🟢 Verde (`#2ecc71`): todos los archivos conocidos son AV1.
  - 🔴 Rojo (`#e74c3c`): ningún archivo conocido es AV1.
  - 🟡 Amarillo (`#f39c12`): mezcla de AV1 y no-AV1.
  - ⬜ Gris (`#7f8c8d`): todos sin información.
- El menú contextual (clic derecho) funciona en ambos modos; en árbol ignora los ítems de carpeta automáticamente.
- **Tamaño de archivo**: se agregó columna "TAMAÑO" en la biblioteca. El tamaño se almacena en la DB (`"size"` en bytes) al escanear o al analizar con MediaInfo. Se muestra en KB/MB/GB. Los nodos de carpeta en vista árbol muestran el tamaño total acumulado del subárbol.

### 3.4. Barra de progreso de HandBrake reparada (Claude, sesión 7)
El regex `r"Progress: (\d+\.\d+) %"` no coincidía con la salida real de HandBrake CLI (`"Encoding: task 1 of 1, 5.24 %"`). Corregido a `r"(\d+\.\d+) %"`.

### 3.5. Refresco automático de la biblioteca al terminar la cola (Claude, sesión 7)
El evento `"finished"` en `process_queue` ahora llama a `render_library()` y `_update_lib_count()` para que los estados actualizados (incluyendo `NO_TRANSCODIFICAR`) sean visibles inmediatamente sin acción del usuario.

### 3.3. Estado "NO EN ORIGEN" y Deploy Inverso al NAS.
Originalmente el flujo final era transcodificar y mover al momento; pero cuando el *"Destino Local (GPU)"* se configuro distinto a la fuente de *Origen NAS*, los AV1 tendían a quedarse aislados en un SSD veloz pero desvinculados de su hogar permanente.
- **Implementación**: Se integró en tiempo de lectura sobre la tabla local `self.get_expected_local_output(ruta)`. Esta función detecta si el transcodificado exitoso ya subyace pacíficamente en una carpeta de control local pero el metadato del NAS no ha sido actualizado físicamente.
- **Solución Visual de un click:** Etiquetara su fila como *"LISTO LOCAL / NO EN ORIGEN"*(Amarillo). Y a través del menú de botón secundario ofrece un automatizado `self.move_to_nas_worker`. Ese nuevo código envía sigilosamente el archivo completado directo a su carpeta padre en el NAS, extrae e invoca `src.unlink()` para asesinar permanentemente el viejo formato en la NAS, e indexar en la BD que ahora está plenamente migrado e instalado (`"Mover a NAS"`). Todo eso validándose con un nuevo filtro de búsqueda UI llamado ["LISTO LOCAL"].
