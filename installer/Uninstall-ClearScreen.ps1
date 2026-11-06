<#
.SYNOPSIS
    Removes ClearScreen from this server.
.DESCRIPTION
    Stops and removes the three services, the program folder, the shortcuts, the firewall rule and
    the uninstall entry. By default it KEEPS the data folder (e.g. D:\ClearScreen\Data: settings,
    encrypted keys, logs, the embedding model) and the SQL Server database, so a reinstall picks up
    where it left off. -RemoveData also deletes the data folder. The database is never dropped by
    this script, and SQL Server and SSMS stay installed: remove them yourself if you really want them gone.
#>
[CmdletBinding()]
param([switch]$RemoveData, [switch]$Unattended)

$ErrorActionPreference = "Continue"
$Reg = "HKLM:\SOFTWARE\ClearScreen"                 # where setup recorded the installation (e.g. D:\ClearScreen\...)
$loc = Get-ItemProperty $Reg -ErrorAction SilentlyContinue
$Install = if ($loc.InstallDir) { $loc.InstallDir } else { Join-Path $env:ProgramFiles "ClearScreen" }
$Data = if ($loc.DataDir) { $loc.DataDir } else { Join-Path $env:ProgramData "ClearScreen" }

Write-Host ""
Write-Host "  ClearScreen uninstall" -ForegroundColor White
if (-not $Unattended) {
    $answer = Read-Host "  Remove ClearScreen from this server? Type YES to continue"
    if ($answer -ne "YES") { Write-Host "  Cancelled."; exit 0 }
    if (-not $RemoveData) {
        $answer = Read-Host "  Also delete settings, client API keys and logs ($Data)? Type DELETE to delete them, Enter to keep"
        $RemoveData = $answer -eq "DELETE"
    }
}

foreach ($name in "ClearScreenHttps", "ClearScreenApi", "ClearScreenEmbed") {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($svc) {
        if ($svc.Status -ne "Stopped") { Stop-Service -Name $name -Force -ErrorAction SilentlyContinue }
        $wrapper = Join-Path $Install "services\$name.exe"
        if (Test-Path $wrapper) { & $wrapper uninstall | Out-Null } else { & sc.exe delete $name | Out-Null }
        Write-Host "    removed service $name"
    }
}
Get-NetFirewallRule -Name "ClearScreen-HTTPS" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
foreach ($dir in @([Environment]::GetFolderPath("CommonDesktopDirectory"), (Join-Path ([Environment]::GetFolderPath("CommonStartMenu")) "Programs"))) {
    Remove-Item (Join-Path $dir "ClearScreen Admin.lnk") -Force -ErrorAction SilentlyContinue
}
Remove-Item "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\ClearScreen" -Force -ErrorAction SilentlyContinue

# The private Python was installed with the official installer: remove it the same way.
$pyExe = Get-ChildItem (Join-Path $env:ProgramData "Package Cache") -Recurse -Filter "python-3.13.9-amd64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pyExe -and (Test-Path (Join-Path $Install "python\python.exe"))) {
    Start-Process $pyExe.FullName -ArgumentList "/uninstall", "/quiet" -Wait
}
Start-Sleep 2
Remove-Item $Install -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "    removed $Install"
if ($RemoveData) {
    Remove-Item $Data -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $Reg -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "    removed $Data"
} else {
    Remove-ItemProperty $Reg InstallDir -ErrorAction SilentlyContinue    # keep DataDir so a reinstall finds the data
    Write-Host "    kept $Data (settings, encrypted keys, logs)"
}
Write-Host ""
Write-Host "  ClearScreen removed. SQL Server, SQL Server Management Studio and the ClearScreen database" -ForegroundColor Green
Write-Host "  were not touched (remove them in Settings > Apps if you really want them gone)." -ForegroundColor Green
