# ClearScreen — changes

## 1.2.1 — 2026-11-06

- Fixed: setup stopped at step 11 (*Folder permissions*) with "Some or all identity references could
  not be translated" when run with `-SkipHttps`: it tried to give the HTTPS service, which is not
  installed then, access to folders. Service accounts are now only granted once the service exists.
- SQL Server: *updates for other Microsoft products* is turned on **before** SQL Server setup runs, so
  setup can include Microsoft's latest updates. Setup warns when SQL Server has no security updates
  (version 16.0.1000.x) and says how to install them.
- Docs: Windows Server 2025 listed; *After installing* now includes installing SQL Server's updates.
- Re-running setup after the 1.2.0 error continues where it stopped: SQL Server, SSMS, Python and the
  embedding service are not installed again.

## 1.2.0 — 2026-11-06

**Installs on a data drive**
- Setup asks for a drive (default D:) and puts everything that can move under `D:\ClearScreen`:
  the program (`Program`), settings / keys / logs / embedding model (`Data`), SQL Server's program
  files (`SQLServer`), the databases (`SQLData`), backups (`Backups`) and SSMS (`SSMS`). Installer
  temp files and downloads go there too. Only Windows' own parts stay on C:.
- The `D:\ClearScreen` folder gets its own permissions (administrators full control, everyone else
  read-only); databases and backups are readable only by administrators and SQL Server.
- Setup checks free space on both drives before starting, and records the locations in the
  registry (`HKLM\SOFTWARE\ClearScreen`); the app, the Admin app and uninstall read them from there.

**SQL Server Management Studio**
- Setup installs SSMS 22 (Microsoft's installer, pinned by SHA-256, Microsoft signature checked).
- ClearScreen Admin → Data → **Open SQL Server Management Studio** opens it connected to the
  ClearScreen database, to view the raw data.

## 1.1.0 — 2026-11-06

**HTTPS through Cloudflare**
- The default address is now `https://api.clearscreen.example`.
- HTTPS uses a certificate you supply, such as a Cloudflare Origin Certificate (`*.clearscreen.example`)
  with Cloudflare proxying the domain. Setup asks for the certificate and private key (PEM files).
  It checks that they belong together, cover the domain and have not expired, then stores them
  where only administrators and the HTTPS service can read them.
- With a supplied certificate only port 443 is used; port 80 is no longer opened.
- ClearScreen Admin → Settings shows the certificate and its expiry date, and has
  **Replace certificate…**. The health check shows the certificate too.
- Let's Encrypt is still available: run setup with `-LetsEncrypt`.

**SQL Server installed by setup**
- If the server has no SQL Server, setup offers to install SQL Server 2022 Developer (free;
  development, testing and demos only). It installs from Microsoft's ISO, pinned by SHA-256, and
  checks the setup program's Microsoft signature.
- SQL Server is installed with Windows sign-in only and no network access (this server only),
  with a memory cap and Microsoft updates.
- The health check shows the SQL Server edition and warns while it is Developer.

**Upgrading from 1.0.0:** run the new `Setup.cmd`. It keeps clients, keys, settings and data; it
asks for the certificate files and switches HTTPS over.

## 1.0.0 — 2026-11-04

First release.
