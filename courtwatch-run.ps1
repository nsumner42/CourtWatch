# Wrapper for the CourtWatchDaily scheduled task.
#
# courtwatch.py does its own healthchecks.io pinging (it is the only thing that
# knows how many rows were scraped, which is what actually decides success), so
# this wrapper deliberately does NOT ping - that would double-count and could
# report success over the script's own failure verdict. It just runs the script,
# captures the real exit code, and records the outcome locally.
#
# Exit codes from courtwatch.py:
#   0 - ran fine
#   1 - unhandled error (traceback in courtwatch_log.txt)
#   2 - scraped 0 rows; the site flow or page structure changed

$ErrorActionPreference = 'Continue'
$PSNativeCommandUseErrorActionPreference = $false

$root       = 'C:\Scripts\CourtWatch'
$script     = Join-Path $root 'courtwatch.py'
$statusFile = Join-Path $root 'last-run-status.txt'

# Prefer the absolute interpreter path: a scheduled task can run with a minimal
# environment where PATH lookups fail. Fall back to the py launcher.
$python = 'C:\Program Files\Python314\python.exe'
if (-not (Test-Path $python)) { $python = 'py' }

$began = Get-Date
& $python $script --watch
$code = $LASTEXITCODE
$elapsed = [int]((Get-Date) - $began).TotalSeconds

$meaning = switch ($code) {
    0       { 'OK' }
    1       { 'FAILED - unhandled error, see courtwatch_log.txt' }
    2       { 'FAILED - 0 rows scraped, site structure likely changed' }
    default { "FAILED - unexpected exit code $code" }
}

Set-Content -Path $statusFile -Encoding UTF8 -Value @(
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  CourtWatchDaily"
    "  exit code : $code  ($meaning)"
    "  duration  : ${elapsed}s"
    "  (healthchecks ping is sent by courtwatch.py itself)"
)

exit $code
