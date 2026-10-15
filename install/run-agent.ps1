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

while ($true) {
    $ip = $null
    while (-not $ip) {
        $ip = Get-TsIp
        if (-not $ip) { Start-Sleep -Seconds 5 }
    }
    $env:AGENT_HOST = $ip
    Start-Process -FilePath $py -ArgumentList 'agent.py' -WorkingDirectory $agent -WindowStyle Hidden -Wait `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    Start-Sleep -Seconds 5
}
