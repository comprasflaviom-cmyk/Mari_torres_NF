; Instalador Windows do Emissor de NFS-e (Inno Setup 6+)
;
; Pré-requisito: rodar antes o PyInstaller —
;   pyinstaller empacotamento/emissor.spec --noconfirm
; que produz dist\EmissorNFSe\.
;
; Depois, abra este arquivo no Inno Setup Compiler e compile.
; O instalador sai em empacotamento\saida\.

#define Nome        "Emissor de NFS-e"
#define Versao      "1.0.0"
#define Publicador  "Consultoria"
#define Executavel  "EmissorNFSe.exe"

[Setup]
AppId={{8E2C4A17-9D3B-4F6E-A1C8-5B7D9E0F2A31}
AppName={#Nome}
AppVersion={#Versao}
AppPublisher={#Publicador}
DefaultDirName={autopf}\EmissorNFSe
DefaultGroupName={#Nome}
OutputDir=saida
OutputBaseFilename=EmissorNFSe-{#Versao}-instalador
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Instala por usuário quando não há privilégio de administrador — evita
; travar a instalação num laptop corporativo com conta limitada.
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
; A configuração e os dados ficam em %APPDATA%\EmissorNFSe e NÃO são
; removidos na desinstalação: lá está o controle de numeração da DPS.
UninstallDisplayIcon={app}\{#Executavel}

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "atalhodesktop"; Description: "Criar atalho na área de trabalho"; \
  GroupDescription: "Atalhos:"

[Files]
Source: "..\dist\EmissorNFSe\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#Nome}";              Filename: "{app}\{#Executavel}"
Name: "{group}\Desinstalar {#Nome}";  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#Nome}";        Filename: "{app}\{#Executavel}"; Tasks: atalhodesktop

[Run]
Filename: "{app}\{#Executavel}"; Description: "Abrir o {#Nome} agora"; \
  Flags: nowait postinstall skipifsilent

[Messages]
brazilianportuguese.FinishedLabel=O {#Nome} foi instalado.%n%nNa primeira execução, preencha a tela de Configuração: certificado A1, CNPJ, série da DPS e pasta de backup.%n%nATENÇÃO: se houver mais de um computador emitindo, cada um precisa de uma SÉRIE DIFERENTE.
