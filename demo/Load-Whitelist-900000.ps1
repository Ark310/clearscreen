<#
.SYNOPSIS
    Loads set 2: 10,000 more Client A records for ClientID 001, Unique_IDs 900000-909999.

.DESCRIPTION
    Same as Load-Whitelist.ps1, but sends the files in whitelist_900000\. These are different Client A
    records from set 1 (none of set 1's records, none of the demo cases' rows), renumbered from 900000.
    whitelist_900000\id_map.csv lists each new Unique_ID next to its original one in Whitelist.xlsx.
    Safe to run again: the same Unique_IDs are replaced, not duplicated.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Load-Whitelist-900000.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Load-Whitelist-900000.ps1 -Api http://127.0.0.1:8000 -NoWait
#>
param(
    [string]$Api = "https://api.clearscreen.example",
    [switch]$NoWait
)
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "Load-Whitelist.ps1") -Api $Api -Folder (Join-Path $PSScriptRoot "whitelist_900000") -NoWait:$NoWait
