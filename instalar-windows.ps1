# Instala ou atualiza o Impera Meta MCP no Windows, em %USERPROFILE%\meta-ads-mcp.
# Clientes, tokens, ajustes e relatórios que já existam ficam como estão.
#
# Pelo PowerShell, numa linha (baixa a versão mais nova publicada):
#   irm https://raw.githubusercontent.com/marcelolucas11/impera-meta-mcp/main/instalar-windows.ps1 | iex
# O instalador Impera-Meta-MCP-Instalador.exe roda este mesmo script depois de copiar os arquivos.
param(
  [string]$Origem = "",      # pasta com os arquivos do programa (vazio: baixa a versão mais nova)
  [string]$Uv = "",          # uv.exe que veio com o instalador (vazio: usa o do computador ou baixa)
  [switch]$SemJanela         # não abre o painel no fim (o instalador oferece abrir na última tela)
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Destino = Join-Path $env:USERPROFILE 'meta-ads-mcp'
# O que é do usuário (ou não faz parte do programa) nunca é copiado por cima.
$Preservar = @('.env', '.limites.json', '.ritmo.json', 'clientes', 'relatorios', 'historico', '.venv', '.painel.json',
               'painel.log', '.aviso-versao', '.versoes-anteriores', '.git', '.github', 'testes', '__pycache__')
$Temporaria = $null

function Baixar-Pacote {
  $url = $env:IMPERA_URL_PACOTE
  if (-not $url) { $url = 'https://github.com/marcelolucas11/impera-meta-mcp/releases/latest/download/impera-meta-mcp.zip' }
  $script:Temporaria = Join-Path ([IO.Path]::GetTempPath()) ('impera-' + [Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $script:Temporaria | Out-Null
  $zip = Join-Path $script:Temporaria 'pacote.zip'
  Write-Host 'Baixando a versão mais nova do Impera Meta MCP...'
  Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
  $esperado = ((Invoke-WebRequest -UseBasicParsing -Uri ($url + '.sha256')).Content -split '\s+')[0].Trim().ToLower()
  $obtido = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
  if ($esperado -ne $obtido) { throw 'O arquivo baixado não confere com o checksum publicado. Nada foi instalado; tente de novo.' }
  Expand-Archive -Path $zip -DestinationPath $script:Temporaria -Force
  return (Join-Path $script:Temporaria 'impera-meta-mcp')
}

function Python-Serve([string]$py) {
  if (-not (Test-Path $py)) { return $false }
  & $py -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
  return ($LASTEXITCODE -eq 0)
}

function Achar-Uv {
  if ($Uv -and (Test-Path $Uv)) { return $Uv }
  $c = Get-Command uv -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'), (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'))) {
    if (Test-Path $p) { return $p }
  }
  # Sem uv: o instalador oficial coloca um em %USERPROFILE%\.local\bin, sem senha de administrador.
  Write-Host 'Baixando o uv (gerenciador de Python)...'
  $env:UV_NO_MODIFY_PATH = '1'
  Invoke-RestMethod -UseBasicParsing https://astral.sh/uv/install.ps1 | Invoke-Expression
  $p = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
  if (-not (Test-Path $p)) { throw 'Não consegui instalar o uv.' }
  return $p
}

try {
  if (-not $Origem) {
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'VERSAO')) -and (Test-Path (Join-Path $PSScriptRoot 'painel.py'))) {
      $Origem = $PSScriptRoot
    } else {
      $Origem = Baixar-Pacote
    }
  }
  $Origem = (Resolve-Path $Origem).Path
  New-Item -ItemType Directory -Force -Path $Destino | Out-Null
  $DestinoReal = (Resolve-Path $Destino).Path
  if ($Origem.TrimEnd('\') -ne $DestinoReal.TrimEnd('\')) {
    Write-Host "Instalando em $DestinoReal..."
    Get-ChildItem -LiteralPath $Origem -Force | Where-Object { $Preservar -notcontains $_.Name } | ForEach-Object {
      Copy-Item -LiteralPath $_.FullName -Destination $DestinoReal -Recurse -Force
    }
  }
  Set-Location $DestinoReal
  # Arquivos baixados da internet vêm marcados pelo Windows; os nossos não precisam disso.
  Get-ChildItem -LiteralPath $DestinoReal -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notlike '*\.venv\*' } | Unblock-File -ErrorAction SilentlyContinue

  $py = Join-Path $DestinoReal '.venv\Scripts\python.exe'
  if (Python-Serve $py) {
    Write-Host ('Ambiente Python já existe (' + (& $py --version) + ').')
  } else {
    if (Test-Path '.venv') { Remove-Item -Recurse -Force '.venv' }
    # O uv baixa um Python próprio só para o app: não depende do Python do Windows nem pede administrador.
    $uvExe = Achar-Uv
    Write-Host 'Preparando o Python do app...'
    & $uvExe venv --seed --python 3.13 .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) { throw 'Não consegui criar o ambiente Python (.venv).' }
  }

  Write-Host 'Instalando dependências...'
  & $py -m pip install -q --disable-pip-version-check --upgrade pip
  & $py -m pip install -q --disable-pip-version-check -r requirements.txt
  if ($LASTEXITCODE -ne 0) { throw 'A instalação das dependências falhou.' }

  Write-Host ('Versão instalada: ' + (Get-Content VERSAO -Raw).Trim())
  # Se o painel estiver aberto com a versão anterior, fecha para ele voltar com o código novo.
  & $py -X utf8 painel.py --encerrar
  # Cria os atalhos (Menu Iniciar e Área de Trabalho), liga ao Claude Desktop e abre o painel.
  if ($SemJanela) { $env:PAINEL_SEM_JANELA = '1' }
  & $py -X utf8 painel.py --instalar-app
  if ($LASTEXITCODE -ne 0) { throw 'A etapa final (atalhos e ligação com o Claude) falhou.' }
  Write-Host ''
  Write-Host 'Pronto. Abra o Impera Meta MCP pelo Menu Iniciar ou pelo atalho na Área de Trabalho.'
  $codigo = 0
} catch {
  Write-Host ''
  Write-Host ('ERRO: ' + $_.Exception.Message)
  $codigo = 1
} finally {
  if ($Temporaria -and (Test-Path $Temporaria)) { Remove-Item -Recurse -Force $Temporaria -ErrorAction SilentlyContinue }
}
if ($PSCommandPath) { exit $codigo }  # pela linha (irm | iex) não fecha a janela do PowerShell
