"""
Lançador do aplicativo: escolhe a porta, gera o token e abre o navegador.

É o que o atalho do Menu Iniciar executa. O servidor fica preso a `127.0.0.1`,
então nada na rede local — nem o Wi-Fi do café — alcança o aplicativo.
"""

from __future__ import annotations

import socket
import threading
import webbrowser

import uvicorn

from .seguranca import Guardiao
from .servidor import criar_app

ENDERECO = "127.0.0.1"   # nunca 0.0.0.0: isso exporia o app para a rede local
PORTA_PREFERIDA = 8765


def porta_livre(preferida: int = PORTA_PREFERIDA) -> int:
    """Usa a porta preferida; se estiver ocupada, deixa o sistema escolher."""
    for candidata in (preferida, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as teste:
            teste.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                teste.bind((ENDERECO, candidata))
                return teste.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("Não foi possível reservar uma porta local.")


def executar(abrir_navegador: bool = True, porta: int | None = None) -> None:
    guardiao = Guardiao()
    porta = porta or porta_livre()
    url = f"http://{ENDERECO}:{porta}/?t={guardiao.token}"

    print("=" * 68)
    print("  Emissor de NFS-e")
    print("=" * 68)
    print(f"  Aberto em: {url}")
    print("  Feche esta janela para encerrar o aplicativo.")
    print("=" * 68)

    if abrir_navegador:
        # Espera o servidor subir antes de abrir a aba.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        criar_app(guardiao),
        host=ENDERECO,
        port=porta,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    executar()
