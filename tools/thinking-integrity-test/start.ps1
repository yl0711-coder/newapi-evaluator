param([switch]$SelfTest)

$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$pythonCandidates = @(
    @{ Executable = Join-Path $projectDirectory '.venv\Scripts\python.exe'; Arguments = @() },
    @{ Executable = 'python'; Arguments = @() },
    @{ Executable = 'py'; Arguments = @('-3') }
)

foreach ($candidate in $pythonCandidates) {
    if (-not (Get-Command $candidate.Executable -ErrorAction SilentlyContinue)) {
        continue
    }
    try {
        $candidateArguments = $candidate.Arguments
        & $candidate.Executable @candidateArguments -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $selectedPython = $candidate
            break
        }
    }
    catch {
        continue
    }
}

if (-not $selectedPython) {
    Write-Error 'A runnable Python 3.10+ was not found. Install Python or create .venv in this project.'
    exit 1
}

$pythonArguments = $selectedPython.Arguments
$dependenciesReady = $false
try {
    & $selectedPython.Executable @pythonArguments -c 'import fastapi, httpx, uvicorn; from pydantic import field_validator, model_validator' 2>$null
    $dependenciesReady = $LASTEXITCODE -eq 0
}
catch {
    $dependenciesReady = $false
}
if (-not $dependenciesReady) {
    Write-Error ('Dependencies are missing. Run: & "{0}" {1} -m pip install -r "{2}"' -f $selectedPython.Executable, ($pythonArguments -join ' '), (Join-Path $projectDirectory 'requirements.txt'))
    exit 1
}

$scriptName = if ($SelfTest) { 'selftest.py' } else { 'main.py' }
Push-Location -LiteralPath $projectDirectory
try {
    & $selectedPython.Executable @pythonArguments (Join-Path $projectDirectory $scriptName)
    $scriptExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $scriptExitCode
