param(
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")

$py = Get-PythonExe
$unifiedArgs = @((Join-Path $PSScriptRoot "deploy_unified_mcp.py"))
if ($SkipBuild) { $unifiedArgs += "--skip-build" }
& $py @unifiedArgs
if ($LASTEXITCODE -ne 0) { throw "Unified MCP deployment failed" }
