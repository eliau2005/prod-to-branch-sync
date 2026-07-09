<#
  build.ps1 — produce the self-contained Windows installer for Prod Branch Sync.

  Stages a fully self-contained app folder (bundled Node + bundled PostgreSQL
  client tools + the web UI), generates an icon, then compiles the Inno Setup
  installer.

  Usage:
    pwsh -File packaging/build.ps1                 # full build (stage + installer)
    pwsh -File packaging/build.ps1 -StageOnly      # just stage dist/app, no installer
    pwsh -File packaging/build.ps1 -NodeExe <path> -PgBin <dir>   # override autodetect
#>
[CmdletBinding()]
param(
  [string]$NodeExe,
  [string]$PgBin,
  [string]$Version = '1.0.1',
  [switch]$StageOnly
)
$ErrorActionPreference = 'Stop'

$pkgDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$root    = Split-Path -Parent $pkgDir
$dist    = Join-Path $root 'dist'
$appOut  = Join-Path $dist 'app'

function Info($m){ Write-Host "[build] $m" -ForegroundColor Cyan }
function Die($m){ Write-Host "[build] ERROR: $m" -ForegroundColor Red; exit 1 }

# ---- 1. Resolve toolchain ----------------------------------------------------
if (-not $NodeExe) { $NodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source }
if (-not $NodeExe -or -not (Test-Path $NodeExe)) { Die "node.exe not found. Pass -NodeExe <path>." }

if (-not $PgBin) {
  $psql = (Get-Command psql -ErrorAction SilentlyContinue).Source
  if ($psql) { $PgBin = Split-Path -Parent $psql }
}
if (-not $PgBin -or -not (Test-Path (Join-Path $PgBin 'pg_dump.exe'))) {
  Die "PostgreSQL client tools (pg_dump.exe) not found. Pass -PgBin <dir-with-psql.exe>."
}
Info "node:   $NodeExe"
Info "pg bin: $PgBin"

# ---- 2. Stage the app folder -------------------------------------------------
if (Test-Path $appOut) { Remove-Item $appOut -Recurse -Force }
New-Item -ItemType Directory -Force -Path $appOut | Out-Null

Info "Copying app files…"
foreach ($f in @('server.mjs','launcher.mjs','launch.vbs','package.json','README.md')) {
  $src = Join-Path $root $f
  if (Test-Path $src) { Copy-Item $src -Destination $appOut }
}
Copy-Item (Join-Path $root 'public') -Destination $appOut -Recurse

Info "Bundling Node runtime…"
Copy-Item $NodeExe -Destination (Join-Path $appOut 'node.exe')

Info "Bundling PostgreSQL client tools (this can be ~30MB)…"
$pgOut = Join-Path $appOut 'pgsql\bin'
New-Item -ItemType Directory -Force -Path $pgOut | Out-Null
# Copy the whole bin dir so psql/pg_dump get their DLLs.
Copy-Item (Join-Path $PgBin '*') -Destination $pgOut -Recurse

# ---- 3. Generate the icon ----------------------------------------------------
$icoPath = Join-Path $pkgDir 'app.ico'
Info "Generating icon…"
Add-Type -AssemblyName System.Drawing
$size = 256
$bmp  = New-Object System.Drawing.Bitmap $size, $size
$g    = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.Clear([System.Drawing.Color]::Transparent)
# Green rounded-square background (#3ECF8E).
$green = [System.Drawing.Color]::FromArgb(62, 207, 142)
$brush = New-Object System.Drawing.SolidBrush $green
$rect  = New-Object System.Drawing.Drawing2D.GraphicsPath
$r = 48; $d = $r * 2; $m = 8; $w = $size - 2*$m
$rect.AddArc($m, $m, $d, $d, 180, 90)
$rect.AddArc($m + $w - $d, $m, $d, $d, 270, 90)
$rect.AddArc($m + $w - $d, $m + $w - $d, $d, $d, 0, 90)
$rect.AddArc($m, $m + $w - $d, $d, $d, 90, 90)
$rect.CloseFigure()
$g.FillPath($brush, $rect)
# White right-arrow (transfer/sync).
$white = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
$pts = @(
  [System.Drawing.Point]::new(64,108),  [System.Drawing.Point]::new(150,108),
  [System.Drawing.Point]::new(150,76),  [System.Drawing.Point]::new(206,128),
  [System.Drawing.Point]::new(150,180), [System.Drawing.Point]::new(150,148),
  [System.Drawing.Point]::new(64,148)
)
$g.FillPolygon($white, $pts)
$g.Dispose()

# Wrap the PNG in a single-image .ico container (256x256, valid on modern Windows).
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
$png = $ms.ToArray(); $ms.Dispose(); $bmp.Dispose()
$ico = New-Object System.IO.MemoryStream
$bw  = New-Object System.IO.BinaryWriter $ico
$bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]1)          # ICONDIR
$bw.Write([Byte]0);   $bw.Write([Byte]0)                                  # 256x256 (0 means 256)
$bw.Write([Byte]0);   $bw.Write([Byte]0)                                  # palette, reserved
$bw.Write([UInt16]1); $bw.Write([UInt16]32)                              # planes, bpp
$bw.Write([UInt32]$png.Length); $bw.Write([UInt32]22)                    # size, offset
$bw.Write($png)
$bw.Flush()
[System.IO.File]::WriteAllBytes($icoPath, $ico.ToArray())
$ico.Dispose()
Copy-Item $icoPath -Destination (Join-Path $appOut 'app.ico')

$bytes = (Get-ChildItem $appOut -Recurse | Measure-Object Length -Sum).Sum
Info ("Staged app: {0} ({1:N1} MB)" -f $appOut, ($bytes/1MB))

if ($StageOnly) { Info 'Stage-only: done.'; exit 0 }

# ---- 4. Ensure Inno Setup compiler ------------------------------------------
function Find-Iscc {
  $cmd = (Get-Command iscc -ErrorAction SilentlyContinue).Source
  if ($cmd) { return $cmd }
  $candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
  )
  foreach ($p in $candidates) { if (Test-Path $p) { return $p } }
  return $null
}
$iscc = Find-Iscc
if (-not $iscc) {
  Info 'Inno Setup not found — installing via winget…'
  winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements --silent
  $iscc = Find-Iscc
}
if (-not (Test-Path $iscc)) {
  Die "Inno Setup compiler (ISCC.exe) not available. Install it from https://jrsoftware.org/isdl.php and re-run."
}
Info "iscc:   $iscc"

# ---- 5. Compile the installer -----------------------------------------------
$iss = Join-Path $pkgDir 'prod-branch-sync.iss'
Info 'Compiling installer…'
& $iscc "/DAppVersion=$Version" "/DSourceDir=$appOut" $iss
if ($LASTEXITCODE -ne 0) { Die "ISCC failed with exit code $LASTEXITCODE." }

$setup = Join-Path $dist "ProdBranchSync-Setup-$Version.exe"
if (Test-Path $setup) { Info "DONE → $setup" } else { Die 'Installer not produced.' }
