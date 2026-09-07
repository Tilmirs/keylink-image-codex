[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClientArgs
)

$candidates = @()
if ($env:USERPROFILE) {
    $candidates += Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}

foreach ($commandName in @('python3', 'python', 'py')) {
    $command = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($null -ne $command -and $command.Source -notmatch '[\\/]Microsoft[\\/]WindowsApps[\\/]') {
        $candidates += $command.Source
    }
}

$pythonPath = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $pythonPath) {
    Write-Error 'Python 3.11 or newer is required. Install Python or use the Codex bundled runtime.'
    exit 1
}

$clientPath = Join-Path $PSScriptRoot 'keylink_image.py'
& $pythonPath -X utf8 $clientPath @ClientArgs
exit $LASTEXITCODE
