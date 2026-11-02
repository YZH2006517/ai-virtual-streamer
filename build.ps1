# 打包成单个 exe：release\AI唠嗑主播.exe。需要 uv（https://docs.astral.sh/uv/）。
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
if (-not (Test-Path "$Root\.venv")) { uv venv --python 3.11 "$Root\.venv" }
uv pip install --python "$Root\.venv\Scripts\python.exe" -r "$Root\requirements.txt"
$scenes = Get-ChildItem "$Root\src\scene_*.webp" | ForEach-Object { "--add-data"; "$($_.FullName);." }
& "$Root\.venv\Scripts\pyinstaller.exe" --noconfirm --onefile --windowed --name "AI唠嗑主播" --distpath "$Root\release" `
  --workpath "$Root\build" --specpath "$Root\build" --paths "$Root\src" `
  --add-data "$Root\src\ui.html;." --add-data "$Root\seed;seed" @scenes `
  --collect-all webview --collect-all clr_loader --collect-all pythonnet --collect-all imageio_ffmpeg --collect-all _sounddevice_data `
  --collect-data certifi --hidden-import edge_tts --hidden-import pypinyin --collect-data pypinyin `
  --exclude-module tkinter.test "$Root\src\main.py"
Get-Item "$Root\release\AI唠嗑主播.exe" | Select-Object Name, @{n="MB";e={[int]($_.Length/1MB)}}
