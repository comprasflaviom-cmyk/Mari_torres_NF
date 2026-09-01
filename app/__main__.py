"""Ponto de entrada: `python -m app`."""

import argparse

from .lancador import executar

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interface gráfica do Emissor de NFS-e.")
    parser.add_argument("--porta", type=int, help="Porta fixa (padrão: 8765 ou a primeira livre).")
    parser.add_argument("--sem-navegador", action="store_true",
                        help="Não abre o navegador automaticamente.")
    args = parser.parse_args()
    executar(abrir_navegador=not args.sem_navegador, porta=args.porta)
