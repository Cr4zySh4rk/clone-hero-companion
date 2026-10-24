$ErrorActionPreference = 'Stop'
$env:DOTNET_ROLL_FORWARD = 'LatestMajor'
$version = python -c "from chcompanion import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0) { throw 'Could not read app version' }
dotnet tool install --tool-path build/wix wix --version 5.0.2
if ($LASTEXITCODE -ne 0) { throw 'Could not install WiX' }
$wix = Join-Path $pwd 'build/wix/wix.exe'
& $wix extension add WixToolset.UI.wixext/5.0.2
if ($LASTEXITCODE -ne 0) { throw 'Could not install WiX UI extension' }
python scripts/make_license_rtf.py
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare license' }
$payload = Join-Path $pwd 'dist/CloneHeroCompanion'
# An older package with the same UpgradeCode exercises major upgrades in CI.
foreach ($build in @(@('0.0.1', 'build/smoke-previous.msi'), @($version, $env:ASSET))) {
    & $wix build packaging/windows.wxs -arch x64 -ext WixToolset.UI.wixext -culture en-US `
        -b "Payload=$payload" -d "AppVersion=$($build[0])" -out $build[1]
    if ($LASTEXITCODE -ne 0) { throw "MSI build failed for $($build[0])" }
}
