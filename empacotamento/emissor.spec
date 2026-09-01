# -*- mode: python ; coding: utf-8 -*-
"""
Receita do PyInstaller para o Emissor de NFS-e.

Modo **one-folder**, não one-file, de propósito:

* inicialização bem mais rápida (one-file descompacta tudo a cada abertura);
* muito menos falso-positivo de antivírus, que é comum com executável
  autoextraível;
* atualizar passa a ser substituir arquivos, não trocar um binário de 80 MB.

Build (numa máquina Windows):

    pip install pyinstaller
    pyinstaller empacotamento/emissor.spec --noconfirm

O resultado sai em `dist/EmissorNFSe/`. Depois, gere o instalador com o
Inno Setup usando `empacotamento/instalador.iss`.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

RAIZ = Path(SPECPATH).parent

# Templates, CSS e JS precisam ir junto: o app os lê do disco em tempo de execução.
dados = [
    (str(RAIZ / "app" / "templates"), "app/templates"),
    (str(RAIZ / "app" / "static"), "app/static"),
]

# O keyring escolhe o backend em tempo de execução, então o PyInstaller não
# enxerga o import do Gerenciador de Credenciais do Windows na análise estática.
ocultos = (
    collect_submodules("keyring.backends")
    + collect_submodules("uvicorn")
    + [
        "win32timezone",          # exigido pelo backend do keyring no Windows
        "email.mime.application",
        "openpyxl.cell._writer",  # importado dinamicamente pelo pandas
    ]
)

analise = Analysis(
    [str(RAIZ / "empacotamento" / "iniciar.py")],
    pathex=[str(RAIZ)],
    binaries=[],
    datas=dados,
    hiddenimports=ocultos,
    hookspath=[],
    runtime_hooks=[],
    # Corta peso morto: nada aqui é usado pelo emissor.
    excludes=["tkinter", "matplotlib", "pytest", "playwright", "IPython", "notebook"],
    noarchive=False,
)

pyz = PYZ(analise.pure)

exe = EXE(
    pyz,
    analise.scripts,
    [],
    exclude_binaries=True,
    name="EmissorNFSe",
    console=True,       # a janela mostra a URL local e serve para encerrar o app
    icon=str(RAIZ / "empacotamento" / "icone.ico") if (RAIZ / "empacotamento" / "icone.ico").exists() else None,
    debug=False,
    strip=False,
    upx=False,          # UPX aumenta muito o falso-positivo de antivírus
)

COLLECT(
    exe,
    analise.binaries,
    analise.datas,
    strip=False,
    upx=False,
    name="EmissorNFSe",
)
