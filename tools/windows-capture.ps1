#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$UsbPcapDevice,
    [Parameter(Mandatory=$true)][int]$DeviceAddress,
    [string]$OutputDirectory = "$env:USERPROFILE\Desktop\eh575-private-capture",
    [string]$UsbPcapCmd = "USBPcapCMD.exe"
)

$ErrorActionPreference = "Stop"
$device = Get-PnpDevice -PresentOnly | Where-Object {
    $_.InstanceId -like "USB\VID_1C7A&PID_0575*"
} | Select-Object -First 1
if (-not $device) { throw "EH575 USB device 1c7a:0575 was not found." }
if (-not (Get-Command $UsbPcapCmd -ErrorAction SilentlyContinue)) {
    throw "USBPcapCMD.exe was not found. Install USBPcap/Wireshark and reopen elevated PowerShell."
}

New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop | Out-Null
$principal = "$env:USERDOMAIN\$env:USERNAME"
& icacls.exe $OutputDirectory /inheritance:r /grant:r "${principal}:(OI)(CI)F" | Out-Null
$driverDirectory = Join-Path $OutputDirectory "driver"
New-Item -ItemType Directory -Path $driverDirectory -ErrorAction Stop | Out-Null
$signed = Get-CimInstance Win32_PnPSignedDriver | Where-Object {
    $_.DeviceID -eq $device.InstanceId
} | Select-Object -First 1
if ($signed -and $signed.InfName) {
    & pnputil.exe /export-driver $signed.InfName $driverDirectory |
        Out-File (Join-Path $OutputDirectory "driver-export.txt") -Encoding utf8
}
Get-PnpDeviceProperty -InstanceId $device.InstanceId | ConvertTo-Json -Depth 4 |
    Out-File (Join-Path $OutputDirectory "device-properties.json") -Encoding utf8
& reg.exe export "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WinBio" (Join-Path $OutputDirectory "winbio.reg") /y | Out-Null
Get-FileHash -Algorithm SHA256 (Get-ChildItem $driverDirectory -File -Recurse) |
    ConvertTo-Json | Out-File (Join-Path $OutputDirectory "driver-hashes.json") -Encoding utf8

function Capture-Phase([string]$Name, [string]$Instruction, [bool]$CaptureNew=$false) {
    Write-Host ""
    Write-Host ("=" * 68) -ForegroundColor Cyan
    Write-Host "NEXT PHASE: $Name" -ForegroundColor Yellow
    Write-Host $Instruction
    Read-Host "Press Enter only after reading this and preparing the requested action"
    $capture = Join-Path $OutputDirectory "$Name.pcapng"
    $arguments = @("-d", $UsbPcapDevice, "-o", $capture, "-s", "65535",
        "-b", "33554432", "--devices", "$DeviceAddress", "--inject-descriptors")
    if ($CaptureNew) { $arguments += "--capture-from-new-devices" }
    $process = Start-Process -FilePath $UsbPcapCmd -ArgumentList $arguments -PassThru -NoNewWindow
    Start-Sleep -Seconds 2
    $started = Get-Date -Format o
    Read-Host "Perform the action now. Press Enter when the phase is complete"
    Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
    $process.WaitForExit(5000) | Out-Null
    [pscustomobject]@{phase=$Name; started=$started; stopped=(Get-Date -Format o); instruction=$Instruction} |
        ConvertTo-Json -Compress | Add-Content (Join-Path $OutputDirectory "timeline.jsonl")
    Write-Host "Saved $capture" -ForegroundColor Green
}

Write-Host "EH575 instance: $($device.InstanceId)"
Write-Host "All output is biometric/private. Do not upload or commit this directory." -ForegroundColor Red
Capture-Phase "01-initialize" "Open Device Manager. Disable and re-enable the EH575, then wait until it reports ready." $true
Capture-Phase "02-empty" "Do not touch the fingerprint sensor for at least ten seconds."
foreach ($attempt in 1..3) {
    Capture-Phase ("03-genuine-right-index-{0}" -f $attempt) "Lock Windows and unlock once using only your right index finger."
}
foreach ($attempt in 1..3) {
    Capture-Phase ("04-impostor-right-pinky-{0}" -f $attempt) "At Windows Hello verification, touch only your unenrolled right pinky and let it reject."
}
Capture-Phase "05-enrollment" "In Settings > Accounts > Sign-in options, enroll one temporary finger completely. Delete that enrollment after this phase."
Capture-Phase "06-final-empty" "Remove your finger completely and leave the sensor untouched for ten seconds."
Write-Host "Capture complete. Boot Linux and copy this whole directory to .egis-lab/windows-driver/fresh/."
