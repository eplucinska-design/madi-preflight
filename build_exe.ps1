# Builds dist\MADI Preflight\MADI Preflight.exe (one folder, no installation needed).
# Optional: -Target "X:\folder" copies the finished folder there.
param([string]$Target = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
# Library modules only: engine scripts (workflow, spots, sample) ship as data and run via --engine;
# "workflow" would also clash with an unrelated PyInstaller contrib hook.
$engine = 'server','inspector','imposer','cleanup_marks','colors','separations','foil_preflight' | ForEach-Object { "--hidden-import=$_" }
python -m PyInstaller --noconfirm --clean --onedir --name "MADI Preflight" `
  --icon static\favicon.ico `
  --paths . --paths engine `
  --add-data "static;static" --add-data "engine;engine" `
  --add-data "LICENSE;." --add-data "README.md;." `
  @engine `
  --collect-submodules pikepdf `
  desktop.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if ($Target) {
  $dest = Join-Path $Target "MADI Preflight"
  if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
  Copy-Item -Recurse "dist\MADI Preflight" $dest
  Write-Host "Skopiowano do $dest"
}
