# Objavi GitHub Release sa instalacionim .exe (potreban GITHUB_TOKEN).
# Primer:
#   $env:GITHUB_TOKEN = "ghp_...."
#   .\scripts\publish_github_release.ps1 -Tag 2.1.5
#
# param defaults:
#   Tag=2.1.5  ReleaseName=Verzija 2.1.5  NotesFile=releases\release_notes_2.1.5.txt

param(
    [Parameter(Mandatory = $false)]
    [string]$Tag = "2.1.5",
    [string]$ReleaseName = "Verzija 2.1.5",
    [string]$InstallerPath = "",
    [string]$AssetName = "TaurisPdfApp_Lite_Setup.exe",
    [string]$NotesFile = "",
    [string]$Owner = "mungosthe-cpu",
    [string]$Repo = "ObradaPodataka",
    [switch]$Draft
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
if (-not $InstallerPath) {
    $InstallerPath = Join-Path $root "dist\TaurisPdfApp_Lite_Setup.exe"
}
if (-not $NotesFile) {
    $NotesFile = Join-Path $root "releases\release_notes_2.1.5.txt"
}

$token = $env:GITHUB_TOKEN
if (-not $token) {
    Write-Error @"
Nedostaje GITHUB_TOKEN.

1) GitHub -> Settings -> Developer settings -> Personal access tokens
2) Generisite token (repo ili public_repo za ovaj repozitorijum)
3) U PowerShell:
   `$env:GITHUB_TOKEN = 'ghp_...'
   .\scripts\publish_github_release.ps1 -Tag $Tag

Ili objavite rucno: https://github.com/$Owner/$Repo/releases/new
"@
}

if (-not (Test-Path -LiteralPath $InstallerPath)) {
    Write-Error "Nema instalera: $InstallerPath — prvo pokrenite: .\build_installer.ps1 -Flavor Lite"
}

$bodyText = ""
if (Test-Path -LiteralPath $NotesFile) {
    $bodyText = Get-Content -LiteralPath $NotesFile -Raw -Encoding UTF8
}
else {
    $bodyText = "TaurisPdfApp release $Tag"
}

$headers = @{
    Authorization = "Bearer $token"
    Accept        = "application/vnd.github+json"
    "User-Agent"  = "TaurisPdfApp-Publish"
}

$apiBase = "https://api.github.com/repos/$Owner/$Repo"

Write-Host "Proveravam da li tag $Tag vec postoji..."
try {
    $existing = Invoke-RestMethod -Uri "$apiBase/releases/tags/$Tag" -Headers $headers -Method Get
    Write-Error "Release sa tagom '$Tag' vec postoji: $($existing.html_url). Izaberite novi tag ili obrisite stari release na GitHub-u."
}
catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) {
        throw
    }
}

$payload = @{
    tag_name   = $Tag
    name       = $ReleaseName
    body       = $bodyText
    draft      = [bool]$Draft
    prerelease = $false
} | ConvertTo-Json -Depth 3

Write-Host "Kreiram release $Tag..."
$release = Invoke-RestMethod -Uri "$apiBase/releases" -Headers $headers -Method Post -Body $payload -ContentType "application/json; charset=utf-8"
$uploadUrl = $release.upload_url -replace "\{.*$", ""

$fileBytes = [System.IO.File]::ReadAllBytes((Resolve-Path $InstallerPath).Path)
$fileLen = $fileBytes.Length
$uploadUri = "${uploadUrl}?name=$([uri]::EscapeDataString($AssetName))"

Write-Host "Upload $AssetName ($([math]::Round($fileLen/1MB, 2)) MB)..."
$uploadHeaders = @{
    Authorization = "Bearer $token"
    Accept        = "application/vnd.github+json"
    "Content-Type" = "application/octet-stream"
    "User-Agent"  = "TaurisPdfApp-Publish"
}

Invoke-RestMethod -Uri $uploadUri -Headers $uploadHeaders -Method Post -Body $fileBytes | Out-Null

Write-Host ""
Write-Host "Gotovo." -ForegroundColor Green
Write-Host "  Release: $($release.html_url)"
Write-Host "  Asset:   $AssetName"
Write-Host ""
Write-Host "Korisnici na 1.5 mogu: Baza -> Provera Nadogradnje (sa internetom)."
