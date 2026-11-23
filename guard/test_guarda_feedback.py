"""Guarda de conteúdo do feedback: o que NÃO pode chegar ao aluno.

Tests-as-specs (CLAUDE.md §6). A validação estrutural de ``parse_koch_json``
confere chaves e tipos, e aceitava conteúdo não entregável: painel vazio,
perguntas que não são perguntas, e, o mais grave, o texto do aluno devolvido
reescrito. O princípio não negociável de que **a IA nunca reescreve o texto do
aluno** (CLAUDE.md §3, princípio 1) passa a ter verificação em tempo de
execução, e não só um pedido no prompt.
"""

import json

import pytest

from app.config import settings
from app.utils import llm as llm_mod
from app.utils.llm import FeedbackNaoEntregavel, analisar_texto_koch
from app.utils.prompts import (
    FeedbackReprovado,
    parse_koch_json,
    verificar_feedback,
)

_TEXTO_ALUNO = (
    "Era uma vez uma formiga muito corajosa que morava perto do rio grande, "
    "e todos os dias ela atravessava a ponte de folhas para buscar comida "
    "para as suas irmãs que ficavam no formigueiro esperando por ela."
)

_OK = {
    "pontos_fortes": "Sua história tem começo, meio e fim bem marcados.",
    "perguntas_reflexivas": ["O que a formiga sentiu ali?", "E o que houve depois?"],
}


def _com(**mudancas) -> dict:
    base = {
        "pontos_fortes": _OK["pontos_fortes"],
        "perguntas_reflexivas": list(_OK["perguntas_reflexivas"]),
    }
    base.update(mudancas)
    return base


# ── O que passa ──────────────────────────────────────────────────────────────


def test_feedback_bem_formado_passa() -> None:
    assert verificar_feedback(_com(), _TEXTO_ALUNO) == _com()


def test_citacao_curta_do_texto_do_aluno_e_legitima() -> None:
    """Citar um trecho é recurso pedagógico; reescrever é que é proibido."""
    dados = _com(
        pontos_fortes='Você escreveu "uma formiga muito corajosa", e isso ajuda.'
    )
    assert verificar_feedback(dados, _TEXTO_ALUNO)


def test_texto_curto_do_aluno_nao_dispara_falso_positivo() -> None:
    """Com texto menor que a janela de comparação, nada é acusado de cópia."""
    assert verificar_feedback(_com(), "Era uma vez.")


# ── O que é barrado ──────────────────────────────────────────────────────────


def test_painel_vazio_e_reprovado() -> None:
    """O caso que a validação estrutural aceitava: 'pronto' e sem nada dentro."""
    with pytest.raises(FeedbackReprovado) as exc:
        verificar_feedback(
            {"pontos_fortes": "   ", "perguntas_reflexivas": []}, _TEXTO_ALUNO
        )
    assert exc.value.motivo == "pontos_fortes_vazio"


def test_lista_de_perguntas_vazia_e_reprovada() -> None:
    with pytest.raises(FeedbackReprovado) as exc:
        verificar_feedback(_com(perguntas_reflexivas=[]), _TEXTO_ALUNO)
    assert exc.value.motivo == "sem_perguntas"


def test_pergunta_que_nao_e_texto_e_reprovada() -> None:
    """``[123, None]`` passava na checagem de tipo da lista e chegava à tela."""
    with pytest.raises(FeedbackReprovado) as exc:
        verificar_feedback(_com(perguntas_reflexivas=[123, None]), _TEXTO_ALUNO)
    assert exc.value.motivo == "pergunta_invalida"


def test_texto_do_aluno_reescrito_e_reprovado() -> None:
    """O princípio não negociável, agora verificado."""
    reescrito = (
        "Ficou ótimo! Veja como reescrevi: era uma vez uma formiga muito "
        "corajosa que morava perto do rio grande, e todos os dias ela "
        "atravessava a ponte de folhas para buscar comida."
    )
    with pytest.raises(FeedbackReprovado) as exc:
        verificar_feedback(_com(pontos_fortes=reescrito), _TEXTO_ALUNO)
    assert exc.value.motivo == "copia_do_texto_do_aluno"


def test_copia_dentro_de_uma_pergunta_tambem_e_reprovada() -> None:
    copia = "que morava perto do rio grande e todos os dias ela atravessava a ponte?"
    with pytest.raises(FeedbackReprovado) as exc:
        verificar_feedback(_com(perguntas_reflexivas=[copia]), _TEXTO_ALUNO)
    assert exc.value.motivo == "copia_do_texto_do_aluno"


def test_copia_ignora_pontuacao_e_caixa() -> None:
    """Reescrever mudando vírgulas e maiúsculas continua sendo reescrever."""
    disfarcado = "ERA UMA VEZ, UMA FORMIGA! MUITO CORAJOSA... QUE MORAVA; PERTO DO RIO."
    with pytest.raises(FeedbackReprovado):
        verificar_feedback(_com(pontos_fortes=disfarcado), _TEXTO_ALUNO)


def test_resposta_longa_demais_e_reprovada() -> None:
    with pytest.raises(FeedbackReprovado) as exc:
        verificar_feedback(_com(pontos_fortes="a" * 5000), _TEXTO_ALUNO)
    assert exc.value.motivo == "pontos_fortes_longo"


def test_perguntas_demais_e_reprovado() -> None:
    with pytest.raises(FeedbackReprovado) as exc:
        verificar_feedback(
            _com(perguntas_reflexivas=[f"Pergunta {i}?" for i in range(9)]),
            _TEXTO_ALUNO,
        )
    assert exc.value.motivo == "perguntas_demais"


# ── Integração com a camada de LLM ───────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def test_guarda_roda_no_caminho_do_ollama(monkeypatch) -> None:
    """Resposta válida na forma e inválida no conteúdo não chega ao aluno."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    conteudo = json.dumps({"pontos_fortes": "", "perguntas_reflexivas": []})
    resposta = _FakeResp({"message": {"content": conteudo}})
    monkeypatch.setattr(llm_mod.httpx, "post", lambda *a, **k: resposta)

    with pytest.raises(FeedbackNaoEntregavel) as exc:
        analisar_texto_koch(_TEXTO_ALUNO)
    assert exc.value.motivo == "pontos_fortes_vazio"


def test_estrutura_valida_continua_passando_no_parser() -> None:
    """A guarda não substitui o parser: separação de responsabilidades."""
    bruto = json.dumps({"pontos_fortes": "", "perguntas_reflexivas": []})
    assert parse_koch_json(bruto) == {"pontos_fortes": "", "perguntas_reflexivas": []}
