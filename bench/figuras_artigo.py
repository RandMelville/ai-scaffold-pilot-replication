#!/usr/bin/env python3
"""Figuras e métricas de robustez do artigo do SBESC.

Gera PDF vetorial (o template do IEEE aceita, e não perde resolução) em
`figuras/`, mais as métricas que sustentam cada afirmação do texto.

Uso:
    python3 figuras_artigo.py
"""

from __future__ import annotations

import json
import statistics as st
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

AQUI = Path(__file__).parent
RESULTADOS = AQUI / "resultados"
FIGURAS = AQUI.parent.parent / "docs" / "pesquisa" / "artigo-sbesc" / "figuras"
FIGURAS.mkdir(parents=True, exist_ok=True)

LIMIAR_S = 10.0
LIMIAR_CACHE_S = 0.5

# Coluna do IEEEtran tem ~3,5 in. Figura mais larga que isso é reduzida na
# inclusão e a tipografia sai menor que o corpo do texto.
LARGURA = 3.4
plt.rcParams.update({
    "font.size": 8,
    "axes.labelsize": 8,
    "axes.titlesize": 8,
    "legend.fontsize": 7,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "figure.dpi": 200,
    "savefig.bbox": "tight",
    "axes.grid": True,
    "grid.alpha": 0.3,
    "grid.linewidth": 0.4,
})


def carregar(rotulo: str) -> list[dict] | None:
    caminho = RESULTADOS / f"{rotulo}.jsonl"
    if not caminho.exists():
        return None
    linhas = [json.loads(x) for x in caminho.read_text().splitlines() if x.strip()]
    return sorted(linhas, key=lambda r: r["sent_at"])


def servico(r: dict) -> float:
    return r["load_duration_s"] + r["prompt_eval_duration_s"] + r["eval_duration_s"]


def regressao(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Mínimos quadrados. Devolve (intercepto, inclinação, R²)."""
    n = len(xs)
    mx, my = st.mean(xs), st.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sxy / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return a, b, 1 - ss_res / ss_tot


# ---------------------------------------------------------------------------
# Figura 2: de onde vem a variância do tempo de serviço
# ---------------------------------------------------------------------------
def figura_servico(rotulo: str = "t1a-frio") -> dict | None:
    linhas = carregar(rotulo)
    if not linhas:
        print(f"  (pulando figura de serviço: falta {rotulo})")
        return None
    corpo = linhas[1:]  # descarta aquecimento

    tokens = [r["eval_count"] for r in corpo]
    s = [servico(r) for r in corpo]
    a, b, r2 = regressao(tokens, s)

    fig, ax = plt.subplots(figsize=(LARGURA, 2.3))
    ax.scatter(tokens, s, s=14, alpha=0.75, edgecolors="none", color="#402090",
               label="requests")
    xs = [min(tokens), max(tokens)]
    ax.plot(xs, [a + b * x for x in xs], color="#0C7E8C", lw=1.2,
            label=f"fit: $S = {a:.2f} + {b*1000:.1f}\\,$ms$\\cdot n$  ($R^2={r2:.3f}$)")
    ax.set_xlabel("output length (tokens)")
    ax.set_ylabel("service time $S$ (s)")
    ax.legend(loc="upper left", frameon=False)
    fig.savefig(FIGURAS / "fig2_service.pdf")
    plt.close(fig)

    tps = [r["tokens_per_s"] for r in corpo]
    return {
        "intercepto_s": a,
        "por_token_ms": b * 1000,
        "r2": r2,
        "taxa_implicita_tok_s": 1 / b,
        "tps_medido": st.mean(tps),
        "tps_cv_pct": 100 * st.pstdev(tps) / st.mean(tps),
    }


# ---------------------------------------------------------------------------
# Figura 3: a escada (serviço sequencial e determinístico)
# ---------------------------------------------------------------------------
def figura_escada(rotulo: str = "t2-escada") -> dict | None:
    linhas = carregar(rotulo)
    if not linhas:
        print(f"  (pulando figura da escada: falta {rotulo})")
        return None
    ys = sorted(r["latency_s"] for r in linhas)
    ks = list(range(1, len(ys) + 1))
    a, b, r2 = regressao([float(k) for k in ks], ys)

    fig, ax = plt.subplots(figsize=(LARGURA, 2.3))
    ax.plot(ks, [a + b * k for k in ks], color="#0C7E8C", lw=1.2, zorder=1,
            label=f"fit: $t_k = {a:.2f} + {b:.2f}k$  ($R^2={r2:.4f}$)")
    ax.scatter(ks, ys, s=16, color="#402090", zorder=2, edgecolors="none",
               label="measured completion")
    ax.axhline(LIMIAR_S, color="#B45309", ls="--", lw=1,
               label=f"{LIMIAR_S:.0f} s threshold")
    ax.set_xlabel("completion rank $k$")
    ax.set_ylabel("end-to-end latency (s)")
    ax.legend(loc="upper left", frameon=False)
    fig.savefig(FIGURAS / "fig3_staircase.pdf")
    plt.close(fig)
    return {"n": len(ys), "intercepto": a, "inclinacao": b, "r2": r2}


# ---------------------------------------------------------------------------
# Figura 4: modelo de dimensionamento contra medição
# ---------------------------------------------------------------------------
def figura_curva(s_medio: float, alunos: int = 30) -> dict | None:
    pontos = []
    for rotulo in sorted(p.stem for p in RESULTADOS.glob("t3*.resumo.json")):
        resumo = json.loads((RESULTADOS / f"{rotulo}.json").read_text()) \
            if (RESULTADOS / f"{rotulo}.json").exists() else None
        if resumo is None:
            resumo = json.loads((RESULTADOS / f"{rotulo}.resumo.json").read_text()) \
                if (RESULTADOS / f"{rotulo}.resumo.json").exists() else None
        if resumo is None:
            caminho = RESULTADOS / f"{rotulo}.json"
            print(f"  (sem resumo para {caminho})")
            continue
        pontos.append((resumo["window_s"], resumo["within_threshold_pct"],
                       resumo["latency_p50"], resumo["latency_p95"]))
    if not pontos:
        print("  (pulando figura da curva: faltam as rodadas T3)")
        return None

    joelho = alunos * s_medio

    # Previsão: com chegada espaçada de W/N e serviço S, a espera do k-esimo
    # cresce como k*max(0, S - W/N). Dentro do limiar enquanto essa espera
    # somada ao serviço nao passa do limiar.
    def previsto(w: float) -> float:
        passo = w / alunos
        atraso = max(0.0, s_medio - passo)
        dentro = sum(1 for k in range(alunos)
                     if k * atraso + s_medio <= LIMIAR_S)
        return 100.0 * dentro / alunos

    ws = [30 + 5 * i for i in range(0, 60)]
    fig, ax = plt.subplots(figsize=(LARGURA, 2.3))
    ax.plot(ws, [previsto(w) for w in ws], color="#0C7E8C", lw=1.2,
            label="model prediction")
    ax.axvline(joelho, color="#B45309", ls="--", lw=1,
               label=f"knee $NS = {joelho:.0f}$ s")
    ax.scatter([p[0] for p in pontos], [p[1] for p in pontos], s=26,
               color="#402090", zorder=3, edgecolors="none", label="measured")
    ax.set_xlabel("arrival window $W$ (s)")
    ax.set_ylabel(f"% within {LIMIAR_S:.0f} s")
    ax.set_ylim(-5, 105)
    ax.legend(loc="lower right", frameon=False)
    fig.savefig(FIGURAS / "fig4_curve.pdf")
    plt.close(fig)
    return {"joelho_s": joelho, "pontos": pontos}


# ---------------------------------------------------------------------------
# Figura 5: disponibilidade da plataforma durante o pico (sonda)
# ---------------------------------------------------------------------------
def figura_sonda() -> dict | None:
    series = {
        "baseline (no load)": "t4-baseline-r1",
        "level 1 (inference outside platform)": "t4-nivel1-r1",
        "level 2 (through platform)": "t4-nivel2-depois-r1",
    }
    dados = {}
    for nome, rotulo in series.items():
        caminho = RESULTADOS / f"{rotulo}.probe.jsonl"
        if not caminho.exists():
            continue
        linhas = [json.loads(x) for x in caminho.read_text().splitlines() if x.strip()]
        if linhas:
            dados[nome] = linhas
    if not dados:
        print("  (pulando figura da sonda: faltam as rodadas T4)")
        return None

    fig, ax = plt.subplots(figsize=(LARGURA, 2.3))
    cores = {"baseline (no load)": "#9CA3AF",
             "level 1 (inference outside platform)": "#0C7E8C",
             "level 2 (through platform)": "#402090"}
    for nome, linhas in dados.items():
        # A sonda grava o instante em `at`; o gerador de carga usa `sent_at`.
        t0 = min(r["at"] for r in linhas)
        xs = [r["at"] - t0 for r in linhas]
        ys = [r.get("latency_s", 0) * 1000 for r in linhas]
        ax.plot(xs, ys, lw=1.0, marker="o", ms=2, color=cores.get(nome),
                label=nome)
    ax.set_yscale("log")
    ax.set_xlabel("time since burst (s)")
    ax.set_ylabel("probe response (ms, log)")
    ax.legend(loc="upper right", frameon=False)
    fig.savefig(FIGURAS / "fig5_probe.pdf")
    plt.close(fig)
    return {nome: len(v) for nome, v in dados.items()}


# ---------------------------------------------------------------------------
# Figura 1: arquitetura de implantação
# ---------------------------------------------------------------------------
def figura_arquitetura() -> None:
    """Diagrama de implantação, gerado em vez de desenhado.

    Reprodutível e em inglês. Ocupa as duas colunas (figure* no LaTeX).
    """
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    VIOLETA, TEAL, AMBAR, CINZA = "#402090", "#0C7E8C", "#B45309", "#6B7280"

    fig, ax = plt.subplots(figsize=(7.0, 2.15))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 32)
    ax.axis("off")
    ax.grid(False)

    def caixa(x, y, w, h, texto, cor, fundo="white", lw=1.1, fs=7.5, negrito=False):
        ax.add_patch(FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.6,rounding_size=1.2",
            linewidth=lw, edgecolor=cor, facecolor=fundo, zorder=2))
        ax.text(x + w / 2, y + h / 2, texto, ha="center", va="center",
                fontsize=fs, color=cor, zorder=3,
                fontweight="bold" if negrito else "normal")

    def seta(x1, y1, x2, y2, cor=CINZA, estilo="-|>", tracejada=False):
        ax.add_patch(FancyArrowPatch(
            (x1, y1), (x2, y2), arrowstyle=estilo, mutation_scale=9,
            linewidth=0.9, color=cor, zorder=1,
            linestyle="--" if tracejada else "-"))

    # --- ilha offline, dentro da escola ---
    ax.add_patch(FancyBboxPatch(
        (1.5, 1.5), 66, 29, boxstyle="round,pad=0.6,rounding_size=1.5",
        linewidth=1.0, edgecolor=TEAL, facecolor="#F0FAFB", zorder=0))
    ax.text(3.5, 28.2, "OFFLINE ISLAND (school)", fontsize=7,
            color=TEAL, fontweight="bold", va="center")

    caixa(4, 14, 15, 8, "Students\n(Chromebooks)", VIOLETA)
    caixa(4, 4, 15, 7, "Teacher", VIOLETA)
    caixa(23, 9.5, 13, 12, "Network\nunit\n(radio)", AMBAR, "#FFFBEB", negrito=True)

    # --- appliance: unidade de computação ---
    ax.add_patch(FancyBboxPatch(
        (40, 3.5), 25.5, 24, boxstyle="round,pad=0.6,rounding_size=1.5",
        linewidth=1.3, edgecolor=VIOLETA, facecolor="white", zorder=1))
    ax.text(52.7, 25.2, "COMPUTATION UNIT (mini-PC)", fontsize=6.8,
            color=VIOLETA, fontweight="bold", ha="center", zorder=3)
    caixa(42, 18.0, 21.5, 5.3, "Application (API + SPA)", VIOLETA, "#F5F3FF", fs=7)
    caixa(42, 12, 21.5, 5.5, "Queue $\\rightarrow$ SLM (CPU)", TEAL, "#F0FAFB", fs=7)
    caixa(42, 5.5, 21.5, 5.5, "Encrypted vault (DB)", AMBAR, "#FFFBEB", fs=7)

    seta(19, 18, 23, 16)
    seta(19, 7.5, 23, 13)
    seta(36, 15.5, 40, 15.5)

    # --- fora da escola ---
    caixa(72, 19, 25, 8, "Platform + model\nupdates (firmware-like)", CINZA, fs=7)
    caixa(72, 8.5, 25, 8, "Researcher custody\nof the vault", CINZA, fs=7)
    caixa(72, 1.5, 25, 5.5, "Cloud: closed to student data", "#B91C1C",
          "#FEF2F2", fs=6.8, negrito=True)

    seta(72, 23, 65.5, 23, tracejada=True)
    seta(65.5, 8, 72, 12, tracejada=True)
    ax.text(84.5, 29, "OUTSIDE THE SCHOOL", fontsize=7, color=CINZA,
            fontweight="bold", ha="center", va="center")

    fig.savefig(FIGURAS / "fig1_architecture.pdf")
    plt.close(fig)


def main() -> int:
    print("== Figura 1: arquitetura ==")
    figura_arquitetura()

    print("== Figura 2: variância do serviço ==")
    m = figura_servico()
    if m:
        print(f"   S = {m['intercepto_s']:.2f} s + {m['por_token_ms']:.1f} ms/token, "
              f"R2 = {m['r2']:.3f}")
        print(f"   taxa implícita pelo ajuste: {m['taxa_implicita_tok_s']:.1f} tok/s; "
              f"medida: {m['tps_medido']:.1f} tok/s (CV {m['tps_cv_pct']:.1f}%)")

    print("== Figura 3: escada ==")
    e = figura_escada()
    if e:
        print(f"   n={e['n']}  t_k = {e['intercepto']:.2f} + {e['inclinacao']:.2f}k, "
              f"R2 = {e['r2']:.4f}")

    print("== Figura 4: curva de dimensionamento ==")
    s_medio = m["intercepto_s"] + m["por_token_ms"] / 1000 * 134 if m else 5.83
    c = figura_curva(s_medio=5.83)
    if c:
        print(f"   joelho previsto em {c['joelho_s']:.0f} s")
        for w, dentro, p50, p95 in c["pontos"]:
            print(f"   W={w:.0f}s -> dentro={dentro}% p50={p50:.1f}s p95={p95:.1f}s")

    print("== Figura 5: sonda ==")
    s = figura_sonda()
    if s:
        print(f"   séries: {s}")

    print(f"\nfiguras em {FIGURAS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
