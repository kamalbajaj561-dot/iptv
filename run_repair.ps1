# Daily run: fetch the latest iptv-org lists, re-check every link, repair the playlist, optionally publish it.
#
# Manual run:     powershell -ExecutionPolicy Bypass -File .\run_repair.ps1
# Run + publish:  powershell -ExecutionPolicy Bypass -File .\run_repair.ps1 -Push
# Schedule it:    see the setup steps (schtasks line) - run it from the country you watch in.
param(
    [string]$Folder = $PSScriptRoot,
    [switch]$Push,
    [string]$Countries = ""          # e.g. "in,us,gb" for a smaller playlist; empty = whole world
)
Set-Location $Folder
$logArgs = @('--update', '--work', '.\upstream', '--out', '.\out')
if ($Countries) { $logArgs += @('--countries', $Countries) }

"=== $(Get-Date -Format s) ===" | Out-File -Append -Encoding utf8 .\last_run.log
python .\iptv_repair.py @logArgs 2>&1 | Tee-Object -Append -FilePath .\last_run.log
if ($LASTEXITCODE -ne 0) { Write-Host 'Repair run failed, see last_run.log' -ForegroundColor Red; exit 1 }

if ($Push) {
    # state.json stays on this PC (it holds the per-link history); only the playlists are published.
    git add out/all.m3u out/by-country out/report.txt out/repairs.log
    git commit -m "playlist $(Get-Date -Format s)" 2>$null | Out-Null
    git push
    if ($LASTEXITCODE -ne 0) { Write-Host 'git push failed' -ForegroundColor Red; exit 1 }
}
Write-Host 'Done. Playlist: out\all.m3u   Report: out\report.txt'
