# PyInstaller + Inno Setup: pita Full ili Lite izdanje, ili: .\build_installer.ps1 -Flavor Lite
# Lite: bez menija „Otvori zaključan folder“ i „Otvori zaključan paket“ (Baza).
param(
    [Parameter(Mandatory = $false)]
    [ValidateSet("Full", "Lite")]
    [string]$Flavor
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-InnoSetupCompiler {
    $cmd = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    foreach ($candidate in @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
        )) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    return $null
}

Write-Host ""
Write-Host "=== Instalacioni paket (EXE + Inno Setup) ===" -ForegroundColor Cyan
Write-Host "  [F] Full - kompletan program (Baza: zakljucan folder i zakljucan paket)"
Write-Host "  [L] Lite - bez otvaranja zakljucanog foldera i zakljucanog paketa"
Write-Host ""
if ($Flavor) {
    $choice = if ($Flavor -eq "Lite") { "L" } else { "F" }
    Write-Host "Izabrano (param): $Flavor" -ForegroundColor Cyan
}
else {
    do {
        $choice = (Read-Host "Izaberite F ili L").Trim().ToUpperInvariant()
    } while ($choice -notin @("F", "L"))
}

$marker = Join-Path $PSScriptRoot "TAURIS_LITE"
if ($choice -eq "L") {
    $emailCfg = Join-Path $PSScriptRoot "tauris_club_email.json"
    if (-not (Test-Path -LiteralPath $emailCfg)) {
        Write-Error @"
Za Lite instaler potreban je fajl tauris_club_email.json u korenu projekta
(kopirajte od tauris_club_email.sample.json i popunite Gmail App lozinku).
"@
    }
    $env:TAURIS_BUILD_FLAVOR = "lite"
    New-Item -ItemType File -Path $marker -Force | Out-Null
    $issExtraArgs = @(
        '/DAppDisplayName=TaurisPdfApp Lite',
        '/DSetupBaseFilename=TaurisPdfApp_Lite_Setup',
        '/DIncludeClubEmail'
    )
    Write-Host "Izabrano: Lite (ukljucen tauris_club_email.json za slanje)" -ForegroundColor Yellow
}
else {
    Remove-Item Env:TAURIS_BUILD_FLAVOR -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
    $issExtraArgs = @()
    Write-Host "Izabrano: Full" -ForegroundColor Green
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Error "Nema .venv. Prvo: python -m venv .venv && pip install -r requirements.txt"
}

Write-Host "[1/6] pip (PyInstaller, Pillow, cryptography)..."
& $venvPy -m pip install --quiet -r (Join-Path $PSScriptRoot "requirements.txt") pyinstaller pillow
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "[2/6] ICO ikona..."
& $venvPy (Join-Path $PSScriptRoot "generate_app_ico.py")
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "[3/6] PyInstaller (TaurisPdfApp.spec)..."
try {
    & $venvPy -m PyInstaller --noconfirm TaurisPdfApp.spec
    $piExit = $LASTEXITCODE
}
finally {
    Remove-Item Env:TAURIS_BUILD_FLAVOR -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
}
if ($piExit -ne 0) {
    exit $piExit
}

$iscc = Find-InnoSetupCompiler
if (-not $iscc) {
    Write-Error "ISCC.exe nije pronađen. Instalirajte Inno Setup 6 (https://jrsoftware.org/isinfo.php) i/ili dodajte ga u PATH."
}

Write-Host "[4/6] BRICON USB drajveri (bricon.be)..."
$fetchDrivers = Join-Path $PSScriptRoot "scripts\fetch_bricon_drivers.ps1"
if (-not (Test-Path -LiteralPath $fetchDrivers)) {
    Write-Error "Nedostaje $fetchDrivers"
}
& powershell -NoProfile -ExecutionPolicy Bypass -File $fetchDrivers
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "[5/6] Inno Setup..."
$iss = Join-Path $PSScriptRoot "installer.iss"
& $iscc @issExtraArgs $iss
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Gotovo." -ForegroundColor Green
if ($choice -eq "L") {
    Write-Host "  Instaler: dist\TaurisPdfApp_Lite_Setup.exe"
}
else {
    Write-Host "  Instaler: dist\TaurisPdfApp_Setup.exe"
}
Write-Host "  EXE:      dist\TaurisPdfApp.exe"
