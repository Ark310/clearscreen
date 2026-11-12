# ClearScreen — changes

## 1.5.0 — 2026-11-12

Faster AI-reviewed alerts. No change to decisions, to the database, or to what `/match/main` returns.

- **AI questions at the same time.** New setting Admin → Settings → *AI speed* → **AI questions at
  once** (1–5, default 1 = one after another, as before). On a paid Groq plan set it to 5: an alert's
  up-to-5 AI reviews then run together, so the AI step takes about as long as one question. Each
  record is still reviewed on its own and the reviews are combined exactly as before. Keep 1 on the
  free plan. Applies within 10 seconds, no restart.
- **Stored embeddings reused.** Parts of an alert's listing that are already embedded (the same text
  in a whitelisted record) reuse the stored vector instead of being embedded again on the CPU.
- **Search data loaded at start-up.** After a restart or upgrade the API loads every client's search
  data in the background, instead of the first AI-path alert doing it (about 30 seconds for 20,000
  records). An alert that arrives during the load waits for it rather than starting a second one.
- `/match/test` timings are clock time: when AI calls overlap, `ai_seconds` and `waited_seconds`
  count overlapping time once, so `seconds` − `ai_seconds` − `waited_seconds` is still the search and
  checks.
- Docs: Administrator Guide (Groq plan, AI speed, troubleshooting), Developer and Installation
  Guides. The Integration Guide is unchanged.

## 1.4.1 — 2026-11-12

- Fixed: an upgrade stopped at step 1 with "C: needs at least 3 GB free" although nothing was to be
  installed on C:. The disk-space check reserved room for SQL Server Management Studio even when it
  was already installed. It now counts only what this run will actually install (SQL Server, SSMS),
  so an upgrade on a server that has both needs 1 GB free on C:.
- Everything else as in 1.4.0.

## 1.4.0 — 2026-11-12

**Two match endpoints: `/match/main` for clients, `/match/test` for you**
- `POST /match/groq` is renamed **`POST /match/test`**. It returns everything it returned before, and
  is **off by default**: switched on and off in Admin → Settings → *Test endpoint* (no restart). While
  off it is hidden from the `/docs` page and answers `404`, as if it did not exist.
- New **`POST /match/main`** for clients: the same checks and the same decisions, but each result
  holds only `ClientID`, `Unique_ID`, `disposition`, `confidence`, `rule`, `explanation`, `score`,
  `repeat_of`, `precedent` and `precedent_reasoning`.
- **Nothing client-facing names the AI provider.** Error codes `GROQ_LIMIT_REACHED` and
  `GROQ_NOT_CONFIGURED` are now `AI_LIMIT_REACHED` and `AI_NOT_CONFIGURED` (with neutral messages;
  Groq's own message only on `/match/test`); `/health` reports `ai_service` instead of `groq_key`; the
  `/docs` page and the Integration Guide no longer mention it. A test checks every client-facing
  response, error and the published API description for it.

**Embedding queue (Admin → Embedding, new tab)**
- Records waiting per client, records embedded and time taken in the current run, estimated time
  left, and each client's waiting records (`Unique_ID`, when added, listing size).
- **Pause / Resume.** A pause takes effect after the current small batch and lasts, also across
  restarts, until resumed.
- The queue works one client at a time, oldest first, 100 records per step — the waiting count now
  moves steadily instead of in jumps of 500.

**Faster answers while records are being embedded**
- **Alerts go first:** while an alert is in its similar-case search, the background embedding waits
  (it sends 16 text pieces at a time instead of 64, so an alert waits at most for one small batch).
- The in-memory search index of a client is **extended** with newly embedded records instead of being
  re-read in full from SQL Server for every alert while a queue is running.
- Decisions are unchanged: both are about when work happens, not what is decided.

**Upgrade:** run the new Setup.cmd (keeps settings, clients, keys and data). Client applications
must change `/match/groq` to `/match/main` (and the two renamed error codes).

## 1.3.0 — 2026-11-06

**HTTPS certificate as a .pfx file**
- Setup and ClearScreen Admin (Settings → Replace certificate…) accept a .pfx / .p12 file
  (certificate and private key in one) as well as two PEM files. The password is typed at a hidden
  prompt, passed to the helper over stdin, never stored and never put on a command line; a wrong
  password can be typed again.
- The .pfx is unpacked to PEM (what the HTTPS service reads) and checked exactly like PEM files — key
  belongs to the certificate, covers the domain, not expired — before anything is replaced. Tested with
  .pfx files from Windows' own export and from OpenSSL (modern and legacy encryption).
- New dependency: `cryptography` 50.0.2 (with `cffi`, `pycparser`), pinned by SHA-256 in the lock file.

**Fixed**
- Setup would have stopped at step 12 whenever HTTPS was set up: Caddy's normal progress messages on
  stderr were treated as an error by Windows PowerShell. Only Caddy's exit code decides now.

## 1.2.2 — 2026-11-06

- Fixed: setup stopped at step 12 (*Settings*) with "argument --email: expected one argument" when
  using a certificate (no e-mail): an empty value was dropped when setup called its Python helper.
  Setup now quotes every value by Windows' command-line rules (empty values, spaces, quotes,
  trailing backslashes).
- Fixed: the Groq key step (step 14) would have rejected every key: Windows PowerShell put an
  invisible byte-order mark in front of it. Setup now sends plain UTF-8, and the helper strips such a
  mark anyway and refuses keys with spaces or unusual characters with a clear message.
- Steps 12–14 were rehearsed through setup's real call path (settings, database, Groq key).

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
