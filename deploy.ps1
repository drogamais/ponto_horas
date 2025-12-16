# -----------------------------------------------------------------
# SCRIPT DE DEPLOY AUTOMATIZADO (deploy.ps1)
# -----------------------------------------------------------------

# --- 1. Definição das Variáveis ---
$computadorRemoto = "192.168.21.251"
$usuarioRemoto    = "DROGAMAIS\apf.ti02"
$caminhoProjeto   = "C:\Automacao\ponto_horas"

# --- 2. Obter a Credencial de Forma Segura ---
Write-Host "Iniciando conexao com $computadorRemoto..."
$securePassword = Read-Host -AsSecureString -Prompt "Digite a senha para $usuarioRemoto"
$cred = New-Object System.Management.Automation.PSCredential($usuarioRemoto, $securePassword)


# --- 3. Executar o Deploy Remoto ---
Write-Host "Conectando e executando o deploy..."
Invoke-Command -ComputerName $computadorRemoto -Credential $cred -ScriptBlock { 

    # O '$using:' permite que o script remoto use as variáveis locais definidas acima

    Write-Host "[Servidor] Iniciando deploy..."

    Write-Host "[Servidor] Acessando pasta do projeto: $using:caminhoProjeto"
    cd $using:caminhoProjeto

    Write-Host "[Servidor] Executando git pull..."
    git pull

    Write-Host "[Servidor] Deploy concluido."
}

Write-Host "Processo finalizado."

# Mantém a janela aberta por 5 segundos para você ler o resultado
Start-Sleep -Seconds 2