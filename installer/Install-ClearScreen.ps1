<#
.SYNOPSIS
    Installs (or upgrades / repairs) ClearScreen on a Windows Server.

.DESCRIPTION
    One run sets up everything:
      - Python 3.13 (private copy) and ClearScreen's Python packages (hash-checked)
      - SQL Server 2022 Developer, if this server has no SQL Server and you choose to install it
      - Microsoft ODBC Driver 18 for SQL Server (if missing)
      - the embedding service (Ollama + bge-m3, CPU only, local port only)
      - Caddy, the HTTPS front door with an automatic Let's Encrypt certificate
      - three Windows services, each under its own low-privilege virtual account
      - the ClearScreen database, tables and a read/write login for the API service
      - the Groq API key (asked for, tested, stored encrypted)
      - firewall rules for HTTPS, and the "ClearScreen Admin" shortcut

    Every download is checked against a pinned SHA-256 (and, for Python and the ODBC driver, a
    valid publisher signature) before it is used. Running it again upgrades or repairs the
    installation and keeps the configuration, clients and keys.

    Run Setup.cmd (it asks for administrator rights), or from an elevated PowerShell:
        powershell -ExecutionPolicy Bypass -File installer\Install-ClearScreen.ps1

.PARAMETER SqlServer         SQL Server instance (default: localhost).
.PARAMETER Database          Database name (default: ClearScreen).
.PARAMETER Domain            Public DNS name for HTTPS (default: api.clearscreen.example).
.PARAMETER TlsCertFile       HTTPS certificate, PEM (e.g. Cloudflare Origin Certificate). Asked for if not given.
.PARAMETER TlsKeyFile        Its private key, PEM.
.PARAMETER LetsEncrypt       Instead of certificate files: automatic Let's Encrypt certificate (needs port 80 too).
.PARAMETER AcmeEmail         Contact e-mail for Let's Encrypt (with -LetsEncrypt; asked for if not given).
.PARAMETER SkipHttps         Do not install the HTTPS service (testing behind another proxy).
.PARAMETER ApiPort           Local API port (default 8000).
.PARAMETER EmbedPort         Local embedding service port (default 11434).
.PARAMETER Unattended        Do not ask questions; use the parameters (the Groq key is then set in the Admin app).
.PARAMETER InstallSqlServer  Install SQL Server 2022 Developer on this server if it has no SQL Server (without asking).
.PARAMETER AcceptSqlServerLicense  Accept Microsoft's SQL Server license terms (https://aka.ms/useterms); needed
                             with -Unattended -InstallSqlServer.
.PARAMETER NoSqlServerInstall  Never offer to install SQL Server.
.PARAMETER InstallDrive      Drive for everything that can move (default D: if present): programs, data, SQL Server,
                             databases, backups and SSMS, all under <drive>\ClearScreen. Asked for if not given.
                             An existing installation stays where it is.
.PARAMETER NoSsms            Do not install SQL Server Management Studio.
#>
[CmdletBinding()]
param(
    [string]$SqlServer = "localhost",
    [string]$Database = "ClearScreen",
    [string]$Domain = "api.clearscreen.example",
    [string]$TlsCertFile,
    [string]$TlsKeyFile,
    [switch]$LetsEncrypt,
    [string]$AcmeEmail,
    [switch]$SkipHttps,
    [int]$ApiPort = 8000,
    [int]$EmbedPort = 11434,
    [switch]$Unattended,
    [switch]$InstallSqlServer,
    [switch]$AcceptSqlServerLicense,
    [switch]$NoSqlServerInstall,
    [string]$InstallDrive,
    [switch]$NoSsms
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"            # Invoke-WebRequest is far faster without the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Package = Split-Path $PSScriptRoot -Parent
$Version = (Get-Content (Join-Path $Package "VERSION") -Raw).Trim()
$Reg = "HKLM:\SOFTWARE\ClearScreen"        # where the installation is (read by the app, the Admin app and uninstall)
# $Root, $Install, $Data, $Downloads, $Logs are set by Resolve-Locations (below) before anything is written.

# ---------------------------------------------------------- pinned components ---
$C = @{
    Python = @{ Version = "3.13.9"; Url = "https://www.python.org/ftp/python/3.13.9/python-3.13.9-amd64.exe"
                Sha256 = "200ddff856bbff949d2cc1be42e8807c07538abd6b6966d5113a094cf628c5c5"; Signer = "Python Software Foundation" }
    Odbc   = @{ Version = "18.5.1.1"; Url = "https://download.microsoft.com/download/26bc9eb1-ba24-4b62-8274-bff0f935bb75/amd64/1033/msodbcsql.msi"
                Sha256 = "0604d2505a4a656038c39d4447b8170dd1cef90bab2e3015c40c12a6996b09c2"; Signer = "Microsoft Corporation" }
    Ollama = @{ Version = "0.40.14"; Url = "https://github.com/ollama/ollama/releases/download/v0.40.14/ollama-windows-amd64.zip"
                Sha256 = "65d68cade6cf6375f398b518224f8c8a15e062387f38cf477abfc848de7e7e29" }
    Caddy  = @{ Version = "2.11.7"; Url = "https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_windows_amd64.zip"
                Sha512 = "c308154504e53755958ffc62d8a2657ab17eefd6dd25c45dbb2c3d93d29519971f77ca652f7d0f2bcb7f980b9ba3822478865e29969d0272d7613550a43152b1" }
    # SQL Server 2022 Developer (free; development, testing and demos only). Microsoft's ISO: pinned by SHA-256,
    # and its setup.exe must carry Microsoft's signature. Only used when this server has no SQL Server.
    SqlDev = @{ Version = "2022 Developer"; Url = "https://download.microsoft.com/download/3/8/d/38de7036-2433-4207-8eae-06e247e17b25/SQLServer2022-x64-ENU-Dev.iso"
                Sha256 = "80d2cd75dd2b28098c0182f50ad86bd44f9b3a9f357c30ff617480a1497daffd" }
    # SQL Server Management Studio 22: Microsoft's installer (bootstrapper), pinned by SHA-256 + Microsoft signature.
    # It downloads SSMS itself from Microsoft (the Visual Studio installer verifies those signed packages).
    Ssms   = @{ Version = "22"; Url = "https://download.visualstudio.microsoft.com/download/pr/5f474104-723f-4007-bf4c-92e42a23dcc6/2c1ae4f6bd3aeb7314282ef5c94abee6aa2d68a2cbfd4b8825438f8542f66a36/vs_SSMS.exe"
                Sha256 = "2c1ae4f6bd3aeb7314282ef5c94abee6aa2d68a2cbfd4b8825438f8542f66a36"; Signer = "Microsoft Corporation" }
    WinSW  = @{ Version = "2.12.0"; File = (Join-Path $Package "installer\vendor\WinSW-x64.exe")
                Sha256 = "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da" }
}
$Services = @(
    @{ Name = "ClearScreenEmbed"; Display = "ClearScreen - Embedding service" }
    @{ Name = "ClearScreenApi";   Display = "ClearScreen - API" }
    @{ Name = "ClearScreenHttps"; Display = "ClearScreen - HTTPS" }
)

# ------------------------------------------------------------------- helpers ---
function Step($n, $text) { Write-Host ""; Write-Host "[$n] $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "    $text" }
function Good($text) { Write-Host "    $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Fail($text) { throw $text }

function Get-Verified {
    param([string]$Name, [hashtable]$Spec)
    $file = Join-Path $Downloads ([IO.Path]::GetFileName(([Uri]$Spec.Url).AbsolutePath))
    $ok = $false
    if (Test-Path $file) { $ok = Test-Hash $file $Spec }
    if (-not $ok) {
        Info "downloading $Name $($Spec.Version)..."
        Invoke-WebRequest -Uri $Spec.Url -OutFile $file -UseBasicParsing
        if (-not (Test-Hash $file $Spec)) {
            Remove-Item $file -Force
            Fail "$Name download does not match its pinned checksum - refused. (Network interference or a changed file.)"
        }
    }
    if ($Spec.Signer) {
        $sig = Get-AuthenticodeSignature $file
        if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notlike "*$($Spec.Signer)*") {
            Fail "$Name is not validly signed by $($Spec.Signer) - refused."
        }
    }
    Good "$Name $($Spec.Version) verified"
    return $file
}

function Test-Hash($file, $spec) {
    if ($spec.Sha512) { return (Get-FileHash $file -Algorithm SHA512).Hash -eq $spec.Sha512.ToUpper() }
    return (Get-FileHash $file -Algorithm SHA256).Hash -eq $spec.Sha256.ToUpper()
}

function Invoke-Py {
    # Runs a setup step with ClearScreen's Python. Secrets go in through -Stdin, never the command line.
    param([string[]]$Arguments, [string]$Stdin)
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $Py
    $psi.Arguments = ($Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join " "
    $psi.WorkingDirectory = Join-Path $Install "app"
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $p = [Diagnostics.Process]::Start($psi)
    if ($Stdin) { $p.StandardInput.WriteLine($Stdin) }
    $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEnd()
    $err = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    $out.Trim().Split("`n") | Where-Object { $_.Trim() } | ForEach-Object { Info $_.Trim() }
    return @{ Code = $p.ExitCode; Error = $err.Trim() }
}

function Set-Acl-Folder {
    # Replace a folder's permissions: Administrators + SYSTEM full control, plus the given accounts.
    param([string]$Path, [hashtable]$Grants)
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [Security.AccessControl.InheritanceFlags]"ContainerInherit,ObjectInherit"
    $none = [Security.AccessControl.PropagationFlags]::None
    foreach ($who in @("BUILTIN\Administrators", "NT AUTHORITY\SYSTEM")) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($who, "FullControl", $inherit, $none, "Allow")))
    }
    foreach ($who in $Grants.Keys) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($who, $Grants[$who], $inherit, $none, "Allow")))
    }
    Set-Acl -Path $Path -AclObject $acl
}

function Read-PemPath($prompt, $marker) {
    # Asks for a file path until it points to a PEM file containing $marker (the content is checked fully later).
    while ($true) {
        $path = (Read-Host $prompt).Trim().Trim('"')
        if ($path -and (Test-Path -LiteralPath $path -PathType Leaf)) {
            if ((Get-Content -LiteralPath $path -Raw) -match [regex]::Escape($marker)) { return $path }
            Warn "That file is not the right PEM file (it should contain $marker). Try again."
        } else { Warn "File not found. Try again." }
    }
}

function Resolve-Locations {
    # Everything that can move goes under <drive>\ClearScreen (default D:). An existing installation keeps its place.
    $prev = Get-ItemProperty $Reg -ErrorAction SilentlyContinue
    if ($prev -and $prev.InstallDir -and (Test-Path (Join-Path $prev.InstallDir "app\clearscreen\__init__.py"))) {
        $script:Root = $prev.Root; $script:Install = $prev.InstallDir; $script:Data = $prev.DataDir
    } elseif (Test-Path (Join-Path $env:ProgramFiles "ClearScreen\app\clearscreen\__init__.py")) {
        $script:Root = $null                                                  # a 1.0 installation on C: stays there
        $script:Install = Join-Path $env:ProgramFiles "ClearScreen"; $script:Data = Join-Path $env:ProgramData "ClearScreen"
    } else {
        $default = if ($InstallDrive) { $InstallDrive } elseif (Test-Path "D:\") { "D:" } else { $env:SystemDrive }
        $drive = $default
        while ($true) {
            if (-not $Unattended -and -not $InstallDrive) {
                $answer = Read-Host "    Drive for ClearScreen, SQL Server and SSMS [$drive]"
                if ($answer) { $drive = $answer }
            }
            $drive = $drive.Trim().TrimEnd("\").TrimEnd(":").ToUpper()
            $vol = if ($drive -match '^[A-Z]$') { Get-Volume -DriveLetter $drive -ErrorAction SilentlyContinue }
            if ($vol -and $vol.DriveType -eq "Fixed" -and $vol.FileSystem -eq "NTFS") { break }
            if ($Unattended -or $InstallDrive) { Fail "Drive $drive`: is not a local NTFS drive." }
            Warn "Drive $drive`: is not a local NTFS drive (e.g. D:). Try again."
            $drive = $default
        }
        $script:Root = "$drive`:\ClearScreen"
        $script:Install = Join-Path $Root "Program"; $script:Data = Join-Path $Root "Data"
    }
    $script:Downloads = Join-Path $Data "downloads"
    $script:Logs = Join-Path $Data "logs"
}

function Get-FreeGB($path) { return (Get-PSDrive ((Split-Path $path -Qualifier).TrimEnd(":"))).Free / 1GB }

function Install-Ssms {
    $exe = if ($Root) { Join-Path $Root "SSMS\Common7\IDE\Ssms.exe" }
           else { Join-Path $env:ProgramFiles "Microsoft SQL Server Management Studio 22\Release\Common7\IDE\Ssms.exe" }
    $known = (Get-ItemProperty $Reg -ErrorAction SilentlyContinue).SsmsExe
    foreach ($candidate in @($known, $exe, (Join-Path $env:ProgramFiles "Microsoft SQL Server Management Studio 22\Release\Common7\IDE\Ssms.exe"))) {
        if ($candidate -and (Test-Path $candidate)) {
            Set-ItemProperty $Reg SsmsExe $candidate
            Good "already installed ($candidate)"; return
        }
    }
    $boot = Get-Verified "SQL Server Management Studio installer" $C.Ssms
    Info "downloading and installing SSMS 22 from Microsoft - about 10-20 minutes, please wait..."
    $arguments = @("--quiet", "--norestart", "--wait", "--nocache")
    if ($Root) {
        $arguments += @("--installPath", "`"$(Join-Path $Root 'SSMS')`"", "--path", "shared=`"$(Join-Path $Root 'SSMS-Shared')`"",
                        "--path", "cache=`"$(Join-Path $Downloads 'ssms-cache')`"")
    }
    $p = Start-Process -FilePath $boot -ArgumentList $arguments -Wait -PassThru
    if ($p.ExitCode -notin 0, 3010 -or -not (Test-Path $exe)) {
        if ($p.ExitCode -in 0, 3010) { Warn "The SSMS installer finished, but $exe was not found. ClearScreen itself is not affected." }
        else { Warn "SSMS could not be installed (exit code $($p.ExitCode)). ClearScreen itself is not affected." }
        Warn "Its logs (ssms_*.log) are in $env:TEMP. Run Setup.cmd again later to retry."
        return
    }
    if ($p.ExitCode -eq 3010) { Warn "SSMS asks for a Windows restart - restart the server after setup finishes." }
    Set-ItemProperty $Reg SsmsExe $exe
    Good "SQL Server Management Studio installed ($exe). Open it from ClearScreen Admin > Data."
}

function Test-RebootPending {
    return (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending") -or
           (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired")
}

function Get-SqlEdition {
    # Signs in to the local default instance with the administrator's Windows login (shared memory).
    $cn = New-Object System.Data.SqlClient.SqlConnection "Server=localhost;Database=master;Integrated Security=true;Encrypt=true;TrustServerCertificate=true;Connect Timeout=15"
    try {
        $cn.Open()
        $cmd = $cn.CreateCommand()
        $cmd.CommandText = "SELECT CAST(SERVERPROPERTY('Edition') AS nvarchar(100)) + ', version ' + CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50))"
        return [string]$cmd.ExecuteScalar()
    } finally { $cn.Dispose() }
}

function Install-SqlServerDeveloper {
    if (Test-RebootPending) { Fail "Windows has a restart pending, which blocks SQL Server setup. Restart the server, then run Setup.cmd again." }
    Info "installation media: about 1.1 GB from download.microsoft.com"
    $iso = Get-Verified "SQL Server" $C.SqlDev
    $img = Mount-DiskImage -ImagePath $iso -Access ReadOnly -StorageType ISO -PassThru
    try {
        $drive = ($img | Get-Volume).DriveLetter + ":"
        $setupExe = Join-Path $drive "setup.exe"
        $sig = Get-AuthenticodeSignature $setupExe
        if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notlike "*O=Microsoft Corporation*") {
            Fail "SQL Server setup.exe is not validly signed by Microsoft - refused."
        }
        if (-not $AcceptSqlServerLicense) {
            if ($Unattended) { Fail "Add -AcceptSqlServerLicense to accept Microsoft's SQL Server license terms (https://aka.ms/useterms)." }
            Info "Before installing, read Microsoft's SQL Server 2022 Developer license terms: https://aka.ms/useterms"
            $license = Join-Path $drive "1033_ENU_LP\x64\1033\license.rtf"
            if (Test-Path $license) {
                $copy = Join-Path $Downloads "SQL Server 2022 Developer - license terms.rtf"
                Copy-Item $license $copy -Force
                try { Start-Process $copy; Info "(the license terms are open in a separate window)" } catch { Info "(license terms saved to $copy)" }
            }
            $answer = Read-Host "    Type ACCEPT to accept the license terms and install SQL Server, or press Enter to skip"
            if ($answer -cne "ACCEPT") {
                Warn "SQL Server not installed. Install it yourself, then run Setup.cmd again (or set the connection in the Admin app)."
                return
            }
        }
        $ramMB = [int]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB)
        $maxMemMB = [Math]::Min(8192, [Math]::Max(2048, [int]($ramMB / 4)))   # leave room for the API and embedding service
        $admin = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Info "installing the database engine - this takes about 10-20 minutes, please wait..."
        $arguments = @("/Q", "/ACTION=Install", "/IACCEPTSQLSERVERLICENSETERMS", "/SUPPRESSPRIVACYSTATEMENTNOTICE",
            "/FEATURES=SQLENGINE", "/INSTANCENAME=MSSQLSERVER",
            "/SQLSVCACCOUNT=`"NT Service\MSSQLSERVER`"", "/SQLSVCSTARTUPTYPE=Automatic", "/AGTSVCSTARTUPTYPE=Manual",
            "/SQLSYSADMINACCOUNTS=`"$admin`" `"BUILTIN\Administrators`"",        # Windows sign-in only: no SQL passwords
            "/SQLCOLLATION=SQL_Latin1_General_CP1_CI_AS",
            "/TCPENABLED=0", "/NPENABLED=0",                                     # reachable from this server only
            "/SQLMAXMEMORY=$maxMemMB", "/SQLMINMEMORY=0",
            "/UPDATEENABLED=True")                                               # include Microsoft's latest updates
        if ($Root) {
            # Program files, databases and backups on the ClearScreen drive. Databases and backups: administrators
            # only (SQL Server setup adds its own service account).
            Set-Acl-Folder (Join-Path $Root "SQLData") @{}
            Set-Acl-Folder (Join-Path $Root "Backups") @{}
            $arguments += @("/INSTANCEDIR=`"$(Join-Path $Root 'SQLServer')`"",
                            "/INSTALLSHAREDDIR=`"$(Join-Path $Root 'SQLServer\Shared')`"",
                            "/INSTALLSHAREDWOWDIR=`"$(Join-Path $Root 'SQLServer\Shared (x86)')`"",
                            "/INSTALLSQLDATADIR=`"$(Join-Path $Root 'SQLData')`"",
                            "/SQLBACKUPDIR=`"$(Join-Path $Root 'Backups')`"")
        }
        $p = Start-Process -FilePath $setupExe -ArgumentList $arguments -Wait -PassThru -NoNewWindow
        $summary = @((Join-Path $env:ProgramFiles "Microsoft SQL Server\160\Setup Bootstrap\Log\Summary.txt"),
                     $(if ($Root) { Join-Path $Root "SQLServer\Shared\160\Setup Bootstrap\Log\Summary.txt" })) |
                   Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
        if ($p.ExitCode -notin 0, 3010) { Fail "SQL Server setup failed (exit code $($p.ExitCode)). Details: $summary" }
        if ($p.ExitCode -eq 3010) { Warn "SQL Server asks for a Windows restart - restart the server after setup finishes." }
    } finally {
        Dismount-DiskImage -ImagePath $iso | Out-Null
    }
    if ((Get-Service MSSQLSERVER).Status -ne "Running") { Start-Service MSSQLSERVER }
    $telemetry = Get-Service SQLTELEMETRY -ErrorAction SilentlyContinue        # Developer edition allows turning it off
    if ($telemetry) { Stop-Service SQLTELEMETRY -Force -ErrorAction SilentlyContinue; Set-Service SQLTELEMETRY -StartupType Disabled }
    try {
        # Opt in to "updates for other Microsoft products" so Windows Update also delivers SQL Server security fixes.
        (New-Object -ComObject Microsoft.Update.ServiceManager).AddService2("7971f918-a847-4430-9279-4a52d1efe18d", 7, "") | Out-Null
        Info "Windows Update will also deliver SQL Server security updates"
    } catch { Warn "Could not turn on Microsoft Update; install SQL Server updates manually." }
    Remove-Item $iso -Force -ErrorAction SilentlyContinue                       # 1.1 GB no longer needed
    Good ("SQL Server installed: {0}; memory capped at {1:N0} MB; network access off (this server only); {2} and Administrators are SQL administrators" -f (Get-SqlEdition), $maxMemMB, $admin)
    if ($Root) { Info "SQL Server program: $Root\SQLServer   databases: $Root\SQLData   backups: $Root\Backups" }
    Warn "Developer edition: for development, testing and demos only. Upgrade to Standard before real client data (see the Installation Guide)."
}

function Wait-Url($url, $seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        try { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5 | Out-Null; return $true } catch { Start-Sleep 2 }
    }
    return $false
}

function Write-ServiceXml($name, $display, $description, $exe, $arguments, $workdir, $vars) {
    $envXml = ($vars.GetEnumerator() | ForEach-Object { "  <env name=`"$($_.Key)`" value=`"$([Security.SecurityElement]::Escape($_.Value))`" />" }) -join "`n"
    @"
<service>
  <id>$name</id>
  <name>$display</name>
  <description>$description</description>
  <executable>$([Security.SecurityElement]::Escape($exe))</executable>
  <arguments>$([Security.SecurityElement]::Escape($arguments))</arguments>
  <workingdirectory>$([Security.SecurityElement]::Escape($workdir))</workingdirectory>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <stoptimeout>30 sec</stoptimeout>
  <onfailure action="restart" delay="10 sec" />
  <onfailure action="restart" delay="30 sec" />
  <onfailure action="restart" delay="60 sec" />
  <resetfailure>1 hour</resetfailure>
  <logpath>$([Security.SecurityElement]::Escape($Logs))</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>8</keepFiles></log>
$envXml
</service>
"@ | Set-Content -Path (Join-Path $Install "services\$name.xml") -Encoding UTF8
}

# ---------------------------------------------------------------------- start ---
Write-Host ""
Write-Host "  ClearScreen $Version setup" -ForegroundColor White
Write-Host "  ---------------------------" -ForegroundColor DarkGray
try {
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) { Fail "Run setup as an administrator (use Setup.cmd)." }
    Resolve-Locations
    if ($Root) {
        # A new drive's root lets every user create files: ClearScreen's folder gets its own permissions -
        # administrators and SYSTEM full control, everyone else (incl. the services) read and run only.
        Set-Acl-Folder $Root @{ "NT AUTHORITY\Authenticated Users" = "ReadAndExecute" }
    }
    New-Item -ItemType Directory -Force -Path $Install, $Data, $Downloads, $Logs | Out-Null
    New-Item -Path $Reg -Force | Out-Null
    if ($Root) { Set-ItemProperty $Reg Root $Root }
    Set-ItemProperty $Reg InstallDir $Install
    Set-ItemProperty $Reg DataDir $Data
    # Installer temp files (SQL Server, SSMS, Python, pip) on the ClearScreen drive too.
    $Temp = Join-Path $Data "temp"
    Set-Acl-Folder $Temp @{}
    $env:TEMP = $Temp; $env:TMP = $Temp
} catch {
    Write-Host "  SETUP STOPPED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
Start-Transcript -Path (Join-Path $Logs "install.log") -Append | Out-Null
try {
    Step 1 "Checking this server"
    if ([Environment]::OSVersion.Version.Build -lt 14393) { Fail "Windows Server 2016 or newer is required." }
    if (-not [Environment]::Is64BitOperatingSystem) { Fail "A 64-bit Windows is required." }
    $existing = Test-Path (Join-Path $Install "app\clearscreen\__init__.py")
    Good ("Windows build {0}; installing to {1}{2}" -f [Environment]::OSVersion.Version.Build, $(if ($Root) { $Root } else { $Install }),
          $(if ($existing) { " - existing installation found: upgrading / repairing" } else { "" }))

    if (-not $Unattended) {
        if (-not $PSBoundParameters.ContainsKey("SqlServer")) {
            $answer = Read-Host "    SQL Server instance [$SqlServer]"
            if ($answer) { $SqlServer = $answer }
        }
    }
    # SQL Server on this server: offer to install SQL Server 2022 Developer if there is none.
    $sqlLocal = $SqlServer -match ('^(localhost|\.|\(local\)|127\.0\.0\.1|' + [regex]::Escape($env:COMPUTERNAME) + ')$')
    $sqlInstalled = [bool](Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL" -Name MSSQLSERVER -ErrorAction SilentlyContinue)
    $doSqlInstall = $false
    if ($sqlLocal -and -not $sqlInstalled -and -not $NoSqlServerInstall) {
        if ($InstallSqlServer) { $doSqlInstall = $true }
        elseif (-not $Unattended) {
            Warn "SQL Server is not installed on this server."
            $answer = Read-Host "    Install SQL Server 2022 Developer now? Free; for development, testing and demos only [Y/n]"
            $doSqlInstall = (-not $answer) -or ($answer -match '^[Yy]')
        }
    }
    if (-not $Unattended) {
        if (-not $SkipHttps -and -not $PSBoundParameters.ContainsKey("Domain")) {
            $answer = Read-Host "    Public domain name for HTTPS [$Domain]"
            if ($answer) { $Domain = $answer }
        }
        if (-not $SkipHttps -and $LetsEncrypt -and -not $AcmeEmail) {
            $AcmeEmail = Read-Host "    E-mail for the HTTPS certificate (Let's Encrypt expiry notices)"
        }
    }
    # HTTPS certificate files (default mode): e.g. the Cloudflare Origin Certificate + its private key, PEM.
    $haveCert = (Test-Path (Join-Path $Data "config\tls\origin.pem")) -and (Test-Path (Join-Path $Data "config\tls\origin.key"))
    if (-not $SkipHttps -and -not $LetsEncrypt -and -not $TlsCertFile) {
        if ($Unattended) {
            if (-not $haveCert) { Fail "Pass -TlsCertFile and -TlsKeyFile (PEM), or -LetsEncrypt." }
        } else {
            $replace = $true
            if ($haveCert) {
                $answer = Read-Host "    An HTTPS certificate is already installed. Press Enter to keep it, or type R to replace it"
                $replace = $answer -match '^[Rr]'
            }
            if ($replace) {
                Info "The HTTPS certificate for $Domain and its private key: two PEM files (e.g. from Cloudflare)."
                Info "Tip: in File Explorer, Shift + right-click a file > 'Copy as path', then right-click here to paste."
                $TlsCertFile = Read-PemPath "    Path of the certificate file" "-----BEGIN CERTIFICATE-----"
                $TlsKeyFile = Read-PemPath "    Path of the private key file" "PRIVATE KEY-----"
            }
        }
    }
    if ($TlsCertFile -and -not $TlsKeyFile) { Fail "-TlsKeyFile is needed with -TlsCertFile." }

    # Disk space: on the ClearScreen drive, and on C: (Windows keeps some SQL Server / SSMS files there regardless).
    $needRoot = 12 + $(if ($doSqlInstall) { 10 } else { 0 }) + $(if (-not $NoSsms) { 6 } else { 0 })
    $needSys = 1 + $(if ($doSqlInstall) { 3 } else { 0 }) + $(if (-not $NoSsms) { 2 } else { 0 })
    $rootDrive = Split-Path $Install -Qualifier
    if ($rootDrive -eq $env:SystemDrive) { $needRoot += $needSys; $needSys = 0 }
    $freeRoot = Get-FreeGB $Install
    if ($freeRoot -lt $needRoot) { Fail ("{0} needs at least {1} GB free for this installation (found {2:N1} GB)." -f $rootDrive, $needRoot, $freeRoot) }
    if ($needSys -gt 0) {
        $freeSys = Get-FreeGB $env:SystemDrive
        if ($freeSys -lt $needSys) { Fail ("{0} needs at least {1} GB free: Windows keeps some SQL Server / SSMS files there (found {2:N1} GB)." -f $env:SystemDrive, $needSys, $freeSys) }
    }
    Good ("disk space OK: {0} {1:N0} GB free{2}" -f $rootDrive, $freeRoot, $(if ($needSys -gt 0) { ", {0} {1:N0} GB free" -f $env:SystemDrive, $freeSys } else { "" }))

    if ($doSqlInstall) {
        Step 2 "SQL Server $($C.SqlDev.Version)"
        Install-SqlServerDeveloper
    } elseif ($sqlLocal -and $sqlInstalled) {
        Step 2 "SQL Server: already installed on this server"
        try { Good (Get-SqlEdition) } catch { Warn "installed, but this account could not sign in to it: $($_.Exception.Message)" }
    } else {
        Step 2 "SQL Server: $(if ($sqlLocal) { 'not installed - skipped' } else { "using $SqlServer" })"
    }

    if ($NoSsms) { Step 3 "SQL Server Management Studio: skipped (-NoSsms)" }
    else { Step 3 "SQL Server Management Studio 22"; Install-Ssms }

    Step 4 "Stopping running ClearScreen services (if any)"
    foreach ($s in $Services[2], $Services[1], $Services[0]) {
        $svc = Get-Service -Name $s.Name -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -ne "Stopped") { Stop-Service -Name $s.Name -Force; Info "stopped $($s.Name)" }
    }

    Step 5 "Python $($C.Python.Version)"
    $PyHome = Join-Path $Install "python"
    if (-not (Test-Path (Join-Path $PyHome "python.exe"))) {
        $exe = Get-Verified "Python" $C.Python
        $p = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList @("/quiet", "InstallAllUsers=1", "TargetDir=`"$PyHome`"",
            "PrependPath=0", "Include_launcher=0", "Include_test=0", "Include_doc=0", "Shortcuts=0", "AssociateFiles=0")
        if ($p.ExitCode -ne 0) { Fail "Python setup failed (exit code $($p.ExitCode))." }
    }
    Good "Python at $PyHome"

    Step 6 "ClearScreen application files and packages"
    New-Item -ItemType Directory -Force -Path $Install, (Join-Path $Install "services") | Out-Null
    $rc = Start-Process robocopy -Wait -PassThru -NoNewWindow -ArgumentList @("`"$(Join-Path $Package 'app')`"",
        "`"$(Join-Path $Install 'app')`"", "/MIR", "/XD", "__pycache__", "tests", ".pytest_cache", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    if ($rc.ExitCode -ge 8) { Fail "Copying the application files failed (robocopy $($rc.ExitCode))." }
    Copy-Item (Join-Path $Package "VERSION") $Install -Force
    $Venv = Join-Path $Install "venv"
    if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) { & (Join-Path $PyHome "python.exe") -m venv $Venv }
    $Py = Join-Path $Venv "Scripts\python.exe"
    & $Py -m pip install --disable-pip-version-check --no-input --quiet --no-cache-dir --require-hashes --only-binary=:all: `
        -r (Join-Path $Install "app\requirements.lock")
    if ($LASTEXITCODE -ne 0) { Fail "Installing the Python packages failed (see the log above)." }
    Good "application $Version installed, packages verified"

    Step 7 "Microsoft ODBC Driver 18 for SQL Server"
    if (Get-OdbcDriver -Name "ODBC Driver 18 for SQL Server" -Platform "64-bit" -ErrorAction SilentlyContinue) {
        Good "already installed"
    } else {
        $msi = Get-Verified "ODBC Driver 18" $C.Odbc
        $p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @("/i", "`"$msi`"", "/quiet", "/norestart", "IACCEPTMSODBCSQLLICENSETERMS=YES")
        if ($p.ExitCode -notin 0, 3010) { Fail "ODBC driver setup failed (exit code $($p.ExitCode))." }
        Good "installed"
    }

    Step 8 "Embedding service (Ollama $($C.Ollama.Version), CPU only)"
    $OllamaDir = Join-Path $Install "ollama"
    $OllamaStamp = Join-Path $OllamaDir ".version"
    if (-not (Test-Path $OllamaStamp) -or (Get-Content $OllamaStamp -Raw).Trim() -ne $C.Ollama.Version) {
        $zip = Get-Verified "Ollama" $C.Ollama
        if (Test-Path $OllamaDir) { Remove-Item $OllamaDir -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $OllamaDir | Out-Null
        Info "unpacking (large: GPU libraries are included but unused)..."
        tar.exe -xf $zip -C $OllamaDir
        if ($LASTEXITCODE -ne 0) { Fail "Unpacking Ollama failed." }
        Set-Content $OllamaStamp $C.Ollama.Version
    }
    Good "ready"

    if (-not $SkipHttps) {
        Step 9 "HTTPS front door (Caddy $($C.Caddy.Version))"
        $CaddyDir = Join-Path $Install "caddy"
        $CaddyStamp = Join-Path $CaddyDir ".version"
        if (-not (Test-Path $CaddyStamp) -or (Get-Content $CaddyStamp -Raw).Trim() -ne $C.Caddy.Version) {
            $zip = Get-Verified "Caddy" $C.Caddy
            New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null
            tar.exe -xf $zip -C $CaddyDir caddy.exe
            Set-Content $CaddyStamp $C.Caddy.Version
        }
        Good "ready"
    } else { Step 9 "HTTPS front door: skipped (-SkipHttps)" }

    Step 10 "Windows services"
    if ((Get-FileHash $C.WinSW.File -Algorithm SHA256).Hash -ne $C.WinSW.Sha256.ToUpper()) { Fail "The bundled WinSW does not match its pinned checksum." }
    $embedUrl = "http://127.0.0.1:$EmbedPort"
    Write-ServiceXml "ClearScreenEmbed" "ClearScreen - Embedding service" `
        "Turns whitelist listings into vectors for the similar-case search (bge-m3, CPU). Local only." `
        (Join-Path $OllamaDir "ollama.exe") "serve" $OllamaDir @{
            OLLAMA_HOST = "127.0.0.1:$EmbedPort"; OLLAMA_MODELS = (Join-Path $Data "ollama\models")
            OLLAMA_KEEP_ALIVE = "-1"; OLLAMA_NUM_PARALLEL = "1"; CUDA_VISIBLE_DEVICES = "-1"
            USERPROFILE = (Join-Path $Data "ollama\home"); HOME = (Join-Path $Data "ollama\home") }
    Write-ServiceXml "ClearScreenApi" "ClearScreen - API" `
        "The ClearScreen API (127.0.0.1:$ApiPort). Reached from outside only through the HTTPS service." `
        $Py "-m uvicorn clearscreen.api:app --host 127.0.0.1 --port $ApiPort --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header --log-level warning --timeout-keep-alive 30" `
        (Join-Path $Install "app") @{ CLS_INSTALL_DIR = $Install; CLS_DATA_DIR = $Data; PYTHONDONTWRITEBYTECODE = "1" }
    if (-not $SkipHttps) {
        Write-ServiceXml "ClearScreenHttps" "ClearScreen - HTTPS" `
            "HTTPS front door for $Domain (Caddy, automatic Let's Encrypt certificate). Forwards to the local API." `
            (Join-Path $CaddyDir "caddy.exe") "run --config `"$(Join-Path $Data 'config\Caddyfile')`" --adapter caddyfile" `
            $CaddyDir @{ APPDATA = (Join-Path $Data "caddy"); XDG_DATA_HOME = (Join-Path $Data "caddy"); XDG_CONFIG_HOME = (Join-Path $Data "caddy") }
    }
    foreach ($s in $Services) {
        if ($SkipHttps -and $s.Name -eq "ClearScreenHttps") { continue }
        $wrapper = Join-Path $Install "services\$($s.Name).exe"
        Copy-Item $C.WinSW.File $wrapper -Force
        if (-not (Get-Service -Name $s.Name -ErrorAction SilentlyContinue)) {
            & $wrapper install | Out-Null
            if ($LASTEXITCODE -ne 0) { Fail "Could not register the $($s.Name) service." }
        }
        # Each service runs as its own virtual account (NT SERVICE\<name>): no password, minimal rights.
        & sc.exe config $s.Name obj= "NT SERVICE\$($s.Name)" password= "" | Out-Null
        & sc.exe failure $s.Name reset= 3600 actions= restart/10000/restart/30000/restart/60000 | Out-Null
        Good "$($s.Name) registered (runs as NT SERVICE\$($s.Name))"
    }

    Step 11 "Folder permissions"
    Set-Acl-Folder (Join-Path $Data "config") @{ "NT SERVICE\ClearScreenApi" = "ReadAndExecute"; "NT SERVICE\ClearScreenHttps" = "ReadAndExecute" }
    Set-Acl-Folder $Logs @{ "NT SERVICE\ClearScreenApi" = "Modify"; "NT SERVICE\ClearScreenEmbed" = "Modify"; "NT SERVICE\ClearScreenHttps" = "Modify" }
    Set-Acl-Folder (Join-Path $Data "ollama") @{ "NT SERVICE\ClearScreenEmbed" = "Modify" }
    if (-not $SkipHttps) { Set-Acl-Folder (Join-Path $Data "caddy") @{ "NT SERVICE\ClearScreenHttps" = "Modify" } }
    # The certificate's private key: readable by the HTTPS service only (not even the API service).
    Set-Acl-Folder (Join-Path $Data "config\tls") @{ "NT SERVICE\ClearScreenHttps" = "ReadAndExecute" }
    Set-Acl-Folder $Downloads @{}
    Good "configuration readable only by administrators and the services; secrets encrypted on top (DPAPI)"

    Step 12 "Settings"
    $email = if ($AcmeEmail) { $AcmeEmail } else { "" }
    $tlsMode = if ($LetsEncrypt) { "automatic" } else { "certificate" }
    $r = Invoke-Py @("-m", "clearscreen.setup_cli", "settings", "--domain", $Domain, "--email", $email,
                     "--api-port", "$ApiPort", "--embed-url", $embedUrl, "--tls-mode", $tlsMode)
    if ($r.Code -ne 0) { Fail "Saving settings failed: $($r.Error)" }
    if (-not $SkipHttps) {
        if ($TlsCertFile) {
            $r = Invoke-Py @("-m", "clearscreen.setup_cli", "tls-cert", "--cert", $TlsCertFile, "--key", $TlsKeyFile)
            if ($r.Code -ne 0) { Fail "The HTTPS certificate was not accepted: $($r.Error)" }
            Good "certificate stored in $Data\config\tls (readable only by administrators and the HTTPS service)"
            Warn "Now delete the key file you used ($TlsKeyFile) and any other copies - ClearScreen keeps its own."
        }
        $out = & (Join-Path $CaddyDir "caddy.exe") validate --config (Join-Path $Data "config\Caddyfile") --adapter caddyfile 2>&1
        if ($LASTEXITCODE -ne 0) { Fail "The HTTPS configuration is not valid: $(($out | Select-Object -Last 1))" }
        Good "HTTPS configuration valid ($(if ($LetsEncrypt) { 'automatic Let''s Encrypt certificate' } else { 'your certificate, port 443 only' }))"
    }

    Step 13 "SQL Server database ($SqlServer / $Database)"
    $r = Invoke-Py @("-m", "clearscreen.setup_cli", "sql-init", "--server", $SqlServer, "--database", $Database,
                     "--service-login", "NT SERVICE\ClearScreenApi")
    if ($r.Code -ne 0) {
        Warn "Database setup failed: $($r.Error)"
        Warn "Fix the SQL Server access, then set the connection in ClearScreen Admin > Settings."
    } else { Good "database ready; the API service signs in as NT SERVICE\ClearScreenApi (read/write only)" }

    Step 14 "Groq API key"
    Push-Location (Join-Path $Install "app")
    try { $haveKey = ((& $Py -m clearscreen.setup_cli status 2>$null) -join "`n" | ConvertFrom-Json).groq_key -eq "set" }
    catch { $haveKey = $false } finally { Pop-Location }
    if ($Unattended) {
        Warn "Skipped (unattended). Set it in ClearScreen Admin > Settings."
    } else {
        $prompt = if ($haveKey) { "    A Groq key is already set. Paste a new one to replace it, or press Enter to keep it" }
                  else { "    Paste the Groq API key (input is hidden)" }
        while ($true) {
            $secure = Read-Host $prompt -AsSecureString
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
            if (-not $plain) {
                if ($haveKey) { Good "kept the existing key"; break }
                Warn "No key entered - set it later in ClearScreen Admin > Settings."; break
            }
            $r = Invoke-Py @("-m", "clearscreen.setup_cli", "groq-key") -Stdin $plain
            $plain = $null
            if ($r.Code -eq 0) { Good "Groq key tested and stored encrypted"; break }
            Warn "That key did not work: $($r.Error). Try again, or press Enter to skip."
        }
    }

    Step 15 "Firewall"
    if (-not $SkipHttps) {
        Remove-NetFirewallRule -Name "ClearScreen-HTTPS" -ErrorAction SilentlyContinue
        if ($LetsEncrypt) {
            New-NetFirewallRule -Name "ClearScreen-HTTPS" -DisplayName "ClearScreen HTTPS (TCP 80, 443)" -Direction Inbound `
                -Protocol TCP -LocalPort 80, 443 -Action Allow -Profile Any | Out-Null
            Good "inbound TCP 80 and 443 allowed (80 only redirects to HTTPS and answers certificate checks)"
        } else {
            New-NetFirewallRule -Name "ClearScreen-HTTPS" -DisplayName "ClearScreen HTTPS (TCP 443)" -Direction Inbound `
                -Protocol TCP -LocalPort 443 -Action Allow -Profile Any | Out-Null
            Good "inbound TCP 443 allowed (port 80 is not used)"
        }
    }
    Info "the API ($ApiPort) and embedding service ($EmbedPort) listen on 127.0.0.1 only - not reachable from outside"

    Step 16 "Starting the services"
    Start-Service ClearScreenEmbed
    if (-not (Wait-Url "$embedUrl/api/version" 90)) { Fail "The embedding service did not start (see $Logs)." }
    Good "embedding service running"
    Info "downloading the embedding model bge-m3 (about 1.2 GB, first install only)..."
    $r = Invoke-Py @("-m", "clearscreen.setup_cli", "pull-model")
    if ($r.Code -ne 0) { Fail "Downloading bge-m3 failed: $($r.Error)" }
    Start-Service ClearScreenApi
    if (-not (Wait-Url "http://127.0.0.1:$ApiPort/health" 90)) { Fail "The API did not start (see $Logs)." }
    Good "API running"
    if (-not $SkipHttps) { Start-Service ClearScreenHttps; Good "HTTPS service running" }

    Step 17 "ClearScreen Admin shortcut and uninstall entry"
    Copy-Item (Join-Path $Package "Uninstall.cmd") $Install -Force
    Copy-Item (Join-Path $Package "installer\Uninstall-ClearScreen.ps1") (Join-Path $Install "services") -Force
    $pyw = Join-Path $Venv "Scripts\pythonw.exe"
    foreach ($dir in @([Environment]::GetFolderPath("CommonDesktopDirectory"), (Join-Path ([Environment]::GetFolderPath("CommonStartMenu")) "Programs"))) {
        $lnk = Join-Path $dir "ClearScreen Admin.lnk"
        $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
        $sh.TargetPath = $pyw
        $sh.Arguments = "-m clearscreen.admin"
        $sh.WorkingDirectory = Join-Path $Install "app"
        $sh.Description = "Manage ClearScreen on this server"
        $sh.IconLocation = "$env:SystemRoot\System32\imageres.dll,109"
        $sh.Save()
        $bytes = [IO.File]::ReadAllBytes($lnk); $bytes[0x15] = $bytes[0x15] -bor 0x20; [IO.File]::WriteAllBytes($lnk, $bytes)  # run as administrator
    }
    $key = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\ClearScreen"
    New-Item -Path $key -Force | Out-Null
    Set-ItemProperty $key DisplayName "ClearScreen"
    Set-ItemProperty $key DisplayVersion $Version
    Set-ItemProperty $key Publisher "Contoso"
    Set-ItemProperty $key InstallLocation $Install
    Set-ItemProperty $key UninstallString "`"$(Join-Path $Install 'Uninstall.cmd')`""
    Set-ItemProperty $key NoModify 1
    Set-ItemProperty $key NoRepair 1
    Good "'ClearScreen Admin' on the desktop and in the Start menu"

    Step 18 "Health check"
    $h = Invoke-RestMethod "http://127.0.0.1:$ApiPort/health"
    Info ("status: {0}  (database: {1}, embedding service: {2}, Groq key: {3})" -f $h.status, $h.components.database,
          $h.components.embedding_service, $h.components.groq_key)

    Write-Host ""
    Write-Host "  ClearScreen $Version is installed." -ForegroundColor Green
    Write-Host "  Program: $Install    Data, settings and logs: $Data"
    if ($Root -and (Test-Path (Join-Path $Root "SQLData"))) { Write-Host "  Databases: $Root\SQLData    Backups: $Root\Backups" }
    Write-Host ""
    Write-Host "  Next steps:"
    if (-not $SkipHttps -and $LetsEncrypt) {
        Write-Host "   1. DNS: point $Domain (A record, not proxied) to this server's public IP address."
        Write-Host "   2. AWS security group: allow inbound TCP 443 and 80. Keep RDP (3389) limited to your office IP."
        Write-Host "      The HTTPS certificate is issued automatically once DNS points here."
    } elseif (-not $SkipHttps) {
        Write-Host "   1. Cloudflare: $Domain is an A record to this server's public IP, PROXIED (orange cloud),"
        Write-Host "      and SSL/TLS encryption mode is 'Full (strict)'."
        Write-Host "   2. AWS security group: allow inbound TCP 443 from Cloudflare's IP ranges only; no port 80."
        Write-Host "      Keep RDP (3389) limited to your office IP. Test: https://$Domain/health"
    }
    Write-Host "   3. Open 'ClearScreen Admin' (desktop) > Clients > Add client, and give each client its API key."
    Write-Host "   4. Load whitelist records: via POST /whitelist/add, or Admin > Data > Load from Excel."
    Write-Host ""
} catch {
    Write-Host ""
    Write-Host "  SETUP STOPPED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Full log: $(Join-Path $Logs 'install.log')" -ForegroundColor Red
    Write-Host "  Fix the problem and run Setup.cmd again - it continues where it left off." -ForegroundColor Red
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
