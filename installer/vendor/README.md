# installer/vendor

The release package bundles **WinSW 2.12.0** (`WinSW-x64.exe`, the Windows service wrapper) here.
Binaries are not kept in this repository. To build a package, download `WinSW-x64.exe` 2.12.0 from
the [WinSW releases](https://github.com/winsw/winsw/releases/tag/v2.12.0) and put it in this folder.
`Install-ClearScreen.ps1` checks it against the SHA-256 pinned in the script and refuses to install it
if the hash does not match.
