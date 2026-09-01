"""Gravação dos arquivos de retorno em pastas estruturadas por mês."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .cliente import RespostaEmissao
from .planilha import LinhaFaturamento

MESES = {
    1: "01-janeiro", 2: "02-fevereiro", 3: "03-marco", 4: "04-abril",
    5: "05-maio", 6: "06-junho", 7: "07-julho", 8: "08-agosto",
    9: "09-setembro", 10: "10-outubro", 11: "11-novembro", 12: "12-dezembro",
}


def pasta_do_mes(raiz: Path, referencia: date) -> Path:
    """notas/2026/09-setembro/ — criada sob demanda."""
    destino = raiz / str(referencia.year) / MESES[referencia.month]
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def salvar_nota(
    raiz: Path,
    referencia: date,
    linha: LinhaFaturamento,
    resposta: RespostaEmissao,
    xml_dps_assinada: bytes,
    pdf_danfse: bytes | None = None,
) -> dict[str, str]:
    """Grava XML da NFS-e, XML da DPS enviada, PDF e metadados. Devolve os caminhos."""
    destino = pasta_do_mes(raiz, referencia)
    identificador = resposta.chave_acesso or f"linha{linha.numero_linha}-sem-chave"
    prefixo = f"{identificador}_{linha.documento_tomador}"
    gravados: dict[str, str] = {}

    if resposta.xml_nfse:
        caminho = destino / f"{prefixo}_nfse.xml"
        caminho.write_bytes(resposta.xml_nfse)
        gravados["xml_nfse"] = str(caminho)

    # Guardar a DPS enviada é exigência prática de auditoria: é a prova do que
    # foi transmitido e assinado.
    caminho_dps = destino / f"{prefixo}_dps-assinada.xml"
    caminho_dps.write_bytes(xml_dps_assinada)
    gravados["xml_dps"] = str(caminho_dps)

    if pdf_danfse:
        caminho_pdf = destino / f"{prefixo}_danfse.pdf"
        caminho_pdf.write_bytes(pdf_danfse)
        gravados["pdf"] = str(caminho_pdf)

    metadados = {
        "linha_planilha": linha.numero_linha,
        "tomador": linha.razao_social,
        "documento_tomador": linha.documento_tomador,
        "valor_servico": str(linha.valor_servico),
        "chave_acesso": resposta.chave_acesso,
        "id_dps": resposta.id_dps,
        "status_http": resposta.status_http,
        "alertas": resposta.mensagens,
        "emitida_em": datetime.now().isoformat(timespec="seconds"),
        "arquivos": gravados,
    }
    caminho_meta = destino / f"{prefixo}_retorno.json"
    caminho_meta.write_text(
        json.dumps(metadados, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    gravados["metadados"] = str(caminho_meta)
    return gravados


def salvar_rejeicao(
    raiz: Path, referencia: date, linha: LinhaFaturamento, resposta: RespostaEmissao,
    xml_dps_assinada: bytes | None = None,
) -> str:
    """Guarda o retorno de uma nota rejeitada para conferência posterior."""
    destino = pasta_do_mes(raiz, referencia) / "rejeitadas"
    destino.mkdir(parents=True, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefixo = f"linha{linha.numero_linha:03d}_{linha.documento_tomador}_{marca}"

    if xml_dps_assinada:
        (destino / f"{prefixo}_dps.xml").write_bytes(xml_dps_assinada)

    caminho = destino / f"{prefixo}_erro.json"
    caminho.write_text(
        json.dumps(
            {
                "linha_planilha": linha.numero_linha,
                "tomador": linha.razao_social,
                "status_http": resposta.status_http,
                "erros": resposta.mensagens,
                "retorno_bruto": resposta.corpo_bruto,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return str(caminho)
