# ============================================================
#  风物之诗琴自动化工具 · 环境检测与启动
#  由「启动.bat」调用，负责在 Windows 各种 Python 环境下找到
#  一个真正可用且版本兼容的解释器：
#    - 兼容窗口：Python 3.9 / 3.10 / 3.11（basic-pitch 0.4.0）
#    - 自动跳过 Microsoft Store 的 python.exe 占位符
#    - 版本太新(3.12+) / 太老(<3.9) 时给出明确中文提示并自动
#      安装独立的 Python 3.10（不影响电脑上已有的 Python）
#    - 每个候选解释器必须真实执行代码成功才算数（带超时，
#      防止 Store 占位符 / 损坏解释器把流程挂住）
# ============================================================

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition

# 只声明主、次分量（其余分量按未指定处理），这样比较边界才准确：
# 例如 3.12.0 不能因为第四分量 -1 而被误判为 < 3.12.0.0。
$MinVersion      = New-Object Version 3,9         # >= 3.9
$MaxExclusive    = New-Object Version 3,12        # <  3.12
$InstallVersion  = "3.10.11"

# 探测到、但版本不在窗口内的解释器（用于给用户明确的提示）
$Script:WrongVersions = @()


function Write-Step($m) { Write-Host "[setup] $m" }

function Test-VersionOk([Version]$v) {
    return (($v -ge $MinVersion) -and ($v -lt $MaxExclusive))
}

# 解释器选择优先级：3.10 最优(走 onnxruntime，下载最小)，其次 3.9，最后 3.11(tensorflow)
function Get-Preference([Version]$v) {
    if ($v.Minor -eq 10) { return 0 }
    if ($v.Minor -eq 9)  { return 1 }
    return 2
}

# 真正执行 python.exe，返回其版本号；任何失败/超时/无效输出返回 $null。
# 使用 .NET Process API（Windows PowerShell 2.0 也兼容）。
function Get-PythonVersion($exe) {
    if (-not (Test-Path $exe)) { return $null }
    $code = "import sys;print('%d.%d.%d'%sys.version_info[:3])"
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = '-c "' + $code + '"'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.CreateNoWindow = $true
    $p = $null
    try {
        $p = [Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit(10000)) {
            try { $p.Kill() } catch {}
            return $null
        }
        $text = $p.StandardOutput.ReadToEnd()
    }
    catch { return $null }
    finally { if ($p) { $p.Dispose() } }

    foreach ($line in ($text -split "`n")) {
        $line = $line.Trim()
        if ($line -match "^\d+\.\d+\.\d+$") {
            try { return New-Object Version($line) } catch {}
        }
    }
    return $null
}

# 去重加入候选；Microsoft Store 占位符直接排除。
function Add-Candidate($list, $exe) {
    if (-not $exe) { return }
    if (-not (Test-Path $exe)) { return }
    $full = (Resolve-Path $exe).Path
    if ($full -match "WindowsApps") { return }
    foreach ($e in $list) {
        if ($e.ToLower() -eq $full.ToLower()) { return }
    }
    [void]$list.Add($full)
}

# 收集所有可能的 python.exe 路径（不做版本判断）。
function Get-Candidates {
    $list = New-Object System.Collections.Generic.List[string]

    # ---- 1) 注册表（python.org 安装器的权威记录）----
    $regRoots = @(
        @{ Base = [Microsoft.Win32.Registry]::CurrentUser; Path = "Software\Python\PythonCore" }
        @{ Base = [Microsoft.Win32.Registry]::LocalMachine; Path = "Software\Python\PythonCore" }
        @{ Base = [Microsoft.Win32.Registry]::LocalMachine; Path = "Software\WOW6432Node\Python\PythonCore" }
    )
    foreach ($r in $regRoots) {
        $rk = $r.Base.OpenSubKey($r.Path)
        if ($rk) {
            foreach ($name in $rk.GetSubKeyNames()) {
                $ip = $rk.OpenSubKey("$name\InstallPath")
                if ($ip) {
                    $exe = $ip.GetValue("ExecutablePath")
                    if (-not $exe) {
                        $dir = $ip.GetValue("")
                        if ($dir) { $exe = Join-Path $dir "python.exe" }
                    }
                    Add-Candidate $list $exe
                    $ip.Close()
                }
            }
            $rk.Close()
        }
    }

    # ---- 2) py 启动器：py -0p 会列出所有已安装版本及路径 ----
    try {
        foreach ($line in (& py -0p 2>$null)) {
            $m = [Text.RegularExpressions.Regex]::Match($line, "([A-Za-z]:\\[^\r\n]+?python\.exe)")
            if ($m.Success) { Add-Candidate $list $m.Groups[1].Value.Trim() }
        }
    } catch {}

    # ---- 3) PATH 中的 python（where.exe）----
    try {
        foreach ($exe in (& where.exe python 2>$null)) { Add-Candidate $list $exe }
    } catch {}

    # ---- 4) 常见安装位置（兜底）----
    $common = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python39\python.exe")
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
        "C:\Python39\python.exe"
        "C:\Python310\python.exe"
        "C:\Python311\python.exe"
        "C:\Program Files\Python39\python.exe"
        "C:\Program Files\Python310\python.exe"
        "C:\Program Files\Python311\python.exe"
    )
    foreach ($exe in $common) { Add-Candidate $list $exe }

    return $list
}

# 逐个验证候选，返回最优的兼容解释器（找不到返回 $null）。
function Select-Python {
    $valid = @()
    foreach ($exe in (Get-Candidates)) {
        $v = Get-PythonVersion $exe
        if ($v -and (Test-VersionOk $v)) {
            $valid += (New-Object psobject -Property @{ Exe = $exe; Version = $v; Pref = (Get-Preference $v) })
        }
        elseif ($v) {
            $Script:WrongVersions += (New-Object psobject -Property @{ Exe = $exe; Version = $v })
        }
    }
    if ($valid.Count -gt 0) {
        return ($valid | Sort-Object Pref | Select-Object -First 1)
    }
    return $null
}

function Get-ExpectedExe($ver) {
    $folder = "Python" + ($ver.Split(".")[0]) + ($ver.Split(".")[1])
    return Join-Path $env:LOCALAPPDATA "Programs\Python\$folder\python.exe"
}

function Wait-Key {
    Write-Host ""
    Write-Host "按回车键退出…"
    [void](Read-Host)
}

# ============================================================
#  主流程
# ============================================================
function Main {
    param([string[]]$ForwardArgs)

    Write-Host "============================================================"
    Write-Host "  风物之诗琴自动化工具 · 环境自动检测 / 安装"
    Write-Host "============================================================"

    $sel = Select-Python

    if (-not $sel) {
        if ($Script:WrongVersions.Count -gt 0) {
            Write-Step "检测到以下 Python，但版本不兼容（本项目仅支持 3.9 ~ 3.11）："
            foreach ($w in $Script:WrongVersions) {
                Write-Host ("          Python {0}  ->  {1}" -f $w.Version, $w.Exe)
            }
            Write-Step "将自动安装一个独立的 Python $InstallVersion（不会卸载或影响上面的 Python）…"
        }
        else {
            Write-Step "未检测到可用的 Python（Microsoft Store 占位符不算真正安装）。"
            Write-Step "将自动下载安装 Python $InstallVersion …"
        }
        Write-Host ""

        & (Join-Path $Root "install_python.ps1") -Version $InstallVersion
        if ($LASTEXITCODE -ne 0) { Wait-Key; exit 1 }

        $expected = Get-ExpectedExe $InstallVersion
        $v = Get-PythonVersion $expected
        if ((-not $v) -or (-not (Test-VersionOk $v))) {
            Write-Host "[setup][错误] Python 安装后验证失败：$expected"
            Wait-Key; exit 1
        }
        $sel = New-Object psobject -Property @{ Exe = $expected; Version = $v }
    }

    Write-Step ("使用 Python {0}（解释器：{1}）" -f $sel.Version, $sel.Exe)
    & $sel.Exe (Join-Path $Root "bootstrap.py") @ForwardArgs
    $code = $LASTEXITCODE

    if ($code -ne 0) {
        Write-Host ""
        Write-Host "[启动失败] 程序退出码 $code。请把本窗口截图反馈；详细错误见 data\bootstrap_error.log"
        Wait-Key
    }
    exit $code
}

# 直接运行 -> 执行主流程；被 dot-source（单元测试）-> 只加载函数。
if ($MyInvocation.InvocationName -ne ".") {
    Main @($args)
}
