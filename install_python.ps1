# 风物之诗琴自动化工具 · Python 自动下载安装
# 在未检测到兼容 Python 的电脑上，由 launcher.ps1 调用。
# 下载源自动切换：华为云镜像 -> npmmirror 镜像 -> python.org 官方。
# 以“当前用户”方式静默安装（无需管理员权限），并自动加入 PATH。
# 注意：安装的是独立的 Python 3.10，不会影响电脑上已有的其他 Python 版本。

param(
    [string]$Version = "3.10.11"
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # 关闭进度条，大幅加快 Invoke-WebRequest

# Python 安装包下载地址（按优先级，前面失败自动换下一个）
$Urls = @(
    "https://mirrors.huaweicloud.com/python/$Version/python-$Version-amd64.exe"
    "https://registry.npmmirror.com/-/binary/python/$Version/python-$Version-amd64.exe"
    "https://www.python.org/ftp/python/$Version/python-$Version-amd64.exe"
)

$Installer = Join-Path $env:TEMP "python-$Version-amd64.exe"

# 官方安装器默认的用户级目录：%LOCALAPPDATA%\Programs\Python\Python<主><次>
$FolderName = "Python" + ($Version.Split(".")[0]) + ($Version.Split(".")[1])
$ExpectedPython = Join-Path $env:LOCALAPPDATA "Programs\Python\$FolderName\python.exe"

Write-Host "[install-python] 准备下载 Python $Version 安装包（约 25 MB）..."

$downloaded = $false
foreach ($url in $Urls) {
    $host_ = ([Uri]$url).Host
    Write-Host "[install-python] 尝试下载源：$host_"
    try {
        if (Test-Path $Installer) { Remove-Item $Installer -Force }
        Invoke-WebRequest -Uri $url -OutFile $Installer -UseBasicParsing -TimeoutSec 180
        # 校验文件确实下载下来（正常安装包 > 10 MB）
        $size = (Get-Item $Installer).Length
        if ($size -gt 10MB) {
            Write-Host "[install-python] 下载成功（$([math]::Round($size/1MB,1)) MB）"
            $downloaded = $true
            break
        }
        Write-Host "[install-python] 文件大小异常（$size 字节），换下一个源…"
    }
    catch {
        Write-Host "[install-python] 下载失败：$($_.Exception.Message)，换下一个源…"
    }
}

if (-not $downloaded) {
    Write-Host "[install-python][错误] 所有下载源均失败，请手动安装 Python 3.10："
    Write-Host "    https://www.python.org/downloads/release/python-31011/"
    exit 1
}

Write-Host "[install-python] 开始静默安装（仅当前用户，自动加入 PATH）..."
$installArgs = @(
    "/quiet",
    "InstallAllUsers=0",
    "PrependPath=1",
    "Include_launcher=1",
    "Include_pip=1",
    "Include_test=0",
    "Include_doc=0"
)
$proc = Start-Process -FilePath $Installer -ArgumentList $installArgs -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    Write-Host "[install-python][错误] 安装程序退出码：$($proc.ExitCode)"
    exit $proc.ExitCode
}

if (Test-Path $ExpectedPython) {
    $ver = (& $ExpectedPython --version) 2>&1
    Write-Host "[install-python] 安装完成：$ver"
    Write-Host "[install-python] 路径：$ExpectedPython"
    exit 0
}
else {
    Write-Host "[install-python][错误] 安装结束但未在预期位置找到 python.exe：$ExpectedPython"
    exit 1
}
