$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:NIGHTMARE_LAUNCHER = '1'
Set-Location -LiteralPath $PSScriptRoot
Write-Host 'Nightmare Help: запуск сервиса...'
$runtimePath = Join-Path $PSScriptRoot 'data\runtime.json'
if (Test-Path -LiteralPath $runtimePath) {
    $runningService = $null
    try {
        $runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
        $runningService = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$runtime.pid)"
    } catch {
        Write-Host 'Проверка предыдущего запуска: продолжаю запуск.'
    }
    if ($runningService -and $runningService.Name -match '^python(w)?\.exe$' -and $runningService.CommandLine -and ($runningService.CommandLine.Contains((Join-Path $PSScriptRoot 'manager.py')) -or $runningService.CommandLine.Contains((Join-Path $PSScriptRoot 'bot.py')) -or ($runningService.ExecutablePath -eq (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -and $runningService.CommandLine -match '\smanager\.py\s*$'))) {
        Write-Host 'Перезапускаю текущий экземпляр...'
        Stop-Process -Id $runningService.ProcessId -Force
        Wait-Process -Id $runningService.ProcessId -ErrorAction SilentlyContinue
    }
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& '.\.venv\Scripts\python.exe' -c "import importlib.util; raise SystemExit(any(importlib.util.find_spec(name) is None for name in ('aiogram', 'cryptography')))"
if ($LASTEXITCODE -ne 0) {
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& '.\.venv\Scripts\python.exe' (Join-Path $PSScriptRoot 'manager.py')
exit $LASTEXITCODE
