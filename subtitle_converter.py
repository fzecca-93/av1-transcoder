import customtkinter as ctk
import threading
import queue
import subprocess
import shutil
import json
import re
from pathlib import Path
from datetime import datetime
from tkinter import filedialog

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

SUPPORTED_VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.ts', '.m4v', '.wmv'}
CONFIG_FILE = Path(__file__).parent / "subtitle_converter_config.json"
_CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class SubtitleConverterApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Conversor de Subtítulos ASS → SRT")
        self.geometry("950x720")
        self.minsize(700, 520)

        self._ffprobe_cache = None
        self._ffmpeg_cache = None
        self.update_queue = queue.Queue()
        self.is_scanning = False
        self.is_converting = False
        self._stop_flag = False
        self.scan_items = []

        self._load_config()
        self._setup_ui()
        self.process_queue()

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self):
        try:
            if CONFIG_FILE.exists():
                data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                self._last_folder = data.get("last_folder", "")
            else:
                self._last_folder = ""
        except Exception:
            self._last_folder = ""

    def _save_config(self):
        try:
            CONFIG_FILE.write_text(
                json.dumps({"last_folder": self.folder_var.get()}, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
        except Exception:
            pass

    # ── UI Setup ──────────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # Top: folder selector
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, padx=12, pady=(12, 4), sticky="ew")
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top, text="Carpeta:", width=70).grid(row=0, column=0, padx=(0, 6))
        self.folder_var = ctk.StringVar(value=self._last_folder)
        ctk.CTkEntry(top, textvariable=self.folder_var,
                     placeholder_text="Selecciona una carpeta...").grid(
            row=0, column=1, sticky="ew", padx=(0, 6))
        ctk.CTkButton(top, text="Buscar", width=90,
                      command=self.browse_folder).grid(row=0, column=2)

        # Options
        opt = ctk.CTkFrame(self)
        opt.grid(row=1, column=0, padx=12, pady=4, sticky="ew")
        opt.grid_columnconfigure((0, 1, 2), weight=1)

        self.opt_external = ctk.BooleanVar(value=True)
        self.opt_embedded = ctk.BooleanVar(value=True)
        self.opt_delete   = ctk.BooleanVar(value=False)

        ctk.CTkCheckBox(opt, text="Subtítulos externos (.ass/.ssa → .srt)",
                        variable=self.opt_external).grid(row=0, column=0, padx=16, pady=8, sticky="w")
        ctk.CTkCheckBox(opt, text="Extraer embebidos en vídeos (.ass → .srt)",
                        variable=self.opt_embedded).grid(row=0, column=1, padx=16, pady=8, sticky="w")
        ctk.CTkCheckBox(opt, text="Eliminar .ass/.ssa originales tras convertir",
                        variable=self.opt_delete).grid(row=0, column=2, padx=16, pady=8, sticky="w")

        # Buttons
        btn = ctk.CTkFrame(self, fg_color="transparent")
        btn.grid(row=2, column=0, padx=12, pady=4, sticky="ew")

        self.btn_scan = ctk.CTkButton(btn, text="ESCANEAR", width=130, command=self.start_scan)
        self.btn_scan.pack(side="left", padx=(0, 8))
        self.btn_convert = ctk.CTkButton(btn, text="CONVERTIR", width=130,
                                         state="disabled", command=self.start_convert)
        self.btn_convert.pack(side="left", padx=(0, 8))
        self.btn_stop = ctk.CTkButton(btn, text="DETENER", width=130, state="disabled",
                                      fg_color="#c0392b", hover_color="#922b21",
                                      command=self.request_stop)
        self.btn_stop.pack(side="left", padx=(0, 8))
        self.status_label = ctk.CTkLabel(btn, text="", text_color="gray")
        self.status_label.pack(side="left", padx=12)

        # Main area: file list + log
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=3, column=0, padx=12, pady=4, sticky="nsew")
        main.grid_columnconfigure(0, weight=3)
        main.grid_columnconfigure(1, weight=2)
        main.grid_rowconfigure(0, weight=1)

        # File list panel
        lf = ctk.CTkFrame(main)
        lf.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        lf.grid_rowconfigure(1, weight=1)
        lf.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(lf, text="Archivos encontrados",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, pady=(8, 2))
        self.file_list_frame = ctk.CTkScrollableFrame(lf)
        self.file_list_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.file_list_frame.grid_columnconfigure(0, weight=1)
        self._row_widgets = []

        # Log panel
        logf = ctk.CTkFrame(main)
        logf.grid(row=0, column=1, sticky="nsew")
        logf.grid_rowconfigure(1, weight=1)
        logf.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(logf, text="Log",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, pady=(8, 2))
        self.log_text = ctk.CTkTextbox(logf, state="disabled", wrap="word")
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))

        # Progress bar
        prog = ctk.CTkFrame(self, fg_color="transparent")
        prog.grid(row=4, column=0, padx=12, pady=(0, 10), sticky="ew")
        prog.grid_columnconfigure(0, weight=1)
        self.progress_bar = ctk.CTkProgressBar(prog)
        self.progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.progress_bar.set(0)
        self.progress_label = ctk.CTkLabel(prog, text="0/0", width=60)
        self.progress_label.grid(row=0, column=1)

    # ── Folder ────────────────────────────────────────────────────────────────

    def browse_folder(self):
        folder = filedialog.askdirectory(title="Seleccionar carpeta")
        if folder:
            self.folder_var.set(folder)
            self._save_config()

    # ── Tool detection ────────────────────────────────────────────────────────

    def _find_ffprobe(self):
        if self._ffprobe_cache:
            return self._ffprobe_cache
        script_dir = Path(__file__).parent
        for name in ("ffprobe.exe", "ffprobe"):
            c = script_dir / name
            if c.exists():
                self._ffprobe_cache = str(c)
                return self._ffprobe_cache
        self._ffprobe_cache = shutil.which("ffprobe")
        return self._ffprobe_cache

    def _find_ffmpeg(self):
        if self._ffmpeg_cache:
            return self._ffmpeg_cache
        script_dir = Path(__file__).parent
        for name in ("ffmpeg.exe", "ffmpeg"):
            c = script_dir / name
            if c.exists():
                self._ffmpeg_cache = str(c)
                return self._ffmpeg_cache
        self._ffmpeg_cache = shutil.which("ffmpeg")
        return self._ffmpeg_cache

    # ── Scan ──────────────────────────────────────────────────────────────────

    def start_scan(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            self.log("Selecciona una carpeta válida primero.")
            return
        if self.is_scanning or self.is_converting:
            return
        self._save_config()
        self.is_scanning = True
        self._stop_flag = False
        self.scan_items = []
        self._clear_file_list()
        self.btn_scan.configure(state="disabled")
        self.btn_convert.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress_bar.set(0)
        self.progress_label.configure(text="0/0")
        self.status_label.configure(text="Escaneando...")
        self.log(f"Escaneando: {folder}")
        threading.Thread(target=self._scan_worker, args=(Path(folder),), daemon=True).start()

    def _scan_worker(self, root: Path):
        found = []
        try:
            all_files = sorted(root.rglob("*"))
            for f in all_files:
                if self._stop_flag:
                    break
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                if ext in {'.ass', '.ssa'} and self.opt_external.get():
                    item = {"path": f, "type": "external", "streams": [], "status": "Pendiente"}
                    found.append(item)
                    self.update_queue.put(("add_item", item))
                elif ext in SUPPORTED_VIDEO_EXTENSIONS and self.opt_embedded.get():
                    streams = self._detect_embedded_ass(f)
                    if streams:
                        item = {"path": f, "type": "embedded", "streams": streams, "status": "Pendiente"}
                        found.append(item)
                        self.update_queue.put(("add_item", item))
        except Exception as e:
            self.update_queue.put(("log", f"[{_ts()}] Error en escaneo: {e}\n"))

        self.scan_items = found
        self.update_queue.put(("log", f"[{_ts()}] Escaneo completo: {len(found)} elemento(s).\n"))
        self.update_queue.put(("scan_done", len(found)))

    def _detect_embedded_ass(self, video: Path) -> list:
        ffprobe = self._find_ffprobe()
        if not ffprobe:
            return []
        try:
            result = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "s", str(video)],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                creationflags=_CREATE_NO_WINDOW
            )
            data = json.loads(result.stdout or "{}")
            streams = []
            for s in data.get("streams", []):
                if s.get("codec_name", "").lower() in {"ass", "ssa"}:
                    streams.append({
                        "index": s["index"],
                        "lang": s.get("tags", {}).get("language", "")
                    })
            return streams
        except Exception:
            return []

    # ── Convert ───────────────────────────────────────────────────────────────

    def start_convert(self):
        if not self.scan_items:
            self.log("No hay nada que convertir. Escanea primero.")
            return
        if self.is_converting:
            return
        self.is_converting = True
        self._stop_flag = False
        self.btn_scan.configure(state="disabled")
        self.btn_convert.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress_bar.set(0)
        self.progress_label.configure(text=f"0/{len(self.scan_items)}")
        self.status_label.configure(text="Convirtiendo...")
        threading.Thread(target=self._convert_worker, daemon=True).start()

    def _convert_worker(self):
        total = len(self.scan_items)
        done = errors = 0

        for i, item in enumerate(self.scan_items):
            if self._stop_flag:
                self.update_queue.put(("log", f"[{_ts()}] Detenido por el usuario.\n"))
                break
            path: Path = item["path"]
            try:
                if item["type"] == "external":
                    dst = path.with_suffix(".srt")
                    self.update_queue.put(("log", f"[{_ts()}] Convirtiendo: {path.name}\n"))
                    self._convert_ass_to_srt(path, dst)
                    if self.opt_delete.get():
                        path.unlink()
                        self.update_queue.put(("log", f"[{_ts()}]   Eliminado: {path.name}\n"))
                    item["status"] = "Listo"
                    done += 1
                elif item["type"] == "embedded":
                    self.update_queue.put(("log", f"[{_ts()}] Extrayendo de: {path.name}\n"))
                    ok = self._extract_embedded_subs(path, item["streams"])
                    if ok:
                        item["status"] = "Listo"
                        done += 1
                    else:
                        item["status"] = "Error"
                        errors += 1
            except Exception as e:
                self.update_queue.put(("log", f"[{_ts()}]   Error en {path.name}: {e}\n"))
                item["status"] = "Error"
                errors += 1

            self.update_queue.put(("progress", (i + 1, total, item)))

        self.update_queue.put(("log", f"[{_ts()}] Finalizado: {done} OK, {errors} error(es).\n"))
        self.update_queue.put(("convert_done", None))

    def _extract_embedded_subs(self, video: Path, streams: list) -> bool:
        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            self.update_queue.put(("log", f"[{_ts()}]   ffmpeg no encontrado.\n"))
            return False

        lang_count: dict = {}
        for s in streams:
            k = s["lang"] or "und"
            lang_count[k] = lang_count.get(k, 0) + 1
        lang_seen: dict = {}

        success = True
        for s in streams:
            lang = s["lang"] or "und"
            lang_seen[lang] = lang_seen.get(lang, 0) + 1

            if lang_count[lang] > 1:
                suffix = f".{lang}{lang_seen[lang]}.srt"
            elif lang and lang != "und":
                suffix = f".{lang}.srt"
            else:
                suffix = ".srt"

            dst = video.with_name(video.stem + suffix)
            try:
                result = subprocess.run(
                    [ffmpeg, "-i", str(video),
                     "-map", f"0:{s['index']}",
                     "-c:s", "srt", str(dst), "-y"],
                    capture_output=True, text=True, timeout=120,
                    encoding="utf-8", errors="replace",
                    creationflags=_CREATE_NO_WINDOW
                )
                if result.returncode == 0:
                    self.update_queue.put(("log", f"[{_ts()}]   Extraído: {dst.name}\n"))
                else:
                    self.update_queue.put(("log", f"[{_ts()}]   Error ffmpeg [{result.returncode}]: {dst.name}\n"))
                    success = False
            except Exception as e:
                self.update_queue.put(("log", f"[{_ts()}]   Excepción extrayendo {dst.name}: {e}\n"))
                success = False

        return success

    def _convert_ass_to_srt(self, src: Path, dst: Path):
        def ass_time_to_srt(t):
            h, m, s_cs = t.split(':')
            s, cs = s_cs.split('.')
            return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(cs) * 10:03d}"

        def strip_ass_tags(text):
            text = re.sub(r'\{[^}]*\}', '', text)
            return text.replace('\\N', '\n').replace('\\n', '\n').replace('\\h', ' ').strip()

        raw = src.read_text(encoding='utf-8-sig', errors='replace').splitlines()
        in_events = False
        start_idx, end_idx, text_idx = 1, 2, 9
        dialogues = []

        for line in raw:
            stripped = line.strip()
            if stripped == '[Events]':
                in_events = True
                continue
            if in_events:
                if stripped.startswith('['):
                    break
                if stripped.startswith('Format:'):
                    parts = [p.strip() for p in stripped[7:].split(',')]
                    try:
                        start_idx = parts.index('Start')
                        end_idx   = parts.index('End')
                        text_idx  = parts.index('Text')
                    except ValueError:
                        pass
                    continue
                if stripped.startswith('Dialogue:'):
                    parts = stripped[9:].split(',', text_idx)
                    if len(parts) > text_idx:
                        try:
                            start = ass_time_to_srt(parts[start_idx].strip())
                            end   = ass_time_to_srt(parts[end_idx].strip())
                            text  = strip_ass_tags(parts[text_idx])
                            if text:
                                dialogues.append((parts[start_idx].strip(), start, end, text))
                        except Exception:
                            pass

        dialogues.sort(key=lambda x: x[0])
        srt_blocks = [f"{i}\n{s} --> {e}\n{t}\n"
                      for i, (_, s, e, t) in enumerate(dialogues, 1)]
        dst.write_text('\n'.join(srt_blocks), encoding='utf-8')

    def request_stop(self):
        self._stop_flag = True
        self.btn_stop.configure(state="disabled")
        self.status_label.configure(text="Deteniendo...")

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _clear_file_list(self):
        for w in self._row_widgets:
            w.destroy()
        self._row_widgets = []

    def _add_file_row(self, item: dict):
        row = len(self._row_widgets)
        bg = "#2b2b2b" if row % 2 == 0 else "#242424"
        frame = ctk.CTkFrame(self.file_list_frame, fg_color=bg, corner_radius=4)
        frame.grid(row=row, column=0, sticky="ew", pady=1)
        frame.grid_columnconfigure(0, weight=1)

        path: Path = item["path"]
        icon = "📄" if item["type"] == "external" else "🎬"
        text = str(path)
        if len(text) > 72:
            text = "…" + text[-72:]
        if item["type"] == "embedded":
            text += f"  [{len(item['streams'])} stream(s)]"

        ctk.CTkLabel(frame, text=f"{icon} {text}", anchor="w",
                     font=ctk.CTkFont(family="Courier", size=11)).grid(
            row=0, column=0, sticky="ew", padx=8, pady=2)

        lbl = ctk.CTkLabel(frame, text="Pendiente", width=80,
                           text_color="gray", anchor="e",
                           font=ctk.CTkFont(size=11))
        lbl.grid(row=0, column=1, padx=8)
        item["_lbl"] = lbl
        self._row_widgets.append(frame)

    def _update_item_status(self, item: dict):
        lbl = item.get("_lbl")
        if not lbl:
            return
        status = item["status"]
        if status == "Listo":
            color, display = "#27ae60", "✓ Listo"
        elif status == "Error":
            color, display = "#c0392b", "✗ Error"
        else:
            color, display = "gray", status
        lbl.configure(text=display, text_color=color)

    def log(self, msg: str):
        self.update_queue.put(("log", f"[{_ts()}] {msg}\n"))

    def process_queue(self):
        try:
            while True:
                task, data = self.update_queue.get_nowait()
                if task == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", data)
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif task == "add_item":
                    self._add_file_row(data)
                elif task == "progress":
                    current, total, item = data
                    self._update_item_status(item)
                    self.progress_bar.set(current / total if total else 0)
                    self.progress_label.configure(text=f"{current}/{total}")
                elif task == "scan_done":
                    self.is_scanning = False
                    self.btn_scan.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.btn_convert.configure(state="normal" if data > 0 else "disabled")
                    self.status_label.configure(text=f"{data} elemento(s)" if data else "Sin resultados")
                elif task == "convert_done":
                    self.is_converting = False
                    self.btn_scan.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.status_label.configure(text="Completado")
        except queue.Empty:
            pass
        self.after(100, self.process_queue)


def _ts() -> str:
    return datetime.now().strftime('%H:%M:%S')


if __name__ == "__main__":
    app = SubtitleConverterApp()
    app.mainloop()
