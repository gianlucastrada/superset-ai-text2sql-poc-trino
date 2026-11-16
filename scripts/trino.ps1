<#
.SYNOPSIS
  Accende, spegne o controlla Trino senza toccare il resto della POC.

.PARAMETER Azione
  spegni  : ferma il pod di Trino e libera la sua memoria (circa 2-3 GB)
  accendi : lo riavvia
  stato   : mostra il pod e le tabelle che Trino vede nel catalogo postgresql

.EXAMPLE
  .\scripts\trino.ps1 spegni
  .\scripts\trino.ps1 stato
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("spegni", "accendi", "stato")]
  [string]$Azione
)
$Ns = "superset-ai"

switch ($Azione) {
  "spegni" {
    & kubectl scale deployment/trino -n $Ns --replicas=0
    Write-Host "Trino fermato. Nella pagina AI il motore Trino dara' errore finche' non lo riaccendi."
  }
  "accendi" {
    & kubectl scale deployment/trino -n $Ns --replicas=1
    & kubectl rollout status deployment/trino -n $Ns --timeout=600s
  }
  "stato" {
    & kubectl get pods -n $Ns -l app=trino -o wide
    Write-Host "`nTabelle e viste viste da Trino (catalogo postgresql, schema public):"
    & kubectl exec -n $Ns deploy/trino -- trino --execute "SHOW TABLES FROM postgresql.public"
    Write-Host "`nColonne di v_vendite (importo e margine devono esserci):"
    & kubectl exec -n $Ns deploy/trino -- trino --execute "DESCRIBE postgresql.public.v_vendite"
  }
}
