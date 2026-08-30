$ErrorActionPreference = 'Stop'

# Only stop the two processes owned by the local console ports. This avoids
# terminating unrelated Python or Node processes on the workstation.
$ports = @(8765, 5173)
$stopped = @{}
foreach ($port in $ports) {
  $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  foreach ($connection in $connections) {
    $ownerPid = [int]$connection.OwningProcess
    if ($ownerPid -gt 0 -and -not $stopped.ContainsKey($ownerPid)) {
      $process = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
      if ($process) {
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
        $stopped[$ownerPid] = $true
        Write-Host "已停止端口 $port 上的进程 PID=$ownerPid ($($process.ProcessName))"
      }
    }
  }
}
if ($stopped.Count -eq 0) {
  Write-Host '未发现正在运行的 SEMI-KB 前后端进程。'
} else {
  Write-Host 'SEMI-KB 前后端服务已停止。'
}
