# Preuzima zvanicne BRICON USB drajvere za ugradnju u instalacioni paket.
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$dest = Join-Path $root "third_party\bricon\drivers"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$cdmUrl = "https://www.bricon.be/download/software/Drivers/CDM_Setup.exe"
$win7ZipUrl = "https://www.bricon.be/download/software/Drivers/USBDriver_win7.zip"
$cdmPath = Join-Path $dest "CDM_Setup.exe"
$win7ZipPath = Join-Path $dest "USBDriver_win7.zip"
$win7Dir = Join-Path $dest "USBDriver_win7"
$win7InstallerName = "USBDriverInstallerV2.04.16.exe"
$win7InstallerPath = Join-Path $win7Dir $win7InstallerName

function Ensure-Download {
    param(
        [string]$Url,
        [string]$OutFile,
        [long]$MinBytes
    )
    $need = $Force -or -not (Test-Path -LiteralPath $OutFile) -or ((Get-Item -LiteralPath $OutFile).Length -lt $MinBytes)
    if (-not $need) {
        Write-Host "  OK (kes): $(Split-Path $OutFile -Leaf)"
        return
    }
    Write-Host "  Preuzimam: $Url"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    }
    catch {
        throw "Ne mogu preuzeti $Url - proverite internet ili stavite fajl rucno u $dest"
    }
    if (-not (Test-Path -LiteralPath $OutFile) -or (Get-Item -LiteralPath $OutFile).Length -lt $MinBytes) {
        throw "Preuzeti fajl je premali ili nedostaje: $OutFile"
    }
}

Write-Host "BRICON USB drajveri -> $dest"
Ensure-Download -Url $cdmUrl -OutFile $cdmPath -MinBytes 300000
Ensure-Download -Url $win7ZipUrl -OutFile $win7ZipPath -MinBytes 500000

if ($Force -or -not (Test-Path -LiteralPath $win7InstallerPath)) {
    if (Test-Path -LiteralPath $win7Dir) {
        Remove-Item -LiteralPath $win7Dir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $win7Dir | Out-Null
    Expand-Archive -LiteralPath $win7ZipPath -DestinationPath $win7Dir -Force
}

if (-not (Test-Path -LiteralPath $win7InstallerPath)) {
    $found = Get-ChildItem -LiteralPath $win7Dir -Filter "USBDriverInstaller*.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        Copy-Item -LiteralPath $found.FullName -Destination $win7InstallerPath -Force
    }
}

if (-not (Test-Path -LiteralPath $win7InstallerPath)) {
    throw "U $win7ZipPath nema ocekivanog $win7InstallerName"
}

Write-Host "  CDM_Setup.exe: $((Get-Item $cdmPath).Length) bajtova"
Write-Host ('  ' + $win7InstallerName + ': ' + (Get-Item $win7InstallerPath).Length + ' bajtova')
Write-Host "BRICON drajveri spremni za Inno Setup."
