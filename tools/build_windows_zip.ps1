[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$OutputDirectory,

    [string]$BuildPython = 'python'
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$repository = Split-Path $PSScriptRoot -Parent
$pythonRelease = '3.13.17'
$pythonAbi = '313'
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)

Push-Location $repository
try {
    $version = & $BuildPython -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
    $name = "fileUploadPlatform-$version-windows-x64"
    [void](New-Item -ItemType Directory -Path $output -Force)
    $zip = Join-Path $output "$name.zip"
    if (Test-Path -LiteralPath $zip) { throw "ZIP already exists: $zip" }
    $build = Join-Path $output ("build-" + [guid]::NewGuid().ToString('N'))
    $stage = Join-Path $build $name
    $runtime = Join-Path $stage 'runtime'
    $packages = Join-Path $runtime 'Lib/site-packages'
    [void](New-Item -ItemType Directory -Path $packages)

    $embed = Join-Path $build 'python-embed.zip'
    Invoke-WebRequest "https://www.python.org/ftp/python/$pythonRelease/python-$pythonRelease-embed-amd64.zip" -OutFile $embed
    Expand-Archive -LiteralPath $embed -DestinationPath $runtime
    Set-Content -LiteralPath (Join-Path $runtime "python$pythonAbi._pth") -Encoding ascii -Value @(
        "python$pythonAbi.zip"
        '.'
        'Lib/site-packages'
        'import site'
    )

    $wheels = Join-Path $build 'wheels'
    & $BuildPython -m pip wheel --no-deps --wheel-dir $wheels .
    $projectWheel = Get-ChildItem -LiteralPath $wheels -Filter 'upload_platform-*.whl'
    if (@($projectWheel).Count -ne 1) { throw 'Expected one upload-platform wheel.' }
    $install = @(
        '-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile',
        '--constraint', 'requirements-dev.lock', '--target', $packages, '--only-binary=:all:',
        '--platform', 'win_amd64', '--implementation', 'cp', '--python-version', $pythonAbi,
        $projectWheel.FullName
    )
    & $BuildPython @install

    npm --prefix web ci --no-audit --no-fund
    npm --prefix web run build
    Copy-Item -LiteralPath (Join-Path $repository 'web/dist') -Destination (Join-Path $stage 'dashboard') -Recurse
    $schemas = Join-Path $stage 'schemas'
    [void](New-Item -ItemType Directory -Path $schemas)
    Get-ChildItem -LiteralPath (Join-Path $repository 'schemas') -Filter '*.json' -File |
        Copy-Item -Destination $schemas
    $nodeModules = Join-Path $repository 'web/node_modules'
    $licenses = Join-Path $stage 'licenses/frontend'
    foreach ($file in (Get-ChildItem -LiteralPath $nodeModules -Recurse -File -Filter 'LICENSE*')) {
        $target = Join-Path $licenses ([IO.Path]::GetRelativePath($nodeModules, $file.FullName))
        [void](New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force)
        Copy-Item -LiteralPath $file.FullName -Destination $target
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows_start.ps1') -Destination (Join-Path $stage 'Start.ps1')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows_package_readme.md') -Destination (Join-Path $stage 'README.md')
    & (Join-Path $runtime 'python.exe') -B -c "import sqlite3, win32file; from upload_platform import __version__; assert __version__ == '$version'; print(__version__)"
    Compress-Archive -Path $stage -DestinationPath $zip -CompressionLevel Optimal
    Write-Output $zip
}
finally {
    Pop-Location
}
