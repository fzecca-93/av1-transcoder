import os
import sys
import shutil
import subprocess
import time
import threading
import queue
import re
import json
import ctypes
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
import customtkinter as ctk

try:
    from pymediainfo import MediaInfo
except ImportError:
    print("\n[ERROR] pymediainfo no está instalado.")
    sys.exit(1)

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

SUPPORTED_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.ts', '.wmv', '.m4v'}
SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.sub', '.vtt'}
HANDBRAKE_CLI_PATH = "HandBrakeCLI"
NETWORK_RETRY_DELAY = 10
MAX_RETRIES = 3
CONFIG_FILE = "config.json"


def get_db_filename(library_name):
    safe = re.sub(r'[^\w\-]', '_', library_name)
    return f"biblioteca_{safe}.json"


def _format_size(size_bytes):
    if not size_bytes or size_bytes <= 0:
        return "N/A"
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024 ** 3:.1f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / 1024 ** 2:.0f} MB"
    return f"{size_bytes / 1024:.0f} KB"


class TranscoderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AV1 Ultra Transcoder & Library Manager")
        self.geometry("1100x850")

        self.input_dir = tk.StringVar(value="")
        self.output_dir = tk.StringVar(value="")
        self.encode_mode   = tk.StringVar(value="normal")   # "normal" | "anime"
        self.audio_denoise = tk.BooleanVar(value=False)
        self.denoise_level = tk.StringVar(value="Normal")   # Suave | Normal | Fuerte

        # Multi-library state
        self.libraries = []   # [{name, input_dir, output_dir}, ...]
        self.active_lib_idx = 0
        self.load_config()

        self.is_processing = False
        self.is_scanning = False
        self.transcode_queue = []   # list of file path strings pending transcoding

        self.total_savings_mb = 0.0
        self.files_processed = 0

        # Load DB from disk instantly (no NAS needed at startup)
        self.vistos_data = self.load_vistos()
        self.all_found_files = self._db_to_file_list()
        self.current_filter = "TODOS"
        self.view_mode = "flat"
        self._render_gen = 0

        self.update_queue = queue.Queue()
        self.setup_ui()
        self.after(100, self.process_queue)

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.update_queue.put(("log", f"[{timestamp}] {message}\n"))

    # ── DB helpers ──────────────────────────────────────────────────────────
    # Una sola DB global. Las librerías son filtros por input_dir, no DBs separadas.

    def _db_to_file_list(self):
        """Builds all_found_files from DB (instant, no NAS access)."""
        return [{"name": Path(p).name, "path": p} for p in self.vistos_data]

    def _path_under(self, path_str, prefix_path):
        """Returns True if path_str is inside prefix_path."""
        try:
            Path(path_str).relative_to(prefix_path)
            return True
        except ValueError:
            return False

    def _get_lib_files(self):
        """Returns only files belonging to the active library (by input_dir prefix)."""
        prefix = self.input_dir.get().strip()
        if not prefix:
            return self.all_found_files
        lib_path = Path(prefix)
        return [item for item in self.all_found_files if self._path_under(item['path'], lib_path)]

    def load_vistos(self):
        db_file = "biblioteca_vistos.json"
        if os.path.exists(db_file):
            try:
                with open(db_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_vistos(self):
        with open("biblioteca_vistos.json", "w", encoding="utf-8") as f:
            json.dump(self.vistos_data, f, ensure_ascii=False, indent=4)

    # ── Config (multi-library) ───────────────────────────────────────────────

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            self.libraries = []
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "libraries" in data:
                self.libraries = data["libraries"]
                self.active_lib_idx = data.get("active_library", 0)
            else:
                # Migrate old single-library format automatically
                self.libraries = [{
                    "name": "Librería 1",
                    "input_dir": data.get("input_dir", ""),
                    "output_dir": data.get("output_dir", "")
                }]
                self.active_lib_idx = 0
            self.active_lib_idx = max(0, min(self.active_lib_idx, len(self.libraries) - 1))
            self._apply_active_library()
        except Exception:
            self.libraries = []

    def save_config(self):
        if self.libraries:
            self.libraries[self.active_lib_idx]['input_dir'] = self.input_dir.get()
            self.libraries[self.active_lib_idx]['output_dir'] = self.output_dir.get()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"libraries": self.libraries, "active_library": self.active_lib_idx}, f, indent=4)

    def _apply_active_library(self):
        if self.libraries:
            lib = self.libraries[self.active_lib_idx]
            self.input_dir.set(lib.get("input_dir", ""))
            self.output_dir.set(lib.get("output_dir", ""))

    def switch_library(self, name=None):
        if name is None:
            name = self.lib_combo.get()
        if not name or not self.libraries:
            return
        idx = next((i for i, l in enumerate(self.libraries) if l['name'] == name), None)
        if idx is None:
            return
        self.active_lib_idx = idx
        self._apply_active_library()   # actualiza input_dir y output_dir
        self.render_library()          # filtra por nuevo input_dir
        self.save_config()
        self._update_lib_count()
        lib_files = self._get_lib_files()
        self.log(f"Librería: {self.libraries[idx]['name']} — {len(lib_files)} archivos")

    def add_library(self):
        name = simpledialog.askstring("Nueva Librería", "Nombre de la librería:", parent=self)
        if not name:
            return
        if any(l['name'] == name for l in self.libraries):
            messagebox.showerror("Error", f"Ya existe una librería con el nombre '{name}'.")
            return
        input_dir = filedialog.askdirectory(title=f"Carpeta origen para '{name}'")
        if not input_dir:
            return
        output_dir = filedialog.askdirectory(title=f"Carpeta destino para '{name}' (donde se guardan los AV1)")
        if not output_dir:
            return
        self.libraries.append({"name": name, "input_dir": input_dir, "output_dir": output_dir})
        self.save_config()
        self._refresh_library_combo()
        self.lib_combo.set(name)
        self.switch_library(name)

    def open_library_settings(self):
        if not self.libraries:
            return
        lib = self.libraries[self.active_lib_idx]

        win = ctk.CTkToplevel(self)
        win.title(f"Configurar: {lib['name']}")
        win.geometry("540x240")
        win.resizable(False, False)
        win.grab_set()
        win.grid_columnconfigure(1, weight=1)

        name_var   = tk.StringVar(value=lib.get('name', ''))
        input_var  = tk.StringVar(value=lib.get('input_dir', ''))
        output_var = tk.StringVar(value=lib.get('output_dir', ''))

        def row(r, label, var, hint=None):
            ctk.CTkLabel(win, text=label, font=("Segoe UI", 11, "bold"), anchor="e", width=130).grid(
                row=r, column=0, padx=(15, 8), pady=6, sticky="e")
            ctk.CTkEntry(win, textvariable=var, width=280).grid(
                row=r, column=1, padx=4, pady=6, sticky="ew")
            ctk.CTkButton(win, text="...", width=30,
                          command=lambda v=var: v.set(filedialog.askdirectory() or v.get())).grid(
                row=r, column=2, padx=(4, 15), pady=6)
            if hint:
                ctk.CTkLabel(win, text=hint, font=("Segoe UI", 9), text_color="#888").grid(
                    row=r+1, column=1, columnspan=2, padx=4, sticky="w")

        ctk.CTkLabel(win, text="Nombre:", font=("Segoe UI", 11, "bold"), anchor="e", width=130).grid(
            row=0, column=0, padx=(15, 8), pady=6, sticky="e")
        ctk.CTkEntry(win, textvariable=name_var, width=280).grid(
            row=0, column=1, columnspan=2, padx=(4, 15), pady=6, sticky="ew")

        row(1, "Origen (NAS):", input_var)
        row(2, "Destino local (GPU):", output_var,
            hint="Debe ser una unidad LOCAL para que HandBrake pueda usar la GPU")

        def save():
            new_name = name_var.get().strip()
            if not new_name:
                return
            lib['name']       = new_name
            lib['input_dir']  = input_var.get()
            lib['output_dir'] = output_var.get()
            self.input_dir.set(input_var.get())
            self.output_dir.set(output_var.get())
            self.save_config()
            self._refresh_library_combo()
            win.destroy()

        btn_frame = ctk.CTkFrame(win, fg_color="transparent")
        btn_frame.grid(row=4, column=0, columnspan=3, pady=(10, 15))
        ctk.CTkButton(btn_frame, text="Guardar", width=100, command=save).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="Cancelar", width=100, fg_color="#555",
                      hover_color="#444", command=win.destroy).pack(side="left", padx=10)

    def delete_library(self):
        if len(self.libraries) <= 1:
            messagebox.showwarning("Atención", "Debe haber al menos una librería.")
            return
        lib = self.libraries[self.active_lib_idx]
        if not messagebox.askyesno("Confirmar", f"¿Eliminar la librería '{lib['name']}'?\n(El archivo de base de datos NO se borrará)"):
            return
        self.libraries.pop(self.active_lib_idx)
        self.active_lib_idx = max(0, self.active_lib_idx - 1)
        self._apply_active_library()
        self.save_config()
        self._refresh_library_combo()
        self.render_library()
        self._update_lib_count()

    def _refresh_library_combo(self):
        names = [l['name'] for l in self.libraries]
        self.lib_combo['values'] = names if names else ["Sin librerías"]
        if self.libraries:
            self.lib_combo.current(self.active_lib_idx)

    def _update_lib_count(self):
        files = self._get_lib_files()
        total = len(files)
        av1 = sum(1 for p in files if self.vistos_data.get(p['path'], {}).get('is_av1') is True)
        omitido = sum(1 for p in files if self.vistos_data.get(p['path'], {}).get('is_av1') == "NO_TRANSCODIFICAR")
        unknown = sum(1 for p in files if self.vistos_data.get(p['path'], {}).get('is_av1') is None)
        pending = total - av1 - unknown - omitido
        self.lib_count_label.configure(text=f"{total} archivos en DB")
        self.stat_total_label.configure(text=f"Total: {total}")
        self.stat_av1_label.configure(text=f"AV1: {av1}")
        self.stat_pending_label.configure(text=f"Pendiente: {pending}")
        self.stat_omitido_label.configure(text=f"Omitidos: {omitido}")
        self.stat_unknown_label.configure(text=f"Sin Info: {unknown}")

    # ── Browse ──────────────────────────────────────────────────────────────

    def browse_input(self):
        p = filedialog.askdirectory()
        if p:
            self.input_dir.set(p)
            self.save_config()

    def browse_output(self):
        p = filedialog.askdirectory()
        if p:
            self.output_dir.set(p)
            self.save_config()

    # ── UI Setup ────────────────────────────────────────────────────────────

    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)

        # ── Library bar ──
        lib_bar = ctk.CTkFrame(self, height=50)
        lib_bar.grid(row=0, column=0, padx=10, pady=(10, 0), sticky="ew")

        ctk.CTkLabel(lib_bar, text="Librería activa:", font=("Segoe UI", 12, "bold")).pack(side="left", padx=(15, 5))

        lib_names = [l['name'] for l in self.libraries] if self.libraries else ["Sin librerías"]
        combo_style = ttk.Style()
        combo_style.configure("LibCombo.TCombobox", background="#343638", foreground="white",
                              fieldbackground="#343638", selectbackground="#1f6aa5", selectforeground="white")
        self.lib_combo = ttk.Combobox(lib_bar, values=lib_names, state="readonly",
                                      width=24, style="LibCombo.TCombobox", font=("Segoe UI", 11))
        self.lib_combo.pack(side="left", padx=5, pady=8)
        self.lib_combo.bind("<<ComboboxSelected>>", lambda e: self.switch_library(self.lib_combo.get()))
        if self.libraries:
            self.lib_combo.current(self.active_lib_idx)

        ctk.CTkButton(lib_bar, text="+ Nueva", width=80, command=self.add_library).pack(side="left", padx=5)
        ctk.CTkButton(lib_bar, text="⚙ Configurar", width=110, fg_color="#555", hover_color="#444", command=self.open_library_settings).pack(side="left", padx=5)
        ctk.CTkButton(lib_bar, text="✕ Borrar", width=80, fg_color="#c0392b", hover_color="#a93226", command=self.delete_library).pack(side="left", padx=5)

        self.lib_count_label = ctk.CTkLabel(lib_bar, text=f"{len(self.all_found_files)} archivos en DB", font=("Segoe UI", 10), text_color="#aaa")
        self.lib_count_label.pack(side="right", padx=15)

        # ── Tabs ──
        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=1, column=0, padx=10, pady=10, sticky="nsew")

        self.tab_transcode = self.tabview.add("Transcodificador")
        self.tab_library = self.tabview.add("Biblioteca")

        self.setup_transcode_tab()
        self.setup_library_tab()

    def setup_transcode_tab(self):
        self.tab_transcode.grid_columnconfigure(0, weight=1)
        self.tab_transcode.grid_rowconfigure(4, weight=1)  # log_text crece en fila 4

        header_frame = ctk.CTkFrame(self.tab_transcode)
        header_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        header_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header_frame, text="Origen (NAS):", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, padx=10, pady=5, sticky="w")
        ctk.CTkEntry(header_frame, textvariable=self.input_dir).grid(row=0, column=1, padx=10, pady=5, sticky="ew")
        ctk.CTkButton(header_frame, text="Examinar", width=80, command=self.browse_input).grid(row=0, column=2, padx=10, pady=5)
        ctk.CTkLabel(header_frame, text="Destino:", font=("Segoe UI", 12, "bold")).grid(row=1, column=0, padx=10, pady=5, sticky="w")
        ctk.CTkEntry(header_frame, textvariable=self.output_dir).grid(row=1, column=1, padx=10, pady=5, sticky="ew")
        ctk.CTkButton(header_frame, text="Examinar", width=80, command=self.browse_output).grid(row=1, column=2, padx=10, pady=5)

        status_frame = ctk.CTkFrame(self.tab_transcode, fg_color="#1a1a1a")
        status_frame.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        status_frame.grid_columnconfigure(0, weight=1)
        self.current_file_label = ctk.CTkLabel(status_frame, text="Esperando...", font=("Segoe UI", 14, "bold"), text_color="#3b8ed0")
        self.current_file_label.grid(row=0, column=0, padx=20, pady=(15, 0), sticky="w")
        self.current_action_label = ctk.CTkLabel(status_frame, text="Estado: Idle")
        self.current_action_label.grid(row=1, column=0, padx=20, pady=(0, 5), sticky="w")
        self.progress_bar = ctk.CTkProgressBar(status_frame)
        self.progress_bar.grid(row=2, column=0, padx=20, pady=(5, 15), sticky="ew")
        self.progress_bar.set(0)
        self.percentage_label = ctk.CTkLabel(status_frame, text="0%", font=("Segoe UI", 12, "bold"))
        self.percentage_label.grid(row=2, column=1, padx=(0, 20), pady=(5, 15))

        stats_frame = ctk.CTkFrame(self.tab_transcode)
        stats_frame.grid(row=2, column=0, padx=10, pady=5, sticky="ew")
        self.saved_label = ctk.CTkLabel(stats_frame, text="Ahorro: 0.00 MB", font=("Segoe UI", 12, "bold"), text_color="#2ecc71")
        self.saved_label.pack(side="left", padx=20, pady=5)
        self.processed_label = ctk.CTkLabel(stats_frame, text="Procesados: 0", font=("Segoe UI", 12))
        self.processed_label.pack(side="right", padx=20, pady=5)

        # ── Selector de modo de codificación ────────────────────────────────
        mode_frame = ctk.CTkFrame(self.tab_transcode)
        mode_frame.grid(row=3, column=0, padx=10, pady=(0, 5), sticky="ew")
        mode_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(mode_frame, text="Modo:", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, padx=(15, 10), pady=8, sticky="w")
        self.mode_selector = ctk.CTkSegmentedButton(
            mode_frame,
            values=["Pelicula / Serie", "Anime / Dibujos"],
            variable=self.encode_mode,
            command=self._on_mode_changed,
        )
        # Mapear los textos de display al valor interno
        self.mode_selector.configure(
            values=["Pelicula / Serie", "Anime / Dibujos"]
        )
        self.encode_mode.set("Pelicula / Serie")
        self.mode_selector.grid(row=0, column=1, padx=10, pady=8, sticky="w")
        self.mode_info_label = ctk.CTkLabel(
            mode_frame,
            text="CQ 30 · Sin filtros",
            font=("Segoe UI", 11),
            text_color="#888888",
        )
        self.mode_info_label.grid(row=0, column=2, padx=(0, 15), pady=8, sticky="e")

        # ── Fila 1: reducción de ruido de audio ─────────────────────────────
        ctk.CTkLabel(mode_frame, text="Audio:", font=("Segoe UI", 12, "bold")).grid(
            row=1, column=0, padx=(15, 10), pady=(0, 8), sticky="w")
        self.denoise_check = ctk.CTkCheckBox(
            mode_frame,
            text="Reducción de ruido de fondo  (ventiladores, HVAC, zumbidos)",
            variable=self.audio_denoise,
            command=self._on_denoise_changed,
            font=("Segoe UI", 11),
        )
        self.denoise_check.grid(row=1, column=1, padx=10, pady=(0, 8), sticky="w")
        self.denoise_menu = ctk.CTkOptionMenu(
            mode_frame,
            values=["Suave", "Normal", "Fuerte"],
            variable=self.denoise_level,
            width=100,
            state="disabled",
        )
        self.denoise_menu.grid(row=1, column=2, padx=(0, 15), pady=(0, 8), sticky="e")

        self.log_text = ctk.CTkTextbox(self.tab_transcode, font=("Consolas", 11), fg_color="#121212")
        self.log_text.grid(row=4, column=0, padx=10, pady=5, sticky="nsew")
        self.start_button = ctk.CTkButton(self.tab_transcode, text="INICIAR TRANSCODIFICACIÓN", height=45, command=self.toggle_processing)
        self.start_button.grid(row=5, column=0, padx=10, pady=10, sticky="ew")

    def _on_denoise_changed(self):
        """Habilita o deshabilita el selector de intensidad según el checkbox."""
        state = "normal" if self.audio_denoise.get() else "disabled"
        self.denoise_menu.configure(state=state)

    def _on_mode_changed(self, value):
        """Actualiza la etiqueta de info al cambiar el modo de codificación."""
        if value == "Anime / Dibujos":
            self.mode_info_label.configure(
                text="CQ 32 · Denoise suave (NLMeans light)", text_color="#e67e22"
            )
        else:
            self.mode_info_label.configure(
                text="CQ 30 · Sin filtros", text_color="#888888"
            )

    def setup_library_tab(self):
        self.tab_library.grid_columnconfigure(0, weight=1)
        self.tab_library.grid_rowconfigure(5, weight=1)

        # Row 0: action buttons
        ctrl_frame = ctk.CTkFrame(self.tab_library)
        ctrl_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")

        self.scan_button = ctk.CTkButton(ctrl_frame, text="1. BUSCAR NUEVOS (NAS)", width=220, command=self.start_scan)
        self.scan_button.pack(side="left", padx=5, pady=10)

        self.import_excel_button = ctk.CTkButton(ctrl_frame, text="2. IMPORTAR EXCEL JELLYFIN", width=220, fg_color="#27ae60", hover_color="#219150", command=self.import_jellyfin_excel)
        self.import_excel_button.pack(side="left", padx=5, pady=10)

        self.deep_scan_button = ctk.CTkButton(ctrl_frame, text="3. ANALIZAR RESTO (MediaInfo)", width=220, fg_color="#8e44ad", hover_color="#7d3c98", command=self.start_deep_scan)
        self.deep_scan_button.pack(side="left", padx=5, pady=10)

        self.cleanup_button = ctk.CTkButton(ctrl_frame, text="Limpiar faltantes", width=130, fg_color="#7f8c8d", hover_color="#6d7a8a", command=self.start_cleanup)
        self.cleanup_button.pack(side="left", padx=5, pady=10)

        self.queue_btn = ctk.CTkButton(ctrl_frame, text="Cola vacía", width=160,
                                       fg_color="#2c3e50", hover_color="#1a252f",
                                       state="disabled", command=self.show_queue_manager)
        self.queue_btn.pack(side="left", padx=5, pady=10)

        self.reset_button = ctk.CTkButton(ctrl_frame, text="Reset DB", width=80, fg_color="#c0392b", hover_color="#a93226", command=self.confirm_reset)
        self.reset_button.pack(side="right", padx=10, pady=10)

        # Row 1: status text
        self.scan_status_label = ctk.CTkLabel(self.tab_library, text="Librería cargada desde DB. Usa '1. BUSCAR NUEVOS' para detectar archivos agregados al NAS.", font=("Segoe UI", 10, "italic"))
        self.scan_status_label.grid(row=1, column=0, padx=20, pady=(0, 2), sticky="w")

        # Row 2: deep scan progress bar (hidden by default)
        self.deep_scan_progress_bar = ctk.CTkProgressBar(self.tab_library)
        self.deep_scan_progress_bar.set(0)
        self.deep_scan_progress_bar.grid(row=2, column=0, padx=10, pady=(0, 2), sticky="ew")
        self.deep_scan_progress_bar.grid_remove()

        # Row 3: stats bar
        stats_bar = ctk.CTkFrame(self.tab_library, fg_color="#1a1a1a", height=28)
        stats_bar.grid(row=3, column=0, padx=10, pady=(0, 4), sticky="ew")
        self.stat_total_label = ctk.CTkLabel(stats_bar, text="Total: 0", font=("Segoe UI", 10), text_color="#aaa")
        self.stat_total_label.pack(side="left", padx=(15, 20))
        self.stat_av1_label = ctk.CTkLabel(stats_bar, text="AV1: 0", font=("Segoe UI", 10), text_color="#2ecc71")
        self.stat_av1_label.pack(side="left", padx=20)
        self.stat_pending_label = ctk.CTkLabel(stats_bar, text="Pendiente: 0", font=("Segoe UI", 10), text_color="#3498db")
        self.stat_pending_label.pack(side="left", padx=20)
        self.stat_omitido_label = ctk.CTkLabel(stats_bar, text="Omitidos: 0", font=("Segoe UI", 10), text_color="#9b59b6")
        self.stat_omitido_label.pack(side="left", padx=20)
        self.stat_unknown_label = ctk.CTkLabel(stats_bar, text="Sin Info: 0", font=("Segoe UI", 10), text_color="#7f8c8d")
        self.stat_unknown_label.pack(side="left", padx=20)

        # Row 4: filter buttons
        filter_frame = ctk.CTkFrame(self.tab_library)
        filter_frame.grid(row=4, column=0, padx=10, pady=(0, 4), sticky="ew")
        ctk.CTkLabel(filter_frame, text="Filtrar por:").pack(side="left", padx=10)
        self.btn_f_all = ctk.CTkButton(filter_frame, text="TODOS", width=80, fg_color="#34495e", command=lambda: self.set_filter("TODOS"))
        self.btn_f_all.pack(side="left", padx=5)
        self.btn_f_new = ctk.CTkButton(filter_frame, text="NUEVOS", width=80, fg_color="transparent", border_width=1, command=lambda: self.set_filter("NUEVO"))
        self.btn_f_new.pack(side="left", padx=5)
        self.btn_f_pending = ctk.CTkButton(filter_frame, text="PENDIENTES", width=100, fg_color="transparent", border_width=1, command=lambda: self.set_filter("PENDIENTE"))
        self.btn_f_pending.pack(side="left", padx=5)
        self.btn_f_av1 = ctk.CTkButton(filter_frame, text="YA AV1", width=80, fg_color="transparent", border_width=1, command=lambda: self.set_filter("AV1"))
        self.btn_f_av1.pack(side="left", padx=5)
        self.btn_f_omitidos = ctk.CTkButton(filter_frame, text="OMITIDOS", width=80, fg_color="transparent", border_width=1, command=lambda: self.set_filter("OMITIDO"))
        self.btn_f_omitidos.pack(side="left", padx=5)
        self.btn_f_local = ctk.CTkButton(filter_frame, text="LISTO LOCAL", width=100, fg_color="transparent", border_width=1, command=lambda: self.set_filter("LISTO_LOCAL"))
        self.btn_f_local.pack(side="left", padx=5)
        self.btn_f_unknown = ctk.CTkButton(filter_frame, text="SIN INFO", width=80, fg_color="transparent", border_width=1, command=lambda: self.set_filter("DESCONOCIDO"))
        self.btn_f_unknown.pack(side="left", padx=5)

        self.btn_view_toggle = ctk.CTkButton(filter_frame, text="Vista: Lista", width=110,
                                              fg_color="#2c3e50", hover_color="#1a252f",
                                              command=self.toggle_view_mode)
        self.btn_view_toggle.pack(side="right", padx=10)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#121212", foreground="#ecf0f1", fieldbackground="#121212", borderwidth=0, font=("Segoe UI", 10), rowheight=30)
        style.configure("Treeview.Heading", background="#2c3e50", foreground="white", relief="flat", font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[('selected', '#3498db')])

        # Row 5: file tree (expands)
        self.tree_frame = ctk.CTkFrame(self.tab_library, fg_color="#121212")
        self.tree_frame.grid(row=5, column=0, padx=10, pady=(0, 10), sticky="nsew")
        self.tree_frame.columnconfigure(0, weight=1)
        self.tree_frame.rowconfigure(0, weight=1)

        columns = ("nombre", "size", "estado", "origen", "subs")
        self.tree = ttk.Treeview(self.tree_frame, columns=columns, show='headings', selectmode="extended")
        self.tree.heading("#0", text="CARPETA / ARCHIVO", anchor="w")
        self.tree.column("#0", width=380, anchor="w", minwidth=150)
        self.tree.heading("nombre", text="ARCHIVO")
        self.tree.heading("size", text="TAMAÑO")
        self.tree.heading("estado", text="ESTADO / CODEC")
        self.tree.heading("origen", text="SOPORTE")
        self.tree.heading("subs", text="SUBTÍTULOS")
        self.tree.column("nombre", width=340, anchor="w")
        self.tree.column("size", width=80, anchor="center")
        self.tree.column("estado", width=220, anchor="center")
        self.tree.column("origen", width=110, anchor="center")
        self.tree.column("subs", width=100, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(self.tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.context_menu = tk.Menu(self, tearoff=0, bg="#2c3e50", fg="white", activebackground="#3498db")
        self.context_menu.add_command(label="+ Agregar a cola de transcodificación", command=self.enqueue_selected)
        self.context_menu.add_command(label="🚫 Marcar como NO TRANSCODIFICAR", command=self.mark_no_transcode_selected)
        self.context_menu.add_command(label="↩ Desmarcar → volver a PENDIENTE", command=self.unmark_no_transcode_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Mover local a NAS y borrar original", command=self.move_to_nas_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🔇 Mejorar audio (solo denoise, sin video)", command=self.improve_audio_selected)
        self.context_menu.add_command(label="Analizar a fondo (MediaInfo)", command=self.analyze_selected)
        self.context_menu.add_command(label="Abrir carpeta", command=self.open_selected_folder)
        self.tree.bind("<Button-3>", self.show_context_menu)

        # Initial render from DB
        self.render_library()
        self._update_lib_count()

    # ── Library management ──────────────────────────────────────────────────

    def confirm_reset(self):
        lib_name = self.libraries[self.active_lib_idx]['name'] if self.libraries else "esta librería"
        if messagebox.askyesno("Confirmar", f"¿Borrar solo los archivos de '{lib_name}' de la base de datos?"):
            prefix = Path(self.input_dir.get())
            keys = [k for k in self.vistos_data if self._path_under(k, prefix)]
            for k in keys:
                del self.vistos_data[k]
            self.save_vistos()
            self.all_found_files = self._db_to_file_list()
            self.render_library()
            self._update_lib_count()
            messagebox.showinfo("Listo", f"{len(keys)} archivos eliminados de la base de datos.")

    def import_jellyfin_excel(self):
        path = filedialog.askopenfilename(title="Seleccionar Reporte Jellyfin", filetypes=[("Excel files", "*.xlsx")])
        if not path:
            return
        if not self.all_found_files:
            messagebox.showwarning("Atención", "La base de datos está vacía. Usa '1. BUSCAR NUEVOS' primero para registrar los archivos.")
            return
        self.import_excel_button.configure(state="disabled", text="Importando...")
        threading.Thread(target=self.excel_import_worker, args=(path,), daemon=True).start()

    def excel_import_worker(self, excel_path):
        try:
            import openpyxl
            self.update_queue.put(("scan_progress", "Abriendo archivo Excel..."))
            wb = openpyxl.load_workbook(excel_path, read_only=True)
            ws = wb.active

            local_map = {os.path.basename(f['path']): f['path'] for f in self.all_found_files}

            codec_col = 16
            path_col = 22
            count = 0

            rows = list(ws.iter_rows(min_row=2, values_only=True))
            total_rows = len(rows)

            for i, row in enumerate(rows):
                if len(row) < 22:
                    continue
                remote_path = str(row[path_col - 1]) if row[path_col - 1] else ""
                codec = str(row[codec_col - 1]) if row[codec_col - 1] else ""
                if not remote_path:
                    continue
                filename = os.path.basename(remote_path.replace("\\", "/"))
                if filename in local_map:
                    real_path = local_map[filename]
                    is_av1 = "AV1" in codec.upper()
                    self.vistos_data[str(real_path)] = {
                        "mtime": 0,
                        "is_av1": is_av1,
                        "seen": True,
                        "identified_by": "Jellyfin Export"
                    }
                    count += 1
                if i % 20 == 0:
                    self.update_queue.put(("scan_progress", f"Procesando Excel: {i}/{total_rows}..."))

            self.save_vistos()
            self.all_found_files = self._db_to_file_list()
            self.update_queue.put(("scan_progress", f"¡Importación finalizada! {count} archivos vinculados."))
            self.update_queue.put(("import_done", f"Excel: {count} archivos sincronizados."))
        except Exception as e:
            self.update_queue.put(("log", f"Error Excel: {e}\n"))
            self.update_queue.put(("import_done", None))

    def set_filter(self, filter_name):
        self.current_filter = filter_name
        for btn in [self.btn_f_all, self.btn_f_new, self.btn_f_pending, self.btn_f_av1, self.btn_f_omitidos, self.btn_f_local, self.btn_f_unknown]:
            btn.configure(fg_color="transparent", border_width=1)
        if filter_name == "TODOS":
            self.btn_f_all.configure(fg_color="#34495e", border_width=0)
        elif filter_name == "NUEVO":
            self.btn_f_new.configure(fg_color="#e67e22", border_width=0)
        elif filter_name == "PENDIENTE":
            self.btn_f_pending.configure(fg_color="#3498db", border_width=0)
        elif filter_name == "AV1":
            self.btn_f_av1.configure(fg_color="#2ecc71", border_width=0)
        elif filter_name == "OMITIDO":
            self.btn_f_omitidos.configure(fg_color="#9b59b6", border_width=0)
        elif filter_name == "LISTO_LOCAL":
            self.btn_f_local.configure(fg_color="#f1c40f", border_width=0)
        elif filter_name == "DESCONOCIDO":
            self.btn_f_unknown.configure(fg_color="#95a5a6", border_width=0)
        self.render_library()

    def render_library(self):
        self._render_gen += 1
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        if self.view_mode == "tree":
            self._render_tree_mode()
        else:
            self._render_flat_mode()

    def toggle_view_mode(self):
        self.view_mode = "tree" if self.view_mode == "flat" else "flat"
        self.btn_view_toggle.configure(text="Vista: Árbol" if self.view_mode == "tree" else "Vista: Lista")
        self.render_library()

    def _tag_configure_all(self):
        self.tree.tag_configure("av1",        foreground="#2ecc71")
        self.tree.tag_configure("no_en_origen", foreground="#f1c40f")
        self.tree.tag_configure("nuevo",      foreground="#e67e22")
        self.tree.tag_configure("pendiente",  foreground="#3498db")
        self.tree.tag_configure("desconocido", foreground="#7f8c8d")
        self.tree.tag_configure("omitido",    foreground="#9b59b6")

    def _file_display_info(self, item):
        """Returns (status_text, source_text, tags, status_code) for a file entry."""
        db_entry = self.vistos_data.get(item['path'])
        status_text = "Sin información (Click derecho para analizar)"
        source_text = "Desconocido"
        tags = ("desconocido",)
        if db_entry:
            source_text = db_entry.get('identified_by', 'Nombre')
            is_av1 = db_entry.get('is_av1')
            if is_av1 is True:
                ff = self.get_expected_local_output(item['path'])
                if ff and ff.exists():
                    status_text = "NO EN ORIGEN (Listo local)"
                    tags = ("no_en_origen",)
                else:
                    status_text = "OPTIMIZADO (AV1)"
                    tags = ("av1",)
            elif is_av1 == "NO_TRANSCODIFICAR":
                status_text = "NO TRANSCODIFICAR (Poco ahorro)"
                tags = ("omitido",)
            elif is_av1 is None:
                status_text = "SIN INFO (Click derecho para analizar)"
                tags = ("desconocido",)
            elif not db_entry.get("seen"):
                status_text = "NUEVO (Pendiente de transcodificar)"
                tags = ("nuevo",)
            else:
                status_text = "PENDIENTE (Transcodificar)"
                tags = ("pendiente",)
        code_map = {"av1": "AV1", "no_en_origen": "LISTO_LOCAL", "omitido": "OMITIDO",
                    "nuevo": "NUEVO", "pendiente": "PENDIENTE", "desconocido": "DESCONOCIDO"}
        return status_text, source_text, tags, code_map.get(tags[0], "TODOS")

    def _render_flat_mode(self):
        self.tree.configure(show='headings', displaycolumns=("nombre", "size", "estado", "origen", "subs"))
        lib_files = self._get_lib_files()
        gen = self._render_gen
        if not lib_files:
            self.tree.insert('', 'end', values=(
                "Esta librería no tiene archivos en la base de datos.",
                "", "Usa '1. BUSCAR NUEVOS' para escanear el NAS.", "", ""
            ), tags=("desconocido",))
            self.tree.tag_configure("desconocido", foreground="#7f8c8d")
            return
        # Preparar filas (sólo dict lookups — rápido)
        rows = []
        for item in lib_files:
            status_text, source_text, tags, status_code = self._file_display_info(item)
            if self.current_filter != "TODOS" and status_code != self.current_filter:
                continue
            db_entry = self.vistos_data.get(item['path'], {})
            sz = _format_size(db_entry.get('size', 0))
            sub_text = self._sub_display(item['path'])
            rows.append((item['path'], item['name'], sz, status_text, source_text, sub_text, tags))
        if not rows:
            msg = "No hay archivos con este filtro." if self.current_filter != "TODOS" else "Esta librería no tiene archivos en la base de datos."
            self.tree.insert('', 'end', values=(msg, "", "", "", ""), tags=("desconocido",))
            self.tree.tag_configure("desconocido", foreground="#7f8c8d")
            return
        # Insertar en lotes para no bloquear la UI
        self._insert_flat_batch(rows, 0, gen)

    def _insert_flat_batch(self, rows, offset, gen, batch=200):
        """Inserta filas en lotes pequeños para no bloquear la UI."""
        if gen != self._render_gen:
            return
        for path, name, sz, status_text, source_text, sub_text, tags in rows[offset:offset + batch]:
            try:
                self.tree.insert('', 'end', iid=path,
                                 values=(name, sz, status_text, source_text, sub_text),
                                 tags=tags)
            except Exception:
                pass
        next_off = offset + batch
        if next_off < len(rows):
            self.after(1, lambda: self._insert_flat_batch(rows, next_off, gen, batch))
        else:
            self._tag_configure_all()

    def _render_tree_mode(self):
        self.tree.configure(show='tree headings', displaycolumns=("size", "estado", "origen", "subs"))
        self.tree.column("#0", width=380, anchor="w", minwidth=150)
        lib_files = self._get_lib_files()
        prefix = self.input_dir.get().strip()
        if not lib_files or not prefix:
            self._render_flat_mode()
            return
        input_path = Path(prefix)

        # Map each file to its relative folder path and accumulate per-folder stats
        folder_stats = {}   # str(rel_folder) -> {av1, total, unknown, omitido, size}
        file_folder = {}    # path_str -> str(rel_folder)
        for item in lib_files:
            try:
                rel_folder = Path(item['path']).parent.relative_to(input_path)
                rel_str = str(rel_folder)
            except ValueError:
                continue
            if rel_str not in folder_stats:
                folder_stats[rel_str] = {'av1': 0, 'total': 0, 'unknown': 0, 'omitido': 0, 'size': 0}
            file_folder[item['path']] = rel_str
            folder_stats[rel_str]['total'] += 1
            db_entry = self.vistos_data.get(item['path'], {})
            is_av1 = db_entry.get('is_av1')
            folder_stats[rel_str]['size'] += db_entry.get('size', 0) or 0
            if is_av1 is True:
                folder_stats[rel_str]['av1'] += 1
            elif is_av1 is None:
                folder_stats[rel_str]['unknown'] += 1
            elif is_av1 == "NO_TRANSCODIFICAR":
                folder_stats[rel_str]['omitido'] += 1

        def recursive_stats(rel_str):
            av1 = total = unknown = omitido = size = 0
            target = Path(rel_str)
            for k, v in folder_stats.items():
                try:
                    Path(k).relative_to(target)
                    av1 += v['av1']; total += v['total']
                    unknown += v['unknown']; omitido += v['omitido']
                    size += v['size']
                except ValueError:
                    pass
            return av1, total, unknown, omitido, size

        def folder_color(rel_str):
            av1, total, unknown, omitido, _ = recursive_stats(rel_str)
            known = total - unknown
            if known == 0:
                return "#7f8c8d"
            if av1 == known:
                return "#2ecc71"
            if av1 == 0:
                return "#e74c3c"
            return "#f39c12"

        # Collect all unique folder paths (no "." root node)
        all_folders = set()
        for rel_str in folder_stats:
            if rel_str == ".":
                continue
            p = Path(rel_str)
            while str(p) != ".":
                all_folders.add(str(p))
                p = p.parent

        sorted_folders = sorted(all_folders, key=lambda x: (len(Path(x).parts), x))
        inserted = set()

        def insert_folder(rel_str):
            if rel_str in inserted:
                return
            p = Path(rel_str)
            parent_str = str(p.parent)
            if parent_str != "." and parent_str in all_folders:
                insert_folder(parent_str)
            folder_iid = f"f:{rel_str}"
            parent_iid = f"f:{parent_str}" if parent_str != "." else ""
            av1, total, unknown, omitido, size = recursive_stats(rel_str)
            color = folder_color(rel_str)
            tag = f"fc_{color[1:]}"
            sz_txt = _format_size(size) if size > 0 else ""
            status = f"{av1}/{total} AV1"
            if unknown > 0:
                status += f"  ({unknown} sin info)"
            self.tree.insert(parent_iid, 'end', iid=folder_iid,
                             text=f"📁 {p.name}",
                             values=(sz_txt, status, ""),
                             tags=(tag,), open=False)
            self.tree.tag_configure(tag, foreground=color)
            inserted.add(rel_str)

        for rel_str in sorted_folders:
            insert_folder(rel_str)

        # Insert file nodes under their folders
        for item in lib_files:
            rel_str = file_folder.get(item['path'])
            if rel_str is None:
                continue
            parent_iid = f"f:{rel_str}" if rel_str != "." and rel_str in inserted else ""
            status_text, source_text, tags, _ = self._file_display_info(item)
            db_entry = self.vistos_data.get(item['path'], {})
            sz = _format_size(db_entry.get('size', 0))
            sub_text = self._sub_display(item['path'])
            try:
                self.tree.insert(parent_iid, 'end', iid=item['path'],
                                 text=item['name'],
                                 values=(sz, status_text, source_text, sub_text),
                                 tags=tags)
            except Exception:
                pass

        self._tag_configure_all()

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            # Keep multi-selection if right-clicking an already-selected item
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def improve_audio_selected(self):
        """Lanza mejora de audio (denoise) sin transcodificar el video."""
        selected = [s for s in self.tree.selection() if not s.startswith("f:")]
        if not selected:
            return

        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("Error",
                "No se encontró ffmpeg.exe junto a HandBrakeCLI.\n"
                "Asegurate de tenerlo en la misma carpeta.")
            return

        if not self.output_dir.get():
            messagebox.showwarning("Atención",
                "Configurá la carpeta destino en la pestaña Transcodificador.")
            return

        # Intensidad: usa la configuración del panel (Normal si no está activo)
        nf_map = {"Suave": "-30", "Normal": "-25", "Fuerte": "-20"}
        nf     = nf_map.get(self.denoise_level.get(), "-25")
        nivel  = self.denoise_level.get()

        input_root  = Path(self.input_dir.get())
        output_root = Path(self.output_dir.get())

        self.scan_status_label.configure(
            text=f"Iniciando mejora de audio ({nivel}) en {len(selected)} archivo(s)...")
        threading.Thread(
            target=self._improve_audio_worker,
            args=(selected, ffmpeg, nf, nivel, input_root, output_root),
            daemon=True,
        ).start()

    def _improve_audio_worker(self, paths, ffmpeg, nf, nivel, input_root, output_root):
        total  = len(paths)
        ok     = 0
        errors = 0

        for idx, path_str in enumerate(paths, 1):
            src = Path(path_str)
            if not src.exists():
                self.update_queue.put(("log", f"[AudioDN] No encontrado: {src.name}\n"))
                errors += 1
                continue

            # Misma estructura de carpetas que el pipeline de transcodificación
            try:
                rel = src.relative_to(input_root)
                dst = output_root / rel.parent / src.name
            except ValueError:
                dst = output_root / src.name

            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_stem(dst.stem + "_audioDN_tmp")

            self.update_queue.put(("scan_progress",
                f"[{idx}/{total}] Mejorando audio ({nivel}): {src.name}"))

            cmd = [
                ffmpeg,
                "-i",   str(src),
                "-map", "0",        # todos los streams
                "-c:v", "copy",     # video: copia exacta, sin re-encodear
                "-c:s", "copy",     # subtítulos: copia exacta
                "-c:a", "aac",      # audio: encode con filtro denoise
                "-b:a", "192k",
                "-af",  f"afftdn=nf={nf}:nt=w",
                str(tmp),
                "-y",
                "-loglevel", "error",
            ]
            result = subprocess.run(cmd, capture_output=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)

            if result.returncode == 0 and tmp.exists():
                # Path.replace() es atómico en Windows y reemplaza el destino
                # aunque exista. Si está bloqueado por otro proceso reintentamos
                # brevemente antes de caer al fallback copy+delete.
                moved = False
                for attempt in range(6):
                    try:
                        tmp.replace(dst)
                        moved = True
                        break
                    except PermissionError:
                        time.sleep(0.5)

                if not moved:
                    # Fallback: copiar y borrar el temp
                    try:
                        shutil.copy2(tmp, dst)
                        tmp.unlink(missing_ok=True)
                        moved = True
                    except Exception as copy_err:
                        self.update_queue.put(("log",
                            f"[AudioDN ✗] No se pudo mover {tmp.name}: {copy_err}\n"))

                if moved:
                    self.update_queue.put(("log",
                        f"[AudioDN ✓] {src.name}  →  {dst.parent.name}/{dst.name}\n"))
                    ok += 1
                else:
                    errors += 1
            else:
                err_msg = result.stderr.decode(errors="replace").strip() \
                          if result.stderr else "error desconocido"
                self.update_queue.put(("log",
                    f"[AudioDN ✗] {src.name}: {err_msg}\n"))
                errors += 1
                if tmp.exists():
                    tmp.unlink()

        resumen = f"Mejora de audio completada: {ok} OK"
        if errors:
            resumen += f", {errors} con error"
        self.update_queue.put(("scan_progress", resumen))
        self.update_queue.put(("log", f"\n{resumen}.\n"))

    def analyze_selected(self):
        selected = [s for s in self.tree.selection() if not s.startswith("f:")]
        if selected:
            self.deep_scan_button.configure(state="disabled", text="Analizando...")
            threading.Thread(target=self.deep_scan_worker, args=(selected,), daemon=True).start()

    def open_selected_folder(self):
        selected = self.tree.selection()
        if not selected:
            return
        iid = selected[0]
        if iid.startswith("f:"):
            rel_str = iid[2:]
            folder = Path(self.input_dir.get()) / rel_str
        else:
            folder = Path(iid).parent
        try:
            os.startfile(folder)
        except Exception:
            pass

    def get_expected_local_output(self, src_str):
        if not self.input_dir.get() or not self.output_dir.get():
            return None
        in_r = Path(self.input_dir.get())
        out_r = Path(self.output_dir.get())
        if str(in_r.resolve()) == str(out_r.resolve()):
            return None
        src = Path(src_str)
        try:
            rel = src.parent.relative_to(in_r)
        except ValueError:
            return None
        codec_tag = r"(?i)x264|h264|h\.264|x265|h265|h\.265|hevc|avc"
        ns = re.sub(codec_tag, "AV1", src.stem)
        of = (ns + ".mkv") if ns != src.stem else (src.stem + ".AV1.mkv")
        ff = out_r / rel / of
        return ff

    def move_to_nas_selected(self):
        selected = [s for s in self.tree.selection() if not s.startswith("f:")]
        if not selected: return

        valid_paths = []
        for path_str in selected:
            if self.tree.item(path_str, "tags")[0] == "no_en_origen":
                valid_paths.append(path_str)
                
        if not valid_paths:
            messagebox.showinfo("Info", "Ningún archivo seleccionado está en estado 'NO EN ORIGEN'.")
            return
            
        if messagebox.askyesno("Confirmar", f"¿Mover {len(valid_paths)} archivo(s) local(es) hacia el origen (NAS) y eliminar el original no-AV1?"):
            threading.Thread(target=self.move_to_nas_worker, args=(valid_paths,), daemon=True).start()

    def _move_to_nas_with_progress(self, src: Path, dst: Path, idx: int, total: int) -> bool:
        """Copia src→dst en chunks reportando progreso, luego borra src. Retorna True si OK."""
        try:
            file_size = src.stat().st_size
        except Exception as e:
            self.update_queue.put(("log", f"Error leyendo {src.name}: {e}\n"))
            return False

        CHUNK = 4 * 1024 * 1024  # 4 MB
        copied = 0
        start_time = time.time()

        try:
            with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
                while True:
                    chunk = fsrc.read(CHUNK)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    copied += len(chunk)

                    elapsed = time.time() - start_time
                    pct = (copied / file_size * 100) if file_size > 0 else 0
                    mb_done = copied / 1024 / 1024
                    mb_total = file_size / 1024 / 1024
                    speed = mb_done / elapsed if elapsed > 0 else 0
                    eta = int((mb_total - mb_done) / speed) if speed > 0 else 0

                    status = (f"[{idx}/{total}] {src.name}  —  "
                              f"{pct:.0f}%  ({mb_done:.0f} / {mb_total:.0f} MB)  "
                              f"{speed:.1f} MB/s  ETA: {eta}s")
                    self.update_queue.put(("scan_progress", status))

            src.unlink()
            return True
        except Exception as e:
            self.update_queue.put(("log", f"Error moviendo {src.name}: {e}\n"))
            if dst.exists():
                try:
                    dst.unlink()
                except Exception:
                    pass
            return False

    def move_to_nas_worker(self, paths):
        total = len(paths)
        moved = 0

        for idx, path_str in enumerate(paths, 1):
            src = Path(path_str)
            ff = self.get_expected_local_output(path_str)
            if not ff or not ff.exists():
                self.update_queue.put(("scan_progress", f"[{idx}/{total}] No encontrado local: {src.name}"))
                continue

            new_nas_path = src.parent / ff.name
            self.update_queue.put(("log", f"[NAS {idx}/{total}] Iniciando: {ff.name}\n"))

            if self._move_to_nas_with_progress(ff, new_nas_path, idx, total):
                if src.exists():
                    try:
                        src.unlink()
                    except Exception as e:
                        self.update_queue.put(("log", f"  No se pudo borrar original: {e}\n"))

                old_size = self.vistos_data.get(path_str, {}).get('size', 0)
                if path_str in self.vistos_data:
                    del self.vistos_data[path_str]
                nas_stat = new_nas_path.stat() if new_nas_path.exists() else None
                self.vistos_data[str(new_nas_path)] = {
                    "mtime": nas_stat.st_mtime if nas_stat else 0,
                    "size": nas_stat.st_size if nas_stat else old_size,
                    "is_av1": True, "seen": True, "identified_by": "Movido al NAS"
                }
                moved += 1
                self.update_queue.put(("log", f"  OK: {ff.name} movido al NAS.\n"))

        self.save_vistos()
        self.all_found_files = self._db_to_file_list()
        msg = f"Completado: {moved}/{total} archivo(s) movidos al NAS."
        self.update_queue.put(("log", f"{msg}\n"))
        self.update_queue.put(("scan_progress", msg))
        self.update_queue.put(("reset_filter_av1", None))
        self.update_queue.put(("scan_done", None))


    # ── Scanning (Incremental) ───────────────────────────────────────────────

    def start_scan(self):
        if not self.input_dir.get() or self.is_scanning:
            return
        self.is_scanning = True
        self.scan_button.configure(text="Escaneando...", fg_color="#c0392b", state="disabled")
        threading.Thread(target=self.library_scanner_thread, daemon=True).start()

    def library_scanner_thread(self):
        """
        Incremental scan: walks the NAS directory but only adds files NOT already
        in the DB. Files already known are loaded from DB instantly at startup.
        """
        path_root = Path(self.input_dir.get())
        re_av1 = re.compile(r"(?i)AV1")
        re_others = re.compile(r"(?i)x264|h264|h\.264|x265|h265|h\.265|hevc|avc")

        # Build a set of known paths for O(1) lookup
        known_paths = set(self.vistos_data.keys())

        new_count = 0
        total_walked = 0

        try:
            for root, _, files in os.walk(path_root):
                for file in files:
                    if not self.is_scanning:
                        break
                    filepath = Path(root) / file
                    if filepath.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue

                    f_str = str(filepath)
                    total_walked += 1

                    if total_walked % 50 == 0:
                        self.update_queue.put(("scan_progress", f"Revisados: {total_walked} | Nuevos: {new_count}..."))

                    if f_str in known_paths:
                        # Already in DB — add to file list if missing (e.g. first run after restart)
                        if not any(f['path'] == f_str for f in self.all_found_files):
                            self.all_found_files.append({"name": file, "path": f_str})
                        continue

                    # New file not in DB
                    new_count += 1
                    self.all_found_files.append({"name": file, "path": f_str})
                    try:
                        sz = filepath.stat().st_size
                    except Exception:
                        sz = 0
                    subs = self._detect_subtitle_langs(filepath)
                    if re_av1.search(file):
                        self.vistos_data[f_str] = {"mtime": 0, "is_av1": True, "seen": False, "identified_by": "Nombre", "size": sz, "subtitles": subs}
                    elif re_others.search(file):
                        self.vistos_data[f_str] = {"mtime": 0, "is_av1": False, "seen": False, "identified_by": "Nombre", "size": sz, "subtitles": subs}
                    else:
                        self.vistos_data[f_str] = {"mtime": 0, "is_av1": None, "seen": False, "identified_by": "Pendiente", "size": sz, "subtitles": subs}

        except Exception as e:
            self.update_queue.put(("log", f"Error en escaneo: {e}\n"))

        self.save_vistos()
        self.update_queue.put(("scan_progress", f"Scan completado: {new_count} nuevos encontrados ({total_walked} revisados)."))
        self.update_queue.put(("scan_done", None))

    def analyze_individual(self, path):
        threading.Thread(target=self.deep_scan_worker, args=([path],), daemon=True).start()

    def start_deep_scan(self):
        pending = [f['path'] for f in self._get_lib_files()
                   if f['path'] not in self.vistos_data
                   or self.vistos_data[f['path']].get('identified_by') in ('Nombre', 'Pendiente')]
        if not pending:
            messagebox.showinfo("Info", "No hay archivos pendientes de análisis profundo.")
            return
        self.deep_scan_button.configure(state="disabled", text="Analizando...")
        threading.Thread(target=self.deep_scan_worker, args=(pending,), daemon=True).start()

    def deep_scan_worker(self, paths):
        self.prevent_sleep()
        try:
            total = len(paths)
            completed = 0
            WORKERS = 4
            TIMEOUT_SEC = 30
    
            self.update_queue.put(("deep_scan_start", total))
    
            with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                future_to_path = {executor.submit(self.check_is_av1, Path(p)): p for p in paths if Path(p).exists()}
                for future in as_completed(future_to_path):
                    p = future_to_path[future]
                    f_path = Path(p)
                    completed += 1
                    try:
                        is_av1 = future.result(timeout=TIMEOUT_SEC)
                        identified_by = "MediaInfo"
                    except FuturesTimeoutError:
                        is_av1 = None
                        identified_by = "Pendiente"
                        self.update_queue.put(("log", f"Timeout analizando: {f_path.name}\n"))
                    except Exception as e:
                        is_av1 = None
                        identified_by = "Pendiente"
                        self.update_queue.put(("log", f"Error en {f_path.name}: {e}\n"))
    
                    fstat = f_path.stat() if f_path.exists() else None
                    self.vistos_data[p] = {
                        "mtime": fstat.st_mtime if fstat else 0,
                        "size": fstat.st_size if fstat else self.vistos_data.get(p, {}).get("size", 0),
                        "is_av1": is_av1,
                        "seen": self.vistos_data.get(p, {}).get("seen", False),
                        "identified_by": identified_by,
                    }
                    self.update_queue.put(("scan_progress", f"Analizando {completed}/{total} ({WORKERS} en paralelo) — {f_path.name}"))
                    self.update_queue.put(("deep_scan_progress", completed / total))
                    if completed % 5 == 0:
                        self.save_vistos()
    
            self.save_vistos()
            self.update_queue.put(("scan_done", None))
        finally:
            self.allow_sleep()

    # ── Queue processor ──────────────────────────────────────────────────────

    def process_queue(self):
        try:
            while True:
                task, data = self.update_queue.get_nowait()
                if task == "log":
                    self.log_text.insert("end", data)
                    self.log_text.see("end")
                elif task == "status":
                    self.current_file_label.configure(text=data['file'])
                    self.current_action_label.configure(text=f"Estado: {data['action']}")
                elif task == "action":
                    self.current_action_label.configure(text=f"Estado: {data}")
                elif task == "progress":
                    self.progress_bar.set(data / 100)
                    self.percentage_label.configure(text=f"{int(data)}%")
                elif task == "stats":
                    self.total_savings_mb += data['savings']
                    self.files_processed += 1
                    self.saved_label.configure(text=f"Total: {self.total_savings_mb:.2f} MB")
                elif task == "queue_updated":
                    self._update_queue_button()
                elif task == "finished":
                    self.is_processing = False
                    self.start_button.configure(text="INICIAR TRANSCODIFICACIÓN")
                    self.render_library()
                    self._update_lib_count()
                elif task == "scan_progress":
                    self.scan_status_label.configure(text=data)
                elif task == "deep_scan_start":
                    self.deep_scan_progress_bar.set(0)
                    self.deep_scan_progress_bar.grid()
                elif task == "deep_scan_progress":
                    self.deep_scan_progress_bar.set(data)
                elif task == "scan_done":
                    self.is_scanning = False
                    self.scan_button.configure(text="1. BUSCAR NUEVOS (NAS)", fg_color=("#3b8ed0", "#1f6aa5"), state="normal")
                    self.deep_scan_button.configure(text="3. ANALIZAR RESTO (MediaInfo)", state="normal")
                    self.deep_scan_progress_bar.grid_remove()
                    self.render_library()
                    self._update_lib_count()
                elif task == "import_done":
                    self.import_excel_button.configure(state="normal", text="2. IMPORTAR EXCEL JELLYFIN")
                    if data:
                        self.log_text.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {data}\n")
                        self.log_text.see("end")
                    self.render_library()
                    self._update_lib_count()
                elif task == "reset_filter_av1":
                    self.set_filter("AV1")
                elif task == "cleanup_done":
                    self.cleanup_button.configure(state="normal", text="Limpiar faltantes")
                    if data:
                        self.log(f"Limpieza: {data} entradas eliminadas de la DB.")
                    self.render_library()
                    self._update_lib_count()
        except queue.Empty:
            pass
        self.after(100, self.process_queue)

    # ── Transcoding ──────────────────────────────────────────────────────────

    def toggle_processing(self):
        if self.is_processing:
            self.is_processing = False
        else:
            if not self.input_dir.get() or not self.output_dir.get():
                return
            self.is_processing = True
            self.start_button.configure(text="DETENER")
            threading.Thread(target=self.transcoding_engine, daemon=True).start()

    def transcoding_engine(self):
        self.prevent_sleep()
        try:
            input_root = Path(self.input_dir.get())
            output_root = Path(self.output_dir.get())
            temp_dir = output_root / "temp"
            (temp_dir / "Input").mkdir(parents=True, exist_ok=True)
            (temp_dir / "Output").mkdir(parents=True, exist_ok=True)
            for root, _, files in os.walk(input_root):
                if not self.is_processing:
                    break
                for f in files:
                    if not self.is_processing:
                        break
                    fp = Path(root) / f
                    if fp.suffix.lower() in SUPPORTED_EXTENSIONS and ".AV1.mkv" not in f:
                        self.process_single_file(fp, input_root, output_root, temp_dir)
            self.update_queue.put(("finished", None))
        finally:
            self.allow_sleep()

    def _copy_with_progress(self, src: Path, dst: Path) -> bool:
        """Copia src→dst reportando progreso en la barra. Retorna True si OK."""
        try:
            total = src.stat().st_size
        except Exception as e:
            self.update_queue.put(("log", f"Error al leer tamaño de {src.name}: {e}\n"))
            return False
        CHUNK = 4 * 1024 * 1024  # 4 MB por chunk
        copied = 0
        try:
            with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
                while True:
                    if not self.is_processing:
                        return False
                    chunk = fsrc.read(CHUNK)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    copied += len(chunk)
                    pct = (copied / total * 100) if total > 0 else 0
                    mb_done = copied / 1024 / 1024
                    mb_total = total / 1024 / 1024
                    self.update_queue.put(("progress", pct))
                    self.update_queue.put(("action",
                        f"Copiando desde NAS... {pct:.0f}%  ({mb_done:.0f} / {mb_total:.0f} MB)"))
            return True
        except Exception as e:
            self.update_queue.put(("log", f"Error copiando {src.name}: {e}\n"))
            return False

    def _update_db_entry(self, src: Path, is_av1, identified_by, seen=True):
        """Actualiza la entrada en vistos_data preservando campos existentes (subtítulos, etc.)."""
        try:
            fstat = src.stat()
            existing = self.vistos_data.get(str(src), {})
            self.vistos_data[str(src)] = {
                **existing,
                "mtime": fstat.st_mtime, "size": fstat.st_size,
                "is_av1": is_av1, "seen": seen, "identified_by": identified_by
            }
            self.save_vistos()
        except Exception:
            pass

    def _prefetch_verify_and_copy(self, src: Path, in_r: Path, out_r: Path, tmp: Path, result: dict):
        """Hilo de fondo: verifica codec y copia NAS→local el siguiente archivo de la cola."""
        try:
            if self.check_is_av1(src):
                self._update_db_entry(src, is_av1=True, identified_by="Verificación directa")
                result['skip'] = True
                return

            codec_tag = r"(?i)x264|h264|h\.264|x265|h265|h\.265|hevc|avc"
            ns = re.sub(codec_tag, "AV1", src.stem)
            of = (ns + ".mkv") if ns != src.stem else (src.stem + ".AV1.mkv")
            ff = out_r / src.parent.relative_to(in_r) / of

            if ff.exists():
                self._update_db_entry(src, is_av1=True, identified_by="Salida ya existe")
                result['skip'] = True
                return

            li = tmp / "Input" / src.name
            if li.exists():
                li.unlink()

            total = src.stat().st_size
            CHUNK = 4 * 1024 * 1024
            copied = 0
            start_t = time.time()

            with open(src, 'rb') as fsrc, open(li, 'wb') as fdst:
                while self.is_processing:
                    chunk = fsrc.read(CHUNK)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    copied += len(chunk)

            if self.is_processing and copied == total:
                elapsed = time.time() - start_t
                speed = total / 1024 / 1024 / elapsed if elapsed > 0 else 0
                result['ready'] = True
                self.update_queue.put(("log",
                    f"  [Pipeline] Pre-carga lista: {src.name} "
                    f"({total/1024/1024:.0f} MB, {elapsed:.0f}s, {speed:.1f} MB/s)\n"))
            else:
                if li.exists():
                    li.unlink()
        except Exception as e:
            self.update_queue.put(("log", f"  [Pipeline] Error prefetch {src.name}: {e}\n"))

    def process_single_file(self, src: Path, in_r: Path, out_r: Path, tmp: Path,
                            on_transcode_start=None, prefetch_data=None):
        self.update_queue.put(("status", {"file": src.name, "action": "Verificando codec..."}))

        if self.check_is_av1(src):
            self._update_db_entry(src, is_av1=True, identified_by="Verificación directa")
            return

        codec_tag = r"(?i)x264|h264|h\.264|x265|h265|h\.265|hevc|avc"
        ns = re.sub(codec_tag, "AV1", src.stem)
        of = (ns + ".mkv") if ns != src.stem else (src.stem + ".AV1.mkv")
        ff = out_r / src.parent.relative_to(in_r) / of
        if ff.exists():
            self._update_db_entry(src, is_av1=True, identified_by="Salida ya existe")
            return
        ff.parent.mkdir(parents=True, exist_ok=True)
        li, lo = tmp / "Input" / src.name, tmp / "Output" / of

        # ── Fase 1: Copia NAS → local (saltar si el pipeline ya lo hizo) ─────
        if prefetch_data and prefetch_data.get('ready') and li.exists():
            self.update_queue.put(("status", {"file": src.name, "action": "Pre-carga lista (pipeline) ✓"}))
            self.update_queue.put(("progress", 100))
        else:
            self.update_queue.put(("status", {"file": src.name, "action": "Copiando desde NAS..."}))
            self.update_queue.put(("progress", 0))
            if not self._copy_with_progress(src, li):
                if li.exists():
                    li.unlink()
                return

        # ── Fase 2: Transcodificación con HandBrake ──────────────────────────
        if on_transcode_start:
            on_transcode_start()   # arranca prefetch del siguiente archivo

        # Parámetros según modo de codificación seleccionado en la UI
        if self.encode_mode.get() == "Anime / Dibujos":
            quality   = "32"
            mode_args = ["--nlmeans=light", "--nlmeans-tune=animation"]
            mode_tag  = "[ANIME]"
        else:
            quality   = "30"
            mode_args = []
            mode_tag  = "[NORMAL]"

        # ── Fase 2b: lanzar denoise de audio EN PARALELO con HandBrake ────────
        # FFmpeg lee li (mismo archivo local) y procesa solo el audio mientras
        # HandBrake codifica el video. Cuando HandBrake termina, el audio
        # probablemente ya esté listo. Luego un mux rápido los combina.
        audio_thread  = None
        audio_result  = {}
        lo_audio      = None
        lo_final      = None

        if self.audio_denoise.get():
            ffmpeg = self._find_ffmpeg()
            if ffmpeg:
                nf_map   = {"Suave": "-30", "Normal": "-25", "Fuerte": "-20"}
                nf       = nf_map.get(self.denoise_level.get(), "-25")
                lo_audio = lo.with_stem(lo.stem + "_audio")

                def _audio_worker(_ff=ffmpeg, _src=li, _dst=lo_audio, _nf=nf):
                    dn_cmd = [
                        _ff,
                        "-i",   str(_src),
                        "-vn",              # sin video — solo audio
                        "-map", "0:a",      # todos los tracks de audio
                        "-c:a", "aac",
                        "-b:a", "192k",
                        "-af",  f"afftdn=nf={_nf}:nt=w",
                        str(_dst),
                        "-y",
                        "-loglevel", "error",
                    ]
                    r = subprocess.run(dn_cmd, capture_output=True,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    audio_result['returncode'] = r.returncode
                    audio_result['stderr']     = r.stderr
                    audio_result['nf']         = _nf

                audio_thread = threading.Thread(target=_audio_worker, daemon=True)
                audio_thread.start()
                self.update_queue.put(("log",
                    f"  [Audio] Denoise iniciado en paralelo (nf={nf} dBFS, {self.denoise_level.get()})\n"))
            else:
                self.update_queue.put(("log",
                    "  [Audio] Aviso: ffmpeg.exe no encontrado — denoise omitido.\n"))

        # HandBrake: passthrough de audio cuando denoise está activo
        # (el audio de HandBrake queda como fallback; lo reemplazamos al final)
        audio_args = ["--aencoder", "copy", "--audio-fallback", "aac"] \
                     if self.audio_denoise.get() else []

        self.update_queue.put(("status", {"file": src.name, "action": f"Transcodificando con GPU... {mode_tag}"}))
        self.update_queue.put(("progress", 0))
        cmd = [HANDBRAKE_CLI_PATH, "-i", str(li), "-o", str(lo),
               "-e", "nvenc_av1_10bit", "-q", quality, "--encoder-preset", "slow",
               "--all-audio", "--all-subtitles"] + mode_args + audio_args
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace',
                                creationflags=subprocess.CREATE_NO_WINDOW)
        pat = re.compile(r"(\d+\.\d+) %")
        while True:
            char = proc.stdout.read(1)
            if not char and proc.poll() is not None:
                break
            lb = char
            while char not in ['\r', '\n']:
                char = proc.stdout.read(1)
                if not char:
                    break
                lb += char
            m = pat.search(lb)
            if m:
                pct = float(m.group(1))
                self.update_queue.put(("progress", pct))
                self.update_queue.put(("action", f"Transcodificando con GPU... {pct:.1f}%"))
            if not self.is_processing:
                proc.terminate()
                break

        if not self.is_processing:
            # El usuario detuvo el proceso — no actualizar DB
            pass
        elif proc.returncode == 0:
            # ── Esperar audio si todavía está procesando ──────────────────────
            if audio_thread and audio_thread.is_alive():
                self.update_queue.put(("status", {"file": src.name,
                    "action": "Finalizando procesamiento de audio..."}))
                audio_thread.join()

            # ── Mux: video/subs de HandBrake + audio denoised de FFmpeg ──────
            if audio_thread and audio_result.get('returncode') == 0 \
                    and lo_audio and lo_audio.exists():
                ffmpeg   = self._find_ffmpeg()
                lo_final = lo.with_stem(lo.stem + "_final")
                self.update_queue.put(("status", {"file": src.name,
                    "action": "Combinando video + audio procesado..."}))
                mux_cmd = [
                    ffmpeg,
                    "-i", str(lo),        # HandBrake: video + subtítulos
                    "-i", str(lo_audio),  # FFmpeg: audio denoised
                    "-map", "0:v",        # video de HandBrake
                    "-map", "0:s?",       # subtítulos de HandBrake
                    "-map", "1:a",        # audio denoised
                    "-c", "copy",         # todo se copia sin re-encodear
                    str(lo_final),
                    "-y",
                    "-loglevel", "error",
                ]
                mux = subprocess.run(mux_cmd, capture_output=True,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
                if mux.returncode == 0 and lo_final.exists():
                    lo.unlink()
                    lo_final.rename(lo)
                    self.update_queue.put(("log",
                        f"  [Audio] Denoise paralelo OK (nf={audio_result.get('nf','?')} dBFS)\n"))
                else:
                    err = mux.stderr.decode(errors="replace").strip() if mux.stderr else ""
                    self.update_queue.put(("log",
                        f"  [Audio] Aviso: mux falló — se usa audio original. {err}\n"))
                    if lo_final and lo_final.exists():
                        lo_final.unlink()
            elif audio_thread:
                raw = audio_result.get('stderr') or b""
                err = raw.decode(errors="replace").strip() if isinstance(raw, bytes) else str(raw)
                self.update_queue.put(("log",
                    f"  [Audio] Aviso: denoise falló — se usa audio original. {err}\n"))

            # ── Verificar tamaño y mover al destino ───────────────────────────
            try:
                original_size = src.stat().st_size
                new_size      = lo.stat().st_size
                if new_size >= 0.9 * original_size:
                    self._update_db_entry(src, is_av1="NO_TRANSCODIFICAR", identified_by="Filtro Tamaño")
                    self.update_queue.put(("log",
                        f"OMITIDO (no ahorra suficiente): {src.name} "
                        f"({new_size/1024/1024:.1f} MB vs {original_size/1024/1024:.1f} MB original)\n"))
                else:
                    self.update_queue.put(("action", "Moviendo resultado al destino..."))
                    s = (original_size - new_size) / (1024 * 1024)
                    if self.safe_io(shutil.move, lo, ff):
                        self.update_queue.put(("stats", {"savings": s}))
                        self._update_db_entry(src, is_av1=True, identified_by="Transcodificado")
                        self.update_queue.put(("log", f"OK: {src.name}  (−{s:.1f} MB)\n"))
                        self.handle_subtitles(src, ff.parent, ff.stem)
            except Exception as e:
                self.update_queue.put(("log", f"Error post-proceso {src.name}: {e}\n"))
                self._update_db_entry(src, is_av1=False, identified_by="Error post-proceso")
        else:
            self.update_queue.put(("log", f"ERROR HandBrake (código {proc.returncode}): {src.name}\n"))
            self._update_db_entry(src, is_av1=False, identified_by="Error HandBrake")

        if li.exists():
            li.unlink()
        if lo.exists():
            lo.unlink()
        if lo_audio and lo_audio.exists():
            lo_audio.unlink()
        if lo_final and lo_final.exists():
            lo_final.unlink()

    def mark_no_transcode_selected(self):
        """Marca los archivos seleccionados como NO TRANSCODIFICAR."""
        selected = [s for s in self.tree.selection() if not s.startswith("f:")]
        if not selected:
            return
        for path_str in selected:
            entry = self.vistos_data.setdefault(path_str, {})
            entry['is_av1'] = "NO_TRANSCODIFICAR"
            entry['identified_by'] = "Manual"
            entry['seen'] = True
            # Quitar de la cola de transcodificación si estaba
            if path_str in self.transcode_queue:
                self.transcode_queue.remove(path_str)
        self._update_queue_button()
        self.save_vistos()
        self.render_library()
        self._update_lib_count()

    def unmark_no_transcode_selected(self):
        """Revierte archivos NO TRANSCODIFICAR → PENDIENTE."""
        selected = [s for s in self.tree.selection() if not s.startswith("f:")]
        if not selected:
            return
        changed = 0
        for path_str in selected:
            entry = self.vistos_data.get(path_str)
            if entry and entry.get('is_av1') == "NO_TRANSCODIFICAR":
                entry['is_av1'] = False
                entry['identified_by'] = "Manual"
                entry['seen'] = True
                changed += 1
        if changed == 0:
            messagebox.showinfo("Info", "Ningún archivo seleccionado estaba marcado como NO TRANSCODIFICAR.")
            return
        self.save_vistos()
        self.render_library()
        self._update_lib_count()

    def enqueue_selected(self):
        selected = [s for s in self.tree.selection() if not s.startswith("f:")]
        added              = 0
        av1_paths          = []   # archivos ya optimizados (AV1)
        no_transcode_paths = []   # archivos marcados como NO TRANSCODIFICAR

        for path_str in selected:
            entry = self.vistos_data.get(path_str, {})
            if entry.get("is_av1") is True:
                av1_paths.append(path_str)
            elif entry.get("is_av1") == "NO_TRANSCODIFICAR":
                no_transcode_paths.append(path_str)
            else:
                if path_str not in self.transcode_queue:
                    self.transcode_queue.append(path_str)
                    added += 1

        def _confirmar_forzado(paths, titulo, motivo):
            """Muestra alerta y devuelve los paths que el usuario quiere agregar."""
            nombres = [Path(p).name for p in paths]
            if len(nombres) <= 5:
                lista = "\n".join(f"  • {n}" for n in nombres)
            else:
                lista = "\n".join(f"  • {n}" for n in nombres[:5])
                lista += f"\n  … y {len(nombres) - 5} más"
            ok = messagebox.askyesno(
                titulo,
                f"Los siguientes {len(paths)} archivo(s) están marcados como {motivo}:\n\n"
                f"{lista}\n\n"
                f"¿Querés agregarlos a la cola de todas formas?",
            )
            return paths if ok else []

        # ── Alerta para archivos ya OPTIMIZADOS (AV1) ────────────────────────
        if av1_paths:
            for path_str in _confirmar_forzado(
                av1_paths,
                "Archivos ya optimizados (AV1)",
                "OPTIMIZADO (AV1)",
            ):
                if path_str not in self.transcode_queue:
                    self.transcode_queue.append(path_str)
                    added += 1

        # ── Alerta para archivos marcados como NO TRANSCODIFICAR ─────────────
        if no_transcode_paths:
            for path_str in _confirmar_forzado(
                no_transcode_paths,
                "Archivos marcados como NO TRANSCODIFICAR",
                "NO TRANSCODIFICAR\n(poco ahorro o marcado manual)",
            ):
                if path_str not in self.transcode_queue:
                    self.transcode_queue.append(path_str)
                    added += 1

        self._update_queue_button()
        msg = f"{added} archivo(s) agregados a la cola."
        if av1_paths and not any(p in self.transcode_queue for p in av1_paths):
            msg += f" ({len(av1_paths)} AV1 ignorados)"
        if no_transcode_paths and not any(p in self.transcode_queue for p in no_transcode_paths):
            msg += f" ({len(no_transcode_paths)} NO TRANSCODIFICAR ignorados)"
        self.scan_status_label.configure(text=msg)

    def _update_queue_button(self):
        n = len(self.transcode_queue)
        if n == 0:
            self.queue_btn.configure(text="Cola vacía", state="disabled", fg_color="#2c3e50")
        elif self.is_processing:
            self.queue_btn.configure(text=f"Procesando... ({n} restantes)", state="disabled", fg_color="#e67e22")
        else:
            self.queue_btn.configure(text=f"📋  Ver cola ({n})", state="normal", fg_color="#27ae60")

    def show_queue_manager(self):
        """Ventana para revisar, reordenar y gestionar la cola antes de iniciar."""
        if self.is_processing:
            messagebox.showinfo("En curso", "Ya hay una transcodificación en curso.")
            return

        win = ctk.CTkToplevel(self)
        win.title("Cola de Transcodificación")
        win.geometry("820x540")
        win.resizable(True, True)
        win.grab_set()
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        # ── Header ───────────────────────────────────────────────────────────
        header = ctk.CTkFrame(win)
        header.grid(row=0, column=0, padx=10, pady=(10, 0), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        count_label = ctk.CTkLabel(header, text="", font=("Segoe UI", 13, "bold"))
        count_label.grid(row=0, column=0, padx=15, pady=8, sticky="w")
        hint_label = ctk.CTkLabel(
            header,
            text="Seleccioná uno o varios · Supr para quitar · arrastrá para reordenar",
            font=("Segoe UI", 10),
            text_color="#888888",
        )
        hint_label.grid(row=0, column=1, padx=15, pady=8, sticky="e")

        # ── Listbox ───────────────────────────────────────────────────────────
        list_frame = ctk.CTkFrame(win)
        list_frame.grid(row=1, column=0, padx=10, pady=8, sticky="nsew")
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(0, weight=1)

        scrollbar = tk.Scrollbar(list_frame, orient="vertical")
        lb = tk.Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            selectmode="extended",
            bg="#111827",
            fg="#e5e7eb",
            selectbackground="#3b8ed0",
            selectforeground="white",
            font=("Consolas", 10),
            borderwidth=0,
            highlightthickness=0,
            activestyle="none",
            relief="flat",
        )
        scrollbar.config(command=lb.yview)
        lb.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        # ── Funciones internas ────────────────────────────────────────────────
        def _refresh():
            sel = list(lb.curselection())
            lb.delete(0, tk.END)
            for i, path_str in enumerate(self.transcode_queue):
                p = Path(path_str)
                parts = p.parts
                # Mostrar hasta 2 carpetas padre + nombre de archivo
                if len(parts) >= 3:
                    display = f"  {i+1:>3}.  …/{parts[-3]}/{parts[-2]}/{p.name}"
                elif len(parts) == 2:
                    display = f"  {i+1:>3}.  {parts[-2]}/{p.name}"
                else:
                    display = f"  {i+1:>3}.  {p.name}"
                lb.insert(tk.END, display)
            # Zebra coloring
            for i in range(lb.size()):
                lb.itemconfig(i, bg="#111827" if i % 2 == 0 else "#1a2336")
            # Restaurar selección si es válida
            for idx in sel:
                if idx < lb.size():
                    lb.selection_set(idx)
            n = len(self.transcode_queue)
            count_label.configure(text=f"{n} archivo{'s' if n != 1 else ''} en cola")
            start_btn.configure(state="normal" if n > 0 else "disabled")
            self._update_queue_button()

        def _remove_selected():
            indices = sorted(lb.curselection(), reverse=True)
            if not indices:
                return
            for idx in indices:
                self.transcode_queue.pop(idx)
            _refresh()

        def _clear_all():
            if not self.transcode_queue:
                return
            if messagebox.askyesno("Confirmar", "¿Limpiar toda la cola?", parent=win):
                self.transcode_queue.clear()
                _refresh()

        def _move_up():
            indices = list(lb.curselection())
            if not indices or indices[0] == 0:
                return
            for idx in indices:
                if idx == 0:
                    continue
                self.transcode_queue[idx - 1], self.transcode_queue[idx] = (
                    self.transcode_queue[idx], self.transcode_queue[idx - 1])
            _refresh()
            lb.selection_clear(0, tk.END)
            for idx in indices:
                lb.selection_set(max(0, idx - 1))
            lb.see(max(0, indices[0] - 1))

        def _move_down():
            indices = list(lb.curselection())
            if not indices or indices[-1] >= len(self.transcode_queue) - 1:
                return
            for idx in reversed(indices):
                if idx >= len(self.transcode_queue) - 1:
                    continue
                self.transcode_queue[idx], self.transcode_queue[idx + 1] = (
                    self.transcode_queue[idx + 1], self.transcode_queue[idx])
            _refresh()
            lb.selection_clear(0, tk.END)
            for idx in indices:
                lb.selection_set(min(len(self.transcode_queue) - 1, idx + 1))
            lb.see(min(lb.size() - 1, indices[-1] + 1))

        def _start_and_close():
            win.grab_release()
            win.destroy()
            self.start_transcoding_from_queue()

        # Teclado: Supr para quitar, flechas para mover
        lb.bind("<Delete>",    lambda e: _remove_selected())
        lb.bind("<BackSpace>", lambda e: _remove_selected())
        lb.bind("<Up>",        lambda e: (_move_up(),   "break"))
        lb.bind("<Down>",      lambda e: (_move_down(), "break"))

        # Menú contextual
        ctx = tk.Menu(win, tearoff=0, bg="#2c3e50", fg="white",
                      activebackground="#3498db", activeforeground="white")
        ctx.add_command(label="↑ Mover arriba",    command=_move_up)
        ctx.add_command(label="↓ Mover abajo",     command=_move_down)
        ctx.add_separator()
        ctx.add_command(label="✕ Quitar seleccionado", command=_remove_selected)
        ctx.add_command(label="Limpiar toda la cola",  command=_clear_all)

        def _show_ctx(event):
            idx = lb.nearest(event.y)
            if idx >= 0:
                lb.selection_set(idx)
            ctx.tk_popup(event.x_root, event.y_root)

        lb.bind("<Button-3>", _show_ctx)

        # ── Botonera inferior ─────────────────────────────────────────────────
        btn_frame = ctk.CTkFrame(win, fg_color="transparent")
        btn_frame.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="ew")

        ctk.CTkButton(btn_frame, text="↑ Subir",   width=85,
                      command=_move_up).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text="↓ Bajar",   width=85,
                      command=_move_down).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text="✕ Quitar",  width=90,
                      fg_color="#c0392b", hover_color="#a93226",
                      command=_remove_selected).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text="Limpiar todo", width=110,
                      fg_color="#7f8c8d", hover_color="#6d7a8a",
                      command=_clear_all).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text="Cerrar", width=80,
                      fg_color="transparent", border_width=1,
                      command=win.destroy).pack(side="right", padx=4)
        start_btn = ctk.CTkButton(
            btn_frame, text="▶  Iniciar Transcodificación", width=210,
            fg_color="#27ae60", hover_color="#219150",
            font=("Segoe UI", 13, "bold"),
            command=_start_and_close,
        )
        start_btn.pack(side="right", padx=8)

        _refresh()

    def start_transcoding_from_queue(self):
        if self.is_processing or not self.transcode_queue:
            return
        if not self.output_dir.get():
            messagebox.showwarning("Atención", "Configurá la carpeta destino en la pestaña Transcodificador.")
            return
        self.is_processing = True
        self.queue_btn.configure(state="disabled", text=f"Procesando... ({len(self.transcode_queue)} restantes)")
        threading.Thread(target=self.transcoding_engine_from_queue, daemon=True).start()

    def transcoding_engine_from_queue(self):
        self.prevent_sleep()
        try:
            input_root = Path(self.input_dir.get())
            output_root = Path(self.output_dir.get())
            temp_dir = output_root / "temp"
            (temp_dir / "Input").mkdir(parents=True, exist_ok=True)
            (temp_dir / "Output").mkdir(parents=True, exist_ok=True)

            # ── Pipeline de pre-carga continua ───────────────────────────────
            # Máximo de archivos pre-descargados esperando en cola (ajustar según
            # espacio en disco local; 2 es un balance razonable).
            MAX_PREFETCH_AHEAD = 2

            prefetch_cache   = {}   # path_str → result dict (listo o en progreso)
            prefetch_threads = {}   # path_str → Thread
            prefetch_lock    = threading.Lock()

            def _launch_prefetch_for(path_str):
                """Inicia la pre-carga de path_str si aún no está en caché."""
                with prefetch_lock:
                    if path_str in prefetch_cache or not self.is_processing:
                        return
                    src_p = Path(path_str)
                    if not src_p.exists():
                        return
                    result = {}
                    prefetch_cache[path_str] = result

                def _on_done():
                    """Al terminar esta descarga, encadenar la siguiente si hay lugar."""
                    current = self.transcode_queue[0] if self.transcode_queue else None
                    with prefetch_lock:
                        buffered = sum(1 for k in prefetch_cache if k != current)
                    if buffered >= MAX_PREFETCH_AHEAD:
                        return
                    # Buscar el primer archivo de la cola sin pre-carga (omitir índice 0)
                    for i, p in enumerate(self.transcode_queue):
                        if i == 0:
                            continue
                        with prefetch_lock:
                            already = p in prefetch_cache
                        if not already:
                            _launch_prefetch_for(p)
                            break

                def _worker():
                    self._prefetch_verify_and_copy(src_p, input_root, output_root, temp_dir, result)
                    _on_done()

                t = threading.Thread(target=_worker, daemon=True)
                with prefetch_lock:
                    prefetch_threads[path_str] = t
                t.start()
                self.update_queue.put(("log",
                    f"  [Pipeline] Iniciando pre-carga: {src_p.name}\n"))

            while self.transcode_queue and self.is_processing:
                path_str = self.transcode_queue[0]
                src = Path(path_str)

                if not src.exists():
                    self.update_queue.put(("log", f"Archivo no encontrado, saltando: {src.name}\n"))
                    self.transcode_queue.pop(0)
                    with prefetch_lock:
                        prefetch_cache.pop(path_str, None)
                        prefetch_threads.pop(path_str, None)
                    self.update_queue.put(("queue_updated", len(self.transcode_queue)))
                    continue

                # Esperar prefetch si ya fue lanzado para este archivo
                cached = {}
                with prefetch_lock:
                    t_ref = prefetch_threads.get(path_str)
                if t_ref is not None:
                    if t_ref.is_alive():
                        self.update_queue.put(("action", f"Esperando pre-carga de {src.name}..."))
                        t_ref.join()
                    with prefetch_lock:
                        cached = prefetch_cache.get(path_str, {})

                # Si el prefetch determinó que se debe saltar, hacerlo directamente
                if cached.get('skip'):
                    self.transcode_queue.pop(0)
                    with prefetch_lock:
                        prefetch_cache.pop(path_str, None)
                        prefetch_threads.pop(path_str, None)
                    self.update_queue.put(("queue_updated", len(self.transcode_queue)))
                    continue

                # Callback justo antes de HandBrake: lanzar la siguiente pre-carga
                # (y ella misma encadenará la siguiente cuando termine)
                def start_next_prefetch():
                    for i, p in enumerate(self.transcode_queue):
                        if i == 0:
                            continue  # archivo en proceso actual
                        with prefetch_lock:
                            already = p in prefetch_cache
                        if not already:
                            _launch_prefetch_for(p)
                            break

                try:
                    self.process_single_file(src, input_root, output_root, temp_dir,
                                             on_transcode_start=start_next_prefetch,
                                             prefetch_data=cached)
                except Exception as e:
                    self.update_queue.put(("log", f"Error procesando {src.name}: {e}\n"))

                if self.transcode_queue and self.transcode_queue[0] == path_str:
                    self.transcode_queue.pop(0)
                with prefetch_lock:
                    prefetch_cache.pop(path_str, None)
                    prefetch_threads.pop(path_str, None)
                self.update_queue.put(("queue_updated", len(self.transcode_queue)))

            self.update_queue.put(("finished", None))
            self.update_queue.put(("queue_updated", len(self.transcode_queue)))
        finally:
            self.allow_sleep()

    def start_cleanup(self):
        if not messagebox.askyesno("Limpiar faltantes", "Esto eliminará de la DB los archivos que ya no existen en el NAS.\n¿Continuar?"):
            return
        self.cleanup_button.configure(state="disabled", text="Limpiando...")
        threading.Thread(target=self.cleanup_worker, daemon=True).start()

    def cleanup_worker(self):
        paths = list(self.vistos_data.keys())
        total = len(paths)
        removed = 0
        for i, p in enumerate(paths):
            if i % 50 == 0:
                self.update_queue.put(("scan_progress", f"Verificando {i}/{total} | Eliminados: {removed}..."))
            if not Path(p).exists():
                del self.vistos_data[p]
                removed += 1
        self.all_found_files = self._db_to_file_list()
        self.save_vistos()
        self.update_queue.put(("scan_progress", f"Limpieza completada: {removed} archivos eliminados."))
        self.update_queue.put(("cleanup_done", removed))

    def _find_ffprobe(self):
        if hasattr(self, '_ffprobe_cache'):
            return self._ffprobe_cache
        hb = shutil.which(HANDBRAKE_CLI_PATH)
        if hb:
            candidate = Path(hb).parent / "ffprobe.exe"
            if candidate.exists():
                self._ffprobe_cache = str(candidate)
                return self._ffprobe_cache
        found = shutil.which("ffprobe")
        self._ffprobe_cache = found
        return found

    def _find_ffmpeg(self):
        if hasattr(self, '_ffmpeg_cache'):
            return self._ffmpeg_cache
        hb = shutil.which(HANDBRAKE_CLI_PATH)
        if hb:
            candidate = Path(hb).parent / "ffmpeg.exe"
            if candidate.exists():
                self._ffmpeg_cache = str(candidate)
                return self._ffmpeg_cache
        found = shutil.which("ffmpeg")
        self._ffmpeg_cache = found
        return found

    def _check_is_av1_ffprobe(self, p, ffprobe_path):
        try:
            result = subprocess.run(
                [ffprobe_path, "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(p)],
                capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            codec = result.stdout.strip().lower()
            return codec == "av1" if codec else None
        except Exception:
            return None

    def check_is_av1(self, p):
        ffprobe = self._find_ffprobe()
        if ffprobe:
            result = self._check_is_av1_ffprobe(p, ffprobe)
            if result is not None:
                return result
        try:
            m = MediaInfo.parse(p)
            for t in m.tracks:
                if t.track_type == 'Video' and 'AV1' in (t.format or '').upper():
                    return True
            return False
        except Exception as e:
            self.log(f"MediaInfo falló en {Path(p).name}: {e}")
            return False

    def _detect_subtitle_langs(self, filepath: Path) -> list:
        """Retorna lista de códigos de idioma para los subtítulos que acompañan al video."""
        stem_lower = filepath.stem.lower()
        langs = []
        try:
            for f in filepath.parent.iterdir():
                if f.suffix.lower() not in SUBTITLE_EXTENSIONS:
                    continue
                if not f.name.lower().startswith(stem_lower):
                    continue
                rest = f.name[len(filepath.stem):]
                if not rest.startswith('.'):
                    continue
                rest_no_ext = rest[:len(rest) - len(f.suffix)].lstrip('.')
                langs.append(rest_no_ext.lower() if rest_no_ext else "und")
        except Exception:
            pass
        return langs

    def _sub_display(self, path_str: str) -> str:
        entry = self.vistos_data.get(path_str, {})
        subs = entry.get("subtitles")
        if not subs:
            return "—"
        langs = ", ".join(s.upper() for s in subs if s and s != "und")
        label = langs if langs else "SÍ"
        return f"✓ {label}" if entry.get("subs_done") else label

    def handle_subtitles(self, src: Path, out_dir: Path, new_stem: str):
        """Busca subtítulos relacionados al video src, los renombra (y convierte si son .ass) al destino."""
        src_stem_lower = src.stem.lower()
        try:
            candidates = [
                f for f in src.parent.iterdir()
                if f.suffix.lower() in SUBTITLE_EXTENSIONS
                and f.name.lower().startswith(src_stem_lower)
                and f.name[len(src.stem):len(src.stem)+1] in ('.', '')
            ]
        except Exception:
            return

        count = 0
        for sub in candidates:
            rest = sub.name[len(src.stem):]          # e.g. ".es.srt" | ".srt" | ".en.ass"
            sub_ext = sub.suffix.lower()
            rest_no_ext = rest[:len(rest) - len(sub_ext)]  # e.g. ".es" | ""

            if sub_ext in {'.ass', '.ssa'}:
                new_name = new_stem + rest_no_ext + '.srt'
                dst = out_dir / new_name
                try:
                    self._convert_ass_to_srt(sub, dst)
                    count += 1
                    self.update_queue.put(("log", f"  Subtítulo convertido ASS→SRT: {new_name}\n"))
                except Exception as e:
                    self.update_queue.put(("log", f"  Error convirtiendo {sub.name}: {e}\n"))
            else:
                new_name = new_stem + rest
                dst = out_dir / new_name
                try:
                    shutil.copy2(sub, dst)
                    count += 1
                    self.update_queue.put(("log", f"  Subtítulo copiado: {new_name}\n"))
                except Exception as e:
                    self.update_queue.put(("log", f"  Error copiando {sub.name}: {e}\n"))

        if count:
            self.update_queue.put(("log", f"  {count} subtítulo(s) procesados para {src.name}\n"))
            if str(src) in self.vistos_data:
                self.vistos_data[str(src)]["subs_done"] = True
                self.save_vistos()

    def _convert_ass_to_srt(self, src: Path, dst: Path):
        """Convierte .ass/.ssa a .srt usando Python puro."""
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
        srt_blocks = []
        for i, (_, start, end, text) in enumerate(dialogues, 1):
            srt_blocks.append(f"{i}\n{start} --> {end}\n{text}\n")
        dst.write_text('\n'.join(srt_blocks), encoding='utf-8')

    def safe_io(self, f, s, d):
        for _ in range(MAX_RETRIES):
            try:
                f(s, d)
                return True
            except Exception:
                time.sleep(NETWORK_RETRY_DELAY)
        return False

    def prevent_sleep(self):
        if os.name == 'nt':
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
                self.update_queue.put(("log", "[SISTEMA] Suspensión de Windows prevenida.\n"))
            except Exception:
                pass

    def allow_sleep(self):
        if os.name == 'nt':
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
                self.update_queue.put(("log", "[SISTEMA] Suspensión de Windows restaurada.\n"))
            except Exception:
                pass

if __name__ == "__main__":
    app = TranscoderApp()
    app.mainloop()
