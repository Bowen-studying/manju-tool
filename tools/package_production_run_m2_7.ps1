param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) { throw "Destination already exists: $destinationPath" }

$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) ("manju-m27-" + [guid]::NewGuid().ToString("N") + ".zip")
try {
    git -C $root archive --format=zip --output=$archivePath HEAD
    New-Item -ItemType Directory -Path $destinationPath | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $destinationPath
    foreach ($relativePath in @(
        "README.md", "pyproject.toml", "requirements.txt", "uv.lock",
        "docs\PRODUCTION_RUN_M2_3_TESTING.md", "docs\PRODUCTION_RUN_M2_3_1_TESTING.md", "docs\PRODUCTION_RUN_M2_4_TESTING.md", "docs\PRODUCTION_RUN_M2_4_1_TESTING.md", "docs\PRODUCTION_RUN_M2_5_TESTING.md", "docs\PRODUCTION_RUN_M2_6_TESTING.md", "docs\PRODUCTION_RUN_M2_7_TESTING.md",
        "tools\package_production_run_m2_3.ps1", "tools\package_production_run_m2_3_1.ps1", "tools\package_production_run_m2_4.ps1", "tools\package_production_run_m2_6.ps1", "tools\package_production_run_m2_7.ps1"
    )) {
        $sourcePath = Join-Path $root $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath)) { throw "Missing package source: $relativePath" }
        $targetPath = Join-Path $destinationPath $relativePath
        New-Item -ItemType Directory -Force -Path (Split-Path $targetPath) | Out-Null
        Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
    }
    foreach ($tree in @("manju", "tests")) {
        Get-ChildItem -LiteralPath (Join-Path $root $tree) -Recurse -File -Filter "*.py" | ForEach-Object {
            $relativePath = $_.FullName.Substring($root.Length + 1)
            $targetPath = Join-Path $destinationPath $relativePath
            New-Item -ItemType Directory -Force -Path (Split-Path $targetPath) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Force
        }
    }
    Get-ChildItem -LiteralPath $destinationPath -Recurse -Directory -Force | Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".venv") } | Remove-Item -Recurse -Force
    $manifestPath = Join-Path $destinationPath "SHA256SUMS.txt"
    $prefixLength = $destinationPath.Length + 1
    $lines = Get-ChildItem -LiteralPath $destinationPath -Recurse -File | Where-Object { $_.FullName -ne $manifestPath } | Sort-Object FullName | ForEach-Object {
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $($_.FullName.Substring($prefixLength).Replace('\', '/'))"
    }
    [System.IO.File]::WriteAllLines($manifestPath, [string[]]$lines, [System.Text.UTF8Encoding]::new($false))
}
finally {
    if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath }
}
