#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$UsbPcapDevice = "\\.\USBPcap2",
    [string]$OutputDirectory,
    [string]$UsbPcapCmd = "C:\Program Files\USBPcap\USBPcapCMD.exe",
    [string]$Tshark = "C:\Program Files\Wireshark\tshark.exe",
    [string]$UsbDestinationRoot = "D:\"
)

$ErrorActionPreference = "Stop"
$SensorId = "USB\VID_1C7A&PID_0575*"
$ActivePhasePattern = "^(genuine-|impostor-|temporary-enrollment)"

function Resolve-Tool([string]$Value, [string]$Label) {
    if (Test-Path -LiteralPath $Value -PathType Leaf) { return (Resolve-Path -LiteralPath $Value).Path }
    $command = Get-Command $Value -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw "$Label was not found: $Value"
}

function Get-Eh575 {
    Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like $SensorId } |
        Select-Object -First 1
}

function Reset-Eh575 {
    $device = Get-Eh575
    if (-not $device) { throw "EH575 USB device 1c7a:0575 was not found." }
    Write-Host "Resetting EH575 before capture..."
    Disable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false
    Start-Sleep -Seconds 2
    Enable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false
    $deadline = (Get-Date).AddSeconds(30)
    do { Start-Sleep -Milliseconds 500; $device = Get-Eh575 }
    until (($device -and $device.Status -eq "OK") -or (Get-Date) -ge $deadline)
    if (-not $device -or $device.Status -ne "OK") {
        throw "EH575 did not return to status OK after its automated reset."
    }
    Start-Sleep -Seconds 3
    return $device
}

function Add-TimelineEntry {
    param([string]$Capture, [string]$Phase, [string]$Instruction,
          [datetime]$Started, [datetime]$Stopped)
    [pscustomobject]@{
        capture=$Capture; phase=$Phase; started=$Started.ToString("o")
        stopped=$Stopped.ToString("o"); instruction=$Instruction
    } | ConvertTo-Json -Compress | Out-File -LiteralPath $script:Timeline `
        -Append -Encoding utf8
}

function Start-RootCapture([string]$Name) {
    $path = Join-Path $OutputDirectory "$Name.pcap"
    $arguments = @(
        "-d", $UsbPcapDevice, "-o", $path, "-s", "65535",
        "-b", "33554432", "--capture-from-all-devices",
        "--capture-from-new-devices", "--inject-descriptors"
    )
    Write-Host "Starting automated root capture on $UsbPcapDevice..."
    $process = Start-Process -FilePath $UsbPcapCmd -ArgumentList $arguments `
        -PassThru -NoNewWindow
    Start-Sleep -Seconds 2
    $process.Refresh()
    if ($process.HasExited) { throw "USBPcap exited during startup with code $($process.ExitCode)." }
    [pscustomobject]@{Name=$Name; Path=$path; Process=$process}
}

function Stop-RootCapture($Capture) {
    # USBPcapCMD exposes no programmatic stop option. Terminating its controller
    # closes its driver/file handles; tshark rejects a truncated result below.
    Stop-Process -Id $Capture.Process.Id -ErrorAction Stop
    $Capture.Process.WaitForExit(5000) | Out-Null
    Start-Sleep -Seconds 1
    if (-not (Test-Path -LiteralPath $Capture.Path -PathType Leaf)) {
        throw "Capture file was not created: $($Capture.Path)"
    }
    & $Tshark -r $Capture.Path -q 2>$null
    if ($LASTEXITCODE -ne 0) { throw "tshark rejected the stopped capture as malformed." }
}

function Show-Countdown([string]$Message, [int]$Seconds = 4) {
    Write-Host ""; Write-Host ("=" * 68) -ForegroundColor Cyan
    Write-Host $Message -ForegroundColor Yellow
    for ($remaining = $Seconds; $remaining -gt 0; $remaining--) {
        Write-Host "Starting in $remaining..."; Start-Sleep -Seconds 1
    }
}

function Wait-ForProcessState([string]$Name, [bool]$Present, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $found = [bool](Get-Process $Name -ErrorAction SilentlyContinue)
        if ($found -eq $Present) { return }
        Start-Sleep -Milliseconds 250
    } until ((Get-Date) -ge $deadline)
    throw "Timed out waiting for process '$Name' present=$Present."
}

function Record-LockAttempt([string]$Phase, [string]$Finger, [bool]$ExpectMatch) {
    $outcome = if ($ExpectMatch) { "unlock" } else { "reject, then use your PIN" }
    $instruction = "Use only your $Finger; expect $outcome. Lift it completely afterward."
    Show-Countdown "NEXT: $instruction"
    $started = Get-Date
    Start-Process rundll32.exe -ArgumentList "user32.dll,LockWorkStation"
    Wait-ForProcessState "LogonUI" $true 15
    Wait-ForProcessState "LogonUI" $false 120
    Add-TimelineEntry $script:Session.Name $Phase $instruction $started (Get-Date)
    Write-Host "$Phase completed; keep the sensor clear." -ForegroundColor Green
    Start-Sleep -Seconds 4
}

function Record-Enrollment {
    $instruction = "Enroll one currently unenrolled finger, then close the Settings window."
    Show-Countdown "NEXT: $instruction"
    $started = Get-Date
    Start-Process "ms-settings:signinoptions"
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $settings = Get-Process SystemSettings -ErrorAction SilentlyContinue
    } until (($settings -and ($settings | Where-Object MainWindowHandle -ne 0)) -or
             (Get-Date) -ge $deadline)
    if (-not $settings) { throw "Windows Settings did not open." }
    $deadline = (Get-Date).AddMinutes(5)
    do {
        Start-Sleep -Milliseconds 500
        $visible = Get-Process SystemSettings -ErrorAction SilentlyContinue |
            Where-Object MainWindowHandle -ne 0
    } until ((-not $visible) -or (Get-Date) -ge $deadline)
    if ($visible) { throw "Timed out waiting for the enrollment Settings window to close." }
    Add-TimelineEntry $script:Session.Name "temporary-enrollment" $instruction $started (Get-Date)
    Start-Sleep -Seconds 4
}

function Get-PhaseSummary($Capture, $Phase) {
    $start = [datetimeoffset]::Parse($Phase.started).ToUnixTimeMilliseconds() / 1000.0
    $stop = [datetimeoffset]::Parse($Phase.stopped).ToUnixTimeMilliseconds() / 1000.0
    $timeFilter = "frame.time_epoch >= $start && frame.time_epoch <= $stop"
    $egis = @(& $Tshark -r $Capture.Path -Y `
        "$timeFilter && usb.capdata contains 45:47:49:53" `
        -T fields -e frame.number 2>$null | Where-Object { $_ })
    $largeIn = @(& $Tshark -r $Capture.Path -Y `
        "$timeFilter && usb.endpoint_address == 0x82 && usb.data_len >= 5356" `
        -T fields -e frame.number 2>$null | Where-Object { $_ })
    [pscustomobject]@{
        phase=$Phase.phase; egis_packets=$egis.Count; large_in_transfers=$largeIn.Count
        valid=($egis.Count -gt 0 -and $largeIn.Count -gt 0)
    }
}

$UsbPcapCmd = Resolve-Tool $UsbPcapCmd "USBPcapCMD.exe"
$Tshark = Resolve-Tool $Tshark "tshark.exe"
if (Get-Process USBPcapCMD -ErrorAction SilentlyContinue) {
    throw "USBPcapCMD.exe is already running. Close it before starting."
}
$stamp = Get-Date -Format "yyyyMMddTHHmmss"
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $env:USERPROFILE "Desktop\eh575-private-capture-v3-$stamp"
}
New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$principal = "$env:USERDOMAIN\$env:USERNAME"
& icacls.exe $OutputDirectory /inheritance:r /grant:r "${principal}:(OI)(CI)F" | Out-Null
$script:Timeline = Join-Path $OutputDirectory "timeline.jsonl"

$device = Reset-Eh575
Write-Host "EH575 ready: $($device.InstanceId)"
Write-Host "Only finger presentation and completing Windows UI remain manual." -ForegroundColor Green
Write-Host "Biometric output remains private; do not upload or commit it." -ForegroundColor Red

$script:Session = Start-RootCapture "01-session"
try {
    $started = Get-Date
    Write-Host "Recording untouched baseline for 10 seconds..."
    Start-Sleep -Seconds 10
    Add-TimelineEntry $script:Session.Name "empty-baseline" "Sensor untouched." $started (Get-Date)
    foreach ($attempt in 1..3) {
        Record-LockAttempt "genuine-right-index-$attempt" "right index finger" $true
    }
    foreach ($attempt in 1..3) {
        Record-LockAttempt "impostor-right-pinky-$attempt" "right pinky" $false
    }
    Record-Enrollment
    $started = Get-Date
    Write-Host "Recording final untouched baseline for 10 seconds..."
    Start-Sleep -Seconds 10
    Add-TimelineEntry $script:Session.Name "post-enrollment-empty" `
        "Sensor untouched after enrollment." $started (Get-Date)
}
finally {
    if (-not $script:Session.Process.HasExited) { Stop-RootCapture $script:Session }
}

$timelineRecords = @(Get-Content -LiteralPath $script:Timeline -Encoding utf8 |
    ForEach-Object { $_ | ConvertFrom-Json })
$phaseResults = @($timelineRecords | Where-Object phase -Match $ActivePhasePattern |
    ForEach-Object { Get-PhaseSummary $script:Session $_ })
$validation = [pscustomobject]@{
    capture=$script:Session.Name; usbpcap_device=$UsbPcapDevice; phases=$phaseResults
    valid=($phaseResults.Count -eq 7 -and
        @($phaseResults | Where-Object { -not $_.valid }).Count -eq 0)
}
$validation | ConvertTo-Json -Depth 4 | Out-File `
    (Join-Path $OutputDirectory "01-session-validation.json") -Encoding utf8
$phaseResults | Format-Table -AutoSize
if (-not $validation.valid) {
    throw "A biometric phase lacked EH575 commands or full-size IN transfers. Preserve the directory; do not repeat blindly."
}

Get-PnpDeviceProperty -InstanceId $device.InstanceId | ConvertTo-Json -Depth 4 |
    Out-File (Join-Path $OutputDirectory "device-properties.json") -Encoding utf8
& reg.exe export "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WinBio" `
    (Join-Path $OutputDirectory "winbio.reg") /y | Out-Null
Get-Item -LiteralPath $UsbPcapCmd, "C:\Windows\System32\drivers\USBPcap.sys" |
    Select-Object FullName,@{Name="Version";Expression={$_.VersionInfo.FileVersion}} |
    ConvertTo-Json | Out-File (Join-Path $OutputDirectory "usbpcap-versions.json") -Encoding utf8

if (-not (Test-Path -LiteralPath $UsbDestinationRoot -PathType Container)) {
    throw "USB destination root is unavailable: $UsbDestinationRoot"
}
$destination = Join-Path $UsbDestinationRoot (Split-Path -Leaf $OutputDirectory)
if (Test-Path -LiteralPath $destination) {
    throw "USB destination already exists; refusing to overwrite it: $destination"
}
Copy-Item -LiteralPath $OutputDirectory -Destination $destination -Recurse
Write-Host "Validated capture copied automatically to $destination" -ForegroundColor Green
