"""
Interface de linha de comando do emissor.

Esta camada só lê argumentos, monta as dependências e traduz o progresso do
`Emissor` para o log. A emissão em si mora em `nfse/servico.py`, compartilhada
com a interface gráfica.

Uso:
    python -m nfse.main --dry-run              # monta e assina, mas NÃO envia
    python -m nfse.main                        # emite em AMBIENTE do .env
    python -m nfse.main --ambiente producao    # força produção
    python -m nfse.main --competencia 2026-08  # mês de referência do serviço
    python -m nfse.main --linhas 2,5,7         # emite só linhas específicas
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from .certificado import ErroCertificado
from .estado import ConflitoDeMaquina
from .config import carregar_configuracao
from .email_envio import ConfiguracaoEmail, ErroEmail
from .logs import Relatorio, configurar_logger, marca_tempo_execucao
from .planilha import ErroPlanilha, iterar_faturamento
from .servico import EventoProgresso, OpcoesEmissao, montar_emissor

# Como cada tipo de evento aparece no log.
NIVEL_POR_TIPO = {
    "detalhe": logging.DEBUG,
    "aviso": logging.WARNING,
    "erro": logging.ERROR,
}
NIVEL_POR_SITUACAO = {
    "AUTORIZADA": logging.INFO,
    "PULADA": logging.INFO,
    "REJEITADA": logging.ERROR,
    "INVALIDA": logging.ERROR,
    "ERRO_LOCAL": logging.ERROR,
}


def _argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emissor de NFS-e Nacional a partir de planilha.")
    parser.add_argument("--ambiente", choices=["homologacao", "producao"],
                        help="Sobrepõe AMBIENTE do .env.")
    parser.add_argument("--planilha", type=Path, help="Caminho do .xlsx (padrão: faturamento.xlsx).")
    parser.add_argument("--competencia", help="Mês de competência no formato AAAA-MM (padrão: mês atual).")
    parser.add_argument("--linhas", help="Lista de linhas do Excel a processar, ex.: 2,5,7.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Monta e assina a DPS, salva o XML localmente e NÃO envia à Sefin.")
    parser.add_argument("--sem-pdf", action="store_true",
                        help="Não baixa a DANFSe em PDF após autorizar.")
    parser.add_argument("--sem-email", action="store_true",
                        help="Não envia a nota ao cliente por e-mail nesta execução.")
    parser.add_argument("--reemitir", action="store_true",
                        help="Ignora o controle de duplicidade. Use com cuidado.")
    parser.add_argument("--assumir-maquina", action="store_true",
                        help="Assume o controle de numeração criado em outro computador. "
                             "Só use se tiver certeza de que a outra máquina não emite mais "
                             "nesta série.")
    return parser.parse_args()


def _competencia(texto: str | None) -> date:
    if not texto:
        return date.today().replace(day=1)
    try:
        return datetime.strptime(texto, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise SystemExit(f"--competencia inválida: {texto!r}. Use AAAA-MM.") from exc


def _ouvinte_de_log(logger: logging.Logger):
    """Traduz `EventoProgresso` em linhas de log — a saída de sempre do CLI."""

    def ao_progredir(evento: EventoProgresso) -> None:
        if evento.tipo == "inicio_lote":
            logger.info("%s\n", evento.mensagem)
        elif evento.tipo == "inicio_linha":
            pass  # o resultado da linha já é logado em fim_linha
        elif evento.tipo == "fim_linha":
            logger.log(NIVEL_POR_SITUACAO.get(evento.situacao, logging.INFO), "%s", evento.mensagem)
        elif evento.tipo == "fim_lote":
            pass  # o resumo é impresso no encerramento
        else:
            logger.log(NIVEL_POR_TIPO.get(evento.tipo, logging.INFO), "   %s", evento.mensagem)

    return ao_progredir


def executar() -> int:
    args = _argumentos()
    marca = marca_tempo_execucao()

    try:
        config = carregar_configuracao()
    except ValueError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 2

    if args.ambiente:
        config.ambiente = args.ambiente
        config.__post_init__()
    if args.planilha:
        config.caminho_planilha = args.planilha

    logger = configurar_logger(config.diretorio_logs, marca)
    competencia = _competencia(args.competencia)

    logger.info("=" * 72)
    logger.info("Emissor NFS-e Nacional | ambiente=%s (tpAmb=%s)", config.ambiente, config.tp_amb)
    logger.info("Planilha: %s", config.caminho_planilha)
    logger.info("Competência: %s | Série: %s", competencia.strftime("%m/%Y"), config.serie_dps)
    if args.dry_run:
        logger.info(">>> DRY-RUN: nada será transmitido à Sefin Nacional.")
    if config.ambiente == "producao" and not args.dry_run:
        logger.warning(">>> ATENÇÃO: PRODUÇÃO. As notas emitidas terão valor fiscal real.")
    logger.info("=" * 72)

    # A configuração de e-mail é resolvida antes (o Emissor precisa dela), mas
    # só é logada depois do certificado, para manter a ordem histórica da saída.
    config_email, avisos_email = _preparar_email(args)

    # ---- Dependências de emissão -----------------------------------------
    try:
        emissor = montar_emissor(config, config_email, args.assumir_maquina)
    except ErroCertificado as exc:
        logger.error("Certificado: %s", exc)
        return 3
    except ConflitoDeMaquina as exc:
        logger.error("Numeração: %s", exc)
        logger.error("Se tiver certeza, repita com --assumir-maquina.")
        return 5

    certificado = emissor.certificado
    logger.info("Certificado: %s", certificado.titular)
    logger.info("Validade até %s (%d dias restantes).",
                certificado.valido_ate.strftime("%d/%m/%Y"), certificado.dias_para_vencer)
    if certificado.dias_para_vencer < 30:
        logger.warning("Certificado vence em menos de 30 dias — providencie a renovação.")

    if emissor.espelho and emissor.espelho.ativo:
        logger.info("Backup automático em: %s", emissor.espelho.destino)
    else:
        logger.warning(
            "Backup automático desligado. Defina DIR_BACKUP para espelhar notas e numeração."
        )

    for nivel, mensagem in avisos_email:
        logger.log(nivel, "%s", mensagem)

    # ---- Emissão ----------------------------------------------------------
    try:
        linhas = list(iterar_faturamento(config.caminho_planilha))
    except ErroPlanilha as exc:
        logger.error("Planilha: %s", exc)
        return 4

    opcoes = OpcoesEmissao(
        competencia=competencia,
        dry_run=args.dry_run,
        reemitir=args.reemitir,
        baixar_pdf=not args.sem_pdf,
        linhas_selecionadas=(
            {int(n) for n in args.linhas.split(",") if n.strip()} if args.linhas else None
        ),
    )
    relatorio = emissor.emitir_lote(linhas, opcoes, _ouvinte_de_log(logger))

    # ---- Encerramento -----------------------------------------------------
    caminho_csv = relatorio.salvar_csv(config.diretorio_logs, marca)
    logger.info("\n" + "=" * 72)
    logger.info("RESUMO: %s", relatorio.resumo())
    logger.info("Relatório CSV: %s", caminho_csv)
    logger.info("Log detalhado: %s", config.diretorio_logs / f"emissao_{marca}.log")
    logger.info("=" * 72)

    return 1 if _houve_falha(relatorio) else 0


def _preparar_email(
    args: argparse.Namespace,
) -> tuple[ConfiguracaoEmail, list[tuple[int, str]]]:
    """Resolve a configuração de e-mail e desliga o envio se estiver incompleta.

    Devolve também as mensagens a logar, para o chamador emiti-las na hora certa.
    """
    config_email = ConfiguracaoEmail.do_ambiente()
    if args.sem_email:
        config_email.ativo = False

    if not config_email.ativo:
        return config_email, [(logging.INFO, "Envio por e-mail: desativado.")]

    avisos: list[tuple[int, str]] = []
    try:
        config_email.validar()
        avisos.append(
            (logging.INFO, f"Envio por e-mail: ATIVO (remetente {config_email.remetente_email}).")
        )
        if config_email.destino_teste:
            avisos.append((
                logging.WARNING,
                f"EMAIL_TESTE_DESTINO ativo: todos os e-mails irão para {config_email.destino_teste}.",
            ))
    except ErroEmail as exc:
        avisos.append((logging.ERROR, f"Envio por e-mail desativado: {exc}"))
        config_email.ativo = False
    return config_email, avisos


def _houve_falha(relatorio: Relatorio) -> bool:
    return bool(
        relatorio.contagem("REJEITADA")
        + relatorio.contagem("ERRO_LOCAL")
        + relatorio.contagem("INVALIDA")
    )


if __name__ == "__main__":
    raise SystemExit(executar())
