[CmdletBinding()]
param(
    [string]$InstallPath,
    [switch]$SkipDependencies
)

$ErrorActionPreference = 'Stop'
$RepositoryUrl = 'https://github.com/Kyan-vd-anker/SAGE.git'

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = @($machinePath, $userPath) -join ';'
}

function Find-Executable([string]$Name, [string[]]$FallbackPaths = @()) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in $FallbackPaths) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    return $null
}

function Install-WingetPackage([string]$Id) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "winget is required to install missing prerequisites. Install Microsoft's App Installer, then run this installer again."
    }
    & $winget.Source install --id $Id --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget could not install $Id (exit code $LASTEXITCODE)." }
    Refresh-Path
}

function Get-Git {
    $git = Find-Executable 'git' @(
        (Join-Path $env:ProgramFiles 'Git\cmd\git.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd\git.exe')
    )
    if (-not $git) {
        Install-WingetPackage 'Git.Git'
        $git = Find-Executable 'git' @(
            (Join-Path $env:ProgramFiles 'Git\cmd\git.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd\git.exe')
        )
    }
    if (-not $git) { throw 'Git was installed but is not available yet. Restart PowerShell and run the installer again.' }
    return $git
}

function Test-Python([string]$Executable) {
    & $Executable --version *> $null
    return $LASTEXITCODE -eq 0
}

function Find-Python {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python310\python.exe')
    )
    foreach ($candidate in $candidates) {
        if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and (Test-Python $candidate)) { return $candidate }
    }
    foreach ($name in @('python', 'py')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and (Test-Python $command.Source)) { return $command.Source }
    }
    return $null
}

function Copy-ReleaseFiles([string]$SourceRoot, [string]$DestinationRoot, [string]$RelativePath = '') {
    foreach ($item in Get-ChildItem -LiteralPath (Join-Path $SourceRoot $RelativePath) -Force) {
        $relativeItem = if ($RelativePath) { Join-Path $RelativePath $item.Name } else { $item.Name }
        $parts = $relativeItem -split '[\\/]'
        if ($parts | Where-Object { $_ -in @('.git', '.vscode', '.venv', 'venv', 'work', '__pycache__', 'data') }) { continue }
        if (-not $item.PSIsContainer -and (
            $item.Name -eq 'start-sage.local.bat' -or
            $item.Name -match '^\.(env)(\.|$)' -or
            $item.Name -match '\.(db|sqlite3?|log|pyc|pyo|pem|p12|pfx|key|token|crt)$'
        )) { continue }

        $destination = Join-Path $DestinationRoot $relativeItem
        if ($item.PSIsContainer) {
            if (-not (Test-Path -LiteralPath $destination)) {
                New-Item -ItemType Directory -Path $destination -Force | Out-Null
            }
            Copy-ReleaseFiles $SourceRoot $DestinationRoot $relativeItem
            continue
        }

        # Existing plugins may contain user edits or additions; only add files that are not already there.
        if ($parts[0] -eq 'plugins' -and (Test-Path -LiteralPath $destination)) { continue }
        $parent = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Copy-Item -LiteralPath $item.FullName -Destination $destination -Force
    }
}

function Get-DefaultModel([string]$ConfigText, [string]$Name) {
    $pattern = '(?m)^\s*' + [regex]::Escape($Name) + '\s*=\s*([^\r\n]+)'
    $match = [regex]::Match($ConfigText, $pattern)
    if (-not $match.Success) { throw "Could not read the $Name default from sage\config.py." }
    $defaults = [regex]::Matches($match.Groups[1].Value, '"([^"]+)"')
    if ($defaults.Count -eq 0) { throw "Could not read the $Name default from sage\config.py." }
    return $defaults[$defaults.Count - 1].Groups[1].Value
}

if ([string]::IsNullOrWhiteSpace($InstallPath)) {
    $defaultPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    $enteredPath = Read-Host "Existing SAGE folder to update, or press Enter to use $defaultPath"
    $InstallPath = if ([string]::IsNullOrWhiteSpace($enteredPath)) { $defaultPath } else { $enteredPath.Trim('"') }
}
$InstallPath = [System.IO.Path]::GetFullPath($InstallPath)
if (-not (Test-Path -LiteralPath $InstallPath)) {
    New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
}
$InstallPath = (Resolve-Path -LiteralPath $InstallPath).Path
if (-not (Test-Path -LiteralPath (Join-Path $InstallPath 'sage\config.py'))) {
    if ((Get-ChildItem -LiteralPath $InstallPath -Force | Measure-Object).Count -gt 0) {
        throw "The selected folder is not an existing SAGE installation and is not empty: $InstallPath"
    }
}

$oldVersionPath = Join-Path $InstallPath 'VERSION'
$oldVersion = if (Test-Path -LiteralPath $oldVersionPath) { (Get-Content -LiteralPath $oldVersionPath -Raw).Trim() } else { 'not installed' }

$git = Get-Git
$gitDirectory = Join-Path $InstallPath '.git'
if (Test-Path -LiteralPath $gitDirectory) {
    $isGitInstall = $true
    if (-not (Test-Path -LiteralPath (Join-Path $InstallPath 'sage\config.py'))) {
        throw "The selected Git folder is not a SAGE installation: $InstallPath"
    }
    $origin = (& $git -C $InstallPath remote get-url origin 2>$null)
    if ($LASTEXITCODE -ne 0 -or $origin.Trim() -notin @(
        $RepositoryUrl,
        'git@github.com:Kyan-vd-anker/SAGE.git',
        'ssh://git@github.com/Kyan-vd-anker/SAGE.git'
    )) {
        throw "The selected installation is a Git checkout, but its origin is not the SAGE repository: $InstallPath"
    }
    $branch = (& $git -C $InstallPath branch --show-current)
    if ($LASTEXITCODE -ne 0 -or $branch.Trim() -ne 'main') {
        throw 'The existing Git installation must be on the main branch to update safely.'
    }
    $changes = @(& $git -C $InstallPath status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0) { throw 'Could not check the existing Git installation.' }
    if ($changes.Count -gt 0) {
        throw 'The existing Git installation has local changes. Commit or back up those source changes, then retry; no files were updated.'
    }
    $trackedData = @(& $git -C $InstallPath ls-files -- data)
    if ($LASTEXITCODE -ne 0) { throw 'Could not check tracked user data in the existing Git installation.' }
    if ($trackedData.Count -gt 0) {
        throw 'The Git installation tracks files under data\. Move those files to a safe location before updating; no files were changed.'
    }
} else {
    $isGitInstall = $false
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('sage-install-' + [guid]::NewGuid().ToString('N'))
$stage = Join-Path $tempRoot 'source'
New-Item -ItemType Directory -Path $tempRoot | Out-Null
try {
    Write-Host 'Downloading the latest SAGE source...'
    if ($isGitInstall) {
        & $git -C $InstallPath fetch --quiet origin main
        if ($LASTEXITCODE -ne 0) { throw 'Could not fetch SAGE updates. Existing files were not changed.' }
        $newVersion = (& $git -C $InstallPath show 'FETCH_HEAD:VERSION')
        $newVersion = $newVersion.Trim()
        $configText = (& $git -C $InstallPath show 'FETCH_HEAD:sage/config.py' 2>$null) -join "`n"
        if ($LASTEXITCODE -ne 0 -or -not $newVersion -or -not $configText) {
            throw 'The downloaded source is missing required SAGE files; the existing installation was not changed.'
        }
        & $git -C $InstallPath merge --ff-only FETCH_HEAD
        if ($LASTEXITCODE -ne 0) { throw 'Could not fast-forward the existing installation. No user data was changed.' }
    } else {
        & $git clone --quiet --depth 1 --branch main $RepositoryUrl $stage
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not download SAGE. Check your internet connection and GitHub access. Existing files were not changed.'
        }
        $newVersionPath = Join-Path $stage 'VERSION'
        $configPath = Join-Path $stage 'sage\config.py'
        if (-not (Test-Path -LiteralPath $newVersionPath) -or -not (Test-Path -LiteralPath $configPath)) {
            throw 'The downloaded source is missing required SAGE files; the existing installation was not changed.'
        }
        $newVersion = (Get-Content -LiteralPath $newVersionPath -Raw).Trim()
        $configText = Get-Content -LiteralPath $configPath -Raw
        Copy-ReleaseFiles $stage $InstallPath
    }

    Write-Host "Updating SAGE $oldVersion to $newVersion in $InstallPath"

    if (-not $SkipDependencies) {
        $python = Find-Python
        if (-not $python) {
            Install-WingetPackage 'Python.Python.3.13'
            $python = Find-Python
        }
        if (-not $python) { throw 'Python was installed but is not available yet. Restart PowerShell and run the installer again.' }

        $ollama = Find-Executable 'ollama' @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
            (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
        )
        if (-not $ollama) {
            Install-WingetPackage 'Ollama.Ollama'
            $ollama = Find-Executable 'ollama' @(
                (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
                (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
            )
        }
        if (-not $ollama) { throw 'Ollama was installed but is not available yet. Restart PowerShell and run the installer again.' }

        $installedModels = @(& $ollama list)
        if ($LASTEXITCODE -ne 0) {
            throw 'Ollama is installed but its local service is not responding. Start Ollama and run the installer again.'
        }
        $modelNames = @($installedModels | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] })
        $models = @(
            (Get-DefaultModel $configText 'MODEL'),
            (Get-DefaultModel $configText 'CODING_MODEL'),
            (Get-DefaultModel $configText 'VISION_MODEL'),
            (Get-DefaultModel $configText 'EMBED_MODEL')
        ) | Select-Object -Unique
        foreach ($model in $models) {
            if ($modelNames -contains $model) { continue }
            Write-Host "Downloading Ollama model $model..."
            & $ollama pull $model
            if ($LASTEXITCODE -ne 0) { throw "Could not download Ollama model $model. Run 'ollama pull $model' and then rerun this installer." }
        }
    }

    if (-not $SkipDependencies) {
        & $python -m compileall -q (Join-Path $InstallPath 'sage')
        if ($LASTEXITCODE -ne 0) { throw 'Python syntax validation failed. The downloaded source remains installed; review the error above.' }
    }

    Write-Host ''
    Write-Host "SAGE $newVersion is installed. User data under data\ and local startup settings were preserved."
    Write-Host "Start SAGE with: $(Join-Path $InstallPath 'start-sage.bat')"
    Write-Host 'Then open http://127.0.0.1:8001'
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
