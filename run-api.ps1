$ErrorActionPreference = "Stop"
Set-Location -LiteralPath "C:\ChatGPTAPI"
$env:PLAYWRIGHT_BROWSERS_PATH = "C:\ChatGPTAPI\.browsers"
$logDirectory = "C:\ChatGPTAPI\logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
& ".\.venv\Scripts\python.exe" -m src.api.server *>> (Join-Path $logDirectory "service_$stamp.log")