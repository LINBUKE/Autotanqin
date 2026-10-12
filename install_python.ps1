# ============================================================================
# 自动下载并安装 Python 3.10.11（用户级、加入 PATH）
# 仅在系统里找不到兼容 Python（3.9~3.11）时由 launcher.ps1 调用。
# 多下载源回退：华为云 -> npmmirror(淘宝) -> python.org
#
# 安装策略（针对 MSI 1603 做了三层保底）：
#   1) /quiet 全静默，参数修正：InstallLauncherAllUsers=0
#      （py 启动器默认按机器级安装，会写 HKLM，per-user 静默无提权 -> 1603）
#   2) 静默失败 -> /passive 带进度条重试（可见错误/可响应 UAC，成功率高）
#   3) 仍失败 -> 解析安装日志真实错误并保留安装包，引导用户双击手动安装
# ============================================================================
$ErrorActionPreference = "Continue"
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
$LogPath = Join-Path $TmpDir "python_install.log"

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

# ---------------------------------------------------------------------------
# 从安装日志提取真正的错误行（1603 只是 MSI 通用码，原因在日志里）
# ---------------------------------------------------------------------------
function Show-InstallLog {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Host "[装Python] （未生成安装日志：$Path）" -ForegroundColor Yellow
        return
    }
    Write-Host "[装Python] 安装日志关键错误（完整日志：$Path）：" -ForegroundColor Yellow
    try {
        $lines = Get-Content $Path -Tail 400 -Encoding Default
        $hits = $lines |
            Where-Object { $_ -match "Return value 3|Error 0x[0-9a-fA-F]+|MainEngineThreadInterval returned|拒绝访问|access is denied|already installed|另一个安装|1603|1620|1618|1622" } |
            Select-Object -Last 12
        if ($hits) { $hits | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow } }
        else { Write-Host "    （日志中没有标准错误标记，请打开完整日志查看末尾内容）" -ForegroundColor Yellow }
    } catch {
        Write-Host "    （日志读取失败：$($_.Exception.Message)）" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# 运行一次安装器；$Mode 为 quiet / passive；返回退出码
# ---------------------------------------------------------------------------
function Invoke-PythonInstall {
    param([string]$Mode)
    if (Test-Path $LogPath) { Remove-Item $LogPath -Force -ErrorAction SilentlyContinue }

    # 关键参数：
    #   InstallAllUsers=0       用户级安装，免管理员
    #   InstallLauncherAllUsers=0  py 启动器也按用户级装（默认=1 会写 HKLM -> 静默 1603）
    #   SimpleInstall=1         只装必需组件，跳过可选组件界面交互（3.10+ 支持）
    #   PrependPath=1           自动加入 PATH
    # 含空格的日志路径显式加引号，手工拼 ArgumentList 避免 Start-Process 数组拼接歧义
    $argString = "$Mode InstallAllUsers=0 InstallLauncherAllUsers=0 SimpleInstall=1 " +
                 "PrependPath=1 AppendPath=1 Include_pip=1 Include_launcher=1 " +
                 "Include_test=0 Include_doc=0 AssociateFiles=0 " +
                 "/log `"$LogPath`""
    $p = Start-Process -FilePath $Installer -ArgumentList $argString -Wait -PassThru
    return $p.ExitCode
}

# 安装后解释器是否真实可用（默认 per-user 路径 + 实际执行 --version）
function Test-NewPython {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
        (Join-Path ${env:ProgramFiles} "Python310\python.exe")
    )
    foreach ($exe in $candidates) {
        if (Test-Path $exe) {
            try {
                $v = & $exe -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
                if ($LASTEXITCODE -eq 0 -and $v) { return $exe }
            } catch { }
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# 第 1 轮：全静默
# ---------------------------------------------------------------------------
Write-Host "[装Python] 正在静默安装（用户级，不弹安装界面）…" -ForegroundColor Cyan
$code1 = Invoke-PythonInstall "/quiet"
$ready = Test-NewPython

# ---------------------------------------------------------------------------
# 第 2 轮：静默失败或解释器未落地 -> 带进度条重试（用户可看到原因/响应提示）
# ---------------------------------------------------------------------------
if (-not $ready) {
    Write-Host "[装Python] 静默安装未成功（退出码 $code1），改用带进度条模式重试一次…" -ForegroundColor Yellow
    Write-Host "[装Python] 如果弹出权限确认，请点“是”；看到安装界面按提示完成即可。" -ForegroundColor Yellow
    $code2 = Invoke-PythonInstall "passive" "/passive"
    $ready = Test-NewPython
} else {
    $code2 = $null
}

# ---------------------------------------------------------------------------
# 结果判定：以“解释器真实可运行”为准，不只看退出码（安装器有时返回非 0 但装好了）
# ---------------------------------------------------------------------------
if ($ready) {
    # 刷新当前进程 PATH，让 launcher 紧接着就能找到新 Python
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
    Write-Host "[装Python] 安装成功：$ready" -ForegroundColor Green
    & $ready --version
    exit 0
}

# ---------------------------------------------------------------------------
# 第 3 层：两轮都失败 -> 展示真实原因，保留安装包让用户手动安装
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "[装Python] 自动安装两次均未成功（静默退出码 $code1，界面模式退出码 $code2）。" -ForegroundColor Red
Show-InstallLog $LogPath
Write-Host ""
Write-Host "[装Python] 常见原因：① 安全软件拦截  ② 系统服务 Windows Installer 被禁用" -ForegroundColor Red
Write-Host "          ③ 以前装过损坏的 Python 3.10（先在“应用和功能”里卸载它）" -ForegroundColor Red
Write-Host ""
Write-Host "[装Python] 安装包已保留，请手动双击安装（务必勾选 Add python.exe to PATH）：" -ForegroundColor Cyan
Write-Host "          $Installer" -ForegroundColor Cyan
Write-Host "[装Python] 手动装好后，重新双击 启动.bat 即可继续。" -ForegroundColor Cyan
exit 1603
