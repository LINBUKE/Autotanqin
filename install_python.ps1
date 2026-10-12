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
$MinGoodSize = 25MB   # 3.10.11 amd64 安装包约 27.7MB，低于此值视为残缺

# ---------------------------------------------------------------------------
# 检查是否有“上次遗留的安装器进程”——它运行期间会锁住自身 exe，
# 导致这次下载写文件时报 “being used by another process”。
# ---------------------------------------------------------------------------
function Get-StaleInstallerProcess {
    try {
        Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -like "python-3*" -or $_.ProcessName -like "python-3.10*" }
    } catch { @() }
}
$stale = Get-StaleInstallerProcess
if ($stale) {
    Write-Host "[装Python] 检测到上次的 Python 安装程序仍在运行（会锁住安装包文件），等待其退出…" -ForegroundColor Yellow
    foreach ($i in 1..10) {
        Start-Sleep -Seconds 2
        $stale = Get-StaleInstallerProcess
        if (-not $stale) { break }
    }
    if ($stale) {
        Write-Host "[装Python] 安装程序长时间未退出，尝试自动关闭它…" -ForegroundColor Yellow
        try { $stale | Stop-Process -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 } catch { }
    }
}

# ---------------------------------------------------------------------------
# 已存在完整安装包（上次下载成功、只是安装失败）-> 直接复用，不再下载
# ---------------------------------------------------------------------------
if ((Test-Path $Installer) -and ((Get-Item $Installer).Length -ge $MinGoodSize)) {
    Write-Host "[装Python] 发现已下载好的完整安装包，直接复用：$Installer" -ForegroundColor Green
    $downloaded = $true
}
else {
    # 清理上次失败的残缺包与 .partial 临时文件
    Get-ChildItem $TmpDir -Filter "$FileName*" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne $FileName -or $_.Length -lt $MinGoodSize } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    # 清理可能残留的 BITS 传输任务（它会持有目标文件句柄）
    try { Get-BitsTransfer -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "windsong_py*" } | Remove-BitsTransfer -ErrorAction SilentlyContinue } catch { }

    Write-Host "[装Python] 开始下载 Python $PyVersion …" -ForegroundColor Cyan
    $downloaded = $false
    $idx = 0
    foreach ($url in $Sources) {
        $idx++
        # 每个源用独立 .partial 文件，杜绝多个源/残留任务争抢同一个文件
        $part = Join-Path $TmpDir ("{0}.part{1}" -f $FileName, $idx)
        if (Test-Path $part) { Remove-Item $part -Force -ErrorAction SilentlyContinue }
        Write-Host "[装Python] 尝试: $url"
        try {
            # 同步 BITS（阻塞到完成）；失败再退回普通 HTTP。两者都写本源独有的 part 文件
            try {
                Start-BitsTransfer -Source $url -Destination $part -ErrorAction Stop
            } catch {
                # BITS 不可用/被禁用时走 WebRequest
                Invoke-WebRequest -Uri $url -OutFile $part -UseBasicParsing -TimeoutSec 300
            }
            if ((Test-Path $part) -and ((Get-Item $part).Length -ge $MinGoodSize)) {
                # 原子落地：目标名空闲就改名；被占用（杀软扫描等）就直接用唯一 part 文件安装
                try {
                    if (Test-Path $Installer) { Remove-Item $Installer -Force -ErrorAction Stop }
                    Move-Item -Path $part -Destination $Installer -Force -ErrorAction Stop
                } catch {
                    Write-Host "[装Python] 标准文件名被占用，改用临时文件安装：$part" -ForegroundColor Yellow
                    $Installer = $part
                }
                $downloaded = $true
                Write-Host "[装Python] 下载完成。" -ForegroundColor Green
                break
            } else {
                Remove-Item $part -Force -ErrorAction SilentlyContinue
                throw "下载文件不完整或为空"
            }
        } catch {
            Write-Host "[装Python] 该源失败：$($_.Exception.Message)" -ForegroundColor Yellow
            Remove-Item $part -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not $downloaded) {
    Write-Host "[装Python] 所有下载源均失败。常见原因：网络受限、安全软件拦截、临时目录文件被占用。" -ForegroundColor Red
    Write-Host "          可手动下载 Python 3.10.11（勾选 Add python.exe to PATH）后重新双击 启动.bat：" -ForegroundColor Red
    Write-Host "          https://www.python.org/downloads/release/python-31011/" -ForegroundColor Red
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
