<#
.SYNOPSIS
    Read-only check of ClearScreen's background embedding. Run ON THE SERVER, as Administrator.

.DESCRIPTION
    Shows: the services, memory, how busy the embedding service (ollama) is, the records still
    waiting per client, whether vectors are being added (measured over 60 s), and the latest
    embedding messages in the logs. Changes nothing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Check-Embedding.ps1
#>
param([int]$Seconds = 60, [string]$SqlServer = "localhost", [string]$Database = "ClearScreen")
$ErrorActionPreference = "Continue"

function Section($t) { Write-Host ""; Write-Host "== $t ==" -ForegroundColor Cyan }

function Query($sql) {
    $con = New-Object System.Data.SqlClient.SqlConnection "Server=$SqlServer;Database=$Database;Integrated Security=SSPI;Connect Timeout=10"
    try {
        $con.Open()
        $cmd = $con.CreateCommand(); $cmd.CommandText = $sql; $cmd.CommandTimeout = 120
        $t = New-Object System.Data.DataTable; $t.Load($cmd.ExecuteReader()); return $t
    } finally { $con.Close() }
}

$reg = Get-ItemProperty "HKLM:\SOFTWARE\ClearScreen" -ErrorAction SilentlyContinue
$data = if ($reg.DataDir) { $reg.DataDir } else { Join-Path $env:ProgramData "ClearScreen" }
$logs = Join-Path $data "logs"
Write-Host "ClearScreen embedding check   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')   data: $data"

Section "Services"
Get-Service "ClearScreen*" | Format-Table Name, Status -AutoSize | Out-String | Write-Host

Section "Memory"
$os = Get-CimInstance Win32_OperatingSystem
"{0:N1} GB free of {1:N1} GB" -f ($os.FreePhysicalMemory / 1MB), ($os.TotalVisibleMemorySize / 1MB) | Write-Host
if ($os.FreePhysicalMemory / 1MB -lt 0.5) { Write-Host "  Very little memory free - this alone can make embedding crawl or fail." -ForegroundColor Yellow }

Section "Records still to embed (per client)"
$q = "SELECT r.ClientID, COUNT(*) AS waiting FROM dbo.WL_Records r WHERE NOT EXISTS (SELECT 1 FROM dbo.WL_Payload_Chunks c WHERE c.RecordKey = r.RecordKey AND c.Model = 'bge-m3') GROUP BY r.ClientID"
try { Query $q | Format-Table -AutoSize | Out-String | Write-Host } catch { Write-Host "  SQL query failed: $($_.Exception.Message)" -ForegroundColor Yellow }

Section "Is it moving? (vectors added and ollama CPU over $Seconds s)"
try {
    $v1 = (Query "SELECT COUNT_BIG(*) AS n FROM dbo.WL_Chunk_Vectors").n
    $p1 = Get-Process ollama* -ErrorAction SilentlyContinue | Measure-Object CPU -Sum
    Start-Sleep $Seconds
    $v2 = (Query "SELECT COUNT_BIG(*) AS n FROM dbo.WL_Chunk_Vectors").n
    $p2 = Get-Process ollama* -ErrorAction SilentlyContinue | Measure-Object CPU -Sum
    $cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
    $busy = if ($p1.Sum -and $p2.Sum) { 100 * ($p2.Sum - $p1.Sum) / $Seconds / $cores } else { 0 }
    "Text pieces embedded: {0:N0} -> {1:N0}  (+{2:N0} in {3} s)" -f $v1, $v2, ($v2 - $v1), $Seconds | Write-Host
    "ollama CPU use: about {0:N0}% of the machine" -f $busy | Write-Host
    if ($v2 -gt $v1) { Write-Host "  MOVING - embedding is working. The waiting count drops once the current 500 records are done." -ForegroundColor Green }
    elseif ($busy -gt 20) { Write-Host "  Busy but nothing saved yet - probably a slow batch (long listings). Run this again in 10 minutes." -ForegroundColor Yellow }
    else { Write-Host "  STUCK - nothing saved and ollama is idle. See the log lines below." -ForegroundColor Red }
} catch { Write-Host "  Check failed: $($_.Exception.Message)" -ForegroundColor Yellow }

Section "Latest embedding messages (api.log)"
$api = Join-Path $logs "api.log"
if (Test-Path $api) { Select-String -Path $api -Pattern "embedding (worker|queue)" | Select-Object -Last 10 | ForEach-Object { $_.Line } | Write-Host }
else { Write-Host "  $api not found" }

Section "Embedding service log (last lines)"
Get-ChildItem $logs -Filter "ClearScreenEmbed*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 2 | ForEach-Object {
    Write-Host "-- $($_.Name)"; Get-Content $_.FullName -Tail 8 | Write-Host }
Write-Host ""
Write-Host "Nothing was changed. Send the output above if it says STUCK."
