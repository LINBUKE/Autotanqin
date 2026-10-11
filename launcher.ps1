# ============================================================================
# 风物之诗琴工具 · 环境引导启动器
# 职责：找到可用 Python（排除微软商店占位符）-> 复用/创建 .venv ->
#       bootstrap 装依赖 -> 启动桌面版。全程自动，缺 Python 会自动下载安装。
# 兼容 Python 3.9 <= v < 3.12（3.10 体积最小，3.11 亦可）。
# ============================================================================
param(
    [ValidateSet("app", "menu")]
    [string]$Mode = "app"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$MinVersion = [version]"3.9.0"
$MaxVersion = [version]"3.12.0"   # 不含 3.12（无可用 wheel）

function Write-Info($m) { Write-Host "[引导] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "[  OK] $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "[警告] $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 判断一个 python.exe 是否真实可用（排除 WindowsApps 商店占位 stub）
# 商店占位符 Get-Command 能找到，但运行会跳转应用商店 / 返回 9009。
# ---------------------------------------------------------------------------
function Test-RealPython {
    param([string]$Exe)
    # 原生程序可能往 stderr 写内容（警告），Stop 模式会误当成终止错误，函数内放宽
    $ErrorActionPreference = "Continue"
    if (-not $Exe) { return $null }
    if (-not (Test-Path $Exe)) { return $null }
    if ($Exe -match "WindowsApps") { return $null }   # 商店别名，直接排除
    try {
        # 注意：PS 5.1 给原生程序传参会剥双引号，Python 代码里一律用单引号
        $code = "import sys;print('%d.%d.%d'%sys.version_info[:3])"
        $ver = & $Exe -c $code 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $ver) { return $null }
        $v = [version]($ver.ToString().Trim())
        if ($v.Major -ne 3) { return $null }
        if ($v -lt $MinVersion -or $v -ge $MaxVersion) { return $null }
        return $v
    } catch { return $null }
}

# ---------------------------------------------------------------------------
# 在系统里找一个兼容版本的真实 Python；找不到返回 $null
# 优先级：py -3.10/3.11/3.9/3 启动器 -> PATH 上的 python/python3 -> 常见安装目录
# ---------------------------------------------------------------------------
function Find-SystemPython {
    $ErrorActionPreference = "Continue"   # 内部多次调用原生 python/py，放宽 stderr 策略
    # 1) py launcher（最可靠，不会返回商店 stub）
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($tag in @("-3.10", "-3.11", "-3.9", "-3")) {
            try {
                # 同上：Python 代码用单引号，避免 PS 剥双引号
                $exe = (& py.exe $tag -c "import sys;print(sys.executable)" 2>$null | Select-Object -First 1)
                if ($exe) {
                    $v = Test-RealPython $exe.ToString().Trim()
                    if ($v) { return @{ Exe = $exe.ToString().Trim(); Version = $v } }
                }
            } catch { }
        }
    }
    # 2) PATH
    foreach ($name in @("python.exe", "python3.exe")) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) {
            foreach ($cand in ($c | ForEach-Object { $c.Source } | Select-Object -Unique)) {
                $v = Test-RealPython $cand
                if ($v) { return @{ Exe = $cand; Version = $v } }
            }
        }
    }
    # 3) 常见用户级 / 机器级安装目录
    $bases = @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:ProgramFiles\Python*",
        "${env:ProgramFiles(x86)}\Python*"
    )
    foreach ($b in $bases) {
        Get-ChildItem -Path $b -Filter "python.exe" -Recurse -Depth 2 -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | ForEach-Object {
                $v = Test-RealPython $_.FullName
                if ($v) { return @{ Exe = $_.FullName; Version = $v } }
            }
    }
    return $null
}

# ---------------------------------------------------------------------------
# venv 的解释器本身能否启动（轻量探测）
# 关键场景：venv 是用某版本 Python 创建的，后来该基础 Python 被卸载/移动，
# 或整个 .venv 是从别的电脑/目录拷来的 —— venv 里的 python.exe 只是转发器，
# 启动时会报 “No Python at '...'”。这种情况补装依赖无意义，必须重建 venv。
# ---------------------------------------------------------------------------
function Test-VenvPythonAlive {
    $ErrorActionPreference = "Continue"
    if (-not (Test-Path $VenvPy)) { return $false }
    # Python 代码不含双引号（PS 5.1 给原生程序传参会剥双引号）
    & $VenvPy -c "import sys;print(1)" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# ---------------------------------------------------------------------------
# venv 是否健康：解释器能启动 + bootstrap.py check 关键库真实导入冒烟
# （能抓出“元数据在但 DLL 加载失败”；不用内联 -c 以避开 PS 传参剥引号）
# ---------------------------------------------------------------------------
function Test-VenvHealthy {
    $ErrorActionPreference = "Continue"   # python 的 warning 走 stderr，Stop 模式会误判
    if (-not (Test-VenvPythonAlive)) { return $false }
    $bootstrap = Join-Path $Root "bootstrap.py"
    if (-not (Test-Path $bootstrap)) { return $false }
    & $VenvPy $bootstrap check 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
# 主流程大量调用原生程序（python/pip 会往 stderr 写进度/警告），
# 退出码都在下面显式判断，这里放宽 EAP 避免 stderr 被误当终止错误。
$ErrorActionPreference = "Continue"
Write-Info "项目目录：$Root"

# 1) 已有健康 venv -> 直接用
if (Test-VenvHealthy) {
    Write-Ok "已检测到可用虚拟环境 .venv"
}
else {
    $venvDir = Join-Path $Root ".venv"
    $venvExists = Test-Path $VenvPy
    $venvAlive = if ($venvExists) { Test-VenvPythonAlive } else { $false }

    if ($venvExists -and -not $venvAlive) {
        Write-Warn2 "现有 .venv 的解释器已失效（它依赖的基础 Python 已卸载/移动，或该 .venv 是从其他电脑拷来的）。"
        Write-Warn2 "补装依赖无法修复，将备份旧环境后用当前 Python 重新创建 .venv …"
    } elseif ($venvExists) {
        Write-Warn2 "现有 .venv 缺少或损坏了部分组件，开始自动补装依赖…"
    } else {
        Write-Info "未发现 .venv，开始准备运行环境（仅首次需要）…"
    }

    # 2) 找系统兼容 Python
    $found = Find-SystemPython

    # 3) 没有就自动下载安装 Python 3.10.11（用户级静默）
    if (-not $found) {
        Write-Warn2 "未找到兼容的 Python（3.9 ~ 3.11）。将自动下载并安装 Python 3.10.11…"
        $installer = Join-Path $Root "install_python.ps1"
        if (-not (Test-Path $installer)) {
            Write-Host "[错误] 缺少 install_python.ps1，无法自动安装 Python。" -ForegroundColor Red
            Write-Host "       请手动安装 Python 3.10/3.11（勾选 Add to PATH）后重新运行。" -ForegroundColor Red
            exit 2
        }
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[错误] Python 自动安装失败，请按上面提示处理后重试。" -ForegroundColor Red
            exit 2
        }
        $found = Find-SystemPython
        if (-not $found) {
            Write-Host "[错误] Python 已安装但当前会话尚未识别，请关闭本窗口后重新双击 启动.bat。" -ForegroundColor Red
            exit 2
        }
    }

    Write-Ok ("使用 Python {0}: {1}" -f $found.Version, $found.Exe)

    # 4) 创建 / 重建 venv
    #    解释器失效（基础 Python 丢失、跨机拷贝）时必须重建：
    #    旧目录先改名备份，新环境全部验证通过后才删备份；创建失败回滚。
    $backupDir = $null
    if (-not $venvAlive) {
        if ($venvExists) {
            $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
            $backupDir = Join-Path $Root ".venv_backup_$stamp"
            try {
                Rename-Item -Path $venvDir -NewName (Split-Path $backupDir -Leaf) -ErrorAction Stop
                Write-Info "旧环境已备份为：$(Split-Path $backupDir -Leaf)（验证通过后自动删除）"
            } catch {
                Write-Host "[错误] 无法备份旧 .venv（可能被占用）。请关闭正在运行的本软件/杀毒软件后重试。" -ForegroundColor Red
                exit 3
            }
        }
        Write-Info "正在创建虚拟环境 .venv …"
        & $found.Exe -m venv $venvDir
        $createOk = ($LASTEXITCODE -eq 0) -and (Test-Path $VenvPy) -and (Test-VenvPythonAlive)
        if (-not $createOk) {
            Write-Host "[错误] 创建虚拟环境失败。" -ForegroundColor Red
            if ($backupDir -and (Test-Path $backupDir)) {
                if (Test-Path $venvDir) { Remove-Item $venvDir -Recurse -Force -ErrorAction SilentlyContinue }
                Rename-Item -Path $backupDir -NewName ".venv" -ErrorAction SilentlyContinue
                Write-Warn2 "已回滚为原来的 .venv。"
            }
            exit 3
        }
    }

    # 5) bootstrap：多镜像源安装依赖
    $bootstrap = Join-Path $Root "bootstrap.py"
    & $VenvPy $bootstrap
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 依赖安装未完成，请检查网络后重新运行（会自动续装）。" -ForegroundColor Red
        exit 4
    }

    # 6) 复查
    if (-not (Test-VenvHealthy)) {
        Write-Host "[错误] 依赖装完但关键库仍无法导入，可能是安装包损坏，请重试或删除 .venv 后再启动。" -ForegroundColor Red
        exit 5
    }

    # 7) 新环境验证通过，删除旧环境备份
    if ($backupDir -and (Test-Path $backupDir)) {
        Remove-Item $backupDir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $backupDir) {
            Write-Warn2 "旧环境备份未能自动删除，可手动删除：$(Split-Path $backupDir -Leaf)"
        } else {
            Write-Ok "旧环境备份已清理"
        }
    }
    Write-Ok "环境准备完成"
}

# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
Set-Location $Root
if ($Mode -eq "menu") {
    Write-Info "启动一键菜单…"
    & $VenvPy (Join-Path $Root "run.py")
} else {
    Write-Info "启动桌面版…"
    & $VenvPy (Join-Path $Root "desktop_app.py")
}
exit $LASTEXITCODE
