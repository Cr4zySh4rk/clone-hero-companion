param([Parameter(Mandatory=$true)][string]$Installer,
      [Parameter(Mandatory=$true)][string]$PreviousInstaller)
$ErrorActionPreference = 'Stop'
$installDir = Join-Path $env:RUNNER_TEMP 'Companion install test'
$dataDir = Join-Path $env:RUNNER_TEMP 'Clone Hero test data'
$report = Join-Path $env:RUNNER_TEMP 'companion-doctor.txt'
$env:APPDATA = Join-Path $env:RUNNER_TEMP 'companion-test-appdata'
$env:LOCALAPPDATA = Join-Path $env:RUNNER_TEMP 'companion-test-localappdata'
New-Item -ItemType Directory -Force "$dataDir\Logs", "$dataDir\Songs\Artist - Song", $env:APPDATA, $env:LOCALAPPDATA | Out-Null
Set-Content "$dataDir\Songs\Artist - Song\song.ini" "[song]`nartist = Artist`nname = Song"

# Install the older package, then upgrade without supplying its location again.
foreach ($package in @($PreviousInstaller, $Installer)) {
    $arguments = @('/i', "`"$package`"", '/qn', '/norestart', '/l*v', "`"$env:RUNNER_TEMP\msi-install.log`"")
    if ($package -eq $PreviousInstaller) { $arguments += "INSTALLFOLDER=`"$installDir`"" }
    $install = Start-Process msiexec.exe -ArgumentList $arguments -Wait -PassThru
    if ($install.ExitCode -notin @(0, 3010)) {
        Get-Content "$env:RUNNER_TEMP\msi-install.log" -Tail 100
        throw "MSI install failed: $($install.ExitCode)"
    }
}
$exe = Join-Path $installDir 'CloneHeroCompanion.exe'
$ffmpeg = Join-Path $installDir '_internal\bin\ffmpeg.exe'
if (!(Test-Path $ffmpeg)) { throw 'The installed FFmpeg executable is missing' }
$doctor = Start-Process -FilePath $exe -ArgumentList @('doctor', '--data-dir', "`"$dataDir`"", '--report', "`"$report`"") -Wait -PassThru
if ($doctor.ExitCode -ne 0) { throw "Installed app failed: $($doctor.ExitCode)" }
$text = Get-Content $report -Raw
Write-Output $text
foreach ($expected in @('Songs       : 1', 'ffmpeg check: OK', 'ffmpeg media: OK', $ffmpeg)) {
    if (!$text.Contains($expected)) { throw "Doctor did not confirm: $expected" }
}
# Uninstall removes the program but must preserve the library and user settings.
$settings = Join-Path $env:APPDATA 'CloneHeroCompanion\keep-settings.txt'
Set-Content $settings 'Keep user settings'
$uninstall = Start-Process msiexec.exe -ArgumentList @('/x', "`"$Installer`"", '/qn', '/norestart') -Wait -PassThru
if ($uninstall.ExitCode -notin @(0, 3010)) { throw "Uninstaller failed: $($uninstall.ExitCode)" }
if (Test-Path $exe) { throw 'App was not uninstalled' }
if (!(Test-Path $settings) -or !(Test-Path "$dataDir\Songs\Artist - Song\song.ini")) {
    throw 'Uninstall removed user data'
}
