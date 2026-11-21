[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$OutputDirectory,

    [string]$BuildPython = 'python',

    [string]$EngineDirectory,
    [string]$EngineNoticesDirectory,
    [string]$EngineBuildId,
    [string]$EngineSourceRevision,
    [string[]]$EngineDependencies = @(),
    [string]$MuMuSourceRevision,
    [ValidatePattern('^[a-z0-9][a-z0-9.-]*$')]
    [string]$PortableRevision = 'mumu-portable.1'
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$repository = Split-Path $PSScriptRoot -Parent
$pythonRelease = '3.13.17'
$pythonAbi = '313'
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
$portable = [bool]($EngineDirectory -or $EngineNoticesDirectory -or $EngineBuildId -or
    $EngineSourceRevision -or $MuMuSourceRevision -or $EngineDependencies.Count)
if ($portable) {
    if (!$EngineDirectory -or !$EngineNoticesDirectory -or
        $EngineBuildId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$' -or $EngineBuildId -like '*-fixture' -or
        $EngineSourceRevision -cnotmatch '^[0-9a-f]{40}$' -or $MuMuSourceRevision -cnotmatch '^[0-9a-f]{40}$') {
        throw 'Portable packaging needs a production engine directory, license directory, build ID, and full engine and MuMu Git revisions.'
    }
    $engineSource = (Resolve-Path -LiteralPath $EngineDirectory).Path
    $noticesSource = (Resolve-Path -LiteralPath $EngineNoticesDirectory).Path
    $engineNames = @('bilibili-upload-engine.exe', 'capabilities.json') + $EngineDependencies
    foreach ($dependency in $EngineDependencies) {
        if ($dependency -notmatch '^[A-Za-z0-9_.-]+\.dll$') { throw "Invalid engine dependency: $dependency" }
    }
    foreach ($name in $engineNames) {
        $item = Get-Item -LiteralPath (Join-Path $engineSource $name)
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Engine input is not a regular file: $name"
        }
    }
    foreach ($name in @('ENGINE.md', 'LICENSE.engine', 'LICENSE.biliup', 'rust-dependencies.json', 'rust')) {
        if (!(Test-Path -LiteralPath (Join-Path $noticesSource $name))) { throw "Missing engine notice: $name" }
    }
}

Push-Location $repository
try {
    $version = & $BuildPython -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
    $name = "fileUploadPlatform-$version-windows-x64"
    if ($portable) { $name += "-$PortableRevision" }
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
    if ($portable) {
        $engine = Join-Path $stage "engines/$EngineBuildId"
        [void](New-Item -ItemType Directory -Path $engine)
        foreach ($entry in $engineNames) {
            Copy-Item -LiteralPath (Join-Path $engineSource $entry) -Destination $engine
        }
        & (Join-Path $runtime 'python.exe') -B -m upload_platform.portable --inspect-engine $engine --build-id $EngineBuildId
        $engineNotices = Join-Path $stage 'licenses/engine'
        [void](New-Item -ItemType Directory -Path $engineNotices)
        foreach ($entry in (Get-ChildItem -LiteralPath $noticesSource -Recurse -File)) {
            $relative = [IO.Path]::GetRelativePath($noticesSource, $entry.FullName)
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
                $entry.Name -notmatch '^(ENGINE\.md|LICENSE[.\w-]*|LICENCE[.\w-]*|COPYING[.\w-]*|COPYRIGHT[.\w-]*|NOTICE[.\w-]*|rust-dependencies\.json|Cargo\.lock)$') {
                throw "Unexpected engine notice input: $relative"
            }
            $destination = Join-Path $engineNotices $relative
            [void](New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force)
            Copy-Item -LiteralPath $entry.FullName -Destination $destination
        }
        if (!(Get-ChildItem -LiteralPath (Join-Path $engineNotices 'rust') -Recurse -File)) {
            throw 'Rust dependency notices are required.'
        }
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows_portable.cmd') -Destination (Join-Path $stage '启动上传平台.cmd')
        @{ release = $name; platform_version = $version
           platform_revision = (git rev-parse HEAD)
           platform_working_tree = [bool](git status --porcelain)
           engine_build_id = $EngineBuildId; engine_revision = $EngineSourceRevision
           engine_dependencies = $EngineDependencies; mumu_revision = $MuMuSourceRevision
           python_version = $pythonRelease; architecture = 'windows-x64' } |
            ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $stage 'release.json') -Encoding utf8NoBOM
    }
    Compress-Archive -Path $stage -DestinationPath $zip -CompressionLevel Optimal
    Write-Output $zip
}
finally {
    Pop-Location
}
