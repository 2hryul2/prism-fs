# build.ps1 -- prism-fs release build (PyInstaller onedir full bundle)
# Windows 11 / PowerShell. Output name injected from VERSION file.
# ASCII-only on purpose: PowerShell 5.1 mis-parses non-ASCII .ps1 without a BOM.
# Output: dist\setup\setup_v{VERSION}\setup_v{VERSION}.exe (+ _internal\, storage\)
#
# Usage:  .\build.ps1
# Note:   run the server on :8021 first so live tests gate too (recommended).

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

# 1) Load VERSION -> inject name
$version = (Get-Content (Join-Path $root "VERSION") -Raw).Trim()
$name = "setup_v$version"
$env:PRISM_VERSION = $version
Write-Host "==> prism-fs build v$version (onedir full bundle)" -ForegroundColor Cyan

# 2) Gate -- unit tests must pass
Write-Host "==> [gate] pytest" -ForegroundColor Cyan
python -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw "pytest failed -- build aborted" }

# 2b) External https must be 0 (closed-network regression gate)
$ext = Select-String -Path "src\static\index.html" -Pattern "https://" -SimpleMatch |
       Where-Object { $_.Line -notmatch "w3\.org|json-schema\.org" }
if ($ext) { $ext; throw "external https reference found -- build aborted (closed-net)" }
Write-Host "    external https = 0 OK" -ForegroundColor Green

# 3) Build deps + model
python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "==> install PyInstaller"; pip install pyinstaller }
if (-not (Test-Path "src\models\ko-sroberta")) {
    Write-Host "==> export ko-sroberta model (first run)"
    $env:HF_HUB_OFFLINE = "1"
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('jhgan/ko-sroberta-multitask').save('src/models/ko-sroberta')"
}

# 4) Clean previous output, then build
if (Test-Path "dist")  { Remove-Item "dist"  -Recurse -Force }
if (Test-Path "build\work") { Remove-Item "build\work" -Recurse -Force }
Write-Host "==> PyInstaller (several minutes, ~2.5GB)" -ForegroundColor Cyan
pyinstaller prism_fs.spec --noconfirm --distpath "dist\setup" --workpath "build\work"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 5) Ship demo data (storage) next to exe (persistent). Exclude .env (secret).
$target = "dist\setup\$name"
if (Test-Path "src\storage") {
    Write-Host "==> bundling storage (demo data)" -ForegroundColor Cyan
    Copy-Item "src\storage" -Destination (Join-Path $target "storage") -Recurse -Force
}

# 6) Report
$exe = Join-Path $target "$name.exe"
if (Test-Path $exe) {
    $sizeGB = [math]::Round(((Get-ChildItem $target -Recurse | Measure-Object Length -Sum).Sum / 1GB), 2)
    Write-Host "==> done: $exe  (total $sizeGB GB)" -ForegroundColor Green
} else {
    throw "build artifact (.exe) missing: $exe"
}
