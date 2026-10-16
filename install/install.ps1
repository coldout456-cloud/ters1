# Установщик агента «Дача» для Windows 10/11.
#
# Обычно запускается из персонального установщика, который выдаёт сайт (dacha-install.bat):
# адрес сайта, токен и ключ Tailscale приходят через переменные окружения DACHA_SERVER,
# DACHA_TOKEN, DACHA_TSKEY, и вопросов нет вообще.
# Ручной запуск (install.bat с GitHub) задаёт три вопроса.
# Повторный запуск безопасен: папки и настройки сохраняются, обновляется код.

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo       = 'coldout456-cloud/ters1'
$Branch     = 'main'
# Папка AppData\Local скрыта в проводнике по умолчанию (нужно включить «Показывать скрытые
# элементы»), поэтому агент здесь, а не в C:\ или на рабочем столе — заметить его случайно сложнее.
$InstallDir = Join-Path $env:LOCALAPPDATA 'DachaAgent'
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

# ----------------------------------------------------------------------------- ввод и настройки
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

function Set-EnvValue($file, $key, $value) {
    $lines = @()
    if (Test-Path $file) { $lines = [IO.File]::ReadAllLines($file) }
    $found = $false
    $out = @(foreach ($l in $lines) {
        if ($l -match "^\s*$key\s*=") { $found = $true; "$key=$value" } else { $l }
    })
    if (-not $found) { $out += "$key=$value" }
    # UTF-8 БЕЗ BOM: иначе агент не прочитает первую строку
    [IO.File]::WriteAllText($file, (($out -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding($false)))
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

# Папки по умолчанию для режима «в один клик»: стандартные папки пользователя (если они есть).
# Изменить потом: файл %LOCALAPPDATA%\DachaAgent\agent\.env, строка ALLOWED_DIRS.
function Get-DefaultFolders {
    $cands = [ordered]@{
        documents = [Environment]::GetFolderPath('MyDocuments')
        desktop   = [Environment]::GetFolderPath('DesktopDirectory')
        pictures  = [Environment]::GetFolderPath('MyPictures')
        videos    = [Environment]::GetFolderPath('MyVideos')
        music     = [Environment]::GetFolderPath('MyMusic')
        downloads = (Join-Path $env:USERPROFILE 'Downloads')
    }
    $parts = @()
    foreach ($k in $cands.Keys) {
        $p = $cands[$k]
        if ($p -and (Test-Path $p -PathType Container) -and -not (Test-FolderAllowed $p)) { $parts += "$k=$p" }
    }
    return ($parts -join ';')
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
    if (-not $best) { Fail 'Не нашёл установщик Tailscale на pkgs.tailscale.com. Поставь Tailscale вручную (tailscale.com/download) и запусти установщик ещё раз.' }
    $msi = Join-Path $env:TEMP $best.Value
    Invoke-WebRequest -UseBasicParsing -Uri ('https://pkgs.tailscale.com/stable/' + $best.Value) -OutFile $msi -ErrorAction Stop
    Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart TS_NOLAUNCH=1" -Wait | Out-Null
    if (-not (Test-Path $TsExe)) { Fail 'Tailscale не установился. Поставь его вручную (tailscale.com/download) и запусти установщик ещё раз.' }
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
        if ($LASTEXITCODE -ne 0) { Fail 'Не удалось подключить Tailscale: ключ неверный, просрочен или уже использован. Создай новый ключ (Reusable) и выполни на сервере dacha-set-tskey.' }
        return
    }

    # Ключа нет: запускаем вход и сами открываем страницу входа в браузере
    $outf = Join-Path $env:TEMP 'ts-up.out'
    $errf = Join-Path $env:TEMP 'ts-up.err'
    Remove-Item $outf, $errf -Force -ErrorAction SilentlyContinue
    $p = Start-Process -FilePath $TsExe -ArgumentList 'up', '--unattended', '--hostname=dacha-pc' -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $outf -RedirectStandardError $errf
    $opened   = $false
    $deadline = (Get-Date).AddMinutes(10)
    while (-not $p.HasExited -and (Get-Date) -lt $deadline) {
        if (-not $opened) {
            $txt = ''
            foreach ($f in $outf, $errf) { if (Test-Path $f) { $txt += (Get-Content $f -Raw -ErrorAction SilentlyContinue) } }
            if ($txt -match '(https://login\.tailscale\.com/\S+)') {
                Write-Host '    Открываю страницу входа в Tailscale. Войди в ТОТ ЖЕ аккаунт, что и на сервере, и нажми «Connect».' -ForegroundColor Yellow
                Start-Process $Matches[1]
                $opened = $true
            }
        }
        Start-Sleep 1
    }
    if (-not $p.HasExited) { try { $p.Kill() } catch {}; Fail 'Вход в Tailscale не завершён за 10 минут. Запусти установщик ещё раз.' }
    $p.WaitForExit()
    if ($p.ExitCode -ne 0) { Fail 'Не удалось подключить Tailscale. Запусти установщик ещё раз.' }
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
$exitCode = 0
try {
    Write-Host ''
    Write-Host '  Дача: установка агента' -ForegroundColor White
    Write-Host '  ----------------------'

    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { Fail 'Нужны права администратора. Запусти установщик двойным кликом и нажми «Да».' }

    # ---- 1. исходные данные: из персонального установщика (без вопросов) или вопросами
    $envServer = "$env:DACHA_SERVER".Trim()
    $envToken  = "$env:DACHA_TOKEN".Trim()
    $envTsKey  = "$env:DACHA_TSKEY".Trim()
    $oneClick  = ($envServer -match '^https://[A-Za-z0-9.\-]+(:\d+)?$') -and ($envToken -match '^[A-Za-z0-9_\-]{24,}$')

    $update     = Test-Path $EnvFile
    $token      = $null
    $allowed    = $null
    $authKey    = ''
    $serverUrl  = ''

    if ($oneClick) {
        $token     = $envToken
        $serverUrl = $envServer
        if ($envTsKey -match '^tskey-') { $authKey = $envTsKey }
        # Без ключа Tailscale подключение потребовало бы входа через браузер (см. Connect-Tailscale) —
        # это ровно то видимое окно, которого установка «в один клик» должна избегать. Поэтому если
        # Tailscale ещё не подключён и ключа нет, останавливаемся сразу и тихо (сообщение уйдёт только
        # в install.log): лучше ничего не показать, чем неожиданно открыть браузер с чужим окном входа.
        if (-not $authKey -and (Get-TsState) -ne 'Running') {
            Fail 'Нужен ключ Tailscale для бесшумной установки (TAILSCALE_AUTHKEY на сервере пуст или устарел). Выполни на сервере: dacha-set-tskey, и скачай установщик на сайте заново.'
        }
        if ($update) {
            Say 'Найдена прежняя установка: обновлю код и заново свяжу компьютер с сайтом'
        } else {
            Say 'Устанавливаю без вопросов: настройки взяты из установщика с сайта'
            $allowed = Get-DefaultFolders
            if (-not $allowed) { Fail 'Не нашёл ни одной подходящей папки пользователя (Документы, Рабочий стол и т.д.).' }
        }
    } elseif ($update) {
        Say 'Найдена прежняя установка: обновлю код, настройки и токен сохраню'
        $token     = Get-EnvValue $EnvFile 'AGENT_TOKEN'
        $serverUrl = "$(Get-EnvValue $EnvFile 'SERVER_URL')"
        if (-not $token) { Fail "Файл настроек повреждён. Удали папку $InstallDir и запусти установку заново." }
    } else {
        Say 'Несколько вопросов (после них можно отойти, дальше всё пойдёт само)'
        Write-Host ''
        Write-Host '  Совет: проще скачать персональный установщик на сайте (вкладка «Установка»), тогда вопросов не будет.'
        Write-Host ''
        Write-Host '  1) Токен агента. Вставь сюда (символы не показываются).'
        for ($try = 1; $try -le 3 -and -not $token; $try++) {
            $t = (ReadSecret '     Токен агента').Trim()
            Write-Host "     Принято символов: $($t.Length)"
            if ($t.Length -ge 24 -and $t -match '^[A-Za-z0-9_\-]+$') { $token = $t }
            else { Warn 'Токен выглядит неверно (около 43 латинских букв и цифр, без пробелов). Попробуй ещё раз.' }
        }
        if (-not $token) { Fail 'Токен не введён.' }

        Write-Host ''
        Write-Host '  2) Адрес сайта (https://...), чтобы дача сама сообщила ему свой адрес. Enter - пропустить.'
        $s = (Read-Host '     Адрес сайта').Trim().TrimEnd('/')
        if ($s -match '^https://[A-Za-z0-9.\-]+(:\d+)?$') { $serverUrl = $s }
        elseif ($s) { Warn 'Это не похоже на адрес вида https://имя.duckdns.org, пропускаю.' }

        Write-Host ''
        Write-Host '  3) Ключ Tailscale (tskey-auth-...). Если ключа нет, нажми Enter: вход будет через браузер.'
        $authKey = (ReadSecret '     Ключ Tailscale').Trim()
        if ($authKey -and $authKey -notmatch '^tskey-') {
            Warn 'Это не похоже на ключ Tailscale, войдём через браузер.'
            $authKey = ''
        }

        Write-Host ''
        Write-Host '  4) Какие папки открыть для доступа с сайта. Сейчас откроется окно выбора.'
        Write-Host '     Через сайт эти папки можно смотреть, скачивать и менять: выбирай только нужное.'
        $folders = Select-Folders
        $allowed = Build-AllowedDirs $folders
    }

    # ---- 2. Python
    Say 'Проверяю Python'
    $py = Find-Python
    if (-not $py) { Warn 'Python 3.10+ не найден, устанавливаю...'; $py = Install-Python }
    if (-not $py) { Fail 'Не получилось установить Python. Поставь его вручную с python.org (галочка "Add python.exe to PATH") и запусти установщик ещё раз.' }
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
        $lines = @(
            "AGENT_TOKEN=$token",
            'AGENT_HOST=127.0.0.1',
            "AGENT_PORT=$Port",
            "ALLOWED_DIRS=$allowed",
            'ALLOW_ALL_FILES=1',   # виден весь диск целиком (а не только папки выше)
            'INPUT_ENABLED=1',     # разрешено управление мышью с сайта
            'SCREEN_BACKEND=mss',
            'MAX_UPLOAD_MB=2048'
        )
        if ($serverUrl) { $lines += "SERVER_URL=$serverUrl" }
        [IO.File]::WriteAllText($EnvFile, (($lines -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding($false)))
        $mySid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        & icacls $EnvFile /inheritance:r /grant:r "*${mySid}:(F)" '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
        Ok 'Настройки сохранены (доступ к файлу только у тебя и администраторов)'
    } elseif ($oneClick) {
        Set-EnvValue $EnvFile 'AGENT_TOKEN' $token
        Set-EnvValue $EnvFile 'SERVER_URL' $serverUrl
        Ok 'Токен и адрес сайта обновлены, папки сохранены'
    }

    # ---- 6. Tailscale
    Say 'Подключаю VPN (Tailscale)'
    if (-not (Test-Path $TsExe)) { Install-Tailscale }
    try { Start-Service -Name 'Tailscale' -ErrorAction SilentlyContinue } catch {}
    Connect-Tailscale $authKey
    # Соединение держит служба Tailscale, а не оконное приложение. Закрываем окно
    # приложения, если оно открылось при установке, чтобы на экране ничего не висело.
    Get-Process tailscale-ipn -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $ip = $null
    for ($i = 0; $i -lt 30 -and -not $ip; $i++) { $ip = Get-TsIp; if (-not $ip) { Start-Sleep 2 } }
    if (-not $ip) { Fail 'Tailscale подключился, но адрес 100.x.x.x не появился. Подожди минуту и запусти установщик ещё раз.' }
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
    # RunLevel Highest: если пользователь — администратор, агент запускается с
    # повышенными правами БЕЗ запроса UAC (так решает планировщик). Это нужно,
    # чтобы кнопка удаления с сайта могла сама снять автозапуск и правило брандмауэра,
    # и чтобы были видны защищённые системные файлы. Если пользователь не админ —
    # работает как обычно, без повышения.
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Ok 'Агент будет запускаться при каждом входе в Windows'

    # ---- 9. проверка, что агент отвечает
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

    # ---- 10. сообщаем сайту адрес (сам агент тоже будет делать это раз в 5 минут)
    $registered = $false
    if ($serverUrl) {
        Say 'Сообщаю сайту адрес этого компьютера'
        $body = @{ host = $ip; port = $Port } | ConvertTo-Json -Compress
        for ($i = 0; $i -lt 5 -and -not $registered; $i++) {
            try {
                Invoke-RestMethod -Method Post -Uri "$serverUrl/api/agent/register" -Headers @{ 'X-Agent-Token' = $token } `
                    -ContentType 'application/json' -Body $body -TimeoutSec 15 -ErrorAction Stop | Out-Null
                $registered = $true
            } catch { Start-Sleep 3 }
        }
        if ($registered) { Ok 'Сайт увидел дачный компьютер' }
        else { Warn 'Сайт пока не ответил. Агент будет пробовать сам каждую минуту.' }
    }

    # ---- итог
    Write-Host ''
    Write-Host '==================== ГОТОВО ====================' -ForegroundColor Green
    Write-Host ''
    if ($registered) {
        Write-Host "  Дача подключена. Открой сайт $serverUrl : вкладки «Файлы» и «Экран» уже работают." -ForegroundColor White
    } elseif ($serverUrl) {
        Write-Host "  Через минуту обнови сайт $serverUrl : дача должна появиться сама." -ForegroundColor White
        Write-Host "  Если не появится, выполни на сервере (по SSH):  dacha-set-agent $ip" -ForegroundColor Yellow
    } else {
        Write-Host '  ОСТАЛСЯ ПОСЛЕДНИЙ ШАГ. Выполни эту команду на сервере (по SSH):' -ForegroundColor White
        Write-Host ''
        Write-Host "      dacha-set-agent $ip" -ForegroundColor Yellow
    }
    Write-Host ''
    $autoLogon = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -ErrorAction SilentlyContinue).AutoAdminLogon
    if ($autoLogon -ne '1') {
        Write-Host '  Чтобы дача сама поднималась после перезагрузки или отключения света (один раз):' -ForegroundColor Yellow
        Write-Host '    - включи автоматический вход в Windows: Win+R, команда  netplwiz ,'
        Write-Host '      снять галочку «Требовать ввод имени и пароля»;'
        Write-Host '    - в BIOS поставь «включаться при появлении питания» (AC Power Recovery).'
    } else {
        Write-Host '  Автоматический вход в Windows включён: дача поднимется сама после перезагрузки.'
    }
    Write-Host '  Экран виден, пока Windows не заблокирован (на экране блокировки картинка чёрная).'
    Write-Host ''
    Write-Host "  Обновить: скачай установщик на сайте ещё раз. Удалить: $InstallDir\uninstall.bat"
    Write-Host ''
}
catch {
    $exitCode = 1
    Write-Host ''
    Write-Host "ОШИБКА: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Подробности записаны в $InstallDir\install.log" -ForegroundColor Red
    try { $_ | Out-String | Write-Host } catch {}
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
}
if ($MyInvocation.MyCommand.Path) { exit $exitCode }
