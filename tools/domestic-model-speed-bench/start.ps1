Set-Location -LiteralPath $PSScriptRoot
$localPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
if (Test-Path -LiteralPath $localPython) {
    & $localPython -m uvicorn main:app --host 127.0.0.1 --port 8091 --no-access-log
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 -m uvicorn main:app --host 127.0.0.1 --port 8091 --no-access-log
} else {
    throw '未找到 Python 3，请先安装 Python 3.10 或更高版本。'
}
