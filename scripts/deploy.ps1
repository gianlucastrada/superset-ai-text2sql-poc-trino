<#
.SYNOPSIS
  Costruisce l'immagine Superset con l'estensione AI e distribuisce Postgres + Superset
  nel namespace "superset-ai" del Kubernetes di Docker Desktop.

.PARAMETER OllamaUrl
  Indirizzo di Ollama raggiungibile DAI POD (default: la Spark, porta 11434).

.PARAMETER Model
  Modello Ollama da usare (default: qwen3.6:35b).

.PARAMETER SkipBuild
  Non ricostruisce l'immagine (usa quella gia' presente).

.PARAMETER Forward
  Alla fine avvia il port-forward su http://localhost:8088

.PARAMETER Config
  Cartella con la configurazione del database (default: config\vendite): database.env
  (impostazioni CHIAVE=valore), istruzioni.md (regole di business per il modello),
  esempi-domande.txt ed esempi-dashboard.txt (pulsanti di esempio). Tutti facoltativi.

.PARAMETER SenzaTrino
  Non avvia Trino (e lo rimuove se c'era): l'AI usa solo PostgreSQL diretto.
  Trino parte comunque solo se il cluster ha almeno 6 GB di memoria.

.EXAMPLE
  .\scripts\deploy.ps1
  .\scripts\deploy.ps1 -Forward
  .\scripts\deploy.ps1 -OllamaUrl http://host.docker.internal:11435
  .\scripts\deploy.ps1 -SenzaTrino
  .\scripts\deploy.ps1 -Config config\altro-database
#>
[CmdletBinding()]
param(
  [string]$OllamaUrl = "http://10.100.208.51:11434",
  [string]$Model = "qwen3.6:35b",
  [switch]$SkipBuild,
  [switch]$Forward,
  [switch]$SenzaTrino,
  [string]$Config = "config\vendite"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Ns = "superset-ai"
$Image = "superset-ai:0.1"

function Invoke-Native {
  param([string]$Exe, [string[]]$Arguments)
  & $Exe @Arguments
  if ($LASTEXITCODE -ne 0) { throw "'$Exe $($Arguments -join ' ')' e' fallito (exit code $LASTEXITCODE)." }
}

function Test-Kube {
  # True se il comando kubectl riesce; silenzioso e senza eccezioni (PowerShell 5.1)
  param([string[]]$Arguments)
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { & kubectl @Arguments 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
  finally { $ErrorActionPreference = $old }
}

function Apply-Yaml {
  # Applica YAML generato con --dry-run=client (ConfigMap da file o letterali)
  param([string[]]$CreateArgs)
  $yaml = & kubectl @CreateArgs --dry-run=client -o yaml
  if ($LASTEXITCODE -ne 0) { throw "Generazione YAML fallita: kubectl $($CreateArgs -join ' ')" }
  $yaml | & kubectl apply -f -
  if ($LASTEXITCODE -ne 0) { throw "kubectl apply fallito per: $($CreateArgs -join ' ')" }
}

function New-Secret([int]$Length) {
  $chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
  $bytes = New-Object byte[] $Length
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($bytes)
  $rng.Dispose()
  return -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function Get-ClusterMemoryGB {
  # Memoria allocabile del nodo Kubernetes (es. "8031452Ki"), in GB. 0 se non leggibile.
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $m = (& kubectl get nodes -o "jsonpath={.items[0].status.allocatable.memory}" 2>$null)
  } finally { $ErrorActionPreference = $old }
  if (-not $m -or $m -notmatch '^(\d+)(Ki|Mi|Gi)?$') { return 0 }
  $n = [double]$Matches[1]
  switch ($Matches[2]) {
    "Ki" { return [math]::Round($n / 1MB, 1) }
    "Mi" { return [math]::Round($n / 1KB, 1) }
    "Gi" { return $n }
    default { return [math]::Round($n / 1GB, 1) }
  }
}

foreach ($c in @("docker", "kubectl")) {
  if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { throw "Comando '$c' non trovato nel PATH." }
}
$ctx = (& kubectl config current-context) 2>$null
Write-Host "Contesto kubectl: $ctx"
if ($ctx -ne "docker-desktop") {
  Write-Warning "Il contesto non e' 'docker-desktop': l'immagine locale potrebbe non essere visibile al cluster."
}

# Trino solo se richiesto e se c'e' memoria (Postgres + Superset + Trino stanno in circa 5-6 GB)
$memGB = Get-ClusterMemoryGB
$conTrino = -not $SenzaTrino
if ($conTrino -and $memGB -gt 0 -and $memGB -lt 6) {
  Write-Warning "Il cluster ha solo $memGB GB di memoria: Trino NON viene avviato (servono almeno 6 GB). Aumenta la memoria di Docker Desktop/WSL2 e rilancia."
  $conTrino = $false
}
Write-Host "Memoria del cluster: $(if ($memGB -gt 0) { "$memGB GB" } else { 'non rilevata' }) - Trino: $(if ($conTrino) { 'si' } else { 'no' })"

# 1. Immagine Superset
if (-not $SkipBuild) {
  Write-Host "`n==> docker build $Image (la prima volta scarica apache/superset:6.1.0, circa 1-2 GB)"
  Invoke-Native docker @("build", "-t", $Image, (Join-Path $Root "superset"))
}

# 2. Namespace
Write-Host "`n==> Namespace $Ns"
Invoke-Native kubectl @("apply", "-f", (Join-Path $Root "k8s\00-namespace.yaml"))

# 3. Secret con le password (creato una sola volta: Postgres le usa solo alla prima inizializzazione)
$adminPassword = $null
if (Test-Kube @("get", "secret", "superset-ai-secrets", "-n", $Ns)) {
  Write-Host "Secret gia' presente: password invariate."
} else {
  Write-Host "`n==> Creo il Secret con password casuali"
  $adminPassword = New-Secret 16
  Invoke-Native kubectl @("create", "secret", "generic", "superset-ai-secrets", "-n", $Ns,
    "--from-literal=POSTGRES_PASSWORD=$(New-Secret 24)",
    "--from-literal=SUPERSET_DB_PASSWORD=$(New-Secret 24)",
    "--from-literal=AI_READER_PASSWORD=$(New-Secret 24)",
    "--from-literal=SUPERSET_SECRET_KEY=$(New-Secret 48)",
    "--from-literal=ADMIN_PASSWORD=$adminPassword")
}

# 4. ConfigMap: script di inizializzazione Postgres e impostazioni di Ollama
Write-Host "`n==> ConfigMap"
Apply-Yaml @("create", "configmap", "postgres-init", "-n", $Ns, "--from-file=$(Join-Path $Root 'postgres\init')")
if ($conTrino) {
  $trinoAi = "trino://ai@trino:8080/postgresql/public"
  $trinoSuperset = "trino://superset@trino:8080/postgresql"
} else {
  $trinoAi = ""
  $trinoSuperset = ""
}
$cfgArgs = @("create", "configmap", "superset-ai-config", "-n", $Ns,
  "--from-literal=OLLAMA_URL=$OllamaUrl", "--from-literal=OLLAMA_MODEL=$Model",
  "--from-literal=AI_TRINO_URI=$trinoAi", "--from-literal=AI_TRINO_SUPERSET_URI=$trinoSuperset")
# Configurazione del database: impostazioni, regole di business ed esempi (nessuno e' obbligatorio)
$cfgDir = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Root $Config }
if (-not (Test-Path $cfgDir)) { throw "Cartella di configurazione non trovata: $cfgDir" }
Write-Host "Configurazione del database: $cfgDir"
$envFile = Join-Path $cfgDir "database.env"
if (Test-Path $envFile) {
  foreach ($riga in Get-Content $envFile) {
    $r = $riga.Trim()
    if ($r -and -not $r.StartsWith("#") -and $r.Contains("=")) { $cfgArgs += "--from-literal=$r" }
  }
}
$testi = [ordered]@{
  "istruzioni.md"        = "AI_ISTRUZIONI"
  "esempi-domande.txt"   = "AI_ESEMPI_DOMANDE"
  "esempi-dashboard.txt" = "AI_ESEMPI_DASHBOARD"
}
foreach ($nomeFile in $testi.Keys) {
  $percorso = Join-Path $cfgDir $nomeFile
  if (Test-Path $percorso) { $cfgArgs += "--from-file=$($testi[$nomeFile])=$percorso" }
}
Apply-Yaml $cfgArgs

# 5. Postgres
Write-Host "`n==> Postgres"
Invoke-Native kubectl @("apply", "-f", (Join-Path $Root "k8s\10-postgres.yaml"))
Invoke-Native kubectl @("rollout", "status", "deployment/postgres", "-n", $Ns, "--timeout=300s")

# 6. Trino (facoltativo)
if ($conTrino) {
  Write-Host "`n==> Trino (la prima volta scarica trinodb/trino:482, circa 1-2 GB)"
  Invoke-Native kubectl @("apply", "-f", (Join-Path $Root "k8s\30-trino.yaml"))
  if (Test-Kube @("get", "deployment/trino", "-n", $Ns)) {
    # Catalogo cambiato o pod fermato con trino.ps1: riparte con la configurazione attuale
    Invoke-Native kubectl @("scale", "deployment/trino", "-n", $Ns, "--replicas=1")
  }
  & kubectl rollout status deployment/trino -n $Ns --timeout=600s
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "Trino non e' ancora pronto. Controlla: kubectl get pods -n $Ns ; kubectl logs -n $Ns deploy/trino"
  }
} else {
  # Rimuove Trino se c'era (Test-Kube non interrompe lo script in caso di errore)
  Test-Kube @("delete", "-f", (Join-Path $Root "k8s\30-trino.yaml"), "--ignore-not-found") | Out-Null
}

# 7. Superset
Write-Host "`n==> Superset"
$alreadyDeployed = Test-Kube @("get", "deployment/superset", "-n", $Ns)
Invoke-Native kubectl @("apply", "-f", (Join-Path $Root "k8s\20-superset.yaml"))
if ($alreadyDeployed) {
  # Immagine ricostruita con lo stesso tag o impostazioni Ollama cambiate: serve un pod nuovo
  Invoke-Native kubectl @("rollout", "restart", "deployment/superset", "-n", $Ns)
}
Write-Host "Attendo Superset (il primo avvio con inizializzazione puo' richiedere alcuni minuti)..."
& kubectl rollout status deployment/superset -n $Ns --timeout=900s
if ($LASTEXITCODE -ne 0) {
  Write-Warning "Superset non e' ancora pronto. Controlla: kubectl get pods -n $Ns ; kubectl logs -n $Ns deploy/superset -c init"
}

Write-Host ""
Write-Host "Fatto."
Write-Host "  Apri il port-forward:  .\scripts\forward.ps1   ->  http://localhost:8088"
Write-Host "  Utente: admin"
if ($adminPassword) {
  Write-Host "  Password: $adminPassword   (copiala; poi la ritrovi con .\scripts\password.ps1)"
} else {
  Write-Host "  Password: .\scripts\password.ps1"
}
Write-Host "  Ollama: $OllamaUrl  modello: $Model"
if ($conTrino) {
  Write-Host "  Trino: attivo (nella pagina AI scegli il motore). Spegnerlo: .\scripts\trino.ps1 spegni"
}

if ($Forward) { & (Join-Path $PSScriptRoot "forward.ps1") }
