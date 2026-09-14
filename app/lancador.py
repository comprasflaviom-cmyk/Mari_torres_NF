"""
Lançador do aplicativo: escolhe a porta, gera o token e abre o navegador.

Duas formas de subir o servidor:

* `executar()` — modo console, usado por `python -m app` (desenvolvimento e
  uso avançado pela linha de comando). Fechar a janela encerra o aplicativo.
* `executar_com_bandeja()` — o que o atalho do Menu Iniciar do executável
  instalado chama. Sem janela de console: um ícone fica na bandeja do sistema
  (perto do relógio), e só "Sair" no menu dele encerra de verdade — fechar
  sem querer deixou de ser possível.

Em ambos, o servidor fica preso a `127.0.0.1`, então nada na rede local — nem
o Wi-Fi do café — alcança o aplicativo.
"""

from __future__ import annotations

import socket
import threading
import webbrowser
from pathlib import Path

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


def executar_com_bandeja(porta: int | None = None) -> None:
    """Sobe o servidor sem janela de console — o ícone da bandeja é que manda.

    O servidor roda numa thread de segundo plano; a thread principal fica com
    o ícone (`pystray` exige isso no Windows). "Abrir painel" reabre o
    navegador; "Sair" é a única forma de derrubar o servidor — pede o
    desligamento gracioso do uvicorn (`should_exit`) e só então fecha o ícone.
    """
    import pystray

    guardiao = Guardiao()
    porta = porta or porta_livre()
    url = f"http://{ENDERECO}:{porta}/?t={guardiao.token}"

    config = uvicorn.Config(
        criar_app(guardiao),
        host=ENDERECO,
        port=porta,
        log_level="warning",
        access_log=False,
    )
    servidor = uvicorn.Server(config)
    threading.Thread(target=servidor.run, daemon=True).start()
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    def abrir(icone=None, item=None) -> None:
        webbrowser.open(url)

    def sair(icone, item) -> None:
        servidor.should_exit = True
        icone.stop()

    icone = pystray.Icon(
        "EmissorNFSe",
        _icone_da_bandeja(),
        "Emissor de NFS-e",
        pystray.Menu(
            pystray.MenuItem("Abrir painel", abrir, default=True),
            pystray.MenuItem("Sair", sair),
        ),
    )
    icone.run()


def _icone_da_bandeja():
    """A imagem do ícone da bandeja.

    Usa `empacotamento/icone.ico` se existir (mesmo ícone do executável, para
    o instalador vir com identidade visual consistente); sem ele, desenha um
    quadrado simples — a bandeja nunca fica sem ícone nenhum por falta de
    arquivo.
    """
    from PIL import Image, ImageDraw

    caminho = Path(__file__).resolve().parent.parent / "empacotamento" / "icone.ico"
    if caminho.exists():
        return Image.open(caminho)

    imagem = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(imagem).rounded_rectangle((4, 4, 60, 60), radius=12, fill=(30, 90, 168, 255))
    return imagem


if __name__ == "__main__":
    executar()
