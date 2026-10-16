# Удаление агента «Дача» с этого компьютера. Python и Tailscale не удаляются.
# -Yes — без вопросов (так запускает кнопка «самоуничтожения» с сайта).

param([switch]$Yes)

$ErrorActionPreference = 'Continue'
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName   = 'DachaAgent'
$Marker     = Join-Path $InstallDir '.uninstalling'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($Yes) {
    # Запущено с сайта: ответа ждать не от кого. Снятие автозапуска и правила
    # брандмауэра требует прав администратора; если их нет, папку и процессы всё
    # равно уберём (они принадлежат пользователю), а задача/правило останутся.
} else {
    if (-not $isAdmin) {
        Write-Host 'Нужны права администратора. Запусти uninstall.bat двойным кликом.' -ForegroundColor Red
        Start-Sleep 5
        exit 1
    }
    $a = Read-Host "Удалить агент «Дача» и все его настройки из $InstallDir ? (y/N)"
    if ($a -notmatch '^[yYдД]') {
        Write-Host 'Отменено.'
        Start-Sleep 2
        exit 0
    }
}

# Ставим метку только теперь, когда точно продолжаем: это говорит сторожевому
# run-agent.ps1 не поднимать агента заново. Без неё была гонка — сторож
# перезапускал агент быстрее, чем мы успевали его остановить (поиск процесса
# по CommandLine через WMI ненадёжен: на многих системах эта информация для
# чужого процесса не отдаётся), и кнопка «Удалить» с сайта на деле ничего не удаляла.
try { New-Item -ItemType File -Path $Marker -Force -ErrorAction Stop | Out-Null } catch {}

try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath -like "$InstallDir\*") -or
    ($_.CommandLine -and $_.CommandLine -like "*$InstallDir\run-agent.ps1*")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName $TaskName -ErrorAction SilentlyContinue

# Папку удаляем отдельным процессом с задержкой: этот скрипт лежит внутри неё
Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "ping 127.0.0.1 -n 6 >nul & rmdir /s /q `"$InstallDir`"" -WindowStyle Hidden

Write-Host ''
Write-Host 'Агент удалён: автозапуск и правило брандмауэра убраны, папка будет стёрта через несколько секунд.' -ForegroundColor Green
Write-Host 'Tailscale остался. Если он больше не нужен, удали его через «Параметры - Приложения».'
Start-Sleep 5
