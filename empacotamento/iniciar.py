"""
Ponto de entrada do executável empacotado.

Existe separado de `app/__main__.py` porque o PyInstaller precisa de um script
de nível superior, e porque aqui tratamos duas coisas próprias do executável:
manter a janela aberta quando algo falha, e ajustar o caminho dos templates
quando o app roda de dentro do pacote.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _ajustar_caminhos() -> None:
    """Faz o app achar templates e estáticos dentro do pacote do PyInstaller."""
    if getattr(sys, "frozen", False):
        # `_MEIPASS` é a pasta temporária onde o PyInstaller extrai os dados.
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        os.environ.setdefault("EMISSOR_RAIZ_PACOTE", str(base))
        sys.path.insert(0, str(base))


def main() -> int:
    _ajustar_caminhos()
    try:
        from app.lancador import executar

        executar()
        return 0
    except KeyboardInterrupt:
        print("\nAplicativo encerrado.")
        return 0
    except Exception:
        # Sem isto, a janela do console fecha antes de a pessoa ler o erro.
        print("\n" + "=" * 68)
        print("  O Emissor de NFS-e não conseguiu iniciar.")
        print("=" * 68)
        traceback.print_exc()
        print("\nEnvie o texto acima para o suporte.")
        input("\nPressione Enter para fechar...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
