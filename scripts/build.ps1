$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Assets = Join-Path $ProjectRoot "assets"

Set-Location $ProjectRoot

$PyInstallerArgs = @(
    "--noconfirm"
    "--clean"
    "--onefile"
    "--windowed"
    "--name=Release-Imprensa"
    "--icon=$(Join-Path $Assets 'logo.ico')"
    "--add-data=$Assets\logo.ico;."
    "--add-data=$Assets\SIDSFlatPequeno.png;."
    "--add-data=$Assets\robot.png;."
    "--add-data=$Assets\instagram.png;."
    "--exclude-module=torch"
    "--exclude-module=torchvision"
    "--exclude-module=scipy"
    "--exclude-module=pandas"
    "--exclude-module=numpy"
    "--exclude-module=matplotlib"
    "--exclude-module=sklearn"
    "--exclude-module=jax"
    "--exclude-module=tensorflow"
    "Release.py"
)

python -m PyInstaller @PyInstallerArgs
Write-Host "Executável criado em: $ProjectRoot\dist\Release-Imprensa.exe"
