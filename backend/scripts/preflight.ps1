# Vérification avant démo : chaque écran est-il vraiment joignable depuis le navigateur ?
#
# Docker Desktop sous Windows publie les ports à travers un relais (`wslrelay`)
# qui perd par intermittence sa liaison quand des conteneurs sont recréés coup
# sur coup. Le conteneur reste "healthy" et répond correctement sur son propre
# réseau — `docker compose ps` ne montre donc aucune anomalie — mais le
# navigateur reçoit ERR_EMPTY_RESPONSE. Redémarrer le service rétablit la
# liaison.
#
# Ce script teste chaque port publié COMME LE FERAIT UN NAVIGATEUR, et répare
# ce qui ne répond pas.
#
#   .\scripts\preflight.ps1           vérifie et répare
#   .\scripts\preflight.ps1 -Check    signale seulement, code de sortie 1 si KO
#
# Version PowerShell de preflight.sh : sous cmd.exe, `bash` désigne le lanceur
# WSL et non Git Bash, et la distribution `docker-desktop` n'a pas /bin/bash.

[CmdletBinding()]
param([switch]$Check)

Set-Location (Join-Path $PSScriptRoot "..")

# Le fichier compose vit à la racine du dépôt : il construit ./backend ET
# ./frontend, il ne peut donc pas tenir dans l'un des deux.
$composeFile = Join-Path $PSScriptRoot "..\..\docker-compose.yml"
$composeArgs = @("-f", $composeFile, "--profile", "tools", "--profile", "monitoring")

# service, port, chemin, libellé — c'est l'URL qui compte, pas l'état du conteneur.
# Flower, Prometheus et Grafana sont derrière des profils compose et ne tournent
# généralement pas ; une cible dont le conteneur est absent est signalée comme
# telle et jamais comptée en échec. Signaler un service optionnel comme tombé,
# c'est la façon la plus sûre qu'une checklist cesse d'être lue.
$targets = @(
    @{ Service = "frontend";   Port = 3000; Path = "/";            Label = "Interface" },
    @{ Service = "frontend";   Port = 3000; Path = "/api/health";  Label = "Proxy API (nginx -> api)" },
    @{ Service = "api";        Port = 8000; Path = "/health";      Label = "API" },
    @{ Service = "mailpit";    Port = 8025; Path = "/";            Label = "Mailpit (emails)" },
    @{ Service = "minio";      Port = 9001; Path = "/";            Label = "MinIO (console)" },
    @{ Service = "flower";     Port = 5555; Path = "/";            Label = "Flower (files Celery)" },
    @{ Service = "prometheus"; Port = 9090; Path = "/-/ready";     Label = "Prometheus" },
    @{ Service = "grafana";    Port = 3001; Path = "/api/health";  Label = "Grafana" }
)

function Test-Endpoint {
    param([int]$Port, [string]$Path)
    # 127.0.0.1 et non localhost : localhost se résout d'abord en ::1, et c'est
    # précisément la liaison IPv6 qui tombe. Toute réponse HTTP — même 404 —
    # prouve que le port est lié ; seul un silence complet est un échec.
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:$Port$Path" -TimeoutSec 6 `
            -UseBasicParsing -ErrorAction Stop
        return $true
    } catch [System.Net.WebException] {
        # Une réponse d'erreur HTTP reste une réponse : la liaison est vivante.
        return $null -ne $_.Exception.Response
    } catch {
        return $null -ne $_.Exception.Response
    }
}

function Test-ServiceRunning {
    param([string]$Service)
    $id = & docker compose @composeArgs ps -q $Service 2>$null
    return -not [string]::IsNullOrWhiteSpace($id)
}

$failed = 0
$repaired = 0

foreach ($t in $targets) {
    if (-not (Test-ServiceRunning -Service $t.Service)) {
        Write-Host ("  --    {0,-28} hors profil, non démarré" -f $t.Label) -ForegroundColor DarkGray
        continue
    }

    if (Test-Endpoint -Port $t.Port -Path $t.Path) {
        Write-Host ("  OK    {0,-28} http://localhost:{1}" -f $t.Label, $t.Port) -ForegroundColor Green
        continue
    }

    if ($Check) {
        Write-Host ("  DOWN  {0,-28} le port {1} ne répond pas" -f $t.Label, $t.Port) -ForegroundColor Red
        $failed++
        continue
    }

    Write-Host ("  ..    {0,-28} port {1} muet, redémarrage de '{2}'" -f $t.Label, $t.Port, $t.Service) -ForegroundColor Yellow
    docker compose @composeArgs restart $t.Service *>$null
    Start-Sleep -Seconds 8

    if (Test-Endpoint -Port $t.Port -Path $t.Path) {
        Write-Host ("  OK    {0,-28} réparé" -f $t.Label) -ForegroundColor Green
        $repaired++
    } else {
        Write-Host ("  KO    {0,-28} toujours muet — voir : docker compose -f ..\..\docker-compose.yml logs {1}" -f $t.Label, $t.Service) -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
if ($repaired -gt 0) { Write-Host "$repaired service(s) réparé(s)." }
if ($failed -gt 0) {
    Write-Host "$failed service(s) indisponible(s)." -ForegroundColor Red
    exit 1
}
Write-Host "Tout est joignable depuis le navigateur." -ForegroundColor Green
