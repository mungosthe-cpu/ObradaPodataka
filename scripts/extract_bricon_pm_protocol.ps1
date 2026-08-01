# Extract BRICON Print Manager protocol bytes from BriconLib.dll (32-bit .NET)
$ErrorActionPreference = "Stop"
$dll = Join-Path $PSScriptRoot "..\third_party\bricon\pm_extract\BriconLib.dll"
if (-not (Test-Path -LiteralPath $dll)) {
    Write-Error "Missing $dll - extract PMSetup.msi to third_party\bricon\pm_extract"
}
$asm = [Reflection.Assembly]::LoadFrom((Resolve-Path $dll).Path)
$out = @{}

# SendVersionInfoCommand — tipicni PM string
$cmdType = $asm.GetType("BriconLib.Communications.SendVersionInfoCommand")
$ctor = $cmdType.GetConstructor(@([string], [byte], [byte], [byte]))
foreach ($menu in @("Print Manager USB", "Print Manager")) {
    foreach ($ver in 1..8) {
        foreach ($stor in 0..2) {
            $cmd = $ctor.Invoke(@($menu, [byte]1, [byte]$ver, [byte]$stor))
            $bytes = $cmdType.GetMethod("ToBytes").Invoke($cmd, @())
            $hex = [BitConverter]::ToString($bytes)
            $out["SendVersionInfo_${menu}_v${ver}_s${stor}"] = $hex
        }
    }
}

# Komande bez parametara (ako imaju .ctor())
foreach ($name in @(
        "GetFancierCountCommand", "GetFlightNamesCommand", "GetFancierCommand",
        "GetTimerCommand", "SearchMasterCommand"
    )) {
    $t = $asm.GetType("BriconLib.Communications.$name")
    if (-not $t) { continue }
    $ci = $t.GetConstructors() | Where-Object { $_.GetParameters().Count -eq 0 } | Select-Object -First 1
    if ($ci) {
        $c = $ci.Invoke(@())
        $b = $t.GetMethod("ToBytes").Invoke($c, @())
        $out[$name] = [BitConverter]::ToString($b)
    }
}

$jsonPath = Join-Path $PSScriptRoot "..\third_party\bricon\pm_protocol_samples.json"
$out | ConvertTo-Json | Set-Content -LiteralPath $jsonPath -Encoding UTF8
Write-Host "Saved: $jsonPath"
$out.GetEnumerator() | Sort-Object Name | ForEach-Object { Write-Host $_.Key ":" $_.Value }
