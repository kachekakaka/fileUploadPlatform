[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('server', 'worker')]
    [string]$Role,

    [string]$DataDirectory,
    [string[]]$ManifestRoots,
    [string[]]$AssetRoots
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot 'runtime/python.exe'
$env:UPLOAD_PLATFORM_DASHBOARD_DIR = Join-Path $PSScriptRoot 'dashboard'
if ($DataDirectory) { $env:UPLOAD_PLATFORM_DATA_DIR = $DataDirectory }
if ($ManifestRoots) { $env:UPLOAD_PLATFORM_MANIFEST_ROOTS = $ManifestRoots -join [IO.Path]::PathSeparator }
if ($AssetRoots) { $env:UPLOAD_PLATFORM_ASSET_ROOTS = $AssetRoots -join [IO.Path]::PathSeparator }
if (-not $env:UPLOAD_PLATFORM_DATA_DIR) {
    throw 'Set UPLOAD_PLATFORM_DATA_DIR or give -DataDirectory with an absolute path.'
}

& $python -m upload_platform $Role
exit $LASTEXITCODE
