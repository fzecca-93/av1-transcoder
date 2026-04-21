# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2025-04-21

### Initial public release

#### Core Features
- NVIDIA NVENC AV1 10-bit encoding via HandBrakeCLI
- Two encoding modes: Movie/Series (CQ 30) and Anime/Animation (CQ 32 + NLMeans)
- Multi-library support with persistent JSON database per library
- Three scan methods: incremental, deep (MediaInfo/ffprobe), Jellyfin Excel import
- Library browser with flat and folder tree views, color-coded by status
- File status system: Pending, AV1, No Transcode, New, Unknown, Ready Local

#### Pipeline
- Continuous pre-fetch pipeline: downloads next files from NAS while GPU encodes
- Up to 2 files buffered ahead simultaneously
- Queue manager window with reorder, remove, and multi-select support

#### Audio
- Parallel audio denoising with FFmpeg `afftdn` (FFT noise reduction)
- Runs concurrently with HandBrake — essentially zero extra time
- Three intensities: Soft / Normal / Strong
- Single audio encode (passthrough + FFmpeg denoise + mux)
- Standalone audio improvement: denoise any file without re-encoding video

#### Library Actions (right-click)
- Add to queue with confirmation dialogs for AV1 and No-Transcode files
- Mark / unmark as NO TRANSCODE
- Improve audio only (standalone denoise)
- Move local file back to NAS
- Deep analyze with MediaInfo
- Open folder in Explorer

#### System
- Windows sleep prevention during transcoding
- No console window flashes on subprocess calls (`CREATE_NO_WINDOW`)
- Network retry logic for NAS I/O
- Subtitle copy: `.srt`, `.ass`, `.ssa`, `.sub`, `.vtt`
