# Запускает агент «Дача» и перезапускает его, если он упал.
# Ждёт, пока появится адрес Tailscale, и слушает только его (а не все сетевые адреса).
# Этот файл запускается планировщиком заданий при входе в Windows.

$ErrorActionPreference = 'Continue'
$dir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$agent  = Join-Path $dir 'agent'
$py     = Join-Path $agent '.venv\Scripts\python.exe'
$ts     = 'C:\Program Files\Tailscale\tailscale.exe'
$outLog = Join-Path $dir 'agent.out.log'
$errLog = Join-Path $dir 'agent.err.log'
$marker = Join-Path $dir '.uninstalling'  # ставит uninstall.ps1: значит, агент больше поднимать не нужно
$port   = [int]($env:AGENT_PORT); if (-not $port) { $port = 8765 }

# Вывод Python в файл должен быть в UTF-8, иначе русские сообщения могут уронить агента
$env:PYTHONUTF8 = '1'

function Get-TsIp {
    try {
        $o  = & $ts ip -4 2>$null
        $ip = "$(@($o)[0])".Trim()
        if ($ip -match '^100\.\d+\.\d+\.\d+$') { return $ip }
    } catch {}
    return $null
}

# Если порт уже занят нашим же старым процессом (например, он не успел закрыться
# после перезапуска Tailscale или перезапуска задачи вручную) — останавливаем
# именно его и только его: проверяем, что путь к .exe владельца порта в точности
# совпадает с нашим $py, поэтому чужой python.exe (другие проекты на компьютере)
# не трогаем.
function Stop-StaleAgent {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    } catch { return }
    foreach ($c in $conns) {
        try {
            $proc = Get-Process -Id $c.OwningProcess -ErrorAction Stop
            if ($proc.Path -and ($proc.Path -ieq $py)) {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
            }
        } catch {}
    }
}

while ($true) {
    $ip = $null
    while (-not $ip) {
        $ip = Get-TsIp
        if (-not $ip) { Start-Sleep -Seconds 5 }
    }
    $env:AGENT_HOST = $ip
    Stop-StaleAgent
    Start-Process -FilePath $py -ArgumentList 'agent.py' -WorkingDirectory $agent -WindowStyle Hidden -Wait `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    # Агент попросил себя удалить (кнопка на сайте): не поднимаем его снова,
    # иначе uninstall.ps1 никогда не успеет остановить автозапуск — агент
    # возрождался бы быстрее, чем его можно было выключить.
    if (Test-Path $marker) { break }
    Start-Sleep -Seconds 5
}
