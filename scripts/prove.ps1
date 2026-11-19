<#
.SYNOPSIS
  Prove automatiche del Text-to-SQL: pone al modello le domande di config\<database>\prove.yaml,
  esegue la SQL generata e confronta i risultati con i valori attesi.

.DESCRIPTION
  Le prove girano DENTRO il pod di Superset, con lo stesso modello, lo stesso database e la
  stessa configurazione della pagina AI. Il report viene copiato in prove-risultati\:
    - <data-ora>.md   da leggere (esiti, tempi, SQL delle prove non superate)
    - <data-ora>.json per i confronti; ultimo.json e' sempre l'ultima esecuzione
  Se esiste un'esecuzione precedente, il report dice cosa e' migliorato e cosa e' peggiorato.
  Una prova sulla domanda dura in media 5-10 secondi: tutte le prove su due motori, 10-15 minuti.

.PARAMETER Config
  Cartella della configurazione del database (default: config\vendite).

.PARAMETER Motore
  diretto, trino oppure tutti (default).

.PARAMETER Solo
  Id delle prove da eseguire, separati da virgola; * vale come jolly (es. "1.2,4.*").

.EXAMPLE
  .\scripts\prove.ps1
  .\scripts\prove.ps1 -Solo 1.2,4.5
  .\scripts\prove.ps1 -Motore trino -Solo "10.*"
#>
[CmdletBinding()]
param(
  [string]$Config = "config\vendite",
  [string]$Motore = "tutti",
  [string]$Solo = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Ns = "superset-ai"

function Invoke-Native {
  param([string]$Exe, [string[]]$Arguments)
  & $Exe @Arguments
  if ($LASTEXITCODE -ne 0) { throw "'$Exe $($Arguments -join ' ')' e' fallito (exit code $LASTEXITCODE)." }
}

# kubectl cp non accetta percorsi Windows con la lettera del disco: si lavora con percorsi
# relativi alla cartella del progetto, scritti con "/"
Push-Location $Root
try {
  $file = (Join-Path $Config "prove.yaml") -replace '\\', '/'
  if (-not (Test-Path $file)) { throw "File delle prove non trovato: $file" }

  $pod = (& kubectl get pod -n $Ns -l app=superset --field-selector=status.phase=Running -o "jsonpath={.items[0].metadata.name}")
  if (-not $pod) { throw "Pod di Superset non trovato o non attivo: lancia prima .\scripts\deploy.ps1" }
  Write-Host "Pod: $pod"

  Invoke-Native kubectl @("exec", "-n", $Ns, $pod, "-c", "superset", "--", "sh", "-c", "rm -rf /tmp/prove && mkdir -p /tmp/prove")
  Invoke-Native kubectl @("cp", $file, "${Ns}/${pod}:/tmp/prove/prove.yaml", "-c", "superset")

  $comando = @("exec", "-n", $Ns, $pod, "-c", "superset", "--",
    "python", "-m", "superset_ai.prove", "/tmp/prove/prove.yaml", "--motore", $Motore, "--report", "/tmp/prove/out")
  if ($Solo) { $comando += @("--solo", $Solo) }

  New-Item -ItemType Directory -Force "prove-risultati" | Out-Null
  if (Test-Path "prove-risultati/ultimo.json") {
    Invoke-Native kubectl @("cp", "prove-risultati/ultimo.json", "${Ns}/${pod}:/tmp/prove/precedente.json", "-c", "superset")
    $comando += @("--precedente", "/tmp/prove/precedente.json")
  }

  Write-Host "Avvio delle prove (i risultati compaiono man mano)...`n"
  Invoke-Native kubectl $comando

  $stamp = Get-Date -Format "yyyyMMdd-HHmm"
  Invoke-Native kubectl @("cp", "${Ns}/${pod}:/tmp/prove/out/risultati.md", "prove-risultati/$stamp.md", "-c", "superset")
  Invoke-Native kubectl @("cp", "${Ns}/${pod}:/tmp/prove/out/risultati.json", "prove-risultati/$stamp.json", "-c", "superset")
  # Il confronto ha senso solo tra esecuzioni complete
  if (-not $Solo) { Copy-Item "prove-risultati/$stamp.json" "prove-risultati/ultimo.json" -Force }

  Write-Host "`nReport: $(Join-Path $Root "prove-risultati\$stamp.md")"
} finally {
  Pop-Location
}
