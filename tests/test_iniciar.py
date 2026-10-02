"""Testes do ponto de entrada do executável empacotado."""

from __future__ import annotations

import sys

from empacotamento.iniciar import _redirecionar_saida_padrao_ausente


def test_substitui_stdout_e_stderr_ausentes(monkeypatch):
    """Reproduz o modo janela (`console=False`): sys.stdout/stderr vêm `None`,
    e isso quebrava o uvicorn (`AttributeError: 'NoneType' object has no
    attribute 'isatty'`) antes da bandeja sequer aparecer."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    _redirecionar_saida_padrao_ausente()

    assert sys.stdout is not None
    assert sys.stderr is not None
    assert sys.stdout.isatty() is False  # é isso que o uvicorn ia chamar
    sys.stdout.write("não deve levantar erro")
    sys.stderr.write("não deve levantar erro")


def test_nao_mexe_em_stdout_e_stderr_reais(monkeypatch, capsys):
    """Rodando com console de verdade (o caso normal em desenvolvimento), a
    função não deve trocar nada."""
    reais_stdout, reais_stderr = sys.stdout, sys.stderr
    _redirecionar_saida_padrao_ausente()
    assert sys.stdout is reais_stdout
    assert sys.stderr is reais_stderr
