"""
Liga a sincronização (nfse/sincronizacao.py) à configuração do app.

`aplicar_configuracao` é chamado ao abrir o app e ao salvar a Configuração;
`iniciar_sincronizacao_periodica` roda em segundo plano enquanto o app está
aberto (chamado por app.lancador, como a verificação das notas mensais).
"""

from __future__ import annotations

import shutil
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from nfse import armazenamento_config as ac
from nfse import sincronizacao
from nfse.clientes import BancoLocal

from .rotas_clientes import caminho_banco

INTERVALO_SEGUNDOS = 60
INTERVALO_BACKUP_SEGUNDOS = 3600
_ultimo_backup: float | None = None   # None = ainda não fez nesta sessão


def aplicar_configuracao(config: ac.ConfiguracaoApp) -> None:
    """Liga ou desliga conforme a pasta compartilhada estiver preenchida."""
    pasta = config.pasta_compartilhada.strip()
    if not pasta:
        sincronizacao.desativar()
        return
    atual = sincronizacao.ativo()
    if atual and str(atual.pasta) == pasta and atual.serie == config.serie_dps:
        return
    BancoLocal(caminho_banco())   # garante as tabelas antes da primeira leitura
    sincronizacao.ativar(Path(pasta), config.serie_dps, caminho_banco(), ac.caminho_config())
    sincronizacao.publicar_tudo()


def sincronizar_agora(forcar_backup: bool = False) -> dict:
    """Uma rodada: manda o que é deste computador e traz o dos outros."""
    global _ultimo_backup
    enviados = sincronizacao.descarregar()
    recebidos = sincronizacao.importar()
    vencido = _ultimo_backup is None or time.monotonic() - _ultimo_backup > INTERVALO_BACKUP_SEGUNDOS
    if forcar_backup or vencido:
        if sincronizacao.snapshot():
            _ultimo_backup = time.monotonic()
    return {"enviados": enviados, "recebidos": recebidos}


def restaurar(serie: str, config: ac.ConfiguracaoApp) -> int:
    """Banco a partir do backup da série + controle de numeração do espelho."""
    total = sincronizacao.restaurar(serie)
    _trazer_controle(config)
    return total


def _trazer_controle(config: ac.ConfiguracaoApp) -> None:
    """Sem o controle de numeração, a série recomeçaria do 1 (rejeição certa)."""
    espelho = config.destino_backup()
    if espelho is None or not espelho.is_dir():
        return
    logs = config.para_configuracao().diretorio_logs
    Path(logs).mkdir(parents=True, exist_ok=True)
    for origem in espelho.glob(f"controle_*_serie{config.serie_dps}.json"):
        destino = Path(logs) / origem.name
        if not destino.exists() or destino.stat().st_mtime < origem.stat().st_mtime:
            shutil.copy2(origem, destino)


def situacao() -> dict:
    estado = sincronizacao.situacao()
    for campo in ("ultima", "ultimo_backup"):
        if estado.get(campo):
            estado[campo + "_br"] = datetime.fromisoformat(estado[campo]).strftime("%d/%m/%Y às %H:%M")
    return estado


def iniciar_sincronizacao_periodica(intervalo_segundos: int = INTERVALO_SEGUNDOS) -> None:
    def laco() -> None:
        while True:
            try:
                aplicar_configuracao(ac.carregar())
                if sincronizacao.ativo():
                    sincronizar_agora()
            except Exception:  # noqa: BLE001 — nunca derruba o app por causa disso
                traceback.print_exc()
            time.sleep(intervalo_segundos)

    threading.Thread(target=laco, daemon=True).start()
