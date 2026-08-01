# Reflect BriconLib.dll from installed Print Manager (32-bit PowerShell required).
param(
    [string]$DllPath = "C:\Bricon\Print Manager\BriconLib.dll"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $DllPath)) {
    Write-Error "Missing: $DllPath"
}
$asm = [Reflection.Assembly]::LoadFrom((Resolve-Path $DllPath).Path)
$out = @{}

foreach ($name in @(
        "GetFancierCountCommand", "GetFlightNamesCommand", "GetFancierCommand", "GetTimerCommand",
        "StartReadoutCommand", "StartTempReadoutCommand", "GetAndClearCommand", "ReadRingCommand",
        "SendFancierCommand", "SendFlightCommand", "RaceCommand", "SearchMasterCommand",
        "GetAndClearCompletedCommand", "AreReadRingCommand", "ActivateMasterCommand"
    )) {
    $t = $asm.GetType("BriconLib.Communications.$name")
    if (-not $t) {
        Write-Host "MISSING $name"
        continue
    }
    $ci = $t.GetConstructors() | Where-Object { $_.GetParameters().Count -eq 0 } | Select-Object -First 1
    if (-not $ci) {
        $ctors = $t.GetConstructors() | ForEach-Object {
            ($_.GetParameters() | ForEach-Object { $_.ParameterType.Name }) -join ", "
        }
        Write-Host "$name - no parameterless ctor; available: $ctors"
        continue
    }
    $c = $ci.Invoke(@())
    $b = $t.GetMethod("ToBytes").Invoke($c, @())
    $hex = [BitConverter]::ToString($b)
    $out[$name] = $hex
    Write-Host "$name : $hex"
}

$cmdType = $asm.GetType("BriconLib.Communications.SendVersionInfoCommand")
$ctor = $cmdType.GetConstructor(@([string], [byte], [byte], [byte]))
foreach ($ver in 1..8) {
    $cmd = $ctor.Invoke(@("Print Manager USB", [byte]1, [byte]$ver, [byte]0))
    $hex = [BitConverter]::ToString($cmdType.GetMethod("ToBytes").Invoke($cmd, @()))
    $key = "SendVersionInfo_USB_v${ver}_s0"
    $out[$key] = $hex
    Write-Host "$key : $hex"
}

$jsonPath = Join-Path $PSScriptRoot "..\third_party\bricon\pm_protocol_installed.json"
$out | ConvertTo-Json | Set-Content -LiteralPath $jsonPath -Encoding UTF8
Write-Host "Saved: $jsonPath"
