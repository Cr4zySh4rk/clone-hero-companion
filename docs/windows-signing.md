# Windows packaging and signing

Windows releases use an MSI containing a PyInstaller `onedir` build. The default
installation is `C:\Program Files\Clone Hero Companion`. The main executable and
`_internal\bin\ffmpeg.exe` keep the same paths across upgrades. Songs and per-user
settings stay outside the installation and are preserved on uninstall.

The workflow builds with WiX 5.0.2, tests a clean install and a major upgrade,
runs the installed application's FFmpeg media check, and verifies that uninstall
preserves user data. Windows process tests also verify that a real child process
has no console window.

## Activate trusted signing

Signing is **not configured yet**. Releases are unsigned until a maintainer provisions
a trusted signing identity. A self-signed certificate does not establish public trust.

The workflow has an optional integration with
[Microsoft Artifact Signing](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart).
Provision an account with completed identity validation and a **Public Trust**
certificate profile. Availability and identity requirements are defined by that service.
Configure an Azure application or managed identity with the **Artifact Signing
Certificate Profile Signer** role scoped to this profile. Use GitHub OIDC federation
for this repository's release tags; do not store private signing keys in the repository.
See the [signing action's OIDC instructions](https://github.com/Azure/artifact-signing-action).

Set these GitHub Actions secrets:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

Set these GitHub Actions repository variables:

- `WINDOWS_SIGNING_ENDPOINT`: the account's regional endpoint
- `WINDOWS_SIGNING_ACCOUNT`: account name
- `WINDOWS_SIGNING_PROFILE`: Public Trust profile name
- `WINDOWS_SIGNING_ENABLED`: `true`, once the identity and permissions are ready

On a release tag, CI signs the app and FFmpeg **before** packaging, signs the MSI
after packaging, and checks all three Authenticode signatures. An enabled signing
step that fails stops publication; it does not silently publish an unsigned file.
Pull requests and ordinary branch builds do not authenticate to the signing service.
Another trusted signing provider can replace these steps without changing the MSI.

Signing identifies the publisher and supports reputation; it does not guarantee
SmartScreen acceptance or grant permission to protected Documents folders.
If Controlled folder access blocks the installed app, allow the exact installed
`CloneHeroCompanion.exe` path in Windows Security, restart the app, and recheck
the folders in **Game & folders**. Keep Controlled folder access enabled.
