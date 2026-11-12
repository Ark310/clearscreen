<#
.SYNOPSIS
    Runs the ClearScreen demo cases for Client A (ClientID 001) and shows a scorecard.

.DESCRIPTION
    Sends the alerts in cases\demo_cases.json to POST /match/main (what clients use) and compares each result with the
    expected outcome. Alerts that need no AI go 5 per request; alerts for the AI review go one per
    request (each request must finish within Cloudflare's 100 seconds). Then three security checks.
    Results are saved to results\demo_results_<date-time>.csv.

    Load the whitelist first (Load-Whitelist.ps1) and wait until it is fully embedded.
    The API key is asked for (hidden) or read from $env:CLS_API_KEY. It is never written anywhere.

.PARAMETER Category  Run only some groups, e.g. -Category exact_repeat,score_below_threshold
.PARAMETER SkipAI    Skip the groups that use the AI review (fast, no Groq usage).
.PARAMETER Endpoint  main (default, what clients use) or test (POST /match/test: also AI calls and time waited
                     for Groq). /match/test works only while switched on in Admin > Settings > Test endpoint.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Run-Demo.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Run-Demo.ps1 -SkipAI
#>
param(
    [string]$Api = "https://api.clearscreen.example",
    [string]$CasesFile = (Join-Path $PSScriptRoot "cases\demo_cases.json"),
    [string[]]$Category,
    [switch]$SkipAI,
    [ValidateSet("main", "test")][string]$Endpoint = "main",
    [int]$TimeoutSec = 0
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Api = $Api.TrimEnd("/")
# Through Cloudflare a request is cut at 100 s, so waiting longer is pointless. On the server itself
# (http://127.0.0.1:8000) there is no such limit: AI cases may wait out Groq's rate limit.
$Local = $Api -match '^http://(127\.0\.0\.1|localhost)(:|/|$)'
if (-not $TimeoutSec) { $TimeoutSec = if ($Local) { 600 } else { 110 } }
$AiGroups = @("send_to_ai", "ai_same_case_new_address", "ai_location_now_matches")
$MatchPath = "/match/$Endpoint"

function Get-ApiKey {
    if ($env:CLS_API_KEY) { return $env:CLS_API_KEY }
    $s = Read-Host "Paste the API key of client 001 (input is hidden)" -AsSecureString
    return [Net.NetworkCredential]::new("", $s).Password
}

function Send-Json($Path, $Object, $Headers) {
    # Returns @{ Ok; Status; Body }. Never throws for HTTP errors, so the demo can show them.
    $json = ConvertTo-Json -InputObject $Object -Depth 6 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$Api$Path" -Headers $Headers -Body $bytes `
            -ContentType "application/json; charset=utf-8" -TimeoutSec $TimeoutSec -UseBasicParsing
        return @{ Ok = $true; Status = 200; Body = $r }
    } catch {
        $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        $body = $null
        if ($_.ErrorDetails.Message) { try { $body = $_.ErrorDetails.Message | ConvertFrom-Json } catch { $body = $_.ErrorDetails.Message } }
        else { $body = $_.Exception.Message }
        return @{ Ok = $false; Status = $code; Body = $body }
    }
}

function Show-Result($case, $res, $seconds) {
    $exp = $case.expect
    $isAi = $case.category -in $AiGroups
    $open = -not $exp.disposition                                  # no fixed expectation: the AI decides
    $okDisp = $open -or ($res.disposition -eq $exp.disposition)
    $okRule = $open -or (-not $exp.rule) -or ($res.rule -eq $exp.rule)
    $mark = if ($open) { "[ AI ]" } elseif ($okDisp -and $okRule) { "[ OK ]" } elseif ($okDisp) { "[RULE]" } else { "[DIFF]" }
    $color = if ($open) { "White" } elseif ($okDisp -and $okRule) { "Green" } elseif ($isAi -and $res.disposition -eq "REVIEW") { "Yellow" } else { "Red" }
    if (-not $exp.disposition) { $exp = [pscustomobject]@{ disposition = "(AI)"; rule = $null } }
    $ai = if ($res.ai_calls) { "AI calls: $($res.ai_calls)" + $(if ([double]$res.waited_seconds -gt 0) { ", waited for Groq: $($res.waited_seconds)s" } else { "" }) } else { "" }
    Write-Host ("  {0} {1,-12} expected {2,-6} got {3,-6} {4,-32} {5,5:N1}s  {6}" -f $mark, $case.id, $exp.disposition,
        $res.disposition, $res.rule, $seconds, $ai) -ForegroundColor $color
    $script:rows.Add([pscustomobject]@{
        case = $case.id; group = $case.category; expected = $exp.disposition; expected_rule = $exp.rule
        result = $res.disposition; rule = $res.rule; match = ($okDisp -and $okRule); confidence = $res.confidence
        seconds = [Math]::Round($seconds, 2); ai_calls = $res.ai_calls; groq_wait_seconds = $res.waited_seconds
        precedent = $res.precedent; precedent_reasoning = $res.precedent_reasoning; explanation = $res.explanation })
    if ($isAi) { if ($okDisp -and $okRule) { $script:aiPass++ } else { $script:aiFail++ } }
    elseif ($okDisp -and $okRule) { $script:pass++ } else { $script:fail++ }
}

Write-Host ""
Write-Host "ClearScreen demo - Client A (ClientID 001)" -ForegroundColor White
Write-Host "API: $Api   endpoint: POST $MatchPath"
$headers = @{ "X-API-Key" = (Get-ApiKey) }
try { $h = Invoke-RestMethod "$Api/health" -Headers $headers -TimeoutSec 60 -UseBasicParsing }
catch { throw "Cannot reach $Api/health : $($_.Exception.Message)" }
if (-not $h.client) { throw "The API did not recognise this key." }
if ($h.client.ClientID -ne "001") { throw "This key belongs to client '$($h.client.ClientID)', not 001." }
Write-Host ("Client 001: {0:N0} whitelist records, {1:N0} still to embed." -f $h.client.whitelist_records, $h.client.pending_embeddings)
if ($h.client.whitelist_records -lt 10000) { Write-Host "Load the whitelist first (Load-Whitelist.ps1)." -ForegroundColor Yellow }
if ($h.client.pending_embeddings -gt 0) {
    Write-Host "Embedding is not finished: similar-case and AI results can differ until it is. Wait, or use -SkipAI." -ForegroundColor Yellow
}

$data = Get-Content $CasesFile -Raw -Encoding UTF8 | ConvertFrom-Json
$cases = @($data.cases)
if ($Category) { $cases = @($cases | Where-Object { $_.category -in $Category }) }
if ($SkipAI) { $cases = @($cases | Where-Object { $_.category -notin $AiGroups }) }
$script:rows = New-Object System.Collections.Generic.List[object]
$script:pass = 0; $script:fail = 0; $script:aiPass = 0; $script:aiFail = 0; $script:noAnswer = $false
$started = Get-Date
if (-not $Local -and @($cases | Where-Object { $_.category -in $AiGroups }).Count -gt 0) {
    Write-Host ""
    Write-Host "Note: through Cloudflare every request must finish within 100 s. AI-reviewed alerts on Groq's free tier" -ForegroundColor Yellow
    Write-Host "often wait longer for Groq's rate limit. For the AI groups, run this on the server against the API itself:" -ForegroundColor Yellow
    Write-Host "  .\Run-Demo.ps1 -Api http://127.0.0.1:8000     (see README, 'AI cases and Groq')" -ForegroundColor Yellow
}

foreach ($group in @($data.groups)) {
    $inGroup = @($cases | Where-Object { $_.category -eq $group.category })
    if (-not $inGroup) { continue }
    Write-Host ""
    Write-Host "$($group.title)" -ForegroundColor Cyan
    Write-Host "  $($group.what)" -ForegroundColor DarkGray
    $size = if ($group.category -in $AiGroups) { 1 } else { 5 }
    for ($i = 0; $i -lt $inGroup.Count; $i += $size) {
        $batch = @($inGroup[$i..([Math]::Min($i + $size, $inGroup.Count) - 1)])
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $r = Send-Json $MatchPath @($batch | ForEach-Object { $_.alert }) $headers
        $each = $watch.Elapsed.TotalSeconds / $batch.Count                 # time per alert, as the client sees it
        if (-not $r.Ok -and $r.Status -eq 404 -and $Endpoint -eq "test") {
            throw "POST /match/test is switched off. Turn it on in ClearScreen Admin > Settings > Test endpoint, or run without -Endpoint test."
        }
        if (-not $r.Ok) {
            $code = if ($r.Body.error_code) { $r.Body.error_code } elseif ($r.Status -eq 0) { "NO ANSWER" } else { "HTTP $($r.Status)" }
            $msg = if ($r.Body.message) { $r.Body.message } elseif ($r.Status -eq 0) { "no answer within 110 s (or the connection failed): $($r.Body)" } else { "$($r.Body)".Substring(0, [Math]::Min(200, "$($r.Body)".Length)) }
            Write-Host ("  [FAIL] {0,-12} {1} - {2}" -f (($batch | ForEach-Object { $_.id }) -join ","), $code, $msg) -ForegroundColor Red
            foreach ($b in $batch) {
                $script:rows.Add([pscustomobject]@{ case = $b.id; group = $b.category; expected = $b.expect.disposition
                    expected_rule = $b.expect.rule; result = $code; rule = ""; match = $false; explanation = $msg })
            }
            if ($code -eq "AI_LIMIT_REACHED") {
                foreach ($res in @($r.Body.completed)) { Show-Result ($batch | Where-Object { $_.alert.Unique_ID -eq $res.Unique_ID }) $res $each }
                Write-Host "  Groq's limit is used up. Run again later, e.g.: -Category $($group.category)" -ForegroundColor Yellow
                break
            }
            if ($r.Status -eq 524) { Write-Host "  Cloudflare cut the request at 100 s (the AI was waiting for Groq's rate limit)." -ForegroundColor Yellow }
            if ($r.Status -in 0, 524) { $script:noAnswer = $true }
            if ($group.category -in $AiGroups) { $script:aiFail += $batch.Count } else { $script:fail += $batch.Count }
            continue
        }
        foreach ($res in @($r.Body)) { Show-Result ($batch | Where-Object { $_.alert.Unique_ID -eq $res.Unique_ID }) $res $each }
    }
}

Write-Host ""
Write-Host "Security checks" -ForegroundColor Cyan
$sample = ($cases | Select-Object -First 1).alert
if (-not $sample) { $sample = @($data.cases)[0].alert }
$checks = @(
    @{ Name = "wrong API key"; Headers = @{ "X-API-Key" = "cls_wrong_key_for_the_demo" }; Alert = $sample; Want = "INVALID_API_KEY" },
    @{ Name = "another client's ID"; Headers = $headers; Alert = ($sample | Select-Object * | ForEach-Object { $_.ClientID = "002"; $_ }); Want = "CLIENT_MISMATCH" },
    @{ Name = "no ClientID"; Headers = $headers; Alert = ($sample | Select-Object * -ExcludeProperty ClientID); Want = "MISSING_CLIENT_ID" }
)
foreach ($c in $checks) {
    $r = Send-Json $MatchPath @($c.Alert) $c.Headers
    $got = if ($r.Ok) { "accepted (!)" } elseif ($r.Body.error_code) { $r.Body.error_code } else { "HTTP $($r.Status)" }
    $ok = $got -eq $c.Want
    if ($ok) { $script:pass++ } else { $script:fail++ }
    Write-Host ("  {0} {1,-22} expected {2,-18} got {3} (HTTP {4})" -f $(if ($ok) { "[ OK ]" } else { "[DIFF]" }), $c.Name, $c.Want, $got, $r.Status) `
        -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
}

$outDir = Join-Path $PSScriptRoot "results"
New-Item -ItemType Directory -Force $outDir | Out-Null
$csv = Join-Path $outDir ("demo_results_{0:yyyyMMdd_HHmmss}.csv" -f (Get-Date))
$script:rows | Export-Csv $csv -NoTypeInformation -Encoding UTF8
Write-Host ""
Write-Host ("Done in {0:N0} s." -f ((Get-Date) - $started).TotalSeconds)
Write-Host ("  Rule-based decisions and security checks: {0} as expected, {1} different." -f $script:pass, $script:fail) `
    -ForegroundColor $(if ($script:fail -eq 0) { "Green" } else { "Red" })
if ($script:aiPass + $script:aiFail -gt 0) {
    Write-Host ("  AI-reviewed alerts: {0} as expected (or AI's own call), {1} different." -f $script:aiPass, $script:aiFail) `
        -ForegroundColor $(if ($script:aiFail -eq 0) { "Green" } else { "Yellow" })
    Write-Host "  AI decisions can vary between runs; REVIEW (a human looks) instead of CLEAR is the safe direction." -ForegroundColor DarkGray
}
if ($script:noAnswer) {
    Write-Host "  Some AI cases got no answer in time: Groq's rate limit made them wait. See README, 'AI cases and Groq'." -ForegroundColor Yellow
}
Write-Host "Details (with ClearScreen's explanation of every decision): $csv"
