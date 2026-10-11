# ============================================================================
# 自动下载并安装 Python 3.10.11（用户级、静默、加入 PATH）
# 仅在系统里找不到兼容 Python（3.9~3.11）时由 launcher.ps1 调用。
# 多下载源回退：华为云 -> npmmirror(淘宝) -> python.org
# ============================================================================
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$PyVersion = "3.10.11"
$Arch = if ($env:PROCESSOR_ARCHITECTURE -eq "AMD64") { "amd64" } else { "" }
$FileName = if ($Arch) { "python-$PyVersion-$Arch.exe" } else { "python-$PyVersion.exe" }

$Sources = @(
    "https://mirrors.huaweicloud.com/python/$PyVersion/$FileName",
    "https://registry.npmmirror.com/-/binary/python/$PyVersion/$FileName",
    "https://www.python.org/ftp/python/$PyVersion/$FileName"
)

$TmpDir = Join-Path $env:TEMP "windsong_python_install"
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null
$Installer = Join-Path $TmpDir $FileName

Write-Host "[装Python] 开始下载 Python $PyVersion …" -ForegroundColor Cyan
$downloaded = $false
foreach ($url in $Sources) {
    Write-Host "[装Python] 尝试: $url"
    try {
        # 优先 BITS（稳、可断点），不可用再退回 WebRequest
        try {
            Start-BitsTransfer -Source $url -Destination $Installer -ErrorAction Stop
        } catch {
            Invoke-WebRequest -Uri $url -OutFile $Installer -UseBasicParsing -TimeoutSec 300
        }
        if ((Test-Path $Installer) -and ((Get-Item $Installer).Length -gt 10MB)) {
            $downloaded = $true
            Write-Host "[装Python] 下载完成。" -ForegroundColor Green
            break
        }
    } catch {
        Write-Host "[装Python] 该源失败：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if (-not $downloaded) {
    Write-Host "[装Python] 所有下载源均失败，请手动安装 Python 3.10/3.11：" -ForegroundColor Red
    Write-Host "           https://www.python.org/downloads/  (安装时勾选 Add python.exe to PATH)" -ForegroundColor Red
    exit 1
}

Write-Host "[装Python] 正在静默安装（用户级，不弹安装界面）…" -ForegroundColor Cyan
# InstallAllUsers=0 仅当前用户、无需管理员；PrependPath=1 自动加入 PATH
$args = @(
    "/quiet",
    "InstallAllUsers=0",
    "PrependPath=1",
    "AppendPath=1",
    "Include_pip=1",
    "Include_launcher=1",
    "Include_test=0",
    "Include_doc=0",
    "AssociateFiles=0",
    "/log", (Join-Path $TmpDir "python_install.log")
)
$proc = Start-Process -FilePath $Installer -ArgumentList $args -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    Write-Host "[装Python] 安装程序退出码：$($proc.ExitCode)" -ForegroundColor Red
    Write-Host "           日志：$(Join-Path $TmpDir 'python_install.log')" -ForegroundColor Red
    exit $proc.ExitCode
}

# 刷新当前进程 PATH，让 launcher 紧接着就能找到新 Python
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = "$machinePath;$userPath"

$pyHome = Join-Path $env:LOCALAPPDATA "Programs\Python\Python310"
$pyExe = Join-Path $pyHome "python.exe"
if (Test-Path $pyExe) {
    Write-Host "[装Python] 安装成功：$pyExe" -ForegroundColor Green
    & $pyExe --version
} else {
    Write-Host "[装Python] 安装流程结束，但未在默认路径找到解释器（可能需重开窗口识别）。" -ForegroundColor Yellow
}
exit 0
