@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$conns = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if (-not $conns) { Write-Host '服务未运行。'; exit 0 }; $pids = $conns.OwningProcess | Sort-Object -Unique; $stopped = 0; foreach ($procId in $pids) { $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $procId) -ErrorAction SilentlyContinue; if ($p -and $p.CommandLine -match 'uvicorn' -and $p.CommandLine -match 'app\.main:app') { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue; Write-Host ('已停止服务 PID ' + $procId); $stopped = 1 } else { Write-Host ('端口 8765 被其他程序占用，未停止 PID ' + $procId) } }; if ($stopped -eq 0) { Write-Host '没有发现可停止的元件库管家服务。' }"
echo.
pause
