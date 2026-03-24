# Commit and push only recipes.json + pump_labels.json (after saving in the GUI).
# Run from repo root:  .\push_recipes.ps1
# Optional message:    .\push_recipes.ps1 -Message "Added dilution recipe"

param(
    [string]$Message = "Update recipes and pump labels"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$files = @("recipes.json", "pump_labels.json")
foreach ($f in $files) {
    if (-not (Test-Path (Join-Path $Root $f))) {
        Write-Host "Missing $f — save from the GUI first or copy from recipes.example.json." -ForegroundColor Yellow
        exit 1
    }
}

git add recipes.json pump_labels.json
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "No changes to recipes.json or pump_labels.json." -ForegroundColor Cyan
    exit 0
}

git commit -m $Message
git push
Write-Host "Pushed recipe data to origin." -ForegroundColor Green
