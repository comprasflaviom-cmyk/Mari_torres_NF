"""
Estado da interface: a planilha importada e o lote em andamento.

O aplicativo é de uso local e individual — um usuário, uma máquina — então o
estado vive em memória, num único objeto. Não há sessões por usuário nem banco
de sessão, porque não há mais de um usuário.

A emissão roda numa thread separada para a tela não congelar, e o andamento é
publicado para o navegador via SSE.
"""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator

from nfse.logs import Relatorio
from nfse.planilha import LinhaFaturamento, iterar_faturamento
from nfse.servico import Emissor, EventoProgresso, OpcoesEmissao

SENTINELA_FIM = object()


# ---------------------------------------------------------------------------
# Planilha importada
# ---------------------------------------------------------------------------
@dataclass
class LinhaPrevia:
    """Uma linha da planilha como aparece na grade de pré-visualização."""

    numero_linha: int
    valida: bool
    documento: str = ""
    razao_social: str = ""
    email: str = ""
    valor: str = ""
    descricao: str = ""
    erro: str = ""
    linha: LinhaFaturamento | None = None

    def para_json(self) -> dict:
        return {
            "numero_linha": self.numero_linha,
            "valida": self.valida,
            "documento": self.documento,
            "razao_social": self.razao_social,
            "email": self.email,
            "valor": self.valor,
            "descricao": self.descricao,
            "erro": self.erro,
        }


@dataclass
class Importacao:
    """Resultado da leitura de um `.xlsx`, antes de qualquer transmissão."""

    caminho: Path
    nome_arquivo: str
    linhas: list[LinhaPrevia]
    importada_em: datetime = field(default_factory=datetime.now)

    @property
    def validas(self) -> list[LinhaPrevia]:
        return [l for l in self.linhas if l.valida]

    @property
    def invalidas(self) -> list[LinhaPrevia]:
        return [l for l in self.linhas if not l.valida]

    @property
    def valor_total(self) -> Decimal:
        return sum(
            (l.linha.valor_servico for l in self.validas if l.linha), Decimal("0")
        )

    def selecionadas(self, numeros: set[int] | None) -> list[LinhaPrevia]:
        if numeros is None:
            return self.validas
        return [l for l in self.validas if l.numero_linha in numeros]


def importar_planilha(caminho: Path, nome_exibicao: str | None = None) -> Importacao:
    """Lê o `.xlsx` e monta a pré-visualização.

    Reaproveita `planilha.iterar_faturamento`, então a validação da interface é
    exatamente a mesma da linha de comando — inclusive as mensagens de erro.
    """
    linhas: list[LinhaPrevia] = []
    for _, linha, erro in iterar_faturamento(caminho):
        if erro is not None:
            linhas.append(LinhaPrevia(
                numero_linha=linha.numero_linha if linha else len(linhas) + 2,
                valida=False,
                erro=erro,
            ))
            continue
        linhas.append(LinhaPrevia(
            numero_linha=linha.numero_linha,
            valida=True,
            documento=linha.documento_tomador,
            razao_social=linha.razao_social,
            email=linha.email,
            valor=f"{linha.valor_servico:.2f}",
            descricao=linha.descricao,
            linha=linha,
        ))
    return Importacao(
        caminho=caminho, nome_arquivo=nome_exibicao or caminho.name, linhas=linhas
    )


# ---------------------------------------------------------------------------
# Lote em andamento
# ---------------------------------------------------------------------------
class LoteEmAndamento(RuntimeError):
    """Já existe uma emissão rodando."""


@dataclass
class TrabalhoEmissao:
    """Um lote de emissão executado em segundo plano.

    Guarda o histórico completo de eventos para que abrir a página no meio do
    processo mostre tudo que já aconteceu, e não só o que vier daqui em diante.
    """

    estado: str = "ocioso"          # ocioso | rodando | concluido | falhou
    eventos: list[dict] = field(default_factory=list)
    relatorio: Relatorio | None = None
    erro: str = ""
    iniciado_em: datetime | None = None
    terminado_em: datetime | None = None
    dry_run: bool = False
    ambiente: str = ""

    _inscritos: list[queue.Queue] = field(default_factory=list)
    _trava: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None

    # -- Publicação para o navegador ---------------------------------------
    def publicar(self, evento: dict) -> None:
        with self._trava:
            self.eventos.append(evento)
            for fila in list(self._inscritos):
                fila.put(evento)

    def inscrever(self) -> tuple[queue.Queue, list[dict]]:
        """Registra um ouvinte e devolve o que já aconteceu, para reproduzir."""
        fila: queue.Queue = queue.Queue()
        with self._trava:
            self._inscritos.append(fila)
            return fila, list(self.eventos)

    def cancelar_inscricao(self, fila: queue.Queue) -> None:
        with self._trava:
            if fila in self._inscritos:
                self._inscritos.remove(fila)

    def _encerrar_inscritos(self) -> None:
        with self._trava:
            for fila in list(self._inscritos):
                fila.put(SENTINELA_FIM)

    # -- Execução -----------------------------------------------------------
    def iniciar(
        self,
        montar: Callable[[], Emissor],
        linhas: list[LinhaFaturamento],
        opcoes: OpcoesEmissao,
        ambiente: str,
    ) -> None:
        """Dispara o lote numa thread.

        Recusa se já houver um rodando: dois lotes simultâneos disputariam a
        numeração da DPS, e número repetido é rejeição garantida (ou pior,
        nota duplicada).
        """
        if self.estado == "rodando":
            raise LoteEmAndamento("Já existe uma emissão em andamento.")

        self.estado = "rodando"
        self.eventos = []
        self.relatorio = None
        self.erro = ""
        self.iniciado_em = datetime.now()
        self.terminado_em = None
        self.dry_run = opcoes.dry_run
        self.ambiente = ambiente

        self._thread = threading.Thread(
            target=self._executar, args=(montar, linhas, opcoes), daemon=True
        )
        self._thread.start()

    def _executar(self, montar, linhas, opcoes) -> None:
        try:
            emissor = montar()
            # `emitir_lote` espera a tripla de `iterar_faturamento`.
            entrada = [(i, linha, None) for i, linha in enumerate(linhas)]
            self.relatorio = emissor.emitir_lote(
                entrada, opcoes, lambda ev: self.publicar(_evento_para_json(ev))
            )
            self.estado = "concluido"
        except Exception as exc:  # noqa: BLE001
            self.estado = "falhou"
            self.erro = str(exc)
            self.publicar({
                "tipo": "erro",
                "mensagem": f"A emissão foi interrompida: {exc}",
                "situacao": "", "linha": 0, "indice": 0, "total": 0,
            })
            traceback.print_exc()
        finally:
            self.terminado_em = datetime.now()
            self.publicar({
                "tipo": "encerrado",
                "mensagem": self.relatorio.resumo() if self.relatorio else self.erro,
                "situacao": "", "linha": 0, "indice": 0, "total": 0,
                "estado": self.estado,
            })
            self._encerrar_inscritos()

    # -- Consumo pelo SSE ---------------------------------------------------
    def transmitir(self) -> Iterator[dict]:
        """Gera os eventos já ocorridos e depois os novos, até o lote acabar."""
        fila, anteriores = self.inscrever()
        try:
            yield from anteriores
            if self.estado != "rodando":
                return
            while True:
                evento = fila.get()
                if evento is SENTINELA_FIM:
                    return
                yield evento
        finally:
            self.cancelar_inscricao(fila)

    def resumo(self) -> dict:
        return {
            "estado": self.estado,
            "dry_run": self.dry_run,
            "ambiente": self.ambiente,
            "erro": self.erro,
            "iniciado_em": self.iniciado_em.isoformat() if self.iniciado_em else None,
            "terminado_em": self.terminado_em.isoformat() if self.terminado_em else None,
            "resumo": self.relatorio.resumo() if self.relatorio else "",
            "contagens": {
                situacao: self.relatorio.contagem(situacao)
                for situacao in ("AUTORIZADA", "REJEITADA", "INVALIDA", "ERRO_LOCAL", "PULADA")
            } if self.relatorio else {},
        }


def _evento_para_json(evento: EventoProgresso) -> dict:
    dados = {
        "tipo": evento.tipo,
        "mensagem": evento.mensagem,
        "linha": evento.linha,
        "situacao": evento.situacao,
        "indice": evento.indice,
        "total": evento.total,
    }
    if evento.registro is not None:
        dados["registro"] = {
            "linha_planilha": evento.registro.linha_planilha,
            "situacao": evento.registro.situacao,
            "razao_social": evento.registro.razao_social,
            "valor_servico": evento.registro.valor_servico,
            "chave_acesso": evento.registro.chave_acesso,
            "detalhe": evento.registro.detalhe,
            "email": evento.registro.email,
        }
    return dados


@dataclass
class EstadoInterface:
    """Estado global do aplicativo — um por processo."""

    importacao: Importacao | None = None
    trabalho: TrabalhoEmissao = field(default_factory=TrabalhoEmissao)


ESTADO = EstadoInterface()
