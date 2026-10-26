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

$publicDesktop = Join-Path $env:PUBLIC 'Desktop\Clone Hero Companion.lnk'
$userDesktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Clone Hero Companion.lnk'
$startMenuLnk = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Clone Hero Companion\Clone Hero Companion.lnk'
$msiLog = Join-Path $env:RUNNER_TEMP 'msi-install.log'
function Show-ShortcutDiagnostics {
    Write-Output '--- shortcut diagnostics ---'
    $roots = @($env:ProgramData, $env:APPDATA, $env:PUBLIC, $env:USERPROFILE, [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory')) |
        Where-Object { $_ } | Select-Object -Unique
    foreach ($root in $roots) {
        Get-ChildItem -Path $root -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match 'Clone Hero' } | ForEach-Object { Write-Output "lnk: $($_.FullName)" }
    }
    if (Test-Path $msiLog) {
        Select-String -Path $msiLog -Pattern 'Property\(\w+\): (DESKTOPSHORTCUT|STARTMENUSHORTCUT|PREV\w*SHORTCUT|Stored\w*Shortcut|UILevel|INSTALLFOLDER) =',
            'Component: (StartMenuShortcut|DesktopShortcut|AppRegistry);', 'Action start .*(RemoveExistingProducts|CostFinalize|InstallValidate)',
            'Doing action: ' -ErrorAction SilentlyContinue |
            Where-Object { $_.Line -notmatch 'Doing action: ' } | ForEach-Object { Write-Output $_.Line.Trim() }
    }
    Write-Output '--- end diagnostics ---'
}
function Invoke-Msi([string[]]$Arguments, [string]$What) {
    $run = Start-Process msiexec.exe -ArgumentList ($Arguments + @('/l*v', "`"$msiLog`"")) -Wait -PassThru
    if ($run.ExitCode -notin @(0, 3010)) {
        if (Test-Path $msiLog) { Get-Content $msiLog -Tail 100 }
        throw "$What failed: $($run.ExitCode)"
    }
}
function Assert-Shortcuts([bool]$Desktop, [bool]$StartMenu, [string]$Case) {
    $desktopFound = (Test-Path $publicDesktop) -or (Test-Path $userDesktop)
    if ($desktopFound -ne $Desktop) { Show-ShortcutDiagnostics; throw "${Case}: desktop shortcut present=$desktopFound, expected $Desktop" }
    if ((Test-Path $startMenuLnk) -ne $StartMenu) { Show-ShortcutDiagnostics; throw "${Case}: Start menu shortcut present=$(Test-Path $startMenuLnk), expected $StartMenu" }
    Write-Output "${Case}: desktop=$Desktop startmenu=$StartMenu OK"
}

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
# 1. A default silent install (and the upgrade from it) creates both shortcuts.
Assert-Shortcuts $true $true 'default install + upgrade'
$ffmpeg = Join-Path $installDir '_internal\bin\ffmpeg.exe'
if (!(Test-Path $ffmpeg)) { throw 'The installed FFmpeg executable is missing' }
$doctor = Start-Process -FilePath $exe -ArgumentList @('doctor', '--data-dir', "`"$dataDir`"", '--report', "`"$report`"") -Wait -PassThru
if ($doctor.ExitCode -ne 0) { throw "Installed app failed: $($doctor.ExitCode)" }
$text = Get-Content $report -Raw
Write-Output $text
# Informational only: a runner without Deno/Node must not fail the build.
Write-Output ('YouTube JS runtime: ' + (($text -split "`n" | Where-Object { $_ -match '^JS runtime' }) -join ''))
foreach ($expected in @('Songs       : 1', 'ffmpeg check: OK', 'ffmpeg media: OK', $ffmpeg)) {
    if (!$text.Contains($expected)) { throw "Doctor did not confirm: $expected" }
}
# Uninstall removes the program but must preserve the library and user settings.
$settings = Join-Path $env:APPDATA 'CloneHeroCompanion\keep-settings.txt'
Set-Content $settings 'Keep user settings'
$uninstall = Start-Process msiexec.exe -ArgumentList @('/x', "`"$Installer`"", '/qn', '/norestart') -Wait -PassThru
if ($uninstall.ExitCode -notin @(0, 3010)) { throw "Uninstaller failed: $($uninstall.ExitCode)" }
if (Test-Path $exe) { throw 'App was not uninstalled' }
# 4. Uninstall removes the shortcuts.
Assert-Shortcuts $false $false 'uninstall of default install'
if (!(Test-Path $settings) -or !(Test-Path "$dataDir\Songs\Artist - Song\song.ini")) {
    throw 'Uninstall removed user data'
}

# Shortcut choices on silent installs, repairs and upgrades, each followed by an uninstall.
$scenarios = @(
    @{ Name = 'neither'; Desktop = '0'; StartMenu = '0' },
    @{ Name = 'start menu only'; Desktop = '0'; StartMenu = '1' },
    @{ Name = 'desktop only'; Desktop = '1'; StartMenu = '0' }
)
foreach ($scenario in $scenarios) {
    $wantDesktop = $scenario.Desktop -eq '1'
    $wantStart = $scenario.StartMenu -eq '1'
    Invoke-Msi @('/i', "`"$Installer`"", '/qn', '/norestart', "INSTALLFOLDER=`"$installDir`"",
                 "DESKTOPSHORTCUT=$($scenario.Desktop)", "STARTMENUSHORTCUT=$($scenario.StartMenu)") "Install ($($scenario.Name))"
    Assert-Shortcuts $wantDesktop $wantStart "install: $($scenario.Name)"
    # A repair without properties remembers the earlier choices.
    Invoke-Msi @('/fa', "`"$Installer`"", '/qn', '/norestart') "Repair ($($scenario.Name))"
    Assert-Shortcuts $wantDesktop $wantStart "repair: $($scenario.Name)"
    Invoke-Msi @('/x', "`"$Installer`"", '/qn', '/norestart') "Uninstall ($($scenario.Name))"
    Assert-Shortcuts $false $false "uninstall: $($scenario.Name)"
}

# Choices made on the older package survive a major upgrade that gives no properties.
Invoke-Msi @('/i', "`"$PreviousInstaller`"", '/qn', '/norestart', "INSTALLFOLDER=`"$installDir`"",
             'DESKTOPSHORTCUT=0', 'STARTMENUSHORTCUT=1') 'Install previous (start menu only)'
Invoke-Msi @('/i', "`"$Installer`"", '/qn', '/norestart') 'Upgrade'
Assert-Shortcuts $false $true 'upgrade remembers choices'
Invoke-Msi @('/x', "`"$Installer`"", '/qn', '/norestart') 'Uninstall after upgrade'
Assert-Shortcuts $false $false 'uninstall after upgrade'
