#!/usr/bin/env python3
"""Gera as estatísticas e as linhas LaTeX das tabelas do artigo do SBESC.

Lê os `.jsonl` brutos de `resultados/` (não os resumos, de propósito: os
resumos não carregam prefill nem comprimento de saída, que são o que a
caracterização de serviço precisa).

Uso:
    python artigo_tabelas.py t1a-frio [t1b-quente ...]
"""

from __future__ import annotations

import json
import math
import statistics as st
import sys
from pathlib import Path

RESULTADOS = Path(__file__).parent / "resultados"
# Acima disso o prefill foi processado; abaixo, veio do cache de prefixo.
# A separação é de ordem de grandeza (0,03 s contra ~2 s), não de ajuste fino.
LIMIAR_CACHE_S = 0.5


def carregar(rotulo: str) -> list[dict]:
    caminho = RESULTADOS / f"{rotulo}.jsonl"
    if not caminho.exists():
        raise SystemExit(f"não encontrei {caminho}")
    linhas = [json.loads(linha) for linha in caminho.read_text().splitlines() if linha.strip()]
    return sorted(linhas, key=lambda r: r["sent_at"])


def servico(r: dict) -> float:
    """Tempo de serviço do pedido, sem espera de fila.

    `total_duration_s` do Ollama inclui a espera, então não serve. A soma das
    fases (carga do modelo, prefill, decodificação) é o serviço de fato.
    """
    return r["load_duration_s"] + r["prompt_eval_duration_s"] + r["eval_duration_s"]


def descrever(rotulo: str) -> dict:
    linhas = carregar(rotulo)
    if not linhas:
        raise SystemExit(f"{rotulo}: sem registros")

    # Aquecimento: o primeiro pedido carrega o modelo e não representa o regime.
    aquecimento, corpo = linhas[0], linhas[1:]

    quentes = [r for r in corpo if r["prompt_eval_duration_s"] < LIMIAR_CACHE_S]
    frios = [r for r in corpo if r["prompt_eval_duration_s"] >= LIMIAR_CACHE_S]

    s = [servico(r) for r in frios] or [servico(r) for r in corpo]
    prefill = [r["prompt_eval_duration_s"] for r in frios] or [0.0]
    decode = [r["eval_duration_s"] for r in corpo]
    saida = [r["eval_count"] for r in corpo]
    tps = [r["tokens_per_s"] for r in corpo]

    erros = [r for r in linhas if r.get("status") != "ok" or r.get("error")]

    return {
        "rotulo": rotulo,
        "n_total": len(linhas),
        "n_corpo": len(corpo),
        "aquecimento_s": servico(aquecimento),
        "cache_quente": len(quentes),
        "S_media": st.mean(s),
        "S_dp": st.pstdev(s) if len(s) > 1 else 0.0,
        "S_min": min(s),
        "S_max": max(s),
        "prefill_media": st.mean(prefill),
        "decode_media": st.mean(decode),
        "saida_media": st.mean(saida),
        "saida_min": min(saida),
        "saida_max": max(saida),
        "tps_media": st.mean(tps),
        "tps_min": min(tps),
        "tps_max": max(tps),
        "erros": len(erros),
        "contrato_ok": sum(1 for r in linhas if r.get("contract_ok")),
    }


def ajuste_escada(rotulo: str) -> dict:
    """Ajusta latencia_k = a + b*k num pico. Espera-se b ≈ S e R² ≈ 1."""
    linhas = sorted(carregar(rotulo), key=lambda r: r["latency_s"])
    ks = list(range(1, len(linhas) + 1))
    ys = [r["latency_s"] for r in linhas]
    n = len(ks)
    mk, my = st.mean(ks), st.mean(ys)
    sxy = sum((k - mk) * (y - my) for k, y in zip(ks, ys))
    sxx = sum((k - mk) ** 2 for k in ks)
    b = sxy / sxx
    a = my - b * mk
    ss_res = sum((y - (a + b * k)) ** 2 for k, y in zip(ks, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return {"rotulo": rotulo, "n": n, "a": a, "b": b, "r2": 1 - ss_res / ss_tot}


def capacidade(s_medio: float, alunos: int, limiar: float) -> dict:
    """Modelo de dimensionamento: serviço sequencial e determinístico.

    Pico sincronizado: a k-ésima conclusão sai em k*S, logo o número de alunos
    dentro do limiar é floor(limiar/S). Chegada distribuída numa janela W: a
    fila não cresce se a taxa de chegada não superar 1/S, ou seja W >= N*S.
    """
    dentro = math.floor(limiar / s_medio) if s_medio > 0 else 0
    return {
        "S": s_medio,
        "mu_por_min": 60.0 / s_medio,
        "dentro_do_limiar_no_pico": min(dentro, alunos),
        "pct_no_pico": 100.0 * min(dentro, alunos) / alunos,
        "p50_previsto": math.ceil(alunos / 2) * s_medio,
        "max_previsto": alunos * s_medio,
        "janela_minima_s": alunos * s_medio,
    }


def main() -> int:
    rotulos = sys.argv[1:]
    if not rotulos:
        raise SystemExit(__doc__)

    for rotulo in rotulos:
        d = descrever(rotulo)
        print(f"\n=== {d['rotulo']} ===")
        print(f"  pedidos: {d['n_total']} (1 descartado como aquecimento, {d['n_corpo']} no corpo)")
        print(f"  aquecimento: {d['aquecimento_s']:.2f} s")
        print(f"  prefill cacheado: {d['cache_quente']} pedidos "
              f"{'<-- REPROVA, variante queimada' if d['cache_quente'] else '(nenhum, ok)'}")
        print(f"  S: media {d['S_media']:.2f} s, dp {d['S_dp']:.2f}, "
              f"min {d['S_min']:.2f}, max {d['S_max']:.2f}")
        print(f"     prefill {d['prefill_media']:.2f} s "
              f"({100 * d['prefill_media'] / d['S_media']:.0f}% do serviço), "
              f"decode {d['decode_media']:.2f} s")
        print(f"  saida: {d['saida_media']:.0f} tokens (min {d['saida_min']}, max {d['saida_max']})")
        print(f"  decodificacao: {d['tps_media']:.1f} tok/s "
              f"(min {d['tps_min']:.1f}, max {d['tps_max']:.1f})")
        print(f"  erros: {d['erros']}   contrato ok: {d['contrato_ok']}/{d['n_total']}")

        c = capacidade(d["S_media"], alunos=30, limiar=10.0)
        print(f"  --- modelo, para 30 alunos e limiar de 10 s ---")
        print(f"  mu = {c['mu_por_min']:.1f} conclusoes/min")
        print(f"  pico: p50 previsto {c['p50_previsto']:.0f} s, "
              f"max {c['max_previsto']:.0f} s, "
              f"dentro do limiar {c['dentro_do_limiar_no_pico']}/30 ({c['pct_no_pico']:.1f}%)")
        print(f"  janela minima de chegada: {c['janela_minima_s']:.0f} s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
