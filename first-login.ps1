$ErrorActionPreference = "Stop"
Set-Location -LiteralPath "C:\ChatGPTAPI"
$env:PLAYWRIGHT_BROWSERS_PATH = "C:\ChatGPTAPI\.browsers"
& ".\.venv\Scripts\python.exe" "scripts\first_login.py"