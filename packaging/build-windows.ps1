param([ValidatePattern('^LiveRecorderDesktop(-[0-9]+\.[0-9]+\.[0-9]+)?$')][string]$ReleaseName = 'LiveRecorderDesktop')
$ErrorActionPreference = 'Stop'
$liveProject = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Push-Location -LiteralPath $liveProject
try {
    $liveBuild = Join-Path $liveProject '.runtime\packaging'
    $liveTemp = Join-Path $liveBuild 'tmp'
    New-Item -ItemType Directory -Path $liveTemp -Force | Out-Null
    $env:PYINSTALLER_CONFIG_DIR = $liveBuild
    $env:TEMP = $liveTemp
    $env:TMP = $liveTemp
    $livePython = if (Test-Path -LiteralPath '.venv\Scripts\python.exe') { '.venv\Scripts\python.exe' } else { 'python' }
    $livePython = (Get-Command $livePython -ErrorAction Stop).Source
    # Avoid repackaging unrelated toolchain DLLs found on this machine's application PATH.
    $env:PATH = "$(Split-Path -Parent $livePython);$([Environment]::SystemDirectory);$env:WINDIR"
    $env:LIVE_BUILD_NAME = $ReleaseName
    $env:LIVE_BUILD_ROOT = $liveProject
    if (Test-Path -LiteralPath '.runtime\deps\streamlink') {
        $env:PYTHONPATH = (Resolve-Path -LiteralPath '.runtime\deps').Path
    }
    & $livePython -m PyInstaller --noconfirm --distpath (Join-Path $liveProject 'dist') `
        --workpath (Join-Path $liveProject 'build') 'packaging/windows.spec'
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
