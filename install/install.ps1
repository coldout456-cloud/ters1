# Установщик агента «Дача» для Windows 10/11.
# Запускается через install.bat. Безопасно запускать повторно: токен, настройки и папки сохраняются,
# обновляется только код.

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo       = 'coldout456-cloud/ters1'
$Branch     = 'main'
$InstallDir = 'C:\DachaAgent'
$AgentDir   = Join-Path $InstallDir 'agent'
$EnvFile    = Join-Path $AgentDir '.env'
$Port       = 8765
$TaskName   = 'DachaAgent'
$TsExe      = 'C:\Program Files\Tailscale\tailscale.exe'

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
try { Start-Transcript -Path (Join-Path $InstallDir 'install.log') -Append | Out-Null } catch {}

# ----------------------------------------------------------------------------- вывод
function Say($t)  { Write-Host ''; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "    $t" -ForegroundColor Green }
function Warn($t) { Write-Host "    ! $t" -ForegroundColor Yellow }
function Fail($t) { throw $t }

# ----------------------------------------------------------------------------- ввод
function ReadSecret($prompt) {
    $s = Read-Host $prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringAuto($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

function Get-EnvValue($file, $key) {
    if (-not (Test-Path $file)) { return $null }
    foreach ($line in [IO.File]::ReadAllLines($file)) {
        if ($line -match "^\s*$key\s*=\s*(.*)$") { return $Matches[1].Trim() }
    }
    return $null
}

function Test-FolderAllowed($path) {
    $p = $path.TrimEnd('\')
    if ($p.Length -le 2) { return 'нельзя открывать диск целиком' }
    if ($p -match ';') { return 'в пути есть символ «;»' }
    $exact = @($env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData,
               (Split-Path $env:USERPROFILE -Parent), $env:USERPROFILE, (Join-Path $env:USERPROFILE 'AppData')) | Where-Object { $_ }
    foreach ($b in $exact) {
        if ($p -ieq $b.TrimEnd('\')) { return 'это системная папка или профиль целиком, выбери конкретную папку внутри' }
    }
    $inside = @($env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:USERPROFILE 'AppData')) | Where-Object { $_ }
    foreach ($b in $inside) {
        if ($p.StartsWith($b.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            return 'системные папки и данные программ открывать нельзя'
        }
    }
    return $null
}

function Select-Folders {
    Add-Type -AssemblyName System.Windows.Forms
    $list = @()
    while ($true) {
        $owner = New-Object System.Windows.Forms.Form
        $owner.TopMost = $true
        $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
        $dlg.Description = 'Выбери папку, к которой можно будет получать доступ с сайта'
        $dlg.ShowNewFolderButton = $false
        $res = $dlg.ShowDialog($owner)
        $owner.Dispose()
        if ($res -eq [System.Windows.Forms.DialogResult]::OK) {
            $why = Test-FolderAllowed $dlg.SelectedPath
            if ($why) { Warn "Эту папку нельзя: $why" }
            elseif ($list -contains $dlg.SelectedPath) { Warn 'Эта папка уже добавлена.' }
            else { $list += $dlg.SelectedPath; Ok "Добавлена: $($dlg.SelectedPath)" }
        }
        if ($list.Count -eq 0) {
            $a = Read-Host '     Папка не выбрана. Выбрать ещё раз? (Enter - да, n - отмена установки)'
            if ($a -match '^[nNнН]') { Fail 'Нужна хотя бы одна папка.' }
            continue
        }
        $more = Read-Host '     Добавить ещё одну папку? (y/N)'
        if ($more -notmatch '^[yYдД]') { break }
    }
    return ,$list
}

function Build-AllowedDirs($folders) {
    $used  = @{}
    $parts = @()
    foreach ($f in $folders) {
        $alias = (Split-Path $f -Leaf) -replace '[^\p{L}\p{N}_\-]', '_'
        if (-not $alias) { $alias = 'folder' }
        $base = $alias; $n = 2
        while ($used.ContainsKey($alias.ToLower())) { $alias = "${base}_$n"; $n++ }
        $used[$alias.ToLower()] = $true
        $parts += "$alias=$f"
    }
    return ($parts -join ';')
}

# ----------------------------------------------------------------------------- Python
function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try { $p = & py -3 -c "import sys; print(sys.executable)" 2>$null; if ($p) { $candidates += @($p)[0] } } catch {}
    }
    $pc = Get-Command python -ErrorAction SilentlyContinue
    if ($pc -and $pc.Source -notlike '*WindowsApps*') { $candidates += $pc.Source }
    foreach ($d in 'C:\Program Files\Python313', 'C:\Program Files\Python312', 'C:\Program Files\Python311', 'C:\Program Files\Python310') {
        $x = Join-Path $d 'python.exe'
        if (Test-Path $x) { $candidates += $x }
    }
    foreach ($c in $candidates) {
        try {
            $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ("$v" -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 10) { return $c }
        } catch {}
    }
    return $null
}

function Install-Python {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host '    Ставлю Python через winget...'
        & winget install -e --id Python.Python.3.12 --silent --scope machine --accept-package-agreements --accept-source-agreements | Out-Null
        $py = Find-Python
        if ($py) { return $py }
    }
    Write-Host '    Скачиваю установщик Python с python.org...'
    $exe = Join-Path $env:TEMP 'python-3.12.8-amd64.exe'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile $exe -ErrorAction Stop
    Start-Process -FilePath $exe -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_launcher=0' -Wait | Out-Null
    return (Find-Python)
}

# ----------------------------------------------------------------------------- Tailscale
function Install-Tailscale {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host '    Ставлю Tailscale через winget...'
        & winget install -e --id Tailscale.Tailscale --silent --accept-package-agreements --accept-source-agreements | Out-Null
    }
    if (Test-Path $TsExe) { return }

    Write-Host '    Скачиваю установщик Tailscale...'
    $arch = switch ($env:PROCESSOR_ARCHITECTURE) { 'ARM64' { 'arm64' } 'x86' { 'x86' } default { 'amd64' } }
    $page = (Invoke-WebRequest -UseBasicParsing -Uri 'https://pkgs.tailscale.com/stable/' -ErrorAction Stop).Content
    $best = [regex]::Matches($page, "tailscale-setup-(\d+\.\d+\.\d+)-$arch\.msi") |
        Sort-Object { [version]$_.Groups[1].Value } -Descending | Select-Object -First 1
    if (-not $best) { Fail 'Не нашёл установщик Tailscale на pkgs.tailscale.com. Поставь Tailscale вручную (tailscale.com/download) и запусти install.bat ещё раз.' }
    $msi = Join-Path $env:TEMP $best.Value
    Invoke-WebRequest -UseBasicParsing -Uri ('https://pkgs.tailscale.com/stable/' + $best.Value) -OutFile $msi -ErrorAction Stop
    Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart TS_NOLAUNCH=1" -Wait | Out-Null
    if (-not (Test-Path $TsExe)) { Fail 'Tailscale не установился. Поставь его вручную (tailscale.com/download) и запусти install.bat ещё раз.' }
}

function Get-TsState {
    try {
        $json = (& $TsExe status --json 2>$null | Out-String)
        if (-not $json.Trim()) { return 'Unknown' }
        return ($json | ConvertFrom-Json).BackendState
    } catch { return 'Unknown' }
}

function Get-TsIp {
    try {
        $o = & $TsExe ip -4 2>$null
        $ip = "$(@($o)[0])".Trim()
        if ($ip -match '^100\.\d+\.\d+\.\d+$') { return $ip }
    } catch {}
    return $null
}

function Connect-Tailscale($authKey) {
    for ($i = 0; $i -lt 20; $i++) { if ((Get-TsState) -ne 'Unknown') { break }; Start-Sleep 1 }
    if ((Get-TsState) -eq 'Running') {
        & $TsExe set --unattended=true 2>$null | Out-Null
        return
    }
    if ($authKey) {
        & $TsExe up "--auth-key=$authKey" --unattended --hostname=dacha-pc
    } else {
        Write-Host '    Сейчас появится ссылка для входа. Открой её в браузере и войди в ТОТ ЖЕ аккаунт Tailscale, что и на сервере.' -ForegroundColor Yellow
        & $TsExe up --unattended --hostname=dacha-pc
    }
    if ($LASTEXITCODE -ne 0) { Fail 'Не удалось подключить Tailscale (проверь ключ или повтори вход).' }
}

# ----------------------------------------------------------------------------- остановка агента
function Stop-Agent {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath -like "$InstallDir\*") -or
        ($_.CommandLine -and $_.CommandLine -like "*$InstallDir\run-agent.ps1*")
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# ============================================================================= основной ход
try {
    Write-Host ''
    Write-Host '  Дача: установка агента' -ForegroundColor White
    Write-Host '  ----------------------'

    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { Fail 'Нужны права администратора. Запусти install.bat двойным кликом и нажми «Да».' }

    # ---- 1. вопросы (все сразу, чтобы потом можно было отойти)
    $update  = Test-Path $EnvFile
    $token   = $null
    $allowed = $null
    $authKey = ''

    if ($update) {
        Say 'Найдена прежняя установка: обновлю код, настройки и токен сохраню'
        $token = Get-EnvValue $EnvFile 'AGENT_TOKEN'
        if (-not $token) { Fail "Файл настроек повреждён. Удали папку $InstallDir и запусти установку заново." }
    } else {
        Say 'Несколько вопросов (после них можно отойти, дальше всё пойдёт само)'
        Write-Host ''
        Write-Host '  1) Токен агента. Ты сохранил его при установке сервера. Вставь сюда (символы не показываются).'
        for ($try = 1; $try -le 3 -and -not $token; $try++) {
            $t = (ReadSecret '     Токен агента').Trim()
            Write-Host "     Принято символов: $($t.Length)"
            if ($t.Length -ge 24 -and $t -match '^[A-Za-z0-9_\-]+$') { $token = $t }
            else { Warn 'Токен выглядит неверно (около 43 латинских букв и цифр, без пробелов). Попробуй ещё раз.' }
        }
        if (-not $token) { Fail 'Токен не введён.' }

        Write-Host ''
        Write-Host '  2) Ключ Tailscale (tskey-auth-...) из панели Tailscale: Settings -> Keys -> Generate auth key.'
        Write-Host '     Если ключа нет, просто нажми Enter: вход будет через браузер.'
        $authKey = (ReadSecret '     Ключ Tailscale').Trim()
        if ($authKey -and $authKey -notmatch '^tskey-') {
            Warn 'Это не похоже на ключ Tailscale, войдём через браузер.'
            $authKey = ''
        }

        Write-Host ''
        Write-Host '  3) Какие папки открыть для доступа с сайта. Сейчас откроется окно выбора.'
        Write-Host '     Выбирай только то, что действительно нужно: через сайт эти папки можно смотреть, скачивать и менять.'
        $folders = Select-Folders
        $allowed = Build-AllowedDirs $folders
    }

    # ---- 2. Python
    Say 'Проверяю Python'
    $py = Find-Python
    if (-not $py) { Warn 'Python 3.10+ не найден, устанавливаю...'; $py = Install-Python }
    if (-not $py) { Fail 'Не получилось установить Python. Поставь его вручную с python.org (галочка "Add python.exe to PATH") и запусти install.bat ещё раз.' }
    Ok "Python: $py"

    # ---- 3. код с GitHub
    Say "Скачиваю код с GitHub ($Repo)"
    $zip = Join-Path $env:TEMP 'dacha-src.zip'
    $ext = Join-Path $env:TEMP 'dacha-src'
    if (Test-Path $ext) { Remove-Item $ext -Recurse -Force }
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip -ErrorAction Stop
    Expand-Archive -Path $zip -DestinationPath $ext -Force
    $src = Get-ChildItem $ext -Directory | Select-Object -First 1
    if (-not $src -or -not (Test-Path (Join-Path $src.FullName 'agent\agent.py'))) {
        Fail 'В репозитории нет agent\agent.py. Проверь, что файлы загружены на GitHub.'
    }

    Stop-Agent
    New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null
    robocopy (Join-Path $src.FullName 'agent') $AgentDir /E /XD '.venv' '__pycache__' /XF '.env' /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Не удалось скопировать файлы (robocopy, код $LASTEXITCODE)." }
    foreach ($f in 'run-agent.ps1', 'uninstall.ps1', 'uninstall.bat') {
        Copy-Item (Join-Path $src.FullName "install\$f") (Join-Path $InstallDir $f) -Force
    }
    Ok "Файлы скопированы в $InstallDir"

    # ---- 4. окружение Python
    Say 'Готовлю окружение Python (1-2 минуты)'
    $venvPy = Join-Path $AgentDir '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        & $py -m venv (Join-Path $AgentDir '.venv')
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail 'Не удалось создать окружение Python.' }
    }
    & $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $AgentDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Fail 'Не удалось установить библиотеки (pip). Проверь интернет и запусти установку ещё раз.' }
    Ok 'Библиотеки установлены'

    # ---- 5. настройки
    if (-not $update) {
        $envText = (@(
            "AGENT_TOKEN=$token",
            'AGENT_HOST=127.0.0.1',
            "AGENT_PORT=$Port",
            "ALLOWED_DIRS=$allowed",
            'SCREEN_BACKEND=mss',
            'MAX_UPLOAD_MB=2048'
        ) -join "`r`n") + "`r`n"
        # UTF-8 БЕЗ BOM: иначе агент не прочитает первую строку
        [IO.File]::WriteAllText($EnvFile, $envText, (New-Object Text.UTF8Encoding($false)))
        $mySid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        & icacls $EnvFile /inheritance:r /grant:r "*${mySid}:(F)" '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
        Ok 'Настройки сохранены (доступ к файлу только у тебя и администраторов)'
    }

    # ---- 6. Tailscale
    Say 'Подключаю VPN (Tailscale)'
    if (-not (Test-Path $TsExe)) { Install-Tailscale }
    try { Start-Service -Name 'Tailscale' -ErrorAction SilentlyContinue } catch {}
    Connect-Tailscale $authKey
    $ip = $null
    for ($i = 0; $i -lt 30 -and -not $ip; $i++) { $ip = Get-TsIp; if (-not $ip) { Start-Sleep 2 } }
    if (-not $ip) { Fail 'Tailscale подключился, но адрес 100.x.x.x не появился. Подожди минуту и запусти install.bat ещё раз.' }
    Ok "Адрес этого компьютера в VPN: $ip"

    # ---- 7. файрвол: порт агента открыт только для адресов Tailscale
    Say 'Настраиваю брандмауэр Windows'
    Remove-NetFirewallRule -DisplayName $TaskName -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $TaskName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress '100.64.0.0/10' -Profile Any | Out-Null
    Ok "Порт $Port открыт только для сети Tailscale"

    # ---- 8. автозапуск при входе в Windows
    Say 'Настраиваю автозапуск'
    $user      = "$env:USERDOMAIN\$env:USERNAME"
    $action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$InstallDir\run-agent.ps1`""
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Ok 'Агент будет запускаться при каждом входе в Windows'

    # ---- 9. проверка
    Say 'Проверяю, что агент отвечает'
    $healthy = $false
    for ($i = 0; $i -lt 30 -and -not $healthy; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "http://${ip}:$Port/health" -Headers @{ 'X-Agent-Token' = $token } -TimeoutSec 3 -ErrorAction Stop
            if ($r.StatusCode -eq 200) { $healthy = $true }
        } catch { Start-Sleep 2 }
    }
    if (-not $healthy) {
        Warn 'Агент пока не ответил. Последние строки его журнала:'
        foreach ($lf in 'agent.err.log', 'agent.out.log') {
            $p = Join-Path $InstallDir $lf
            if (Test-Path $p) { Get-Content $p -Tail 15 | ForEach-Object { Write-Host "      $_" } }
        }
        Fail "Агент не запустился. Пришли мне текст из этого окна и файл $InstallDir\install.log"
    }
    Ok 'Агент работает'

    # ---- итог
    Write-Host ''
    Write-Host '==================== ГОТОВО ====================' -ForegroundColor Green
    Write-Host ''
    Write-Host '  ОСТАЛСЯ ПОСЛЕДНИЙ ШАГ. Выполни эту команду на сервере (по SSH):' -ForegroundColor White
    Write-Host ''
    Write-Host "      dacha-set-agent $ip" -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  Чтобы дача сама поднималась после перезагрузки или отключения света:'
    Write-Host '    - включи автоматический вход в Windows (Win+R, команда  netplwiz,'
    Write-Host '      снять галочку «Требовать ввод имени и пароля»);'
    Write-Host '    - в BIOS поставь «включаться при появлении питания» (AC Power Recovery).'
    Write-Host '  Экран виден, пока Windows не заблокирован (на экране блокировки картинка чёрная).'
    Write-Host ''
    Write-Host "  Обновить: запусти install.bat ещё раз. Удалить: $InstallDir\uninstall.bat"
    Write-Host '  Старый способ удалённого доступа пока не удаляй, пока всё не проверишь.'
    Write-Host ''
}
catch {
    Write-Host ''
    Write-Host "ОШИБКА: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Подробности записаны в $InstallDir\install.log" -ForegroundColor Red
    try { $_ | Out-String | Write-Host } catch {}
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
}
