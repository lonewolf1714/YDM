# Registers YDM with Chrome / Edge / Brave / Chromium for the current user (no admin needed).
#   powershell -ExecutionPolicy Bypass -File install.ps1
param([switch]$SkipYtDlp)
$ErrorActionPreference = "Stop"

$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$hostId  = "com.ydm.host"
$extId   = "jimfaknamlepkebnaemlonbhcemcoimc"   # fixed by the "key" in extension/manifest.json
$dataDir = Join-Path $env:APPDATA "YDM"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python 3.10+ is required. Install it from https://www.python.org and tick 'Add to PATH'." }
$python = & $python -c "import sys; print(sys.executable)"
Write-Host "Python: $python"

if (-not $SkipYtDlp) {
    Write-Host "Installing/updating yt-dlp (used for YouTube and similar sites)..."
    & $python -m pip install --user --upgrade --quiet yt-dlp
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "ffmpeg was not found on PATH. HLS streams will be saved as .ts and YouTube is limited to pre-merged formats. Install it with: winget install Gyan.FFmpeg"
}

# Launcher the browser runs. Must print nothing except the native-messaging protocol.
$bat = Join-Path $dataDir "native_host.bat"
@"
@echo off
"$python" -u "$root\ydm\native_host.py" %*
"@ | Set-Content -Encoding ASCII $bat

$manifest = Join-Path $dataDir "$hostId.json"
@{
    name            = $hostId
    description     = "YDM download manager"
    path            = $bat
    type            = "stdio"
    allowed_origins = @("chrome-extension://$extId/")
} | ConvertTo-Json | Set-Content -Encoding UTF8 $manifest

$browsers = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts",
    "HKCU:\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
    "HKCU:\Software\Chromium\NativeMessagingHosts",
    "HKCU:\Software\Vivaldi\NativeMessagingHosts",
    "HKCU:\Software\Opera Software\NativeMessagingHosts"
)
foreach ($b in $browsers) {
    $key = Join-Path $b $hostId
    New-Item -Force -Path $key | Out-Null
    Set-ItemProperty -Path $key -Name "(default)" -Value $manifest
}
Write-Host "Native messaging host registered: $manifest"

# Start Menu shortcut
$pyw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $python }
$lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "YDM.lnk"
$sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$sh.TargetPath = $pyw
$sh.Arguments = "`"$root\ydm_app.py`""
$sh.WorkingDirectory = $root
$sh.Description = "YDM download manager"
$sh.Save()
Write-Host "Start Menu shortcut: $lnk"

Write-Host ""
Write-Host "Last step - load the browser extension:" -ForegroundColor Cyan
Write-Host "  1. Open chrome://extensions (or edge://extensions)"
Write-Host "  2. Turn on 'Developer mode'"
Write-Host "  3. Click 'Load unpacked' and pick: $root\extension"
Write-Host "  The extension ID must read $extId"
