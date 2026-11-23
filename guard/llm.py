"""Camada de LLM plugável: feedback de Koch via Ollama local ou mock.

O provider é escolhido por ``settings.LLM_PROVIDER`` ("ollama" | "mock"). Em
ambos os casos usa o prompt versionado de Koch (``prompts.py``) — o prompt é
artefato de pesquisa e não muda com o provider. No piloto: Ollama local
(``qwen2.5:3b-instruct``), offline, sem nuvem (ADR-0001/0002).
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.utils.mock_llm import mock_llm
from app.utils.prompts import (
    KOCH_SYSTEM,
    FeedbackReprovado,
    koch_user_prompt,
    parse_koch_json,
    verificar_feedback,
)


class LLMIndisponivel(RuntimeError):
    """Falha ao obter feedback do modelo (rede, timeout ou resposta inválida)."""


class FeedbackNaoEntregavel(LLMIndisponivel):
    """A resposta veio, mas não pode ser entregue ao aluno (guarda de conteúdo).

    Subclasse de ``LLMIndisponivel`` de propósito: o desfecho para o aluno é o
    mesmo (estado de erro recuperável, com nova tentativa), e nada acima precisa
    aprender um caminho novo. O que muda é o ``motivo``, que vira metadado de
    pesquisa: quantas vezes a guarda disparou, e por quê.
    """

    def __init__(self, motivo: str) -> None:
        self.motivo = motivo
        super().__init__(f"feedback reprovado pela guarda: {motivo}")


def _analisar_via_ollama(texto: str) -> dict:
    """Chama o Ollama local (/api/chat, saída em JSON) e parseia o feedback."""
    url = f"{settings.OLLAMA_HOST}/api/chat"
    payload: dict = {
        "model": settings.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": KOCH_SYSTEM},
            {"role": "user", "content": koch_user_prompt(texto)},
        ],
        "stream": False,
        "format": "json",
    }
    # Limita as threads de CPU à cota de vCPUs (evita o thrashing de 48 threads
    # numa cota menor — ver OLLAMA_NUM_THREAD em config.py). 0 = Ollama decide.
    # Temperatura da condição medida no benchmark (ver config). Sem isto, o
    # Ollama aplica o próprio padrão e a plataforma roda um regime de amostragem
    # que nunca foi avaliado.
    opcoes: dict = {"temperature": settings.OLLAMA_TEMPERATURE}
    if settings.OLLAMA_NUM_THREAD > 0:
        opcoes["num_thread"] = settings.OLLAMA_NUM_THREAD
    # Colocação do modelo: -1 deixa o Ollama decidir, 0 força CPU-only (condição
    # do appliance, que não tem acelerador). Ver OLLAMA_NUM_GPU em config.py.
    if settings.OLLAMA_NUM_GPU >= 0:
        opcoes["num_gpu"] = settings.OLLAMA_NUM_GPU
    if opcoes:
        payload["options"] = opcoes
    try:
        resposta = httpx.post(url, json=payload, timeout=settings.OLLAMA_TIMEOUT)
        resposta.raise_for_status()
        conteudo = resposta.json()["message"]["content"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise LLMIndisponivel(f"Ollama indisponível: {exc}") from exc

    try:
        return parse_koch_json(conteudo)
    except (ValueError, TypeError) as exc:
        raise LLMIndisponivel(
            f"resposta do modelo sem JSON de Koch válido: {exc}"
        ) from exc


def analisar_texto_koch(texto: str) -> tuple[dict, str]:
    """Devolve ``(criterios_koch, nome_do_modelo)`` para o texto do aluno.

    A guarda de conteúdo (``verificar_feedback``) roda para **qualquer**
    provider: é propriedade do que se entrega ao aluno, não de quem gerou.

    Raises:
        FeedbackNaoEntregavel: resposta válida na forma, reprovada no conteúdo.
        LLMIndisponivel: quando o provider Ollama falha (rede/parse).
    """
    if settings.LLM_PROVIDER == "ollama":
        criterios, modelo = _analisar_via_ollama(texto), settings.OLLAMA_MODEL
    else:
        criterios, modelo = mock_llm.get_feedback(texto), "mock"
    try:
        verificar_feedback(criterios, texto)
    except FeedbackReprovado as exc:
        raise FeedbackNaoEntregavel(exc.motivo) from exc
    return criterios, modelo


# ── Sugestão de orientação para a professora (Etapa 2, ADR-0037) ─────────────

# A professora espera na tela: teto menor que o da fila de feedback do aluno.
TIMEOUT_ORIENTACAO_S = 120.0


def sugerir_orientacao(
    resumo: str, diagnosticos: list[str], n_cenas: int
) -> tuple[dict, str]:
    """Devolve ``(sugestao, nome_do_modelo)`` para a professora orientar a Etapa 2.

    Sem conexão de banco aberta: quem chama monta o resumo antes e libera a
    sessão (ADR-0024). No mock, a sugestão sai dos diagnósticos determinísticos.

    Raises:
        LLMIndisponivel: quando o Ollama falha ou a resposta não tem o formato.
    """
    from app.utils.prompts_orientacao import (
        ORIENTACAO_SYSTEM,
        orientacao_mock,
        parse_orientacao_json,
    )

    if settings.LLM_PROVIDER != "ollama":
        return orientacao_mock(diagnosticos, n_cenas), "mock"
    payload: dict = {
        "model": settings.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": ORIENTACAO_SYSTEM},
            {"role": "user", "content": resumo},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": settings.OLLAMA_TEMPERATURE},
    }
    if settings.OLLAMA_NUM_THREAD > 0:
        payload["options"]["num_thread"] = settings.OLLAMA_NUM_THREAD
    if settings.OLLAMA_NUM_GPU >= 0:
        payload["options"]["num_gpu"] = settings.OLLAMA_NUM_GPU
    try:
        resposta = httpx.post(
            f"{settings.OLLAMA_HOST}/api/chat",
            json=payload,
            timeout=min(settings.OLLAMA_TIMEOUT, TIMEOUT_ORIENTACAO_S),
        )
        resposta.raise_for_status()
        conteudo = resposta.json()["message"]["content"]
        return parse_orientacao_json(conteudo), settings.OLLAMA_MODEL
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise LLMIndisponivel(f"sugestão de orientação indisponível: {exc}") from exc
