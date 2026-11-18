# start.ps1 —— A 股分析工具启动器（Node 前端 + Python 策略引擎伴侣服务）
$dir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $dir
$port = 8790
$seqPort = 8791
$seqDir = Join-Path $dir "..\sequoia-service"

# 1) 解析 node 可执行文件（优先 PATH，其次 WorkBuddy 内置）
$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) {
    $base = Join-Path $env:USERPROFILE ".workbuddy\binaries\node\versions"
    if (Test-Path $base) {
        $ver = Get-ChildItem $base -Directory | Sort-Object Name -Descending | Select-Object -First 1
        $cand = Join-Path $ver.FullName "node.exe"
        if (Test-Path $cand) { $node = $cand }
    }
}
if (-not $node) {
    Write-Host "没找到 Node.js。请先安装 Node.js 18 或更高版本：https://nodejs.org" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
Write-Host "使用 Node：$node"

# 2) 解析 python3（策略引擎伴侣服务）
# 注意：Windows 应用商店的 python3.exe 只是别名桩，需优先用带依赖的 WorkBuddy 内置 Python
$python = $null
$wuPy = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions"
if (Test-Path $wuPy) {
    $ver = Get-ChildItem $wuPy -Directory | Sort-Object Name -Descending | Select-Object -First 1
    $cand = Join-Path $ver.FullName "python3.exe"
    if (Test-Path $cand) { $python = $cand }
}
if (-not $python) { $python = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if ($python) { Write-Host "使用 Python：$python" } else { Write-Host "未找到 Python，策略引擎将不会启动（ETF/行业监控不受影响）。" -ForegroundColor Yellow }

# 3) 启动 Python 伴侣服务（独立窗口，端口 8791）
if ($python -and (Test-Path $seqDir)) {
    $occupied = netstat -ano | ForEach-Object {
        $c = -split $_
        if ($c.Count -ge 5 -and $c[1] -match ":$seqPort$" -and $c[3] -eq 'LISTENING') { $c[4] }
    }
    if ($occupied) {
        Write-Host "策略引擎端口 $seqPort 已被占用，复用现有实例。"
    } else {
        $dbPath = Join-Path $seqDir "data\sequoia_v2.db"
        if (-not (Test-Path $dbPath)) {
            Write-Host ""
            Write-Host "提示：未检测到行情数据库 ($dbPath)" -ForegroundColor Yellow
            Write-Host "首次使用请先做全市场回填（约 5 分钟，覆盖 5000+ 只 A 股）：" -ForegroundColor Yellow
            Write-Host "    cd `"$seqDir`"" -ForegroundColor Yellow
            Write-Host "    python bulk_backfill.py --reset --source tencent --workers 12" -ForegroundColor Yellow
            Write-Host "回填完成后重新双击 start.bat 即可。" -ForegroundColor Yellow
            Write-Host ""
        }
        Write-Host "正在启动策略引擎伴侣服务（端口 $seqPort）…"
        Start-Process -FilePath $python -ArgumentList "$seqDir\server.py" -WorkingDirectory $seqDir -WindowStyle Normal
        Start-Sleep -Seconds 2
    }
} else {
    if (-not (Test-Path $seqDir)) { Write-Host "未找到 sequoia-service 目录（$seqDir），跳过策略引擎。" -ForegroundColor Yellow }
}

# 4) 释放已被占用的前端端口（只关掉本地监听 $port 的旧实例，绝不误杀其他进程）
$listeners = netstat -ano | ForEach-Object {
    $c = -split $_
    if ($c.Count -ge 5 -and $c[1] -match ":$port$" -and $c[3] -eq 'LISTENING') { $c[4] }
}
foreach ($pidv in $listeners) {
    Write-Host "端口 $port 被进程 $pidv 占用，正在关闭旧实例…"
    Stop-Process -Id $pidv -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# 5) 打开浏览器
try { Start-Process "http://127.0.0.1:$port" } catch { }

# 6) 前台启动前端服务（关闭本窗口即停止服务）
Write-Host "正在启动前端服务（端口 $port），日志如下：" -ForegroundColor Cyan
& $node (Join-Path $dir "server.js")
Write-Host "服务已停止。"
Read-Host "按回车退出"
