"""Prompt do feedback de Koch: o artefato de pesquisa versionado (CLAUDE.md §8).

É o coração pedagógico do projeto: as regras aqui são inegociáveis. Mantê-lo
neste módulo, separado das rotas, facilita a revisão pelo orientador. O prompt
da camada conversacional da Etapa 2 (adiada pela ADR-0010) saiu daqui junto
com o cliente órfão do frontend (ciclo 3, D1).

O formato do feedback segue o PROMPT VALIDADO NO BENCHMARK (ADR-0019): duas
chaves — ``pontos_fortes`` (string) e ``perguntas_reflexivas`` (lista) —, foco
em coesão e coerência (Koch, 2020) e ponte com o Pensamento Computacional. É a
forma que validou conformidade de JSON e latência (~120 tokens, ~3s em CPU);
o formato antigo de 4 critérios (~700 tokens) não foi validado e foi superado.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── Feedback de Koch (2020) ──────────────────────────────────────────────────

KOCH_SYSTEM = """Você é um tutor de Linguística Textual (Koch) para alunos do \
8º-9º ano da escola pública brasileira. Você opera após o aluno escrever um \
texto curto.

REGRAS:
1. NUNCA dê a resposta pronta nem reescreva o texto corrigido.
2. Use linguagem acolhedora e adequada à idade (13-14 anos).
3. Foque em coesão e coerência (Koch, 2020): repetição lexical, pronominalização \
ambígua, conectivos contraditórios, marcadores temporais.
4. Valorize variedades linguísticas e oralidade; não aja como policial gramatical.
5. Quando couber, faça uma ponte com o Pensamento Computacional (lógica, \
sequência, condição).

FORMATO (JSON estrito, sem texto fora):
{
  "pontos_fortes": "...",
  "perguntas_reflexivas": ["...", "..."]
}"""


def koch_user_prompt(text: str) -> str:
    """Mensagem de usuário: o texto do aluno, cru, como no benchmark.

    Sem prefixo. Os 388 registros do benchmark (ADR-0019) foram gerados com o
    texto puro como mensagem de usuário, e a plataforma antepunha "Texto do
    aluno para análise:". Medido em 17/set/2026 (3 rodadas cada, mesmo texto,
    temperatura 0,2): com o prefixo o modelo passou a falar do aluno em
    terceira pessoa ("O aluno demonstrou...") em 3/3; cru, dirige-se a ele
    ("Você demonstrou...") em 2/3. O feedback é lido pelo próprio aluno.
    """
    return text


# ── Utilitário de parsing ────────────────────────────────────────────────────

_KOCH_KEYS = ("pontos_fortes", "perguntas_reflexivas")


def parse_koch_json(raw: str) -> dict[str, Any]:
    """Extrai e valida o JSON de feedback de Koch da resposta do modelo.

    Tolera cercas de código markdown e texto ao redor. Valida o formato do
    benchmark (ADR-0019): ``pontos_fortes`` (string) e ``perguntas_reflexivas``
    (lista). Em desconformidade, levanta ``ValueError`` para que a rota recorra
    ao *fallback* mockado.

    Args:
        raw: Texto bruto devolvido pelo modelo.

    Returns:
        Dicionário com ``pontos_fortes`` e ``perguntas_reflexivas``.

    Raises:
        ValueError: Se não houver JSON válido com as chaves/tipos esperados.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("Resposta do modelo sem JSON reconhecível.")
    data = json.loads(match.group(0))
    missing = [k for k in _KOCH_KEYS if k not in data]
    if missing:
        raise ValueError(f"JSON de feedback sem as chaves: {missing}.")
    if not isinstance(data["pontos_fortes"], str):
        raise ValueError("'pontos_fortes' deve ser uma string.")
    if not isinstance(data["perguntas_reflexivas"], list):
        raise ValueError("'perguntas_reflexivas' deve ser uma lista.")
    return data


# ── Guarda de conteúdo ───────────────────────────────────────────────────────
#
# A validação acima é ESTRUTURAL: confere chaves e tipos. Ela aceita, por
# exemplo, ``{"pontos_fortes": "", "perguntas_reflexivas": []}`` (painel vazio
# entregue ao aluno como se estivesse pronto) e ``[123, None]`` como perguntas.
# Mais grave: nada impedia o modelo de devolver o texto do aluno reescrito, e o
# princípio pedagógico não negociável de que **a IA nunca reescreve o texto do
# aluno** (CLAUDE.md §3) estava garantido apenas pelo prompt, sem nada em tempo
# de execução que o sustentasse. A guarda abaixo é determinística e barata, na
# linha do que o spike de RAG concluiu (determinístico resolve onde crescer o
# modelo não resolvia), em vez de um segundo modelo verificador, que dobraria a
# latência que a ADR-0025 tenta reduzir.

#: Nº de palavras consecutivas copiadas do texto do aluno que reprova a resposta.
#: Citar um trecho curto é legítimo ("você escreveu 'a formiga corajosa'"); sete
#: palavras seguidas já não é citação, é devolver o texto reescrito.
PALAVRAS_PARA_COPIA = 7
#: Tetos de tamanho. Não são estética: resposta muito longa para 13-14 anos não
#: é lida, e um "ponto forte" do tamanho de uma redação é sinal de reescrita.
MAX_PONTOS_FORTES = 1200
MAX_PERGUNTA = 400
MAX_PERGUNTAS = 6

_NAO_PALAVRA = re.compile(r"[^0-9a-záàâãéêíóôõúüç]+", re.IGNORECASE)


class FeedbackReprovado(ValueError):
    """Feedback estruturalmente válido, mas reprovado pela guarda de conteúdo.

    ``motivo`` é um código curto e **sem conteúdo do aluno**, próprio para virar
    metadado de evento de pesquisa (quantas vezes a guarda disparou, e por quê).
    """

    def __init__(self, motivo: str, detalhe: str = "") -> None:
        self.motivo = motivo
        super().__init__(detalhe or motivo)


def _palavras(texto: str) -> list[str]:
    """Normaliza para comparação: minúsculas, sem pontuação, sem espaço extra."""
    return [p for p in _NAO_PALAVRA.split(texto.lower()) if p]


def _copia_trecho(resposta: str, texto_aluno: str) -> bool:
    """Diz se ``resposta`` repete ``PALAVRAS_PARA_COPIA`` palavras seguidas do aluno."""
    do_aluno = _palavras(texto_aluno)
    da_resposta = _palavras(resposta)
    n = PALAVRAS_PARA_COPIA
    if len(do_aluno) < n or len(da_resposta) < n:
        return False
    janelas = {tuple(do_aluno[i : i + n]) for i in range(len(do_aluno) - n + 1)}
    return any(
        tuple(da_resposta[i : i + n]) in janelas
        for i in range(len(da_resposta) - n + 1)
    )


def verificar_feedback(data: dict[str, Any], texto_aluno: str) -> dict[str, Any]:
    """Aplica a guarda de conteúdo ao feedback já validado estruturalmente.

    Args:
        data: Feedback no formato do benchmark (ADR-0019).
        texto_aluno: Texto que originou o feedback, para detectar reescrita.

    Returns:
        O próprio ``data``, quando aprovado.

    Raises:
        FeedbackReprovado: Quando o conteúdo não pode ser entregue ao aluno.
    """
    pontos = data["pontos_fortes"].strip()
    if not pontos:
        raise FeedbackReprovado("pontos_fortes_vazio")
    if len(pontos) > MAX_PONTOS_FORTES:
        raise FeedbackReprovado("pontos_fortes_longo")

    perguntas = data["perguntas_reflexivas"]
    if not perguntas:
        raise FeedbackReprovado("sem_perguntas")
    if len(perguntas) > MAX_PERGUNTAS:
        raise FeedbackReprovado("perguntas_demais")
    for pergunta in perguntas:
        if not isinstance(pergunta, str) or not pergunta.strip():
            raise FeedbackReprovado("pergunta_invalida")
        if len(pergunta) > MAX_PERGUNTA:
            raise FeedbackReprovado("pergunta_longa")

    # O princípio não negociável, agora verificado e não só pedido no prompt.
    if _copia_trecho(pontos, texto_aluno) or any(
        _copia_trecho(p, texto_aluno) for p in perguntas
    ):
        raise FeedbackReprovado("copia_do_texto_do_aluno")

    return data
