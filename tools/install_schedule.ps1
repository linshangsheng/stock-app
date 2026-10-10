# 无人值守盘后任务（可选，需求书 3.26.5 / 附录 B.2-8）
# 默认方案是「应用运行时调度 + 启动补跑」。如果你希望电脑没开着应用也能按时更新数据，就用 Windows 任务计划程序
# 定时唤醒后端执行盘后任务链（数据更新 -> 完整性闸门 -> 扫描 -> 体检 -> 回填 -> 事件 -> 备份）。
#
# 用法（PowerShell，普通用户权限即可）：
#   .\tools\install_schedule.ps1 -CnTime 17:30 -UsTime 07:30          # 安装（时间为本机时区）
#   .\tools\install_schedule.ps1 -Remove                               # 卸载
# 说明：
#   * 任务只执行一次 `python -m server.cli update --market CN|US`；该命令会先探测上游是否已有当日数据，没有就直接退出，
#     因此可以放心设两个时间点（例如 17:30 与 19:30）重复尝试；
#   * 本机时区不影响判断：交易日 / 收盘一律按市场时区；这里的时间只是「什么时候来试一次」；
#   * 以 UTC+7 为例：A 股 15:00（北京）= 14:00 当地，建议 17:30；美股收盘约 03:00~04:00 当地，建议 07:30。
param(
  [string]$CnTime = "17:30",
  [string]$UsTime = "07:30",
  [string]$Python = "",
  [switch]$Remove,
  [switch]$NoUs
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if ($Remove) {
  foreach ($n in "StockApp-CN-Update", "StockApp-US-Update") { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
  Write-Host "已卸载定时任务。"; return
}
if (-not $Python) { $Python = (Get-Command python).Source }
function Add-Task($name, $time, $market) {
  $action = New-ScheduledTaskAction -Execute $Python -Argument "-m server.cli update --market $market" -WorkingDirectory $root
  $trigger = New-ScheduledTaskTrigger -Daily -At $time
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 6) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Description "股票 App 盘后任务链（$market）" -Force | Out-Null
  Write-Host "已创建：$name  每天 $time  ->  $Python -m server.cli update --market $market"
}
Add-Task "StockApp-CN-Update" $CnTime "CN"
if (-not $NoUs) { Add-Task "StockApp-US-Update" $UsTime "US" }
Write-Host "提示：任务以当前用户登录时运行；日志见 data\server.log 与设置页「最近任务日志」。"
