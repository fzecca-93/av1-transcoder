# AV1 Ultra Transcoder & Library Manager

A Windows desktop application to scan, catalog, and mass-transcode your media library from a NAS to AV1 format using NVIDIA GPU acceleration — with automated audio denoising, a continuous pre-fetch pipeline, and a full-featured library browser.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows)
![GPU](https://img.shields.io/badge/GPU-NVIDIA%20NVENC-76B900?logo=nvidia)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Features

### Video Transcoding
- **NVIDIA NVENC AV1 10-bit** encoding via HandBrakeCLI (GPU-accelerated)
- Two encoding presets:
  - **Movie / Series** — CQ 30, no filters
  - **Anime / Animation** — CQ 32 + NLMeans light denoise tuned for animation
- Automatic size filter: skips files where the output would not save at least 10% space

### Audio Processing
- **Parallel audio denoising** with FFmpeg `afftdn` filter (FFT-based, ideal for fans, HVAC, hum)
- Runs simultaneously with HandBrake video encoding — zero extra time on most files
- Three intensity levels: Soft (`nf=-30`), Normal (`nf=-25`), Strong (`nf=-20`)
- Single audio encode (HandBrake passthrough → FFmpeg denoise → final mux)
- **Standalone audio improvement** option: denoise any file without re-encoding the video

### Pipeline & Queue
- **Continuous pre-fetch pipeline**: downloads the next N files from NAS to local SSD while the current file is transcoding
- Up to 2 files buffered ahead (configurable via `MAX_PREFETCH_AHEAD`)
- **Queue manager window**: view, reorder (↑↓), and remove files before starting
- Multi-select support with Ctrl+Click / Shift+Click

### Library Browser
- Flat and **folder tree view** with color-coded status per folder
- File status tracking: `PENDING`, `AV1 (optimized)`, `NO TRANSCODE`, `NEW`, `UNKNOWN`, `READY LOCAL`
- Filters: All / New / Pending / AV1 / No Transcode / Ready Local / Unknown
- Stats bar: total files, AV1, pending, skipped, unknown

### Library Management
- **Multi-library support**: switch between Movies, Series, or any custom library
- Persistent **JSON database** per library — no NAS scan needed at startup
- Three scan methods:
  1. **Incremental scan** — filename-based, instant
  2. **Deep scan** — ffprobe/MediaInfo codec verification
  3. **Jellyfin Excel import** — import codec data from a Jellyfin report
- Right-click actions:
  - Add to transcoding queue (with confirmation for AV1 / No-Transcode files)
  - Mark / unmark as NO TRANSCODE
  - Improve audio only (no video re-encode)
  - Move local file back to NAS
  - Open folder in Explorer
  - Deep analyze with MediaInfo

### System
- Prevents Windows sleep during long transcoding jobs
- Subtitle handling: copies `.srt`, `.ass`, `.ssa`, `.sub`, `.vtt` files to destination
- Network retry logic for NAS operations
- All subprocesses run without console window flashes

---

## Requirements

### Hardware
- **NVIDIA GPU** with NVENC AV1 support (RTX 3000 series or newer recommended; RTX 4070 or better for best performance)
- Network-attached storage (NAS) or any network share accessible via UNC path or mapped drive

### Software
- Windows 10 / 11 (64-bit)
- [HandBrakeCLI](https://handbrake.fr/downloads2.php) — place `HandBrakeCLI.exe` in the same folder as the app
- [FFmpeg & FFprobe](https://www.gyan.dev/ffmpeg/builds/) — place `ffmpeg.exe` and `ffprobe.exe` in the same folder as the app

### Python (for running from source)
- Python 3.11+
- Dependencies listed in `requirements.txt`

---

## Installation

### Option A — Standalone Executable (recommended)

1. Download the latest release from the [Releases](../../releases) page
2. Extract the ZIP — you should have:
   ```
   AV1_Transcoder.exe
   HandBrakeCLI.exe       ← you must provide this
   ffmpeg.exe             ← you must provide this
   ffprobe.exe            ← you must provide this
   ```
3. Run `AV1_Transcoder.exe`

> **Note:** The executable does **not** bundle HandBrakeCLI or FFmpeg. You must download them separately and place them in the same directory.

### Option B — Run from Source

```bash
# 1. Clone the repository
git clone https://github.com/YOUR_USERNAME/av1-transcoder.git
cd av1-transcoder

# 2. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Place HandBrakeCLI.exe, ffmpeg.exe, ffprobe.exe in the project folder

# 5. Run
python transcode_av1.py
```

Alternatively, double-click `iniciar.bat` — it sets up the venv and runs the app automatically.

---

## Building the Executable

```bash
# Activate venv first
venv\Scripts\activate

# Build
compilar.bat
```

The output will be at `dist\AV1_Transcoder.exe`. Copy `HandBrakeCLI.exe`, `ffmpeg.exe`, and `ffprobe.exe` to the same folder before distributing.

---

## Usage

### 1. Set Up Libraries

Click the library selector at the top → **Add Library** → enter a name, source path (NAS), and local destination path (SSD).

### 2. Scan for New Files

In the **Library** tab:
- **1. FIND NEW (NAS)** — quick filename-based scan
- **2. IMPORT JELLYFIN EXCEL** — import codec metadata from a Jellyfin report
- **3. DEEP ANALYZE (MediaInfo)** — verify actual codec for unidentified files

### 3. Queue Files for Transcoding

Select files in the library → right-click → **+ Add to transcoding queue**.

Click **📋 View Queue (N)** to open the queue manager where you can reorder or remove files before starting.

### 4. Configure Encoding Options

In the **Transcoder** tab:
- **Mode**: Movie/Series or Anime/Animation
- **Audio**: Enable noise reduction and choose intensity

### 5. Start

Click **▶ Start Transcoding** in the queue manager window.

---

## Configuration

The app stores its configuration in `config.json` (created automatically on first run). See [`config.example.json`](config.example.json) for the structure.

Each library has:
| Field | Description |
|---|---|
| `name` | Display name |
| `input_dir` | Source path (NAS drive letter or UNC path) |
| `output_dir` | Local destination path (SSD recommended) |

---

## File Status Reference

| Status | Color | Meaning |
|---|---|---|
| `PENDING` | Blue | Needs transcoding |
| `NEW` | Orange | Detected but not yet seen |
| `AV1 (optimized)` | Green | Already in AV1 format |
| `NO TRANSCODE` | Purple | Skipped (low savings or manual) |
| `READY LOCAL` | Yellow | Transcoded file exists locally, not yet on NAS |
| `UNKNOWN` | Gray | Codec not yet identified |

---

## Project Structure

```
av1-transcoder/
├── transcode_av1.py        # Main application source
├── requirements.txt        # Python dependencies
├── compilar.bat            # Build script (PyInstaller)
├── iniciar.bat             # Launch script (auto-setup venv)
├── AV1_Transcoder.spec     # PyInstaller spec
├── config.example.json     # Example configuration
└── README.md
```

**Runtime files** (created automatically, not committed):
```
config.json                 # Your library configuration
biblioteca_<name>.json      # Per-library media database
```

**Required alongside the exe** (not included, download separately):
```
HandBrakeCLI.exe
ffmpeg.exe
ffprobe.exe
```

---

## Architecture Notes

- **Threading model**: all heavy operations (HandBrake, MediaInfo, NAS copy) run in daemon threads; UI updates go through a `queue.Queue` polled every 100ms on the main thread
- **Pre-fetch pipeline**: a chained thread system downloads the next file(s) from NAS while the GPU is transcoding, keeping the GPU fed continuously
- **Audio pipeline**: FFmpeg audio thread starts alongside HandBrake; after both finish, a fast mux combines them — the audio cost is essentially free
- **Database**: one JSON file per library maps file paths to `{mtime, is_av1, seen, identified_by, size}`

---

## Contributing

Pull requests are welcome. For major changes, please open an issue first.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes
4. Push and open a Pull Request

---

## License

[MIT](LICENSE)

---

## Acknowledgements

- [HandBrake](https://handbrake.fr/) — video transcoding engine
- [FFmpeg](https://ffmpeg.org/) — audio processing and muxing
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — modern GUI framework
- [pymediainfo](https://github.com/sbraz/pymediainfo) — media metadata
