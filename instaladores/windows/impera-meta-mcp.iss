; Instalador do Windows: Impera-Meta-MCP-Instalador.exe (Inno Setup 6).
; Montado no GitHub Actions a partir do mesmo pacote da release (dist\win\impera-meta-mcp) e do uv.exe oficial.
;   iscc /DVersao=X.Y.Z instaladores\windows\impera-meta-mcp.iss
; Instala em %USERPROFILE%\meta-ads-mcp, sem pedir administrador. Clientes, tokens e relatórios ficam como estão.

#ifndef Versao
  #define Versao "0.0.0"
#endif

[Setup]
AppId={{6F1E2B7C-4D3A-4B8E-9C21-1A7E5D3F9B42}
AppName=Impera Meta MCP
AppVersion={#Versao}
AppVerName=Impera Meta MCP {#Versao}
AppPublisher=Impera Services
AppPublisherURL=https://github.com/marcelolucas11/impera-meta-mcp
DefaultDirName={%USERPROFILE}\meta-ads-mcp
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
UsedUserAreasWarning=no
OutputDir=..\..\dist
OutputBaseFilename=Impera-Meta-MCP-Instalador
SetupIconFile=..\..\recursos\icone.ico
UninstallDisplayIcon={app}\recursos\icone.ico
UninstallDisplayName=Impera Meta MCP
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
ShowLanguageDialog=no
CloseApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Messages]
pt.WelcomeLabel2=Este instalador coloca o Impera Meta MCP {#Versao} neste computador, prepara um Python só para o app (pela internet, leva de 1 a 3 minutos) e liga o servidor meta-ads ao Claude Desktop.%n%nSe o app já estiver instalado, ele é atualizado. Clientes, tokens, permissões e relatórios ficam como estão.
pt.FinishedLabel=O Impera Meta MCP está instalado. Abra pelo Menu Iniciar ou pelo atalho na Área de Trabalho.%n%nFeche o Claude por completo (ícone do Claude perto do relógio, botão direito, Sair) e abra de novo, para ele carregar o servidor meta-ads.

[Files]
Source: "..\..\dist\win\impera-meta-mcp\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\dist\win\uv.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Run]
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-X utf8 ""{app}\painel.py"" --abrir"; WorkingDir: "{app}"; Description: "Abrir o Impera Meta MCP agora"; Flags: postinstall nowait skipifsilent skipifdoesntexist

[UninstallRun]
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-X utf8 ""{app}\painel.py"" --encerrar"; WorkingDir: "{app}"; Flags: runhidden skipifdoesntexist; RunOnceId: "EncerrarPainel"

[UninstallDelete]
; Só o que é do programa. clientes\, relatorios\, historico\ e .env ficam (são do usuário).
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\.versoes-anteriores"
Type: files; Name: "{userprograms}\Impera Meta MCP.lnk"
Type: files; Name: "{userdesktop}\Impera Meta MCP.lnk"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  Codigo: Integer;
  Registro, Parametros: String;
begin
  if CurStep = ssPostInstall then
  begin
    Registro := ExpandConstant('{%TEMP}\impera-meta-mcp-instalacao.log');
    Parametros := '/C powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\instalar-windows.ps1') +
                  '" -Origem "' + ExpandConstant('{app}') + '" -Uv "' + ExpandConstant('{tmp}\uv.exe') + '" -SemJanela > "' +
                  Registro + '" 2>&1';
    WizardForm.StatusLabel.Caption := 'Preparando o Python do app e as dependências (de 1 a 3 minutos)...';
    WizardForm.ProgressGauge.Style := npbstMarquee;
    if (not Exec(ExpandConstant('{cmd}'), Parametros, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Codigo)) or (Codigo <> 0) then
      MsgBox('A preparação do Python ou das dependências não terminou.' + #13#10 + #13#10 +
             'Confira a internet e rode o instalador de novo. Registro da instalação:' + #13#10 + Registro, mbError, MB_OK);
    WizardForm.ProgressGauge.Style := npbstNormal;
  end;
end;
