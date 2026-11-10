"""Gravação dos arquivos de retorno em pastas estruturadas por mês."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from lxml import etree

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


# ---------------------------------------------------------------------------
# Nomes de arquivo legíveis: "NFS-e 84 - CLIENTE LTDA - 09-2026.pdf"
# ---------------------------------------------------------------------------
_PROIBIDOS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
SUFIXO_DPS = " - dps-assinada.xml"
SUFIXO_DADOS = " - dados.json"
# Nomes antigos: "<chave>_<cnpj>_nfse.xml" etc.
_ANTIGO = re.compile(r"^(?P<prefixo>[0-9A-Za-z-]+_\d{11,14})_nfse\.xml$")

log = logging.getLogger(__name__)


def limpar_nome(texto: str, limite: int = 60) -> str:
    """Tira o que o Windows não aceita em nome de arquivo e encurta."""
    limpo = " ".join(_PROIBIDOS.sub(" ", texto or "").split())
    return limpo[:limite].rstrip(" .") or "cliente"


def nome_base(numero: str, tomador: str, competencia: date | None, teste: bool = False,
              rotulo: str = "NFS-e") -> str:
    base = f"{rotulo} {numero} - {limpar_nome(tomador)}"
    if competencia:
        base += f" - {competencia:%m-%Y}"
    return base + (" (teste)" if teste else "")


def dados_do_xml(xml: bytes) -> dict:
    """Número, competência, tomador e ambiente lidos do XML da NFS-e.

    Tolerante: campo ausente (ou XML que nem abre) vira vazio.
    """
    try:
        raiz = etree.fromstring(xml)
    except (etree.XMLSyntaxError, ValueError):
        return {"numero": "", "competencia": None, "tomador": "", "teste": False}

    def primeiro(caminho: str) -> str:
        achados = raiz.xpath(caminho)
        return (achados[0].text or "").strip() if achados else ""

    try:
        competencia = date.fromisoformat(primeiro("//*[local-name()='dCompet']")[:10])
    except ValueError:
        competencia = None
    return {
        "numero": primeiro("//*[local-name()='nNFSe']"),
        "competencia": competencia,
        "tomador": primeiro("//*[local-name()='toma']/*[local-name()='xNome']"),
        "teste": primeiro("//*[local-name()='tpAmb']") == "2",
    }


def caminho_pdf_de(xml: Path) -> Path:
    """PDF que acompanha um XML de nota (nome novo ou antigo)."""
    xml = Path(xml)
    if xml.name.endswith("_nfse.xml"):
        return xml.with_name(xml.name.replace("_nfse.xml", "_danfse.pdf"))
    return xml.with_suffix(".pdf")


def metadados_ao_lado(xml: Path) -> Path:
    """O "- dados.json" (ou "_retorno.json" antigo) que acompanha um XML de nota."""
    xml = Path(xml)
    if xml.name.endswith("_nfse.xml"):
        return xml.with_name(xml.name.replace("_nfse.xml", "_retorno.json"))
    if xml.name.endswith(SUFIXO_DPS):
        return xml.with_name(xml.name[: -len(SUFIXO_DPS)] + SUFIXO_DADOS)
    return xml.with_name(xml.stem + SUFIXO_DADOS)


def _base_livre(destino: Path, base: str, chave: str) -> str:
    """Duas notas com o mesmo nome (ex.: teste e real) não se sobrescrevem."""
    if not any((destino / f"{base}{fim}").exists() for fim in (".xml", ".pdf", SUFIXO_DADOS)):
        return base
    return f"{base} - {(chave or 'x')[-6:]}"


def salvar_nota(
    raiz: Path,
    referencia: date,
    linha: LinhaFaturamento,
    resposta: RespostaEmissao,
    xml_dps_assinada: bytes,
    pdf_danfse: bytes | None = None,
    numero_dps: str = "",
    ambiente: str = "producao",
) -> dict[str, str]:
    """Grava XML da NFS-e, XML da DPS enviada, PDF e metadados. Devolve os caminhos."""
    destino = pasta_do_mes(raiz, referencia)
    do_xml = dados_do_xml(resposta.xml_nfse) if resposta.xml_nfse else {}
    numero = do_xml.get("numero") or ""
    if numero:
        rotulo, identificador = "NFS-e", numero
    elif numero_dps:
        rotulo, identificador = "DPS", numero_dps
    else:
        rotulo, identificador = "Nota", f"linha {linha.numero_linha}"
    base = _base_livre(destino, nome_base(
        identificador, linha.razao_social, referencia, ambiente != "producao", rotulo,
    ), resposta.chave_acesso or "")
    gravados: dict[str, str] = {}

    if resposta.xml_nfse:
        caminho = destino / f"{base}.xml"
        caminho.write_bytes(resposta.xml_nfse)
        gravados["xml_nfse"] = str(caminho)

    # Guardar a DPS enviada é exigência prática de auditoria: é a prova do que
    # foi transmitido e assinado.
    caminho_dps = destino / f"{base}{SUFIXO_DPS}"
    caminho_dps.write_bytes(xml_dps_assinada)
    gravados["xml_dps"] = str(caminho_dps)

    if pdf_danfse:
        caminho_pdf = destino / f"{base}.pdf"
        caminho_pdf.write_bytes(pdf_danfse)
        gravados["pdf"] = str(caminho_pdf)

    metadados = {
        "linha_planilha": linha.numero_linha,
        "tomador": linha.razao_social,
        "documento_tomador": linha.documento_tomador,
        "valor_servico": str(linha.valor_servico),
        "chave_acesso": resposta.chave_acesso,
        "n_nfse": numero,
        "numero_dps": numero_dps,
        "competencia": referencia.strftime("%Y-%m"),
        "id_dps": resposta.id_dps,
        "status_http": resposta.status_http,
        "alertas": resposta.mensagens,
        "emitida_em": datetime.now().isoformat(timespec="seconds"),
        "arquivos": gravados,
    }
    caminho_meta = destino / f"{base}{SUFIXO_DADOS}"
    caminho_meta.write_text(
        json.dumps(metadados, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    gravados["metadados"] = str(caminho_meta)
    return gravados


def arquivos_de_metadados(raiz: Path):
    """Todos os metadados de notas (nome novo e antigo)."""
    raiz = Path(raiz)
    if not raiz.exists():
        return
    vistos = set()
    for padrao in (f"*{SUFIXO_DADOS}", "*_retorno.json"):
        for caminho in raiz.rglob(padrao):
            if caminho not in vistos and "rejeitadas" not in caminho.parts:
                vistos.add(caminho)
                yield caminho


def localizar_xml_da_nota(raiz: Path, chave: str, arquivos: dict | None = None) -> Path | None:
    """Acha o XML da nota pela chave, qualquer que seja o nome do arquivo."""
    if arquivos and arquivos.get("xml_nfse") and Path(arquivos["xml_nfse"]).is_file():
        return Path(arquivos["xml_nfse"])
    for meta in arquivos_de_metadados(raiz):
        try:
            dados = json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if dados.get("chave_acesso") != chave:
            continue
        caminho = Path((dados.get("arquivos") or {}).get("xml_nfse") or "")
        if caminho.is_file():
            return caminho
        # Pasta movida: o caminho gravado ficou velho, mas o arquivo está ao lado.
        vizinho = (meta.with_name(meta.name[: -len(SUFIXO_DADOS)] + ".xml")
                   if meta.name.endswith(SUFIXO_DADOS)
                   else meta.with_name(meta.name.replace("_retorno.json", "_nfse.xml")))
        if vizinho.is_file():
            return vizinho
    antigos = sorted(Path(raiz).rglob(f"{chave}_*_nfse.xml")) if Path(raiz).exists() else []
    return antigos[0] if antigos else None


def renomear_arquivos_antigos(raiz: Path) -> int:
    """Troca "<chave>_<cnpj>_*" por "NFS-e 84 - CLIENTE - 09-2026*". Idempotente.

    Só mexe em arquivos no padrão antigo que o próprio app criou; um arquivo
    que falhar fica como está (e vai para o log), sem parar os outros.
    """
    raiz = Path(raiz)
    if not raiz.exists():
        return 0
    total = 0
    for xml in sorted(raiz.rglob("*_nfse.xml")):
        casamento = _ANTIGO.match(xml.name)
        if not casamento or "rejeitadas" in xml.parts:
            continue
        prefixo = casamento["prefixo"]
        try:
            dados = dados_do_xml(xml.read_bytes())
            meta_antigo = xml.with_name(f"{prefixo}_retorno.json")
            meta = {}
            if meta_antigo.is_file():
                meta = json.loads(meta_antigo.read_text(encoding="utf-8"))
            numero = dados["numero"] or meta.get("numero_dps") or prefixo[-6:]
            rotulo = "NFS-e" if dados["numero"] else "Nota"
            tomador = meta.get("tomador") or dados["tomador"]
            base = _base_livre(xml.parent, nome_base(
                numero, tomador, dados["competencia"], dados["teste"], rotulo,
            ), meta.get("chave_acesso") or prefixo.split("_")[0])

            novos = {}
            for antigo_fim, novo_fim, chave_arq in (
                ("_nfse.xml", ".xml", "xml_nfse"), ("_danfse.pdf", ".pdf", "pdf"),
                ("_dps-assinada.xml", SUFIXO_DPS, "xml_dps"),
            ):
                antigo = xml.with_name(prefixo + antigo_fim)
                if antigo.is_file():
                    destino = xml.with_name(base + novo_fim)
                    antigo.rename(destino)
                    novos[chave_arq] = str(destino)
            if meta_antigo.is_file():
                meta.setdefault("n_nfse", dados["numero"])
                if dados["competencia"]:
                    meta.setdefault("competencia", dados["competencia"].strftime("%Y-%m"))
                meta["arquivos"] = {**(meta.get("arquivos") or {}), **novos}
                novo_meta = xml.with_name(base + SUFIXO_DADOS)
                novo_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                meta_antigo.unlink()
            total += 1
        except Exception:  # noqa: BLE001 — um arquivo ruim não trava os outros
            log.exception("Não foi possível renomear %s", xml)
    return total


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
