# Removes the Helm release. Add -DeleteCluster to delete the minikube profile too.
param([string]$Cluster = "obs", [switch]$DeleteCluster)
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
helm uninstall obs -n observability
kubectl delete namespace demo --ignore-not-found
if ($DeleteCluster) { minikube delete -p $Cluster }
