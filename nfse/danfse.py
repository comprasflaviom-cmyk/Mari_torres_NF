"""
Geração local do DANFSe (PDF), conforme a Nota Técnica SE/CGNFS-e nº 008 v1.02.

A Sefin desligou a API de download do DANFSe em 03/08/2026: daí em diante,
quem emite pela API precisa gerar o PDF sozinho, no leiaute do Anexo I da NT.
Este módulo faz isso a partir do XML da NFS-e autorizada (`*_nfse.xml`), que é
a única fonte permitida — a NT proíbe imprimir o que não estiver no XML.

Coordenadas e tamanhos seguem a tabela do item 2.4.5 da NT (em centímetros,
medidos da borda superior esquerda). Os blocos de destinatário e
intermediário, quando ausentes, viram uma linha só (itens 2.3.1 e 2.3.2), e a
altura economizada vai para "Descrição do Serviço" e "Informações
Complementares", como a NT permite.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from lxml import etree
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

DADOS = Path(__file__).resolve().parent / "dados"
URL_CONSULTA = "https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave="

CINZA_5 = Color(0.95, 0.95, 0.95)   # sombreamento de 5% (item 2.2.3)
VERMELHO = Color(1, 0, 0)           # M100/Y100 (item 2.4.3)

X0, LARG = 0.30, 20.40              # corpo do DANFSe
COL = (0.30, 5.41, 10.51, 15.62)    # colunas do Anexo I
L1, L2, L4 = 5.09, 10.19, 20.40     # larguras de 1, 2 e 4 colunas
ALT_LINHA = 0.64
ALT_BLOCO_VAZIO = 0.40              # mínimo 0,32 (notas 2 a 4)
TOPO_CANHOTO = 28.10
ALT_CANHOTO = 0.67

DESCRICOES = {
    "tpEmit": {"1": "Prestador", "2": "Tomador", "3": "Intermediário"},
    "cStat": {
        "100": "NFS-e Gerada", "101": "NFS-e de Substituição Gerada",
        "102": "NFS-e de Decisão Judicial", "103": "NFS-e Avulsa",
    },
    "finNFSe": {"0": "NFS-e regular"},
    "opSimpNac": {
        "1": "Não Optante",
        "2": "Optante - Microempreendedor Individual (MEI)",
        "3": "Optante - Microempresa ou Empresa de Pequeno Porte (ME/EPP)",
    },
    "regApTribSN": {
        "1": "Regime de apuração dos tributos federais e municipal pelo Simples Nacional",
        "2": "Regime de apuração dos tributos federais pelo SN e o ISSQN por fora do SN "
             "conforme respectiva legislação municipal do tributo",
        "3": "Regime de apuração dos tributos federais e municipal por fora do SN "
             "conforme respectivas legislações federal e municipal de cada tributo",
    },
    "tribISSQN": {
        "1": "Operação Tributável", "2": "Imunidade",
        "3": "Exportação de Serviço", "4": "Não Incidência",
    },
    "tpRetISSQN": {"1": "Não Retido", "2": "Retido pelo Tomador", "3": "Retido pelo Intermediário"},
    "regEspTrib": {
        "0": "Nenhum", "1": "Ato Cooperado (Cooperativa)", "2": "Estimativa",
        "3": "Microempresa Municipal", "4": "Notário ou Registrador",
        "5": "Profissional Autônomo", "6": "Sociedade de Profissionais", "9": "Outros",
    },
    "tpSusp": {
        "1": "Exigibilidade Suspensa por Decisão Judicial",
        "2": "Exigibilidade Suspensa por Processo Administrativo",
    },
    "tpRetPisCofins": {"0": "PIS/COFINS/CSLL Não Retidos"},
}


class ErroDANFSe(ValueError):
    """XML não é de uma NFS-e autorizada (falta infNFSe ou a chave)."""


# ---------------------------------------------------------------------------
# Leitura do XML
# ---------------------------------------------------------------------------
def _no(raiz, caminho: str):
    """Primeiro nó pelo caminho de nomes locais (ignora namespace)."""
    passos = "/".join(f"*[local-name()='{p}']" for p in caminho.split("/"))
    achados = raiz.xpath(passos)
    return achados[0] if achados else None


def _t(raiz, caminho: str) -> str:
    no = _no(raiz, caminho) if raiz is not None else None
    return (no.text or "").strip() if no is not None and no.text else ""


@lru_cache(maxsize=1)
def _municipios() -> dict[str, list[str]]:
    return json.loads((DADOS / "municipios_ibge.json").read_text(encoding="utf-8"))


def _municipio_uf(codigo: str) -> tuple[str, str]:
    nome, uf = _municipios().get(codigo, ["", ""])
    return nome, uf


# ---------------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------------
def _ou_traco(texto: str) -> str:
    return texto if texto else "-"


def _moeda(texto: str) -> str:
    if not texto:
        return "-"
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        return texto
    inteiro = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {inteiro}"


def _percentual(texto: str) -> str:
    if not texto:
        return "-"
    try:
        return f"{Decimal(texto):.2f}".replace(".", ",") + "%"
    except InvalidOperation:
        return texto


def _data(texto: str) -> str:
    try:
        return datetime.fromisoformat(texto[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return _ou_traco(texto)


def _data_hora(texto: str) -> str:
    try:
        return datetime.fromisoformat(texto).strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return _ou_traco(texto)


def _documento(no) -> str:
    if no is None:
        return "-"
    cnpj, cpf, nif = _t(no, "CNPJ"), _t(no, "CPF"), _t(no, "NIF")
    if len(cnpj) == 14:
        return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
    if len(cpf) == 11:
        return f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
    return _ou_traco(nif)


def _cep(cep: str) -> str:
    return f"{cep[:2]}.{cep[2:5]}-{cep[5:]}" if len(cep) == 8 else cep


def _codigo_tributacao(c_trib_nac: str, c_trib_mun: str) -> str:
    if not c_trib_nac:
        return "-"
    nacional = ".".join(c_trib_nac[i:i + 2] for i in range(0, len(c_trib_nac), 2))
    return f"{nacional} / {c_trib_mun}" if c_trib_mun else nacional


def _nbs(c_nbs: str) -> str:
    if len(c_nbs) == 9:
        return f"{c_nbs[0]}.{c_nbs[1:5]}.{c_nbs[5:7]}.{c_nbs[7:]}"
    return _ou_traco(c_nbs)


def _descricao(campo: str, codigo: str) -> str:
    if not codigo:
        return "-"
    return DESCRICOES.get(campo, {}).get(codigo, codigo)


def _endereco(no) -> dict[str, str]:
    """Município/UF, código IBGE/CEP e endereço de um grupo com end/enderNac."""
    if no is None:
        return {"municipio": "-", "ibge_cep": "-", "endereco": "-"}
    nac = _no(no, "end/endNac") if _no(no, "end") is not None else _no(no, "enderNac")
    base = _no(no, "end") if _no(no, "end") is not None else _no(no, "enderNac")
    ext = _no(no, "end/endExt")
    if ext is not None:
        municipio = _ou_traco(_t(ext, "xCidade"))
        ibge_cep = _ou_traco(_t(ext, "cEndPost"))
    else:
        c_mun = _t(nac, "cMun") if nac is not None else ""
        nome, uf = _municipio_uf(c_mun)
        uf = uf or (_t(nac, "UF") if nac is not None else "")
        municipio = f"{nome} / {uf}" if nome else "-"
        cep = _t(nac, "CEP") if nac is not None else ""
        ibge_cep = " / ".join(p for p in (c_mun, _cep(cep)) if p) or "-"
    partes = [_t(base, c) for c in ("xLgr", "nro", "xCpl", "xBairro")] if base is not None else []
    return {
        "municipio": municipio,
        "ibge_cep": ibge_cep,
        "endereco": ", ".join(p for p in partes if p) or "-",
    }


def ler_nfse(xml_nfse: bytes) -> dict:
    """Extrai do XML da NFS-e tudo o que o DANFSe imprime, já formatado."""
    raiz = etree.fromstring(xml_nfse)
    inf = raiz if etree.QName(raiz).localname == "infNFSe" else _no(raiz, "infNFSe")
    if inf is None:
        raise ErroDANFSe("O XML não contém infNFSe — não é uma NFS-e autorizada.")
    chave = (inf.get("Id") or "").removeprefix("NFS")
    if not chave:
        raise ErroDANFSe("A NFS-e não tem chave de acesso (atributo Id do infNFSe).")

    dps = _no(inf, "DPS/infDPS")
    prest = _no(dps, "prest") if dps is not None else None
    emit = _no(inf, "emit")
    toma = _no(dps, "toma") if dps is not None else None
    interm = _no(dps, "interm") if dps is not None else None
    dest = _no(dps, "IBSCBS/dest") if dps is not None else None
    trib_mun = _no(dps, "valores/trib/tribMun") if dps is not None else None
    trib_fed = _no(dps, "valores/trib/tribFed") if dps is not None else None
    valores = _no(inf, "valores")
    ibscbs = _no(inf, "IBSCBS")

    # O prestador vem da DPS; o que ela não traz (nome, endereço — omitidos
    # quando o emitente é o próprio prestador) vem do grupo emit da NFS-e.
    def do_prestador(campo: str) -> str:
        return _t(prest, campo) or _t(emit, campo)

    end_prest = _endereco(prest if _no(prest, "end") is not None else emit) if (prest is not None or emit is not None) else _endereco(None)
    c_mun_emi = _t(emit, "enderNac/cMun")
    _, uf_emi = _municipio_uf(c_mun_emi)
    uf_emi = uf_emi or _t(emit, "enderNac/UF")
    _, uf_prest = _municipio_uf(_t(dps, "serv/locPrest/cLocPrestacao"))
    _, uf_incid = _municipio_uf(_t(inf, "cLocIncid"))

    tp_ret_pc = _t(trib_fed, "piscofins/tpRetPisCofins")
    v_pis, v_cofins = _t(trib_fed, "piscofins/vPis"), _t(trib_fed, "piscofins/vCofins")
    v_csll = _t(trib_fed, "vRetCSLL")
    if tp_ret_pc == "1":
        soma = sum((Decimal(v) for v in (v_csll, v_pis, v_cofins) if v), Decimal("0"))
        contrib_retidas = _moeda(str(soma)) if any((v_csll, v_pis, v_cofins)) else "-"
        v_pis = v_cofins = "0.00"
    else:
        contrib_retidas = _moeda(v_csll)

    def soma_moeda(*textos: str) -> str:
        presentes = [Decimal(t) for t in textos if t]
        return _moeda(str(sum(presentes, Decimal("0")))) if presentes else "-"

    v_ibs, v_cbs = _t(ibscbs, "totCIBS/gIBS/vIBSTot"), _t(ibscbs, "totCIBS/gCBS/vCBS")
    v_tot_fed = _t(dps, "valores/trib/totTrib/vTotTrib/vTotTribFed")
    if v_tot_fed or _t(dps, "valores/trib/totTrib/vTotTrib/vTotTribEst"):
        aprox = [_moeda(_t(dps, f"valores/trib/totTrib/vTotTrib/vTotTrib{s}")) for s in ("Fed", "Est", "Mun")]
    else:
        aprox = [_percentual(_t(dps, f"valores/trib/totTrib/pTotTrib/pTotTrib{s}")) for s in ("Fed", "Est", "Mun")]

    complementares = []
    for rotulo, valor in (
        ("Inf. Cont.", _t(dps, "serv/infoCompl/xInfComp")),
        ("NFS-e Subst.", _t(dps, "subst/chSubstda")),
        ("Doc. Ref.", _t(dps, "serv/infoCompl/docRef")),
        ("Cod. Obra", _t(dps, "serv/obra/cObra")),
        ("Insc. Imob.", _t(dps, "serv/obra/inscImobFisc")),
        ("Cod. Evt.", _t(dps, "serv/atvEvento/idAtvEvt")),
        ("Doc. Tec.", _t(dps, "serv/infoCompl/idDocTec")),
        ("Núm. Ped.", _t(dps, "serv/infoCompl/gItemPed/xPed")),
        ("Item Ped.", _t(dps, "serv/infoCompl/gItemPed/xItemPed")),
        ("Inf. A. T. Mun.", _t(inf, "xOutInf")),
    ):
        if valor:
            complementares.append(f"{rotulo}: {valor}")

    reg_esp = _t(prest, "regTrib/regEspTrib")

    return {
        "chave": chave,
        "tp_amb": _t(dps, "tpAmb"),
        "amb_ger": _t(inf, "ambGer"),
        "municipio_emissor": f"{_t(inf, 'xLocEmi')} / {uf_emi}" if _t(inf, "xLocEmi") else "-",
        "c_trib_nac": _t(dps, "serv/cServ/cTribNac"),
        "n_nfse": _ou_traco(_t(inf, "nNFSe")),
        "competencia": _data(_t(dps, "dCompet")),
        "ano_competencia": _t(dps, "dCompet")[:4],
        "dh_nfse": _data_hora(_t(inf, "dhProc")),
        "n_dps": _ou_traco(_t(dps, "nDPS")),
        "serie": _ou_traco(_t(dps, "serie")),
        "dh_dps": _data_hora(_t(dps, "dhEmi")),
        "emitente": _descricao("tpEmit", _t(dps, "tpEmit")),
        "situacao": _descricao("cStat", _t(inf, "cStat")),
        "finalidade": _descricao("finNFSe", _t(dps, "IBSCBS/finNFSe")),
        "prestador": {
            "documento": _documento(prest if prest is not None else emit),
            "im": _ou_traco(do_prestador("IM")),
            "fone": _ou_traco(do_prestador("fone")),
            "nome": _ou_traco(do_prestador("xNome")),
            **end_prest,
            "email": _ou_traco(do_prestador("email")),
            "simples": _descricao("opSimpNac", _t(prest, "regTrib/opSimpNac")),
            "apuracao": _descricao("regApTribSN", _t(prest, "regTrib/regApTribSN")),
        },
        "tomador": None if toma is None else {
            "documento": _documento(toma), "im": _ou_traco(_t(toma, "IM")),
            "fone": _ou_traco(_t(toma, "fone")), "nome": _ou_traco(_t(toma, "xNome")),
            **_endereco(toma), "email": _ou_traco(_t(toma, "email")),
        },
        "destinatario": None if dest is None else {
            "documento": _documento(dest), "fone": _ou_traco(_t(dest, "fone")),
            "nome": _ou_traco(_t(dest, "xNome")), **_endereco(dest),
            "email": _ou_traco(_t(dest, "email")),
        },
        "intermediario": None if interm is None else {
            "documento": _documento(interm), "im": _ou_traco(_t(interm, "IM")),
            "fone": _ou_traco(_t(interm, "fone")), "nome": _ou_traco(_t(interm, "xNome")),
            **_endereco(interm), "email": _ou_traco(_t(interm, "email")),
        },
        "servico": {
            "codigo": _codigo_tributacao(_t(dps, "serv/cServ/cTribNac"), _t(dps, "serv/cServ/cTribMun")),
            "nbs": _nbs(_t(dps, "serv/cServ/cNBS")),
            "local": " / ".join((_ou_traco(_t(inf, "xLocPrestacao")), _ou_traco(uf_prest),
                                 _ou_traco(_t(dps, "serv/locPrest/cPaisPrestacao")))),
            "desc_codigo": _t(inf, "xTribMun") or _t(inf, "xTribNac") or "-",
            "descricao": _ou_traco(_t(dps, "serv/cServ/xDescServ")),
        },
        "issqn": None if trib_mun is None else {
            "tipo": _descricao("tribISSQN", _t(trib_mun, "tribISSQN")),
            "incidencia": " / ".join((_ou_traco(_t(inf, "xLocIncid")), _ou_traco(uf_incid),
                                      _ou_traco(_t(trib_mun, "cPaisResult")))),
            "linha_especial": [
                "-" if reg_esp in ("", "0") else _descricao("regEspTrib", reg_esp),
                _ou_traco(_t(trib_mun, "tpImunidade")),
                _descricao("tpSusp", _t(trib_mun, "exigSusp/tpSusp")),
                _ou_traco(_t(trib_mun, "exigSusp/nProcesso")),
            ],
            "linha_beneficio": [
                _ou_traco(_t(valores, "tpBM")),
                _moeda(_t(valores, "vCalcBM") or _t(trib_mun, "BM/vRedBCBM")),
                _moeda(_t(dps, "valores/vDedRed/vDR") or _t(valores, "vCalcDR")),
                _moeda(_t(dps, "valores/vDescCondIncond/vDescIncond")),
            ],
            "bc": _moeda(_t(valores, "vBC")),
            "aliquota": _percentual(_t(valores, "pAliqAplic")),
            "retencao": _descricao("tpRetISSQN", _t(trib_mun, "tpRetISSQN")),
            "apurado": _moeda(_t(valores, "vISSQN")),
        },
        "federal": {
            "irrf": _moeda(_t(trib_fed, "vRetIRRF")),
            "cp": _moeda(_t(trib_fed, "vRetCP")),
            "contrib": contrib_retidas,
            "pis": _moeda(v_pis),
            "cofins": _moeda(v_cofins),
            "desc_contrib": _descricao("tpRetPisCofins", tp_ret_pc),
        },
        "ibscbs": {
            "cst": f"{_ou_traco(_t(dps, 'IBSCBS/valores/trib/gIBSCBS/CST'))} / "
                   f"{_ou_traco(_t(dps, 'IBSCBS/valores/trib/gIBSCBS/cClassTrib'))}",
            "indicador": " / ".join(_ou_traco(v) for v in (
                _t(dps, "IBSCBS/cIndOp"), _t(ibscbs, "cLocalidadeIncid"),
                _t(ibscbs, "xLocalidadeIncid"), _municipio_uf(_t(ibscbs, "cLocalidadeIncid"))[1],
            )),
            "exclusoes": soma_moeda(
                _t(dps, "valores/vDescCondIncond/vDescIncond"),
                _t(ibscbs, "valores/vCalcReeRepRes"), _t(valores, "vISSQN"),
                _t(trib_fed, "piscofins/vPis"), _t(trib_fed, "piscofins/vCofins"),
            ) if ibscbs is not None else "-",
            "bc": _moeda(_t(ibscbs, "valores/vBC")),
            "red_aliq": " / ".join(_percentual(_t(ibscbs, c)) for c in (
                "valores/uf/pRedAliqUF", "valores/mun/pRedAliqMun", "valores/fed/pRedAliqCBS")),
            "aliq_ibs": " / ".join(_percentual(_t(ibscbs, c)) for c in (
                "valores/uf/pIBSUF", "valores/mun/pIBSMun")),
            "efet_mun": _percentual(_t(ibscbs, "valores/mun/pAliqEfetMun")),
            "valor_mun": _moeda(_t(ibscbs, "totCIBS/gIBS/gIBSMunTot/vIBSMun")),
            "efet_uf": _percentual(_t(ibscbs, "valores/uf/pAliqEfetUF")),
            "valor_uf": _moeda(_t(ibscbs, "totCIBS/gIBS/gIBSUFTot/vIBSUF")),
            "total_ibs": _moeda(v_ibs),
            "aliq_cbs": _percentual(_t(ibscbs, "valores/fed/pCBS")),
            "efet_cbs": _percentual(_t(ibscbs, "valores/fed/pAliqEfetCBS")),
            "total_cbs": _moeda(v_cbs),
        },
        "totais": {
            "servico": _moeda(_t(dps, "valores/vServPrest/vServ")),
            "desc_incond": _moeda(_t(dps, "valores/vDescCondIncond/vDescIncond")),
            "desc_cond": _moeda(_t(dps, "valores/vDescCondIncond/vDescCond")),
            "retencoes": _moeda(_t(valores, "vTotalRet")),
            "liquido": _moeda(_t(valores, "vLiq")),
            "ibscbs": soma_moeda(v_ibs, v_cbs),
            "liquido_ibscbs": _moeda(_t(ibscbs, "totCIBS/vTotNF")),
        },
        "complementares": complementares,
        "aproximados": (
            "Totais Aproximados dos Tributos cfe. Lei nº 12.741/2012: "
            f"Federais: {aprox[0]}; Estaduais: {aprox[1]}; Municipais: {aprox[2]}"
        ),
    }


# ---------------------------------------------------------------------------
# Fontes: Arial nos títulos, Microsoft Sans Serif no conteúdo (item 2.4).
# Existem em todo Windows; fora dele, Helvetica tem as mesmas métricas do Arial.
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _fontes() -> dict[str, str]:
    pasta = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    fontes = {"titulo": "Helvetica-Bold", "titulo_normal": "Helvetica", "conteudo": "Helvetica"}
    for nome, arquivo, papel in (
        ("Arial-Bold", "arialbd.ttf", "titulo"),
        ("Arial", "arial.ttf", "titulo_normal"),
        ("MicrosoftSansSerif", "micross.ttf", "conteudo"),
    ):
        caminho = pasta / arquivo
        if caminho.exists():
            try:
                pdfmetrics.registerFont(TTFont(nome, str(caminho)))
                fontes[papel] = nome
            except Exception:  # noqa: BLE001 — fonte corrompida: fica a Helvetica
                pass
    return fontes


# ---------------------------------------------------------------------------
# Desenho
# ---------------------------------------------------------------------------
class _Pagina:
    """Canvas em centímetros, com y medido de cima para baixo (como na NT)."""

    def __init__(self, canvas: Canvas):
        self.c = canvas
        self.f = _fontes()
        self.altura = A4[1]

    def y(self, y_cm: float) -> float:
        return self.altura - y_cm * cm

    def caixa(self, x, y, w, h, fundo=None, borda=False):
        self.c.setLineWidth(0.5)
        self.c.setFillColor(fundo or white)
        self.c.rect(x * cm, self.y(y + h), w * cm, h * cm,
                    stroke=1 if borda else 0, fill=1 if fundo else 0)
        self.c.setFillColor(black)

    def linha_h(self, y):
        self.c.setLineWidth(0.5)
        self.c.line(X0 * cm, self.y(y), (X0 + LARG) * cm, self.y(y))

    def texto(self, x, y_base, texto, fonte, tamanho, largura=None, cor=black, centro=False):
        texto = self.caber(texto, fonte, tamanho, largura) if largura else texto
        self.c.setFillColor(cor)
        self.c.setFont(fonte, tamanho)
        if centro:
            self.c.drawCentredString(x * cm, self.y(y_base), texto)
        else:
            self.c.drawString(x * cm, self.y(y_base), texto)
        self.c.setFillColor(black)

    def caber(self, texto, fonte, tamanho, largura_cm):
        """Corta com reticências o que não cabe na largura (item 2.1)."""
        limite = (largura_cm - 0.15) * cm
        if pdfmetrics.stringWidth(texto, fonte, tamanho) <= limite:
            return texto
        while texto and pdfmetrics.stringWidth(texto + "...", fonte, tamanho) > limite:
            texto = texto[:-1]
        return texto.rstrip() + "..."

    def campo(self, x, y, w, rotulo, valor, h=ALT_LINHA, rotulo_id=False, fundo=None):
        """Rótulo em cima, conteúdo embaixo — o formato de todas as células."""
        if fundo:
            self.caixa(x, y, w, h, fundo=fundo)
        tam_rotulo = 7 if rotulo_id else 6
        self.texto(x + 0.07, y + 0.25, rotulo, self.f["titulo"], tam_rotulo, w)
        self.texto(x + 0.07, y + 0.52, valor, self.f["conteudo"], 7, w)

    def titulo_bloco(self, y, titulo, h=ALT_LINHA):
        self.caixa(COL[0], y, L1, h, fundo=CINZA_5)
        self.texto(COL[0] + 0.07, y + 0.30, titulo.upper(), self.f["titulo"], 7, L1)

    def bloco_vazio(self, y, mensagem) -> float:
        self.linha_h(y)
        self.texto(X0 + LARG / 2, y + 0.27, mensagem, self.f["titulo_normal"], 7, LARG, centro=True)
        return y + ALT_BLOCO_VAZIO

    def paragrafo(self, x, y_topo, largura, texto, fonte, tamanho, max_linhas) -> int:
        linhas = []
        for trecho in texto.split("\n"):
            linhas.extend(simpleSplit(trecho, fonte, tamanho, (largura - 0.15) * cm) or [""])
        if len(linhas) > max_linhas:
            linhas = linhas[:max_linhas]
            linhas[-1] = self.caber(linhas[-1] + " ...", fonte, tamanho, largura)
        passo = tamanho * 1.25 / cm * 1.0
        for i, linha in enumerate(linhas):
            self.texto(x + 0.07, y_topo + (i + 1) * passo, linha, fonte, tamanho)
        return len(linhas)


def _altura_texto(texto: str, fonte: str, tamanho: float, largura: float) -> float:
    linhas = sum(
        len(simpleSplit(t, fonte, tamanho, (largura - 0.15) * cm) or [""]) for t in texto.split("\n")
    )
    return linhas * tamanho * 1.25 / cm


def _bloco_pessoa(p: _Pagina, y: float, titulo: str, d: dict, com_im: bool = True) -> float:
    p.linha_h(y)
    p.titulo_bloco(y, titulo)
    p.campo(COL[1], y, L1, "CNPJ / CPF / NIF", d["documento"])
    if com_im:
        p.campo(COL[2], y, L1, "Indicador Municipal (Inscrição)", d["im"])
    p.campo(COL[3], y, L1, "Telefone", d["fone"])
    y += ALT_LINHA
    p.campo(COL[0], y, L2, "Nome / Nome Empresarial", d["nome"])
    p.campo(COL[2], y, L1, "Município / Sigla UF", d["municipio"])
    p.campo(COL[3], y, L1, "Código IBGE / CEP", d["ibge_cep"])
    y += ALT_LINHA
    p.campo(COL[0], y, L2, "Endereço", d["endereco"])
    p.campo(COL[2], y, L2, "E-mail", d["email"])
    return y + ALT_LINHA


def gerar_danfse(xml_nfse: bytes) -> bytes:
    """PDF do DANFSe (A4, uma página) a partir do XML da NFS-e autorizada."""
    d = ler_nfse(xml_nfse)
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=A4)
    canvas.setTitle(f"DANFSe {d['n_nfse']} - {d['chave']}")
    p = _Pagina(canvas)
    f = p.f

    # Borda da página: 1 ponto, a 0,2 cm das bordas do papel (itens 2.2.2/2.2.3).
    canvas.setLineWidth(1)
    canvas.rect(0.2 * cm, 0.2 * cm, (21.0 - 0.4) * cm, (29.7 - 0.4) * cm)

    # ---- Cabeçalho (item 2.4.3) ------------------------------------------
    p.caixa(X0, 0.30, LARG, 1.16, fundo=CINZA_5)
    logo = DADOS / "logo_nfse.png"
    if logo.exists():
        canvas.drawImage(ImageReader(str(logo)), 0.49 * cm, p.y(0.44 + 0.81), 4.0 * cm, 0.81 * cm,
                         mask="auto", preserveAspectRatio=True)
    centro = 5.41 + 10.19 / 2
    p.texto(centro, 0.72, "DANFSe v2.0", f["titulo"], 9, centro=True)
    p.texto(centro, 1.05, "Documento Auxiliar da NFS-e", f["titulo"], 9, centro=True)
    if d["tp_amb"] == "2":
        p.texto(centro, 1.38, "NFS-e SEM VALIDADE JURÍDICA", f["titulo"], 9, cor=VERMELHO, centro=True)
    if not d["c_trib_nac"].startswith("99"):
        p.texto(15.62, 0.62, f"Município: {d['municipio_emissor']}", f["conteudo"], 8, 5.09)
    p.texto(15.62, 1.08, f"Ambiente Gerador: {d['amb_ger'] or '-'}", f["conteudo"], 6, 5.09)
    p.texto(15.62, 1.33, f"Tipo de Ambiente: {d['tp_amb'] or '-'}", f["conteudo"], 6, 5.09)
    p.linha_h(1.46)

    # ---- Dados da NFS-e ---------------------------------------------------
    p.campo(COL[0], 1.48, 15.30, "CHAVE DE ACESSO DA NFS-E", d["chave"], h=0.77, rotulo_id=True)
    linhas_id = (
        (2.27, (("NÚMERO DA NFS-E", d["n_nfse"]), ("COMPETÊNCIA DA NFS-E", d["competencia"]),
                ("DATA E HORA DA EMISSÃO DA NFS-E", d["dh_nfse"]))),
        (2.96, (("NÚMERO DA DPS", d["n_dps"]), ("SÉRIE DA DPS", d["serie"]),
                ("DATA E HORA DA EMISSÃO DA DPS", d["dh_dps"]))),
        (3.65, (("EMITENTE DA NFS-E", d["emitente"]), ("SITUAÇÃO DA NFS-E", d["situacao"]),
                ("FINALIDADE", d["finalidade"]))),
    )
    for y, campos in linhas_id:
        for i, (rotulo, valor) in enumerate(campos):
            fundo = CINZA_5 if rotulo == "EMITENTE DA NFS-E" else None
            p.campo(COL[i], y, L1, rotulo, valor, h=0.67, rotulo_id=True, fundo=fundo)

    qr = QrCodeWidget(URL_CONSULTA + d["chave"], barLevel="M")
    x1, y1, x2, y2 = qr.getBounds()
    lado = 1.52 * cm
    desenho = Drawing(lado, lado, transform=[lado / (x2 - x1), 0, 0, lado / (y2 - y1), 0, 0])
    desenho.add(qr)
    renderPDF.draw(desenho, canvas, 17.48 * cm, p.y(1.67 + 1.52))
    for i, linha in enumerate((
        "A autenticidade desta NFS-e pode ser verificada",
        "pela leitura deste código QR ou pela consulta da",
        "chave de acesso no portal nacional da NFS-e",
    )):
        p.texto(15.80, 3.55 + i * 0.24, linha, f["conteudo"], 6)

    # ---- Blocos de pessoas -------------------------------------------------
    y = 4.34
    pr = d["prestador"]
    y = _bloco_pessoa(p, y, "Prestador / Fornecedor", pr)
    p.campo(COL[0], y, L1, "Simples Nacional na Data de Competência", pr["simples"])
    p.campo(COL[1], y, 15.29, "Regime de Apuração Tributária pelo SN", pr["apuracao"])
    y += ALT_LINHA

    economia = 0.0  # altura liberada por blocos suprimidos (itens 2.3.1/2.3.2)
    if d["tomador"]:
        y = _bloco_pessoa(p, y, "Tomador / Adquirente", d["tomador"])
    else:
        y = p.bloco_vazio(y, "TOMADOR/ADQUIRENTE DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
        economia += 3 * ALT_LINHA - ALT_BLOCO_VAZIO
    if d["destinatario"]:
        y = _bloco_pessoa(p, y, "Destinatário da Operação", d["destinatario"], com_im=False)
    else:
        y = p.bloco_vazio(y, "DESTINATÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
        economia += 3 * ALT_LINHA - ALT_BLOCO_VAZIO
    if d["intermediario"]:
        y = _bloco_pessoa(p, y, "Intermediário da Operação", d["intermediario"])
    else:
        y = p.bloco_vazio(y, "INTERMEDIÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e")
        economia += 3 * ALT_LINHA - ALT_BLOCO_VAZIO

    # ---- Serviço prestado --------------------------------------------------
    s = d["servico"]
    p.linha_h(y)
    p.titulo_bloco(y, "Serviço Prestado")
    p.campo(COL[1], y, L1, "Código de Tributação Nacional / Municipal", s["codigo"])
    p.campo(COL[2], y, L1, "Código da NBS", s["nbs"])
    p.campo(COL[3], y, L1, "Local da Prestação / Sigla UF / País", s["local"])
    y += ALT_LINHA
    p.texto(X0 + 0.07, y + 0.27, s["desc_codigo"], f["conteudo"], 7, LARG)
    y += 0.38
    p.texto(X0 + 0.07, y + 0.25, "Descrição do Serviço", f["titulo"], 6)

    # Altura da descrição: o mínimo do Anexo I (cerca de 3 cm) mais metade do
    # que os blocos suprimidos liberaram; a outra metade fica para as
    # informações complementares.
    alt_desc = max(2.9 + economia / 2, _altura_texto(s["descricao"], f["conteudo"], 7, LARG) + 0.35)
    alt_desc = min(alt_desc, 6.0)
    linhas_desc = int((alt_desc - 0.35) / (7 * 1.25 / cm))
    p.paragrafo(X0, y + 0.30, LARG, s["descricao"], f["conteudo"], 7, linhas_desc)
    y += alt_desc

    # ---- Tributação municipal ----------------------------------------------
    m = d["issqn"]
    p.linha_h(y)
    if m is None:
        y = p.bloco_vazio(y, "TRIBUTAÇÃO MUNICIPAL (ISSQN) - OPERAÇÃO NÃO SUJEITA AO ISSQN")
    else:
        p.titulo_bloco(y, "Tributação Municipal (ISSQN)")
        p.campo(COL[1], y, L1, "Tipo de Tributação do ISSQN", m["tipo"])
        p.campo(COL[2], y, L2, "Município / Sigla UF / País de Incidência do ISSQN", m["incidencia"])
        y += ALT_LINHA
        for rotulos, valores in (
            (("Regime Especial de Tributação do ISSQN", "Tipo de Imunidade do ISSQN",
              "Suspensão da Exigibilidade do ISSQN", "Número Processo Suspensão"), m["linha_especial"]),
            (("Benefício Municipal", "Cálculo do BM", "Total Deduções/Reduções",
              "Desconto Incondicionado"), m["linha_beneficio"]),
        ):
            if all(v == "-" for v in valores):
                continue  # nota 5: linha sem nenhum dado pode ser suprimida
            for i, (rotulo, valor) in enumerate(zip(rotulos, valores)):
                p.campo(COL[i], y, L1, rotulo, valor)
            y += ALT_LINHA
        for i, (rotulo, valor) in enumerate((
            ("BC ISSQN", m["bc"]), ("Alíquota Aplicada", m["aliquota"]),
            ("Retenção do ISSQN", m["retencao"]), ("ISSQN Apurado", m["apurado"]),
        )):
            p.campo(COL[i], y, L1, rotulo, valor)
        y += ALT_LINHA

    # ---- Tributação federal -------------------------------------------------
    fe = d["federal"]
    p.linha_h(y)
    p.titulo_bloco(y, "Tributação Federal (Exceto CBS)")
    p.campo(COL[1], y, L1, "IRRF", fe["irrf"])
    p.campo(COL[2], y, L1, "Contribuição Previdenciária - Retida", fe["cp"])
    p.campo(COL[3], y, L1, "Contribuições Sociais - Retidas", fe["contrib"])
    y += ALT_LINHA
    if d["ano_competencia"] and d["ano_competencia"] <= "2026":  # nota 6
        p.campo(COL[0], y, L1, "PIS - Débito Apuração Própria", fe["pis"])
        p.campo(COL[1], y, L1, "COFINS - Débito Apuração Própria", fe["cofins"])
        p.campo(COL[2], y, L2, "Descrição Contrib. Sociais - Retidas", fe["desc_contrib"])
        y += ALT_LINHA

    # ---- Tributação IBS/CBS --------------------------------------------------
    ib = d["ibscbs"]
    p.linha_h(y)
    p.titulo_bloco(y, "Tributação IBS / CBS")
    p.campo(COL[1], y, L1, "CST / cClassTrib", ib["cst"])
    p.campo(COL[2], y, L2, "Indicador de Operação / Código IBGE Incidência / Município Incidência / Sigla UF",
            ib["indicador"])
    y += ALT_LINHA
    for linha in (
        (("Exclusões e Reduções da Base de Cálculo", ib["exclusoes"]),
         ("Base de Cálculo Após Exclusões e Reduções", ib["bc"]),
         ("Red. Alíquota IBS / Red. Alíquota CBS", ib["red_aliq"]),
         ("Alíquota - IBS UF / IBS Mun", ib["aliq_ibs"])),
        (("Alíq. Efetiva Municipal - IBS", ib["efet_mun"]),
         ("Valor Apurado Municipal - IBS", ib["valor_mun"]),
         ("Alíq. Efetiva Estadual - IBS", ib["efet_uf"]),
         ("Valor Apurado Estadual - IBS", ib["valor_uf"])),
        (("Valor Total Apurado - IBS", ib["total_ibs"]), ("Alíquota - CBS", ib["aliq_cbs"]),
         ("Alíquota Efetiva - CBS", ib["efet_cbs"]), ("Valor Total Apurado - CBS", ib["total_cbs"])),
    ):
        for i, (rotulo, valor) in enumerate(linha):
            p.campo(COL[i], y, L1, rotulo, valor)
        y += ALT_LINHA

    # ---- Valor total --------------------------------------------------------
    t = d["totais"]
    p.linha_h(y)
    p.titulo_bloco(y, "Valor Total da NFS-e", h=0.67)
    p.campo(COL[1], y, L1, "VALOR DA OPERAÇÃO / SERVIÇO", t["servico"], h=0.67, rotulo_id=True)
    p.campo(COL[2], y, L1, "Desconto Incondicionado", t["desc_incond"], h=0.67)
    p.campo(COL[3], y, L1, "Desconto Condicionado", t["desc_cond"], h=0.67)
    y += 0.67
    p.campo(COL[0], y, L1, "Total das Retenções (ISSQN / Federais)", t["retencoes"], h=0.67)
    p.campo(COL[1], y, L1, "VALOR LÍQUIDO DA NFS-E", t["liquido"], h=0.67, rotulo_id=True)
    p.campo(COL[2], y, L1, "Total do IBS/CBS", t["ibscbs"], h=0.67)
    p.campo(COL[3], y, L1, "VALOR LÍQUIDO DA NFS-E + IBS/CBS", t["liquido_ibscbs"], h=0.67,
            rotulo_id=True, fundo=CINZA_5)
    y += 0.67

    # ---- Informações complementares ----------------------------------------
    p.linha_h(y)
    p.texto(X0 + 0.07, y + 0.30, "INFORMAÇÕES COMPLEMENTARES", f["titulo"], 7)
    y += 0.39
    texto = " | ".join(d["complementares"])
    disponivel = TOPO_CANHOTO - 0.15 - y
    max_linhas = max(1, int(disponivel / (7 * 1.25 / cm)) - 1)
    usadas = p.paragrafo(X0, y, LARG, texto, f["conteudo"], 7, max_linhas) if texto else 0
    # Linha fixa e obrigatória (nota 10), nunca cortada pelas reticências.
    p.texto(X0 + 0.07, y + (usadas + 1) * (7 * 1.25 / cm), d["aproximados"], f["conteudo"], 7, LARG)

    # ---- Canhoto (opcional, mantido como no modelo) -------------------------
    p.caixa(X0, TOPO_CANHOTO, LARG, ALT_CANHOTO, borda=True)
    canvas.line(COL[1] * cm, p.y(TOPO_CANHOTO), COL[1] * cm, p.y(TOPO_CANHOTO + ALT_CANHOTO))
    canvas.line(COL[2] * cm, p.y(TOPO_CANHOTO), COL[2] * cm, p.y(TOPO_CANHOTO + ALT_CANHOTO))
    p.texto(COL[0] + 0.07, TOPO_CANHOTO + 0.27, "DATA CIENTIFICAÇÃO:", f["titulo"], 6)
    p.texto(COL[1] + 0.07, TOPO_CANHOTO + 0.27, "IDENTIFICAÇÃO E ASSINATURA", f["titulo"], 6)
    p.campo(COL[2], TOPO_CANHOTO, L2, "Nº NFS-e / CHAVE NFS-e", f"{d['n_nfse']} / {d['chave']}",
            h=ALT_CANHOTO)

    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def gerar_danfse_do_arquivo(caminho_xml: Path) -> Path:
    """Gera `*_danfse.pdf` ao lado de um `*_nfse.xml` já arquivado."""
    caminho_xml = Path(caminho_xml)
    destino = caminho_xml.with_name(caminho_xml.name.replace("_nfse.xml", "_danfse.pdf"))
    destino.write_bytes(gerar_danfse(caminho_xml.read_bytes()))
    return destino


if __name__ == "__main__":
    import sys

    for argumento in sys.argv[1:]:
        print(gerar_danfse_do_arquivo(Path(argumento)))
