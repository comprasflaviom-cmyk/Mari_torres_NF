"""Nomes de arquivo legíveis ("NFS-e 84 - CLIENTE - 09-2026.pdf") e a migração dos antigos."""

from __future__ import annotations

import json
from datetime import date

from nfse.armazenamento import (
    dados_do_xml, limpar_nome, localizar_xml_da_nota, nome_base, renomear_arquivos_antigos,
)
from nfse.clientes import BancoLocal, RepositorioEmissoes
from tests.conftest import CHAVE_EXEMPLO, montar_nfse_xml


def test_limpar_nome_tira_o_que_o_windows_recusa():
    assert limpar_nome('A/B: "C" <D>|E*?') == "A B C D E"
    assert len(limpar_nome("X" * 200)) == 60
    assert limpar_nome("   ") == "cliente"


def test_nome_base():
    assert nome_base("84", "RMF Palhares", date(2026, 9, 1)) == "NFS-e 84 - RMF Palhares - 09-2026"
    assert nome_base("5", "Ana", date(2026, 9, 1), teste=True).endswith("(teste)")


def test_dados_do_xml(config, linha):
    dados = dados_do_xml(montar_nfse_xml(config, linha))
    assert dados["numero"] == "84"
    assert dados["competencia"] is not None
    assert dados_do_xml(b"isto nao e xml")["numero"] == ""


def _nota_antiga(pasta, config, linha):
    pasta.mkdir(parents=True)
    prefixo = f"{CHAVE_EXEMPLO}_11222333000181"
    (pasta / f"{prefixo}_nfse.xml").write_bytes(montar_nfse_xml(config, linha))
    (pasta / f"{prefixo}_danfse.pdf").write_bytes(b"%PDF-1.4")
    (pasta / f"{prefixo}_dps-assinada.xml").write_bytes(b"<DPS/>")
    (pasta / f"{prefixo}_retorno.json").write_text(json.dumps({
        "chave_acesso": CHAVE_EXEMPLO, "tomador": "Cliente Alfa Tecnologia LTDA",
        "documento_tomador": "11222333000181", "valor_servico": "4500.00",
        "arquivos": {"xml_nfse": str(pasta / f"{prefixo}_nfse.xml")},
    }), encoding="utf-8")


def test_renomeia_notas_antigas_e_continua_achando_pela_chave(tmp_path, config, linha):
    pasta = tmp_path / "notas" / "2026" / "09-setembro"
    _nota_antiga(pasta, config, linha)

    assert renomear_arquivos_antigos(tmp_path / "notas") == 1
    nomes = sorted(p.name for p in pasta.iterdir())
    assert all(n.startswith("NFS-e 84 - Cliente Alfa Tecnologia LTDA") for n in nomes), nomes
    assert any(n.endswith(".pdf") for n in nomes)
    assert not any(CHAVE_EXEMPLO in n for n in nomes)

    # Idempotente: rodar de novo não mexe em nada.
    assert renomear_arquivos_antigos(tmp_path / "notas") == 0

    xml = localizar_xml_da_nota(tmp_path / "notas", CHAVE_EXEMPLO)
    assert xml is not None and xml.name.endswith(".xml")

    repo = RepositorioEmissoes(BancoLocal(tmp_path / "dados.db"))
    assert repo.reconstruir(tmp_path / "notas") == 1
    nota = repo.listar(documento="11.222.333/0001-81")[0]
    assert nota["n_nfse"] == "84"
    assert nota["competencia"]

