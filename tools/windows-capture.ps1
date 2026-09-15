#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$UsbPcapDevice,
    [string]$OutputDirectory,
    [string]$UsbPcapCmd = "USBPcapCMD.exe",
    [string]$Tshark = "tshark.exe"
)

$ErrorActionPreference = "Stop"
if (-not $OutputDirectory) {
    $stamp = Get-Date -Format "yyyyMMddTHHmmss"
    $OutputDirectory = Join-Path $env:USERPROFILE "Desktop\eh575-private-capture-$stamp"
}

$device = Get-PnpDevice -PresentOnly | Where-Object {
    $_.InstanceId -like "USB\VID_1C7A&PID_0575*"
} | Select-Object -First 1
if (-not $device) { throw "EH575 USB device 1c7a:0575 was not found." }
if (-not (Get-Command $UsbPcapCmd -ErrorAction SilentlyContinue)) {
    throw "USBPcapCMD.exe was not found. Install USBPcap and reopen elevated PowerShell."
}
if (-not (Get-Command $Tshark -ErrorAction SilentlyContinue)) {
    throw "tshark.exe was not found. Install Wireshark with USBPcap and reopen elevated PowerShell."
}

New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop | Out-Null
$principal = "$env:USERDOMAIN\$env:USERNAME"
& icacls.exe $OutputDirectory /inheritance:r /grant:r "${principal}:(OI)(CI)F" | Out-Null
$timeline = Join-Path $OutputDirectory "timeline.jsonl"
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
& reg.exe export "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WinBio" `
    (Join-Path $OutputDirectory "winbio.reg") /y | Out-Null
$driverFiles = @(Get-ChildItem $driverDirectory -File -Recurse)
if ($driverFiles.Count) {
    Get-FileHash -Algorithm SHA256 $driverFiles | ConvertTo-Json |
        Out-File (Join-Path $OutputDirectory "driver-hashes.json") -Encoding utf8
}

function Start-RootCapture([string]$Name) {
    $capture = Join-Path $OutputDirectory "$Name.pcap"
    $arguments = @(
        "-d", $UsbPcapDevice, "-o", $capture, "-s", "65535",
        "-b", "33554432", "--capture-from-all-devices", "--inject-descriptors"
    )
    $process = Start-Process -FilePath $UsbPcapCmd -ArgumentList $arguments `
        -PassThru -NoNewWindow
    Start-Sleep -Seconds 2
    return [pscustomobject]@{Name=$Name; Path=$capture; Process=$process}
}

function Stop-RootCapture($Capture) {
    Stop-Process -Id $Capture.Process.Id -ErrorAction SilentlyContinue
    $Capture.Process.WaitForExit(5000) | Out-Null
    Start-Sleep -Milliseconds 500
    if (-not (Test-Path $Capture.Path)) { throw "Capture file was not created: $($Capture.Path)" }
}

function Write-Phase([string]$CaptureName, [string]$Phase, [string]$Instruction) {
    Write-Host ""
    Write-Host ("=" * 68) -ForegroundColor Cyan
    Write-Host "NEXT PHASE: $Phase" -ForegroundColor Yellow
    Write-Host $Instruction
    Read-Host "Press Enter when you have read this and are ready"
    $started = Get-Date -Format o
    Read-Host "Perform the action now, then press Enter when it is complete"
    [pscustomobject]@{
        capture=$CaptureName; phase=$Phase; started=$started
        stopped=(Get-Date -Format o); instruction=$Instruction
    } | ConvertTo-Json -Compress | Add-Content $timeline
}

function Get-CaptureSummary([string]$CapturePath) {
    $addressLines = @(& $Tshark -r $CapturePath `
        -Y "usb.idVendor == 0x1c7a && usb.idProduct == 0x0575" `
        -T fields -e usb.device_address 2>$null)
    $addresses = @($addressLines | Where-Object { $_ -match '^\d+$' } |
        Sort-Object -Unique)
    if (-not $addresses.Count) {
        return [pscustomobject]@{addresses=@(); egis=0; large=0; valid=$false}
    }
    $addressTerms = @($addresses | ForEach-Object { "usb.device_address == $_" })
    $deviceFilter = "(" + ($addressTerms -join " || ") + ")"
    $egisLines = @(& $Tshark -r $CapturePath `
        -Y "$deviceFilter && usb.capdata contains 45:47:49:53" `
        -T fields -e frame.number 2>$null | Where-Object { $_ })
    $largeLines = @(& $Tshark -r $CapturePath `
        -Y "$deviceFilter && usb.data_len >= 5356" `
        -T fields -e frame.number 2>$null | Where-Object { $_ })
    return [pscustomobject]@{
        addresses=$addresses; egis=$egisLines.Count; large=$largeLines.Count
        valid=($egisLines.Count -gt 0 -and $largeLines.Count -gt 0)
    }
}

function Assert-Capture([string]$Label, $Capture) {
    $summary = Get-CaptureSummary $Capture.Path
    [pscustomobject]@{
        capture=$Capture.Name; addresses=$summary.addresses
        egis_packets=$summary.egis; large_transfers=$summary.large
        valid=$summary.valid
    } | ConvertTo-Json -Depth 3 | Out-File `
        (Join-Path $OutputDirectory "$($Capture.Name)-validation.json") -Encoding utf8
    Write-Host "$Label addresses: $($summary.addresses -join ', ')"
    Write-Host "$Label EGIS packets: $($summary.egis); large transfers: $($summary.large)"
    if (-not $summary.valid) {
        throw "$Label did not capture usable EH575 traffic. Stop here and keep this directory for diagnosis."
    }
    Write-Host "$Label capture validated." -ForegroundColor Green
}

Write-Host "EH575 instance: $($device.InstanceId)"
Write-Host "Capturing every device on $UsbPcapDevice so EH575 address changes are retained."
Write-Host "All output is biometric/private. Do not upload or commit this directory." -ForegroundColor Red

$preflight = Start-RootCapture "01-preflight"
Write-Phase $preflight.Name "initialize" `
    "In Device Manager, disable and re-enable the EH575, then wait until it reports ready."
Stop-RootCapture $preflight
Assert-Capture "Preflight" $preflight

$verification = Start-RootCapture "02-verification"
Write-Phase $verification.Name "empty-baseline" `
    "Leave the fingerprint sensor completely untouched for ten seconds."
foreach ($attempt in 1..3) {
    Write-Phase $verification.Name ("genuine-right-index-{0}" -f $attempt) `
        "Lock Windows and unlock once using only your right index finger. Lift it completely afterward."
}
foreach ($attempt in 1..3) {
    Write-Phase $verification.Name ("impostor-right-pinky-{0}" -f $attempt) `
        "At Windows Hello verification, touch only your unenrolled right pinky until it rejects. Lift it completely afterward."
}
Stop-RootCapture $verification
Assert-Capture "Verification" $verification

$enrollment = Start-RootCapture "03-enrollment"
Write-Phase $enrollment.Name "temporary-enrollment" `
    "In Settings > Accounts > Sign-in options, completely enroll one temporary finger."
Write-Phase $enrollment.Name "post-enrollment-empty" `
    "Lift the temporary finger completely and leave the sensor untouched for ten seconds."
Stop-RootCapture $enrollment
Assert-Capture "Enrollment" $enrollment

Write-Host ""
Write-Host "Capture complete and validated." -ForegroundColor Green
Write-Host "Delete the temporary Windows enrollment now."
Write-Host "Copy this whole directory to .egis-lab/windows-driver/fresh-v2/:"
Write-Host $OutputDirectory
