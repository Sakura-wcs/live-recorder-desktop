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
    $liveDependencies = @()
    if (Test-Path -LiteralPath '.runtime\deps\streamlink') {
        $liveDependencies = @('--paths', (Resolve-Path -LiteralPath '.runtime\deps').Path)
        $env:PYTHONPATH = $liveDependencies[1]
    }
    $livePlugins = foreach ($livePlatform in @('bilibili','douyu','huya','douyin','youtube','twitch')) {
        '--hidden-import'
        "streamlink.plugins.$livePlatform"
    }
    $liveOptions = @('-m','PyInstaller','--noconfirm','--onedir','--console','--name',$ReleaseName,
        '--specpath',$liveBuild,'--distpath',(Join-Path $liveProject 'dist'),'--workpath',(Join-Path $liveProject 'build'),
        '--paths',$liveProject,'--collect-data','streamlink','--noupx')
    & $livePython @liveOptions @liveDependencies @livePlugins 'packaging/windows_entry.py'
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
