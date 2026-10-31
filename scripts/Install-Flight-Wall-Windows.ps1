#requires -Version 5.1
<#
.SYNOPSIS
Transfers the original Flight Wall 0.3.0 installer to your Batocera PC.
.DESCRIPTION
Uses the archive beside this helper, or a file picker starting at D:\Downloads
when available. Checks the original archive hash locally and on Batocera. Does
not change Windows execution policy, save passwords, configure Google, or update
Batocera itself. SSH host-key and password prompts remain interactive.
.PARAMETER BatoceraHost
The Batocera IPv4 address or DNS hostname. Defaults to batocera.
.PARAMETER PackagePath
Optional existing .tar.gz file, including a browser's renamed duplicate copy.
Its contents must match the pinned original installer, regardless of its name.
#>
[CmdletBinding()]
param(
    [string]$BatoceraHost = 'batocera',
    [string]$PackagePath
)

$script:FlightWallArchiveName = 'flightwall-batocera-0.3.0.tar.gz'
$script:FlightWallExpectedSha256 = '086bf6267e3c96b55de1cfda70ec3adff21c65aff19bb5a525936a70b969de08'
$script:FlightWallRemoteArchive = '/userdata/system/flightwall-batocera-0.3.0.tar.gz'

function Assert-FlightWallHost {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value.Length -gt 253 -or $Value -notmatch '^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$') {
        throw 'Use only the Batocera IPv4 address or DNS hostname, without a username, URL, port, or spaces.'
    }
    if ($Value -match '^[0-9.]+$') {
        $address = $null
        if (-not [System.Net.IPAddress]::TryParse($Value, [ref]$address) -or $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
            throw 'The Batocera IPv4 address is not valid.'
        }
    }
}

function Get-FlightWallInitialDirectory {
    param([Parameter(Mandatory = $true)][string]$HelperDirectory)
    $profileDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    $candidates = @('D:\Downloads')
    if (-not [string]::IsNullOrWhiteSpace($profileDirectory)) {
        $candidates += Join-Path $profileDirectory 'Downloads'
    }
    $candidates += 'D:\'
    $candidates += $HelperDirectory
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Container -ErrorAction SilentlyContinue) {
            return $candidate
        }
    }
    return $HelperDirectory
}

function Select-FlightWallArchive {
    param([Parameter(Mandatory = $true)][string]$HelperDirectory)
    if ([Threading.Thread]::CurrentThread.ApartmentState -ne [Threading.ApartmentState]::STA) {
        throw 'Use Start-Flight-Wall-Install.cmd, or launch Windows PowerShell with -STA, for the file picker.'
    }
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    try {
        $dialog.Title = 'Choose the downloaded Flight Wall 0.3.0 installer'
        $dialog.Filter = 'Installer archives (*.tar.gz)|*.tar.gz|All files (*.*)|*.*'
        $dialog.InitialDirectory = Get-FlightWallInitialDirectory -HelperDirectory $HelperDirectory
        $dialog.FileName = $script:FlightWallArchiveName
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        $dialog.RestoreDirectory = $true
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            throw 'No installer was selected. Nothing was transferred or installed.'
        }
        return $dialog.FileName
    }
    finally {
        $dialog.Dispose()
    }
}

function Resolve-FlightWallArchive {
    param([string]$RequestedPath, [Parameter(Mandatory = $true)][string]$HelperDirectory)
    $candidate = $RequestedPath
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $alongside = Join-Path $HelperDirectory $script:FlightWallArchiveName
        if (Test-Path -LiteralPath $alongside -PathType Leaf) {
            $candidate = $alongside
        }
        else {
            $candidate = Select-FlightWallArchive -HelperDirectory $HelperDirectory
        }
    }
    $item = Get-Item -LiteralPath $candidate -ErrorAction Stop
    if ($item -isnot [System.IO.FileInfo] -or -not $item.Name.EndsWith('.tar.gz', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Choose the installer .tar.gz file, not the source ZIP, starter ZIP, or an extracted folder.'
    }
    $actualHash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256 -ErrorAction Stop).Hash
    if (-not [string]::Equals($actualHash, $script:FlightWallExpectedSha256, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'That file does not match the original Flight Wall 0.3.0 installer. Nothing was sent. Choose the installer supplied with this starter package.'
    }
    return $item.FullName
}

function Get-FlightWallSshTools {
    $ssh = Get-Command 'ssh.exe' -CommandType Application -ErrorAction SilentlyContinue
    $scp = Get-Command 'scp.exe' -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $ssh -or $null -eq $scp) {
        throw 'Windows OpenSSH Client is missing. Install OpenSSH Client through Windows Optional Features, reopen this helper, and try again. Do not use Run as administrator just to run this helper.'
    }
    return @{ Ssh = $ssh.Source; Scp = $scp.Source }
}

function Test-FlightWallScpLegacyFlag {
    param([Parameter(Mandatory = $true)][string]$Executable)
    # Bare scp -O prints local usage. It cannot connect without source/target
    # arguments, and detects old OpenSSH 8.1 clients that reject this flag.
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.Arguments = '-O'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Could not start the local SCP compatibility check.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(10000)) {
            $process.Kill()
            throw 'The local SCP compatibility check timed out.'
        }
        $usage = $stdout.Result + "`n" + $stderr.Result
        if ($usage -match '(?im)(unknown|illegal|invalid|unrecognized)[^\r\n]*option[^\r\n]*\bO\b') { return $false }
        if ($usage -match '(?im)^usage:[^\r\n]*\[[^\]\r\n]*O') { return $true }
        if ($usage -match '(?im)^usage:') { return $false }
        throw 'Could not identify the local SCP client. Use the Windows OpenSSH Client and try again.'
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-FlightWallNative {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$Operation,
        [switch]$CaptureOutput
    )
    # Leave SSH's console input and stderr connected so Windows itself handles
    # host-key/password prompts. Never request, capture, or save a password.
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    if ($CaptureOutput) {
        $output = & $Executable @ArgumentList
        $nativeSucceeded = $?
        $nativeExit = $LASTEXITCODE
    }
    else {
        & $Executable @ArgumentList
        $nativeSucceeded = $?
        $nativeExit = $LASTEXITCODE
    }
    if (-not $nativeSucceeded -or $nativeExit -ne 0) {
        throw ($Operation + ' failed (exit ' + $nativeExit + '). The next installation step was not run.')
    }
    if ($CaptureOutput) { return $output }
}

function Get-FlightWallRemoteInfo {
    param([Parameter(Mandatory = $true)][string]$SshExecutable, [Parameter(Mandatory = $true)][string]$Target)
    # Single quotes only: Windows PowerShell 5.1's native argument quoting must
    # not remove double quotes from an embedded Linux shell command.
    $probe = @'
set -eu
printf 'FLIGHTWALL_ARCH='; uname -m
if test -r /usr/share/batocera/batocera.version; then
  printf 'FLIGHTWALL_VERSION='; head -n 1 /usr/share/batocera/batocera.version
elif test -r /etc/os-release && sed -n 's/^NAME=//p' /etc/os-release | tr -d '\042\047' | grep -Eiq '^Batocera(\.linux)?$'; then
  printf 'FLIGHTWALL_VERSION='; sed -n 's/^VERSION=//p' /etc/os-release | tr -d '\042\047'
else
  echo 'Cannot identify this computer as Batocera.' >&2
  exit 20
fi
test -d /userdata/system
'@
    $probe = $probe.Replace("`r", '')
    $lines = Invoke-FlightWallNative -Executable $SshExecutable -ArgumentList @('-o', 'ConnectTimeout=15', $Target, $probe) -Operation 'Batocera SSH check' -CaptureOutput
    $text = $lines -join "`n"
    $architecture = [regex]::Match($text, '(?m)^FLIGHTWALL_ARCH=([^\r\n]+)')
    $version = [regex]::Match($text, '(?m)^FLIGHTWALL_VERSION=([^\r\n]+)')
    if (-not $architecture.Success -or -not $version.Success -or $version.Groups[1].Value -notmatch '^\s*[0-9]+(?:\.[0-9]+)*') {
        throw 'The remote computer did not report a recognizable Batocera version and architecture. Nothing was copied.'
    }
    if ($architecture.Groups[1].Value.Trim() -ne 'x86_64') {
        throw 'This installer is for an x86_64 Batocera PC. It cannot install the application on a Raspberry Pi or another ARM computer.'
    }
    return [pscustomobject]@{ Architecture = 'x86_64'; Version = $version.Groups[1].Value.Trim() }
}

function Get-FlightWallInstallCommand {
    # Fixed remote names, independent of the chosen Windows filename. The
    # original package hash is checked again before extracting or running it.
    $command = @'
set -eu
cd /userdata/system
printf '%s  %s\n' '__EXPECTED_SHA256__' '/userdata/system/flightwall-batocera-0.3.0.tar.gz' | sha256sum -c -
tar -xzf /userdata/system/flightwall-batocera-0.3.0.tar.gz -C /userdata/system
test -f /userdata/system/flightwall-batocera-0.3.0/install.sh
sh /userdata/system/flightwall-batocera-0.3.0/install.sh
'@
    return $command.Replace('__EXPECTED_SHA256__', $script:FlightWallExpectedSha256).Replace("`r", '')
}

function Invoke-FlightWallWindowsInstall {
    param([string]$Computer = 'batocera', [string]$ArchivePath, [Parameter(Mandatory = $true)][string]$HelperDirectory)
    Set-StrictMode -Version 2.0
    $ErrorActionPreference = 'Stop'
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'This transfer helper runs on Windows. The Batocera application itself remains a Linux program.'
    }
    Assert-FlightWallHost -Value $Computer
    $tools = Get-FlightWallSshTools
    $archive = Resolve-FlightWallArchive -RequestedPath $ArchivePath -HelperDirectory $HelperDirectory
    Write-Host ''
    Write-Host 'Flight Wall installer verified.' -ForegroundColor Green
    Write-Host ('File: ' + $archive)
    Write-Host ('Batocera computer: ' + $Computer)
    Write-Host ''
    Write-Host 'Before continuing, finish the Batocera operating-system update and let it reboot to its main menu.'
    Write-Host 'Close Flight Wall on the TV if it is open. This helper installs the app; it does not update Batocera.'
    $null = Read-Host 'Press Enter only when the OS update/reboot is complete (Ctrl+C cancels)'
    Write-Host ''
    Write-Host 'Checking the Batocera computer. SSH may ask about its host key and your root password.'
    Write-Host 'Use your own Batocera root password. Password typing is invisible and is not saved here.'
    $target = 'root@' + $Computer
    $remote = Get-FlightWallRemoteInfo -SshExecutable $tools.Ssh -Target $target
    Write-Host ('Detected Batocera ' + $remote.Version + ' / ' + $remote.Architecture)
    if ($remote.Version -notmatch '^43(?:\.|\s|$)') {
        Write-Host 'This is the version reported after your readiness confirmation. This helper does not run an OS upgrade.' -ForegroundColor Yellow
    }
    $scpArguments = @('-o', 'ConnectTimeout=15')
    if (Test-FlightWallScpLegacyFlag -Executable $tools.Scp) { $scpArguments += '-O' }
    $scpArguments += @($archive, ($target + ':' + $script:FlightWallRemoteArchive))
    Write-Host ''
    Write-Host 'Copying the verified installer. You may be asked for the same SSH password again.'
    Invoke-FlightWallNative -Executable $tools.Scp -ArgumentList $scpArguments -Operation 'Installer transfer'
    Write-Host ''
    Write-Host 'Checking the received file and installing Flight Wall on Batocera...'
    Invoke-FlightWallNative -Executable $tools.Ssh -ArgumentList @('-o', 'ConnectTimeout=15', $target, (Get-FlightWallInstallCommand)) -Operation 'Remote hash check and installation'
    Write-Host ''
    Write-Host 'Flight Wall installation completed.' -ForegroundColor Green
    Write-Host 'On the TV: MAIN MENU > GAME SETTINGS > UPDATE GAMELISTS, then PORTS > Flight Wall.'
    Write-Host 'Open it once to activate a staged upgrade. Existing private settings and credentials stay on Batocera.'
    Write-Host 'Google, household phones, and other optional services are configured afterward using the supplied guide.'
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-FlightWallWindowsInstall -Computer $BatoceraHost -ArchivePath $PackagePath -HelperDirectory $PSScriptRoot
        exit 0
    }
    catch {
        Write-Host ''
        Write-Host ('Stopped: ' + $_.Exception.Message) -ForegroundColor Red
        Write-Host 'Keep this error text. Fix the reported step and run the helper again.'
        exit 1
    }
}
