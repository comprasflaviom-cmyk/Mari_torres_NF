"""
Orquestração da emissão — o miolo compartilhado entre a linha de comando e a
interface gráfica.

Por que este módulo existe: `main.py` misturava três coisas — ler argumentos,
emitir a nota e escrever no log. Com uma interface gráfica entrando, isso viraria
duas implementações do mesmo fluxo, que divergem na primeira correção feita só de
um lado. Aqui a emissão fica isolada e comunica o andamento por um **callback de
progresso**: o CLI passa um que escreve no logger, a interface passa um que
alimenta a barra de progresso. O caminho de código é o mesmo nos dois.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from .armazenamento import salvar_nota, salvar_rejeicao
from .assinatura import assinar_dps, empacotar_para_envio
from .certificado import CertificadoA1, carregar_certificado, criar_sessao_mtls
from .cliente import ClienteNFSe
from .config import Configuracao
from .dps import dps_para_xml, montar_dps
from .email_envio import ConfiguracaoEmail, ErroEmail, enviar_nfse
from .estado import ControleEmissao, impressao_da_linha
from .logs import RegistroLinha, Relatorio
from .planilha import LinhaFaturamento

# Assinatura do callback de progresso.
AoProgredir = Callable[["EventoProgresso"], None]


@dataclass
class EventoProgresso:
    """Um passo do processamento, comunicado a quem estiver acompanhando.

    `tipo` diz como apresentar:
      inicio_lote | inicio_linha | detalhe | aviso | erro | fim_linha | fim_lote
    """

    tipo: str
    mensagem: str
    linha: int = 0
    situacao: str = ""
    indice: int = 0                       # posição no lote (1..total)
    total: int = 0
    registro: RegistroLinha | None = None


@dataclass
class OpcoesEmissao:
    """Tudo que varia de uma execução para outra."""

    competencia: date
    dry_run: bool = False
    reemitir: bool = False
    baixar_pdf: bool = True
    # Números de linha do Excel a processar. None = todas.
    linhas_selecionadas: set[int] | None = None


def _sem_ouvinte(_: EventoProgresso) -> None:
    """Callback padrão: descarta o progresso."""


@dataclass
class Emissor:
    """Emite NFS-e a partir de linhas já validadas.

    Não sabe nada sobre planilha, argumentos de linha de comando ou HTTP puro —
    só coordena os módulos do núcleo e relata o que aconteceu.
    """

    config: Configuracao
    config_email: ConfiguracaoEmail
    certificado: CertificadoA1
    cliente: ClienteNFSe
    controle: ControleEmissao

    # ------------------------------------------------------------------
    def emitir_lote(
        self,
        linhas: Iterable[tuple[int, LinhaFaturamento | None, str | None]],
        opcoes: OpcoesEmissao,
        ao_progredir: AoProgredir = _sem_ouvinte,
    ) -> Relatorio:
        """Processa o resultado de `planilha.iterar_faturamento`.

        Aceita a tripla `(indice, linha, erro)` para que linhas inválidas
        apareçam no relatório com o motivo, em vez de sumirem.
        """
        relatorio = Relatorio()
        pendentes = list(linhas)

        # Só entra na contagem o que será de fato processado.
        a_processar = [
            item for item in pendentes
            if item[1] is None
            or opcoes.linhas_selecionadas is None
            or item[1].numero_linha in opcoes.linhas_selecionadas
        ]
        total = len(a_processar)
        ao_progredir(EventoProgresso(
            tipo="inicio_lote", mensagem=f"{total} linha(s) a processar.", total=total
        ))

        for posicao, (_, linha, erro_validacao) in enumerate(a_processar, start=1):
            if erro_validacao is not None:
                numero = linha.numero_linha if linha else 0
                registro = RegistroLinha(
                    linha_planilha=numero, situacao="INVALIDA", detalhe=erro_validacao
                )
                relatorio.adicionar(registro)
                ao_progredir(EventoProgresso(
                    tipo="fim_linha", mensagem=erro_validacao, linha=numero,
                    situacao="INVALIDA", indice=posicao, total=total, registro=registro,
                ))
                continue

            registro = self.emitir_uma(
                linha, opcoes, ao_progredir, indice=posicao, total=total
            )
            relatorio.adicionar(registro)

        self.controle.salvar()
        ao_progredir(EventoProgresso(
            tipo="fim_lote", mensagem=relatorio.resumo(), total=total
        ))
        return relatorio

    # ------------------------------------------------------------------
    def emitir_uma(
        self,
        linha: LinhaFaturamento,
        opcoes: OpcoesEmissao,
        ao_progredir: AoProgredir = _sem_ouvinte,
        *,
        indice: int = 1,
        total: int = 1,
    ) -> RegistroLinha:
        """Monta, assina, envia e arquiva UMA nota.

        Nunca levanta exceção para o chamador: qualquer falha vira um
        `RegistroLinha` com a situação e o motivo, para que um problema numa
        linha não derrube o lote inteiro.
        """
        rotulo = f"Linha {linha.numero_linha} | {linha.razao_social[:40]} | R$ {linha.valor_servico}"
        contexto = {"linha": linha.numero_linha, "indice": indice, "total": total}
        ao_progredir(EventoProgresso(tipo="inicio_linha", mensagem=rotulo, **contexto))

        # 1) Duplicidade — protege contra rodar o mesmo lote duas vezes.
        impressao = impressao_da_linha(linha, opcoes.competencia.isoformat())
        if not opcoes.reemitir and (anterior := self.controle.ja_emitida(impressao)):
            registro = RegistroLinha(
                linha_planilha=linha.numero_linha, situacao="PULADA",
                documento_tomador=linha.documento_tomador, razao_social=linha.razao_social,
                valor_servico=str(linha.valor_servico),
                chave_acesso=anterior.get("chave_acesso") or "",
                detalhe="Já emitida em execução anterior. Use --reemitir para forçar.",
            )
            ao_progredir(EventoProgresso(
                tipo="fim_linha", situacao="PULADA", registro=registro, **contexto,
                mensagem=f"{rotulo} | PULADA (já emitida — chave {anterior.get('chave_acesso')})",
            ))
            return registro

        numero_dps = self.controle.proximo_numero()
        registro = RegistroLinha(
            linha_planilha=linha.numero_linha,
            situacao="ERRO_LOCAL",
            documento_tomador=linha.documento_tomador,
            razao_social=linha.razao_social,
            valor_servico=str(linha.valor_servico),
            numero_dps=str(numero_dps),
        )

        try:
            # 2) Monta a DPS e serializa no XML do layout
            dps = montar_dps(self.config, linha, numero_dps, competencia=opcoes.competencia)
            xml_dps = dps_para_xml(dps)
            ao_progredir(EventoProgresso(
                tipo="detalhe", mensagem=f"DPS {numero_dps} montada: {dps}", **contexto
            ))

            # 3) Assina em XMLDSig
            xml_assinado = assinar_dps(xml_dps, self.certificado)

            # 4) Simulação: grava o XML e para aqui
            if opcoes.dry_run:
                caminho = self._salvar_simulacao(linha, xml_assinado)
                registro.situacao = "PULADA"
                registro.detalhe = "dry-run — não transmitida"
                registro.arquivo_xml = str(caminho)
                self.controle.ultimo_numero -= 1   # simulação não consome numeração
                ao_progredir(EventoProgresso(
                    tipo="fim_linha", situacao="PULADA", registro=registro, **contexto,
                    mensagem=f"{rotulo} | DRY-RUN: DPS assinada salva em {caminho}",
                ))
                return registro

            # 5) Transmite
            resposta = self.cliente.emitir(empacotar_para_envio(xml_assinado))

            if resposta.autorizada:
                self._concluir_autorizada(
                    linha, opcoes, resposta, xml_assinado, impressao,
                    numero_dps, registro, rotulo, ao_progredir, contexto,
                )
            else:
                registro.arquivo_xml = salvar_rejeicao(
                    self.config.diretorio_notas, opcoes.competencia,
                    linha, resposta, xml_assinado,
                )
                registro.situacao = "REJEITADA"
                registro.detalhe = resposta.motivo_erro
                # Número não consumido: a Sefin não registrou nada.
                self.controle.ultimo_numero -= 1
                ao_progredir(EventoProgresso(
                    tipo="fim_linha", situacao="REJEITADA", registro=registro, **contexto,
                    mensagem=f"{rotulo} | REJEITADA (HTTP {resposta.status_http}): {resposta.motivo_erro}",
                ))

        except Exception as exc:  # noqa: BLE001 — uma linha ruim não derruba o lote
            registro.situacao = "ERRO_LOCAL"
            registro.detalhe = str(exc)
            self.controle.ultimo_numero -= 1
            ao_progredir(EventoProgresso(
                tipo="fim_linha", situacao="ERRO_LOCAL", registro=registro, **contexto,
                mensagem=f"{rotulo} | ERRO LOCAL: {exc}",
            ))

        return registro

    # ------------------------------------------------------------------
    def _salvar_simulacao(self, linha: LinhaFaturamento, xml_assinado: bytes) -> Path:
        destino = self.config.diretorio_notas / "dry-run"
        destino.mkdir(parents=True, exist_ok=True)
        # O número não é consumido em simulação, então o nome usa a linha da
        # planilha para os arquivos não colidirem entre si.
        caminho = destino / f"dps_simulada_linha{linha.numero_linha:03d}.xml"
        caminho.write_bytes(xml_assinado)
        return caminho

    def _concluir_autorizada(
        self, linha, opcoes, resposta, xml_assinado, impressao,
        numero_dps, registro, rotulo, ao_progredir, contexto,
    ) -> None:
        """Baixa o PDF, arquiva, registra a numeração e envia ao cliente."""
        pdf = None
        if opcoes.baixar_pdf and resposta.chave_acesso:
            try:
                pdf = self.cliente.baixar_danfse(resposta.chave_acesso)
            except RuntimeError as exc:
                # PDF é acessório: a nota já está autorizada.
                ao_progredir(EventoProgresso(
                    tipo="aviso", mensagem=f"DANFSe indisponível: {exc}", **contexto
                ))

        arquivos = salvar_nota(
            self.config.diretorio_notas, opcoes.competencia,
            linha, resposta, xml_assinado, pdf,
        )
        self.controle.registrar(impressao, numero_dps, resposta.chave_acesso)
        # Persiste a cada nota: uma queda no meio do lote não perde a numeração.
        self.controle.salvar()

        registro.situacao = "AUTORIZADA"
        registro.chave_acesso = resposta.chave_acesso or ""
        registro.arquivo_xml = arquivos.get("xml_nfse", arquivos.get("xml_dps", ""))
        registro.detalhe = "; ".join(resposta.mensagens)

        if resposta.mensagens:
            ao_progredir(EventoProgresso(
                tipo="aviso", mensagem=f"Alertas: {'; '.join(resposta.mensagens)}", **contexto
            ))

        # Entrega ao cliente. Falha aqui NÃO invalida a nota: ela já está
        # autorizada na Sefin. Fica no relatório para reenvio posterior.
        registro.email = self._entregar_por_email(linha, opcoes, resposta, arquivos, ao_progredir, contexto)

        ao_progredir(EventoProgresso(
            tipo="fim_linha", situacao="AUTORIZADA", registro=registro, **contexto,
            mensagem=f"{rotulo} | AUTORIZADA | chave {resposta.chave_acesso}",
        ))

    def _entregar_por_email(self, linha, opcoes, resposta, arquivos, ao_progredir, contexto) -> str:
        try:
            situacao = enviar_nfse(
                self.config_email,
                self.config.ambiente,
                linha.email,
                {
                    "tomador": linha.razao_social,
                    "prestador": self.config.prestador.cnpj,
                    "competencia": opcoes.competencia.strftime("%m/%Y"),
                    "descricao": linha.descricao,
                    "valor": _moeda(linha.valor_servico),
                    "chave": resposta.chave_acesso or "",
                    "chave_curta": (resposta.chave_acesso or "")[-8:],
                },
                arquivos,
            )
            ao_progredir(EventoProgresso(tipo="detalhe", mensagem=f"E-mail: {situacao}", **contexto))
            return situacao
        except ErroEmail as exc:
            situacao = f"FALHA no e-mail: {exc}"
            ao_progredir(EventoProgresso(tipo="erro", mensagem=situacao, **contexto))
            return situacao


def _moeda(valor) -> str:
    """1234.5 -> '1.234,50'"""
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def montar_emissor(config: Configuracao, config_email: ConfiguracaoEmail) -> Emissor:
    """Carrega o certificado, abre a sessão mTLS e devolve um `Emissor` pronto.

    Levanta `ErroCertificado` se o A1 estiver ausente, vencido ou ilegível —
    o chamador (CLI ou interface) decide como apresentar a falha.
    """
    certificado = carregar_certificado(config)
    certificado.validar_vigencia()
    cliente = ClienteNFSe(config, criar_sessao_mtls(certificado))
    controle = ControleEmissao.carregar(
        config.diretorio_logs, config.ambiente, config.serie_dps, config.numero_dps_inicial
    )
    return Emissor(
        config=config,
        config_email=config_email,
        certificado=certificado,
        cliente=cliente,
        controle=controle,
    )
