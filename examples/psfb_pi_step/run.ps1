param(
  [string]$OutputDir = "runs",
  [string]$RunId = "psfb-pi-step"
)
pe-sim psfb-step --output-dir $OutputDir --run-id $RunId
