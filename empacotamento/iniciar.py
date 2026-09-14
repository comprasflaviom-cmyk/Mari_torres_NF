"""
Ponto de entrada do executável empacotado.

Existe separado de `app/__main__.py` porque o PyInstaller precisa de um script
de nível superior, e porque aqui tratamos duas coisas próprias do executável:
registrar o erro quando algo falha, e ajustar o caminho dos templates quando o
app roda de dentro do pacote.

O executável é `console=False` (ver `empacotamento/emissor.spec`) — não existe
janela de terminal, o app vive só no ícone da bandeja do sistema (veja
`app.lancador.executar_com_bandeja`). Sem console, não tem como escrever o erro
na tela nem esperar por Enter: por isso, se a inicialização falhar, o erro vai
para um arquivo de log e para uma caixa de mensagem do Windows.
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import traceback
from pathlib import Path

MB_ICONERROR = 0x10


def _ajustar_caminhos() -> None:
    """Faz o app achar templates e estáticos dentro do pacote do PyInstaller."""
    if getattr(sys, "frozen", False):
        # `_MEIPASS` é a pasta temporária onde o PyInstaller extrai os dados.
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        os.environ.setdefault("EMISSOR_RAIZ_PACOTE", str(base))
        sys.path.insert(0, str(base))


def _registrar_erro_inicializacao() -> None:
    """Grava o erro num arquivo e avisa com uma caixa de mensagem nativa.

    Escreve em `%TEMP%`, não na pasta de dados do app: o próprio código do app
    pode ser a causa da falha, e depender dele aqui arrisca perder o erro
    justamente quando mais precisa dele.
    """
    caminho_log = Path(tempfile.gettempdir()) / "EmissorNFSe-erro-inicializacao.log"
    try:
        caminho_log.write_text(traceback.format_exc(), encoding="utf-8")
    except OSError:
        pass

    mensagem = (
        "O Emissor de NFS-e não conseguiu iniciar.\n\n"
        f"Detalhes salvos em:\n{caminho_log}\n\n"
        "Envie esse arquivo para o suporte."
    )
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(0, mensagem, "Emissor de NFS-e", MB_ICONERROR)
    else:
        print(mensagem, file=sys.stderr)


def main() -> int:
    _ajustar_caminhos()
    try:
        from app.lancador import executar_com_bandeja

        executar_com_bandeja()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        _registrar_erro_inicializacao()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
