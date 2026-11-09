<#
.SYNOPSIS
    Loads Client A's 10,000 demo whitelist records (ClientID 001) into ClearScreen through the API.

.DESCRIPTION
    Sends whitelist\client001_whitelist_part*.json to POST /whitelist/add (2,000 records per request),
    then waits until ClearScreen has embedded them for the similar-case search (GET /health,
    pending_embeddings). Safe to run again: records with the same Unique_ID are replaced, not duplicated.

    The API key is asked for (hidden) or read from $env:CLS_API_KEY. It is never written anywhere.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Load-Whitelist.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Load-Whitelist.ps1 -Api http://127.0.0.1:8000
#>
param(
    [string]$Api = "https://api.clearscreen.example",
    [string]$Folder = (Join-Path $PSScriptRoot "whitelist"),
    [switch]$NoWait
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Api = $Api.TrimEnd("/")

function Get-ApiKey {
    if ($env:CLS_API_KEY) { return $env:CLS_API_KEY }
    $s = Read-Host "Paste the API key of client 001 (input is hidden)" -AsSecureString
    return [Net.NetworkCredential]::new("", $s).Password
}

function Invoke-Api($Method, $Path, $Body) {
    # Retries what is temporary (rate limit, service busy, network); everything else is reported as is.
    for ($try = 1; ; $try++) {
        try {
            $req = @{ Method = $Method; Uri = "$Api$Path"; Headers = $script:Headers; TimeoutSec = 110; UseBasicParsing = $true }
            if ($Body) { $req.Body = $Body; $req.ContentType = "application/json; charset=utf-8" }
            return Invoke-RestMethod @req
        } catch {
            $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
            $detail = if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
            if ($try -lt 4 -and $code -in 0, 429, 502, 503, 504, 520, 521, 522, 524) {
                $wait = 15 * $try
                Write-Host "    temporary problem ($code) - retrying in $wait s..." -ForegroundColor Yellow
                Start-Sleep $wait
                continue
            }
            throw "HTTP $code from $Path : $detail"
        }
    }
}

Write-Host ""
Write-Host "ClearScreen demo - load Client A's whitelist (ClientID 001)" -ForegroundColor White
Write-Host "API: $Api"
$script:Headers = @{ "X-API-Key" = (Get-ApiKey) }

$h = Invoke-Api GET "/health"
if (-not $h.client) { throw "The API did not recognise this key." }
if ($h.client.ClientID -ne "001") { throw "This key belongs to client '$($h.client.ClientID)', not 001. Use client 001's key." }
Write-Host ("Key OK: client 001 ({0}). Records already on its whitelist: {1:N0}" -f $h.client.name, $h.client.whitelist_records)

$parts = Get-ChildItem $Folder -Filter "client001_whitelist_part*.json" | Sort-Object { [int]($_.BaseName -replace '\D', '') }
if (-not $parts) { throw "No client001_whitelist_part*.json files in $Folder" }
$added = 0; $replaced = 0; $start = Get-Date
foreach ($p in $parts) {
    $t = Get-Date
    $bytes = [IO.File]::ReadAllBytes($p.FullName)                 # UTF-8 as written - names with accents stay intact
    $r = Invoke-Api POST "/whitelist/add" $bytes
    $added += $r.added; $replaced += $r.replaced
    Write-Host ("  {0}: {1:N0} added, {2:N0} replaced ({3:N1} s)" -f $p.Name, $r.added, $r.replaced, ((Get-Date) - $t).TotalSeconds)
}
Write-Host ("Loaded: {0:N0} added, {1:N0} replaced in {2:N0} s." -f $added, $replaced, ((Get-Date) - $start).TotalSeconds) -ForegroundColor Green
Write-Host "Exact repeats work right away. The similar-case search (and so the AI review) needs the records embedded first."

if ($NoWait) { Write-Host "Not waiting for the embeddings (-NoWait). Check GET /health -> client.pending_embeddings."; exit 0 }
Write-Host "Waiting for the embeddings (checks every 30 s; Ctrl+C to stop waiting - loading is already done)..."
$first = $null; $firstAt = $null
while ($true) {
    $h = Invoke-Api GET "/health"
    $pending = [int]$h.client.pending_embeddings
    if ($pending -le 0) { break }
    if ($null -eq $first) { $first = $pending; $firstAt = Get-Date; $eta = "" }
    else {
        $done = $first - $pending; $mins = ((Get-Date) - $firstAt).TotalMinutes
        $eta = if ($done -gt 0) { " - about {0:N0} min left" -f ($pending / ($done / $mins)) } else { "" }
    }
    Write-Host ("  {0:HH:mm:ss}  {1:N0} records still to embed{2}" -f (Get-Date), $pending, $eta)
    Start-Sleep 30
}
Write-Host ("All {0:N0} records of client 001 are embedded and searchable. Ready for Run-Demo.ps1." -f $h.client.whitelist_records) -ForegroundColor Green
