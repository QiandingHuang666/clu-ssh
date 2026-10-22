# clu 二进制安装脚本（Windows PowerShell）
# 用法：irm https://raw.githubusercontent.com/QiandingHuang666/clu-ssh/main/packaging/install.ps1 | iex
$ErrorActionPreference = "Stop"
$Repo = "QiandingHuang666/clu-ssh"
$Version = if ($env:CLU_VERSION) { $env:CLU_VERSION } else {
  (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest").tag_name
}
$Ver = $Version.TrimStart("v")
$Prefix = if ($env:CLU_PREFIX) { $env:CLU_PREFIX } else { Join-Path $HOME ".local\bin" }
$Asset = "clu-$Ver-windows-x86_64.zip"
$Url = "https://github.com/$Repo/releases/download/$Version/$Asset"
$Tmp = Join-Path $env:TEMP ("clu-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Tmp | Out-Null

Write-Host "下载 $Url"
Invoke-WebRequest -Uri $Url -OutFile (Join-Path $Tmp $Asset)
$ShaUrl = "$Url.sha256"
$haveSha = $true
try {
  Invoke-WebRequest -Uri $ShaUrl -OutFile (Join-Path $Tmp "$Asset.sha256")
} catch { $haveSha = $false }
if ($haveSha) {
  $expected = ((Get-Content (Join-Path $Tmp "$Asset.sha256")) -split "\s+")[0].ToLower()
  $actual = (Get-FileHash (Join-Path $Tmp $Asset) -Algorithm SHA256).Hash.ToLower()
  if ($expected -ne $actual) { throw "sha256 校验失败" }
  Write-Host "sha256 校验通过"
}

Expand-Archive -Path (Join-Path $Tmp $Asset) -DestinationPath $Tmp -Force
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
Copy-Item (Join-Path $Tmp "clu.exe") (Join-Path $Prefix "clu.exe") -Force
Copy-Item (Join-Path $Tmp "clu.exe") (Join-Path $Prefix "clu-mcp.exe") -Force
Write-Host "已安装：$Prefix\clu.exe（clu-mcp.exe 为副本）"
Write-Host "请确保 $Prefix 已加入 PATH。"
