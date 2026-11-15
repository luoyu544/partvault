$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    throw "找不到 .NET Framework 4.0 编译器：$csc"
}

Push-Location $here
try {
    & $csc /nologo /target:exe /optimize+ /codepage:65001 `
        /out:SupvanPrintHelper.exe `
        /reference:Supvan.T50PRO.SDK.dll `
        /reference:SevenZip.dll `
        /reference:zxing.dll `
        /reference:System.Runtime.Serialization.dll `
        /reference:System.Drawing.dll `
        /reference:System.Windows.Forms.dll `
        SupvanPrintHelper.cs

    if ($LASTEXITCODE -ne 0) {
        throw "编译失败，退出码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "Built: $(Join-Path $here 'SupvanPrintHelper.exe')"
