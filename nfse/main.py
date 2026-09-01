"""
Orquestrador: lê a planilha, emite cada NFS-e e produz logs e relatório.

Uso:
    python -m nfse.main --dry-run              # monta e assina, mas NÃO envia
    python -m nfse.main                        # emite em AMBIENTE do .env
    python -m nfse.main --ambiente producao    # força produção
    python -m nfse.main --competencia 2026-08  # mês de referência do serviço
    python -m nfse.main --linhas 2,5,7         # emite só linhas específicas
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from .armazenamento import salvar_nota, salvar_rejeicao
from .assinatura import assinar_dps, empacotar_para_envio
from .certificado import ErroCertificado, carregar_certificado, criar_sessao_mtls
from .cliente import ClienteNFSe
from .config import carregar_configuracao
from .dps import dps_para_xml, montar_dps
from .email_envio import ConfiguracaoEmail, ErroEmail, enviar_nfse
from .estado import ControleEmissao, impressao_da_linha
from .logs import RegistroLinha, Relatorio, configurar_logger, marca_tempo_execucao
from .planilha import ErroPlanilha, iterar_faturamento


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
    return parser.parse_args()


def _competencia(texto: str | None) -> date:
    if not texto:
        hoje = date.today()
        return hoje.replace(day=1)
    try:
        return datetime.strptime(texto, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise SystemExit(f"--competencia inválida: {texto!r}. Use AAAA-MM.") from exc


def executar() -> int:
    args = _argumentos()
    marca = marca_tempo_execucao()

    try:
        config = carregar_configuracao()
    except ValueError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 2

    # Sobreposições de linha de comando
    if args.ambiente:
        config.ambiente = args.ambiente
        config.__post_init__()
    if args.planilha:
        config.caminho_planilha = args.planilha

    logger = configurar_logger(config.diretorio_logs, marca)
    competencia = _competencia(args.competencia)
    filtro_linhas = (
        {int(n) for n in args.linhas.split(",") if n.strip()} if args.linhas else None
    )

    logger.info("=" * 72)
    logger.info("Emissor NFS-e Nacional | ambiente=%s (tpAmb=%s)", config.ambiente, config.tp_amb)
    logger.info("Planilha: %s", config.caminho_planilha)
    logger.info("Competência: %s | Série: %s", competencia.strftime("%m/%Y"), config.serie_dps)
    if args.dry_run:
        logger.info(">>> DRY-RUN: nada será transmitido à Sefin Nacional.")
    if config.ambiente == "producao" and not args.dry_run:
        logger.warning(">>> ATENÇÃO: PRODUÇÃO. As notas emitidas terão valor fiscal real.")
    logger.info("=" * 72)

    # ---- Certificado A1 ---------------------------------------------------
    try:
        certificado = carregar_certificado(config)
        certificado.validar_vigencia()
    except ErroCertificado as exc:
        logger.error("Certificado: %s", exc)
        return 3
    logger.info("Certificado: %s", certificado.titular)
    logger.info("Validade até %s (%d dias restantes).",
                certificado.valido_ate.strftime("%d/%m/%Y"), certificado.dias_para_vencer)
    if certificado.dias_para_vencer < 30:
        logger.warning("Certificado vence em menos de 30 dias — providencie a renovação.")

    config_email = ConfiguracaoEmail.do_ambiente()
    if args.sem_email:
        config_email.ativo = False
    if config_email.ativo:
        try:
            config_email.validar()
            logger.info("Envio por e-mail: ATIVO (remetente %s).", config_email.remetente_email)
            if config_email.destino_teste:
                logger.warning("EMAIL_TESTE_DESTINO ativo: todos os e-mails irão para %s.",
                               config_email.destino_teste)
        except ErroEmail as exc:
            logger.error("Envio por e-mail desativado: %s", exc)
            config_email.ativo = False
    else:
        logger.info("Envio por e-mail: desativado.")

    sessao = criar_sessao_mtls(certificado)
    cliente = ClienteNFSe(config, sessao)

    controle = ControleEmissao.carregar(
        config.diretorio_logs, config.ambiente, config.serie_dps, config.numero_dps_inicial
    )
    relatorio = Relatorio()

    # ---- Loop principal ---------------------------------------------------
    try:
        linhas = list(iterar_faturamento(config.caminho_planilha))
    except ErroPlanilha as exc:
        logger.error("Planilha: %s", exc)
        return 4

    logger.info("%d linha(s) encontrada(s) na planilha.\n", len(linhas))

    for _, linha, erro_validacao in linhas:
        if erro_validacao is not None:
            numero = (linha.numero_linha if linha else 0)
            logger.error("Linha %s | INVÁLIDA: %s", numero or "?", erro_validacao)
            relatorio.adicionar(RegistroLinha(
                linha_planilha=numero, situacao="INVALIDA", detalhe=erro_validacao
            ))
            continue

        if filtro_linhas and linha.numero_linha not in filtro_linhas:
            continue

        _processar_linha(
            linha, config, cliente, certificado, controle, relatorio,
            competencia, logger, args, config_email,
        )

    # ---- Encerramento -----------------------------------------------------
    controle.salvar()
    caminho_csv = relatorio.salvar_csv(config.diretorio_logs, marca)

    logger.info("\n" + "=" * 72)
    logger.info("RESUMO: %s", relatorio.resumo())
    logger.info("Relatório CSV: %s", caminho_csv)
    logger.info("Log detalhado: %s", config.diretorio_logs / f"emissao_{marca}.log")
    logger.info("=" * 72)

    houve_falha = relatorio.contagem("REJEITADA") + relatorio.contagem("ERRO_LOCAL") + relatorio.contagem("INVALIDA")
    return 1 if houve_falha else 0


def _processar_linha(linha, config, cliente, certificado, controle, relatorio,
                     competencia, logger, args, config_email) -> None:
    """Monta, assina, envia e arquiva UMA nota. Nunca aborta o lote."""
    rotulo = f"Linha {linha.numero_linha} | {linha.razao_social[:40]} | R$ {linha.valor_servico}"

    # 1) Duplicidade
    impressao = impressao_da_linha(linha, competencia.isoformat())
    if not args.reemitir and (anterior := controle.ja_emitida(impressao)):
        logger.info("%s | PULADA (já emitida — chave %s)", rotulo, anterior.get("chave_acesso"))
        relatorio.adicionar(RegistroLinha(
            linha_planilha=linha.numero_linha, situacao="PULADA",
            documento_tomador=linha.documento_tomador, razao_social=linha.razao_social,
            valor_servico=str(linha.valor_servico),
            chave_acesso=anterior.get("chave_acesso") or "",
            detalhe="Já emitida em execução anterior. Use --reemitir para forçar.",
        ))
        return

    numero_dps = controle.proximo_numero()
    registro = RegistroLinha(
        linha_planilha=linha.numero_linha,
        situacao="ERRO_LOCAL",
        documento_tomador=linha.documento_tomador,
        razao_social=linha.razao_social,
        valor_servico=str(linha.valor_servico),
        numero_dps=str(numero_dps),
    )

    try:
        # 2) Monta a DPS e serializa
        dps = montar_dps(config, linha, numero_dps, competencia=competencia)
        xml_dps = dps_para_xml(dps)
        logger.debug("DPS %s montada: %s", numero_dps, dps)

        # 3) Assina em XMLDSig
        xml_assinado = assinar_dps(xml_dps, certificado)

        # 4) Dry-run: grava e para aqui
        if args.dry_run:
            destino = config.diretorio_notas / "dry-run"
            destino.mkdir(parents=True, exist_ok=True)
            # O número não é consumido em simulação, então o nome usa a linha
            # da planilha para não colidir entre si.
            caminho = destino / f"dps_simulada_linha{linha.numero_linha:03d}.xml"
            caminho.write_bytes(xml_assinado)
            logger.info("%s | DRY-RUN: DPS assinada salva em %s", rotulo, caminho)
            registro.situacao = "PULADA"
            registro.detalhe = "dry-run — não transmitida"
            registro.arquivo_xml = str(caminho)
            relatorio.adicionar(registro)
            controle.ultimo_numero -= 1     # não consome numeração em simulação
            return

        # 5) Envia
        pacote = empacotar_para_envio(xml_assinado)
        resposta = cliente.emitir(pacote)

        if resposta.autorizada:
            pdf = None
            if not args.sem_pdf and resposta.chave_acesso:
                try:
                    pdf = cliente.baixar_danfse(resposta.chave_acesso)
                except RuntimeError as exc:
                    # PDF é acessório: a nota já está autorizada.
                    logger.warning("Linha %s | DANFSe indisponível: %s", linha.numero_linha, exc)

            arquivos = salvar_nota(
                config.diretorio_notas, competencia, linha, resposta, xml_assinado, pdf
            )
            controle.registrar(impressao, numero_dps, resposta.chave_acesso)
            controle.salvar()   # persiste a cada nota: uma queda não perde a numeração

            logger.info("%s | AUTORIZADA | chave %s", rotulo, resposta.chave_acesso)
            if resposta.mensagens:
                logger.warning("   Alertas: %s", "; ".join(resposta.mensagens))
            registro.situacao = "AUTORIZADA"
            registro.chave_acesso = resposta.chave_acesso or ""
            registro.arquivo_xml = arquivos.get("xml_nfse", arquivos.get("xml_dps", ""))
            detalhes = list(resposta.mensagens)

            # 6) Entrega ao cliente. Falha aqui NÃO invalida a nota: ela já está
            #    autorizada na Sefin. Fica registrada no relatório para reenvio.
            try:
                situacao_email = enviar_nfse(
                    config_email,
                    config.ambiente,
                    linha.email,
                    {
                        "tomador": linha.razao_social,
                        "prestador": config.prestador.cnpj,
                        "competencia": competencia.strftime("%m/%Y"),
                        "descricao": linha.descricao,
                        "valor": f"{linha.valor_servico:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                        "chave": resposta.chave_acesso or "",
                        "chave_curta": (resposta.chave_acesso or "")[-8:],
                    },
                    arquivos,
                )
                logger.info("   E-mail: %s", situacao_email)
            except ErroEmail as exc:
                situacao_email = f"FALHA no e-mail: {exc}"
                logger.error("   %s", situacao_email)
            registro.email = situacao_email
            registro.detalhe = "; ".join(detalhes)
        else:
            caminho_erro = salvar_rejeicao(
                config.diretorio_notas, competencia, linha, resposta, xml_assinado
            )
            logger.error("%s | REJEITADA (HTTP %s): %s",
                         rotulo, resposta.status_http, resposta.motivo_erro)
            registro.situacao = "REJEITADA"
            registro.detalhe = resposta.motivo_erro
            registro.arquivo_xml = caminho_erro
            # Número não consumido: a Sefin não registrou nada.
            controle.ultimo_numero -= 1

    except Exception as exc:  # noqa: BLE001 — uma linha ruim não pode derrubar o lote
        logger.error("%s | ERRO LOCAL: %s", rotulo, exc)
        logger.debug("Detalhes:", exc_info=True)
        registro.situacao = "ERRO_LOCAL"
        registro.detalhe = str(exc)
        controle.ultimo_numero -= 1

    relatorio.adicionar(registro)


if __name__ == "__main__":
    raise SystemExit(executar())
