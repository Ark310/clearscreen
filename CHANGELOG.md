# ClearScreen — changes

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
