[CmdletBinding()]
param(
    [ValidateSet('patch', 'minor')]
    [string]$Bump = 'patch',
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Summary,
    [string]$RepositoryPath,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
$RepositoryUrl = 'https://github.com/Kyan-vd-anker/SAGE.git'
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$VersionPath = Join-Path $SourceRoot 'VERSION'
$ChangelogPath = Join-Path $SourceRoot 'CHANGELOG.md'
if (-not (Test-Path -LiteralPath $VersionPath)) { throw 'VERSION is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot 'sage'))) { throw 'Run this from the SAGE source tree.' }
$current = (Get-Content -LiteralPath $VersionPath -Raw).Trim()
if ($current -notmatch '^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)$') { throw "Invalid version in VERSION: $current" }
$major = [int]$Matches.major
$minor = [int]$Matches.minor
$patch = [int]$Matches.patch
if ($Bump -eq 'minor') { $minor++; $patch = 0 } else { $patch++ }
$newVersion = "$major.$minor.$patch"
$summaryLine = ($Summary -replace '\s+', ' ').Trim()
if ($summaryLine.Length -gt 500) { $summaryLine = $summaryLine.Substring(0, 500) }
$today = Get-Date -Format 'yyyy-MM-dd'
$tag = "v$newVersion"
$releaseHeader = "## $newVersion - $today"

function Update-Changelog([string]$Notes) {
    if ($Notes -match [regex]::Escape($releaseHeader)) { return $Notes }
    $release = "$releaseHeader`n`n- $summaryLine`n"
    $match = [regex]::Match($Notes, '(?ms)^## Unreleased\s*\r?\n(?<content>.*?)(?=^## |\z)')
    if ($match.Success) {
        $pending = $match.Groups['content'].Value.Trim()
        if ($pending -eq '- Pending changes.') { $pending = '' }
        if ($pending) { $release += "`n$pending`n" }
        $replacement = "## Unreleased`n`n$release`n"
        return $Notes.Substring(0, $match.Index) + $replacement + $Notes.Substring($match.Index + $match.Length)
    }
    return [regex]::Replace($Notes, '\A(# SAGE changelog[^\r\n]*\r?\n)', "`$1`n$release`n", 1)
}

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) { throw 'Git for Windows is required. Install it and sign in to GitHub, then retry.' }
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("sage-release-" + [guid]::NewGuid().ToString('N'))
$stage = Join-Path $tempRoot 'repository'
New-Item -ItemType Directory -Path $tempRoot | Out-Null

$excludedDirectories = @('.git', '.vscode', '.venv', 'venv', '__pycache__', 'work', 'data')
$excludedFiles = @('start-sage.local.bat')
function Copy-SourceTree([string]$From, [string]$To, [string]$Relative = '') {
    foreach ($item in Get-ChildItem -LiteralPath $From -Force) {
        if ($item.PSIsContainer -and $excludedDirectories -contains $item.Name) { continue }
        if (-not $item.PSIsContainer -and $excludedFiles -contains $item.Name) { continue }
        if (-not $item.PSIsContainer -and $item.Name -match '^(\.env($|\.)|.*\.(db|sqlite3?|pyc|pyo|log|pem|p12|pfx|key|token)$)') { continue }
        $relativePath = if ($Relative) { Join-Path $Relative $item.Name } else { $item.Name }
        $destination = Join-Path $To $relativePath
        if ($item.PSIsContainer) {
            if (-not (Test-Path -LiteralPath $destination)) { New-Item -ItemType Directory -Path $destination -Force | Out-Null }
            Copy-SourceTree $item.FullName $To $relativePath
        } else {
            $parent = Split-Path -Parent $destination
            if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            Copy-Item -LiteralPath $item.FullName -Destination $destination -Force
        }
    }
}

try {
    if ($RepositoryPath) {
        $stage = (Resolve-Path -LiteralPath $RepositoryPath).Path
        if (-not (Test-Path -LiteralPath (Join-Path $stage '.git'))) { throw 'RepositoryPath must be a Git clone created through VS Code.' }
        $origin = (& git -C $stage remote get-url origin).Trim()
        if ($LASTEXITCODE -ne 0 -or $origin -notin @($RepositoryUrl, 'git@github.com:Kyan-vd-anker/SAGE.git', 'ssh://git@github.com/Kyan-vd-anker/SAGE.git')) { throw 'The selected checkout is not the requested SAGE GitHub repository.' }
    } else {
        & git clone --quiet $RepositoryUrl $stage
        if ($LASTEXITCODE -ne 0) { throw 'Could not clone GitHub. Check sign-in and repository access. No local data was uploaded.' }
    }
    $branch = (& git -C $stage branch --show-current).Trim()
    if (-not $branch) { throw 'The cloned repository has no checked-out release branch.' }
    $tracked = @(& git -C $stage ls-files)
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the GitHub repository contents.' }
    $unsafeTracked = @($tracked | Where-Object { $_ -match '(^|/)data/' -or $_ -match '(^|/)\.env($|\.)' -or $_ -match '\.(db|sqlite3?|pem|p12|pfx|key|token)$' })
    if ($unsafeTracked.Count -gt 0) { throw "The GitHub repository tracks private data or credentials (for example: $($unsafeTracked[0])). Remove those files before publishing." }

    Copy-SourceTree $SourceRoot $stage
    Set-Content -LiteralPath (Join-Path $stage 'VERSION') -Value $newVersion -Encoding ascii
    $notesPath = Join-Path $stage 'CHANGELOG.md'
    $notes = if (Test-Path -LiteralPath $notesPath) { Get-Content -LiteralPath $notesPath -Raw } else { "# SAGE changelog`n`n" }
    Set-Content -LiteralPath $notesPath -Value (Update-Changelog $notes) -Encoding utf8

    if ($PrepareOnly) {
        Write-Host "Release $tag prepared in $stage. Review the Source Control changes, commit them, and push from VS Code."
        return
    }
    & git -C $stage add -A
    if ($LASTEXITCODE -ne 0) { throw 'Could not stage the release files.' }
    $staged = @(& git -C $stage diff --cached --name-only)
    $unsafeStaged = @($staged | Where-Object { $_ -match '(^|/)data/' -or $_ -match '(^|/)\.env($|\.)' -or $_ -match '\.(db|sqlite3?|pem|p12|pfx|key|token)$' -or $_ -eq 'start-sage.local.bat' })
    if ($unsafeStaged.Count -gt 0) { throw "Refusing to upload private file: $($unsafeStaged[0])" }
    if ($staged.Count -eq 0) { throw 'There are no changes to publish.' }
    & git -C $stage show-ref --verify --quiet "refs/tags/$tag"
    if ($LASTEXITCODE -eq 0) { throw "Tag $tag already exists. Update VERSION before publishing another release." }
    & git -C $stage commit -m "SAGE $tag - $summaryLine"
    if ($LASTEXITCODE -ne 0) { throw 'Git commit failed. Configure your Git author name and email, then retry.' }
    & git -C $stage tag -a $tag -m "SAGE $tag"
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the version tag.' }
    & git -C $stage push --atomic origin "HEAD:refs/heads/$branch" "refs/tags/$tag"
    if ($LASTEXITCODE -ne 0) { throw 'GitHub rejected the push. Check sign-in and branch permissions; the branch and tag are pushed atomically.' }

    # Record the release locally only after GitHub accepted the atomic push.
    Set-Content -LiteralPath $VersionPath -Value $newVersion -Encoding ascii
    $localNotes = if (Test-Path -LiteralPath $ChangelogPath) { Get-Content -LiteralPath $ChangelogPath -Raw } else { "# SAGE changelog`n`n" }
    Set-Content -LiteralPath $ChangelogPath -Value (Update-Changelog $localNotes) -Encoding utf8
    Write-Host "Published SAGE $tag to $RepositoryUrl on branch $branch."
    Write-Host 'Release notes: CHANGELOG.md'
} finally {
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
}
