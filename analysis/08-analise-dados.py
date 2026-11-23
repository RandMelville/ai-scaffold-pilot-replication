#!/usr/bin/env python3.15
"""Análise de dados do piloto RemidiAção (v2 do artigo), reprodutível.

Uso:  PILOT_ROOT=... PILOT_DB=... PLATFORM_REPO=... python 08-analise-dados.py

Entradas (somente leitura):
  - backups/remidiacao-20261106-nuvem-pos-fechamento.db  (sqlite, uri mode=ro)
  - artigo-piloto/csv-03-uso-por-estudante-anonimo.csv    (só para reaproveitar os rótulos E1..E22)
  - artigo-piloto/07-avaliacao-feedback-ia.csv            (13 avaliações objetivas do feedback)
  - repositório plataforma-remidiacao (git log, docs/adr, backend/tests, docs/pesquisa/validacao)
Saídas:
  - artigo/v2/figuras/figD*.png (200 dpi) e figD1-pipeline.svg
  - 08-saidas/*.csv e 08-saidas/resultados.json (agregados, sem texto de estudante)

Garantias de privacidade: o banco é aberto com mode=ro; nenhuma coluna de texto de
estudante (texto de versões, feedbacks, orientações, falas, títulos, respostas, pseudônimos)
é selecionada; estudantes aparecem apenas como rótulos E1..E22; datas de calendário não
são emitidas (encontros 1 a 4 e minutos desde o início).
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import PathPatch, Rectangle, FancyBboxPatch
from matplotlib.path import Path as MPath
from scipy import stats

# ----------------------------------------------------------------------------------
# Caminhos e estilo
# ----------------------------------------------------------------------------------
import os
# Caminhos configuráveis por variável de ambiente (os dados de entrada não são públicos)
ROOT = Path(os.environ.get("PILOT_ROOT", "./pilot-data"))
DB = Path(os.environ.get("PILOT_DB", "./pilot-data/closing-database.db"))
REPO = Path(os.environ.get("PLATFORM_REPO", "./platform"))
FIG = ROOT / "artigo" / "v2" / "figuras"
OUT = ROOT / "08-saidas"
FIG.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

VIO, ESM, AMB = "#4c1d95", "#065f46", "#d97706"
G1, G2, G3, G4 = "#374151", "#6b7280", "#9ca3af", "#e5e7eb"
VIO_L, ESM_L, AMB_L = "#a78bfa", "#6ee7b7", "#fcd34d"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 10, "axes.titleweight": "bold",
    "axes.labelsize": 9, "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": G2, "axes.labelcolor": G1, "xtick.color": G1, "ytick.color": G1,
    "axes.grid": True, "grid.color": "#eceff1", "grid.linewidth": 0.7, "axes.axisbelow": True,
    "figure.dpi": 100, "savefig.dpi": 200, "savefig.bbox": "tight", "legend.frameon": False,
    "legend.fontsize": 8,
})
RNG = np.random.default_rng(20261106)
RNG_C = np.random.default_rng(20261107)  # gerador separado: não altera a sequência dos demais sorteios
BOOT = 10000
RES: dict = {}


def savefig(fig, name):
    fig.savefig(FIG / name)
    plt.close(fig)


def fmt(x, nd=1):
    """Formata com vírgula decimal e arredondamento meio-para-cima (552,5 -> 553; 436,5 -> 437)."""
    from decimal import Decimal, ROUND_HALF_UP
    q_ = Decimal(1).scaleb(-nd)
    return f"{Decimal(repr(float(x))).quantize(q_, rounding=ROUND_HALF_UP):f}".replace(".", ",")


def boot_ci(x, stat=np.median, n=BOOT, alpha=0.05):
    x = np.asarray(x, float)
    idx = RNG.integers(0, len(x), size=(n, len(x)))
    s = stat(x[idx], axis=1)
    return float(stat(x)), float(np.quantile(s, alpha / 2)), float(np.quantile(s, 1 - alpha / 2))


def boot_cluster(x, g, stat=np.median, n=BOOT, alpha=0.05, rng=None):
    """Bootstrap percentil por conglomerado: reamostra ESTUDANTES (g) e leva todos os pedidos de cada um."""
    rng = rng or RNG_C
    x = np.asarray(x, float)
    g = np.asarray(g)
    blocos = [x[g == k] for k in np.unique(g)]
    m = len(blocos)
    out = np.empty(n)
    for i in range(n):
        out[i] = stat(np.concatenate([blocos[j] for j in rng.integers(0, m, m)]))
    return float(stat(x)), float(np.quantile(out, alpha / 2)), float(np.quantile(out, 1 - alpha / 2)), out


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0, c - h), min(1, c + h)


def quart(x):
    x = np.asarray(x, float)
    return dict(n=int(len(x)), med=float(np.median(x)), q1=float(np.quantile(x, .25)),
                q3=float(np.quantile(x, .75)), iqr=float(np.quantile(x, .75) - np.quantile(x, .25)),
                min=float(x.min()), max=float(x.max()), mean=float(x.mean()), sd=float(x.std(ddof=1)) if len(x) > 1 else 0.0)


# ----------------------------------------------------------------------------------
# 1. Extração somente leitura (apenas colunas não textuais)
# ----------------------------------------------------------------------------------
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def q(sql, *a):
    return pd.DataFrame([dict(r) for r in con.execute(sql, a)])


ev = q("SELECT id, tipo, timestamp, contexto FROM eventos")
ev["ts"] = pd.to_datetime(ev["timestamp"]) - pd.Timedelta(hours=3)  # UTC -> Brasília
ev["dia"] = ev["ts"].dt.strftime("%Y-%m-%d")
DIAS = {"2026-09-18": 1, "2026-09-25": 2, "2026-10-02": 3, "2026-11-06": 4}
ev["enc"] = ev["dia"].map(DIAS)  # NaN = entre aulas (trabalho da professora)
ctx = ev["contexto"].map(lambda s: json.loads(s) if s else {})

alunos = q("SELECT id, entrou_em FROM alunos")
pa = q("SELECT id AS pa_id, aluno_id, projeto_id FROM projeto_alunos")
vers = q("SELECT id AS vid, projeto_aluno_id AS pa_id, numero, palavras, finalizada, salva_em, validacao, validada_em FROM versoes_texto")
fbk = q("SELECT versao_texto_id AS vid, status, modelo, latencia_ms, recebido_em FROM feedbacks_ia")
nar = q("SELECT id AS nid, projeto_aluno_id AS pa_id, criada_em FROM narrativas")
cenas = q("SELECT id, narrativa_id AS nid, criada_em FROM cenas")
esc = q("SELECT id, cena_id FROM escolhas")
exe = q("SELECT id, narrativa_id AS nid, rodada_em FROM execucoes")
refl = q("SELECT projeto_aluno_id AS pa_id, criada_em, concluida_em FROM reflexoes")
ori = q("SELECT id, versao_texto_id AS vid, criada_em FROM orientacoes_professora")
nvar = con.execute("SELECT count(*) FROM variaveis").fetchone()[0]
ncond = con.execute("SELECT count(*) FROM condicionais").fetchone()[0]
n_chars = {k: con.execute(f"SELECT count(*) FROM {k}").fetchone()[0] for k in
           ["eventos", "versoes_texto", "feedbacks_ia", "narrativas", "cenas", "escolhas", "execucoes",
            "orientacoes_professora", "reflexoes", "alunos", "sessoes_criativas", "personagens"] if k != "personagens"}
sess = q("SELECT data, duracao_min, iniciada_em, encerrada_em FROM sessoes_criativas")
for t in (vers, nar, exe, refl, ori, cenas, fbk):
    for c in t.columns:
        if c.endswith("_em"):
            t[c] = pd.to_datetime(t[c]) - pd.Timedelta(hours=3)
alunos["entrou_em"] = pd.to_datetime(alunos["entrou_em"]) - pd.Timedelta(hours=3)
vers["validada_em"] = pd.to_datetime(vers["validada_em"])
vers["validada_em"] = vers["validada_em"]

# mapeamentos id -> aluno
pa2al = dict(zip(pa.pa_id, pa.aluno_id))
vid2al = {r.vid: pa2al[r.pa_id] for r in vers.itertuples()}
nid2al = {r.nid: pa2al[r.pa_id] for r in nar.itertuples()}


def aluno_do_evento(c):
    if c.get("aluno_id"):
        return c["aluno_id"]
    if c.get("narrativa_id") in nid2al:
        return nid2al[c["narrativa_id"]]
    if c.get("versao_id") in vid2al:
        return vid2al[c["versao_id"]]
    return None


ev["aluno"] = [aluno_do_evento(c) for c in ctx]
ev["ctx"] = ctx

# ----------------------------------------------------------------------------------
# 2. Rótulos anônimos E1..E22 (reaproveita os do dossiê por impressão digital não textual)
# ----------------------------------------------------------------------------------
uso = pd.read_csv(ROOT / "csv-03-uso-por-estudante-anonimo.csv")
n_vers = vers.groupby("pa_id").size()
last_words = vers.sort_values("numero").groupby("pa_id").palavras.last()
n_fb = fbk.merge(vers[["vid", "pa_id"]], on="vid").groupby("pa_id").size()
n_ori = ori.merge(vers[["vid", "pa_id"]], on="vid").groupby("pa_id").size()
fp = {}
for r in pa.itertuples():
    fp[r.aluno_id] = (int(n_vers.get(r.pa_id, 0)), int(last_words.get(r.pa_id, 0)),
                      int(n_fb.get(r.pa_id, 0)), int(n_ori.get(r.pa_id, 0)))
lab = {}
for r in uso.itertuples():
    key = (int(r.versoes), int(r.palavras_ult), int(r.fb_ia), int(r.orient))
    cand = [a for a, k in fp.items() if k == key and a not in lab]
    if len(cand) == 1:
        lab[cand[0]] = r.rotulo
if len(lab) != 22:  # contingência: sorteio com semente fixa para os restantes
    rest = [a for a in sorted(fp) if a not in lab]
    free = [f"E{i}" for i in range(1, 23) if f"E{i}" not in lab.values()]
    RNG2 = np.random.default_rng(7)
    RNG2.shuffle(free)
    for a, l in zip(rest, free):
        lab[a] = l
RES["rotulos_casados_com_dossie"] = sum(1 for a in lab if True)
ALUNOS = sorted(lab, key=lambda a: int(lab[a][1:]))
ev["E"] = ev["aluno"].map(lab)

# ----------------------------------------------------------------------------------
# 3. Janelas dos encontros (minutos desde o início)
# ----------------------------------------------------------------------------------
sess["ini"] = pd.to_datetime(sess["iniciada_em"]) - pd.Timedelta(hours=3)
sess["fim"] = pd.to_datetime(sess["encerrada_em"]) - pd.Timedelta(hours=3)
sess["dia"] = sess["fim"].dt.strftime("%Y-%m-%d")
JAN = {}
for dia, e in DIAS.items():
    s = sess[(sess.dia == dia)].sort_values("fim").iloc[0]
    ent = ev[(ev.tipo == "aluno_entrou") & (ev.dia == dia)].ts.min()
    ini = s.ini if pd.notna(s.ini) else ent
    ini = min(ini, ent)
    JAN[e] = dict(ini=ini, fim=s.fim, dur=int(s.duracao_min))
ev["t_min"] = [(r.ts - JAN[int(r.enc)]["ini"]).total_seconds() / 60 if pd.notna(r.enc) else np.nan for r in ev.itertuples()]

TIPOS = ev.tipo.value_counts()
RES["eventos_total"] = int(len(ev))
RES["tipos_total"] = int(ev.tipo.nunique())
RES["eventos_por_encontro"] = {int(k): int(v) for k, v in ev.enc.value_counts().sort_index().items()}
RES["eventos_entre_aulas"] = int(ev.enc.isna().sum())
RES["janelas_min"] = {e: j["dur"] for e, j in JAN.items()}

# ----------------------------------------------------------------------------------
# 4. Categorias de evento
# ----------------------------------------------------------------------------------
CAT = {}
for t in TIPOS.index:
    if t == "telemetria_cliente":
        c = "Telemetria do navegador"
    elif t in ("versao_salva", "texto_finalizado", "feedback_solicitado", "feedback_recebido", "corrigir_texto", "ajuda_solicitada", "aluno_entrou"):
        c = "Etapa 1: texto e feedback"
    elif t in ("texto_validado", "orientacao_adicionada", "orientacao_ia_sugerida", "ajuda_atendida", "observacao_adicionada", "revisao_professora_vista") or t.startswith("sessao_") or t in ("oficina_criada", "projeto_criado", "codinomes_gerados"):
        c = "Professora e sessão"
    elif t in ("inatividade_alertada", "retorno_alertado", "travado_alerta"):
        c = "Andaimes e alertas"
    elif t.startswith("player_") or t in ("narrativa_executada", "mapa_aberto", "assistir_agora"):
        c = "Etapa 3: player"
    elif t in ("download", "reflexao_concluida"):
        c = "Etapa 4: conclusão"
    else:
        c = "Etapa 2: remidiação"
    CAT[t] = c
ev["cat"] = ev.tipo.map(CAT)
COLC = {"Telemetria do navegador": G3, "Etapa 1: texto e feedback": VIO, "Etapa 2: remidiação": ESM,
        "Etapa 3: player": "#10b981", "Etapa 4: conclusão": "#0f766e", "Professora e sessão": AMB,
        "Andaimes e alertas": "#92400e"}

# ----------------------------------------------------------------------------------
# 5. Telemetria de rede e tempo ativo por etapa
# ----------------------------------------------------------------------------------
tel = ev[ev.tipo == "telemetria_cliente"].copy()
for k in ("n", "falhas", "p50_ms", "p95_ms", "max_ms", "visivel", "online", "tela"):
    tel[k] = [c.get(k) for c in tel.ctx]
tel["p50_ms"] = pd.to_numeric(tel.p50_ms)
tel["p95_ms"] = pd.to_numeric(tel.p95_ms)


def etapa_tela(t):
    t = (t or "")
    if t in ("texto", "/student") or t.startswith("texto"):
        return "Etapa 1 Texto"
    if t.startswith("remidiacao"):
        return "Etapa 2 Remidiação"
    if t.startswith("player"):
        return "Etapa 3 Player"
    if t.startswith("celebration"):
        return "Etapa 4 Conclusão"
    return "Outras"


tel["etapa"] = tel.tela.map(etapa_tela)
tel["vis"] = tel.visivel.astype(bool)
# janela do encontro + 10 min de margem
tel = tel[tel.enc.notna()].copy()
tel["fim_jan"] = tel.enc.map(lambda e: JAN[int(e)]["fim"] + pd.Timedelta(minutes=10))
tel_all = tel.copy()
tel_win = tel[tel.ts <= tel.fim_jan].copy()
RES["telemetria_relatorios"] = int(len(tel_all))
RES["telemetria_na_janela"] = int(len(tel_win))

tempo = (tel_all[tel_all.vis].groupby(["E", "etapa"]).size().unstack(fill_value=0))
ETAPAS = ["Etapa 1 Texto", "Etapa 2 Remidiação", "Etapa 3 Player", "Etapa 4 Conclusão"]
tempo = tempo.reindex(columns=ETAPAS + ["Outras"], fill_value=0)
tempo_enc = tel_all[tel_all.vis].groupby(["enc", "etapa"]).size().unstack(fill_value=0).reindex(columns=ETAPAS + ["Outras"], fill_value=0)
RES["tempo_etapa"] = {}
for e in ETAPAS + ["Outras"]:
    v = tempo[e][tempo[e] > 0].values
    RES["tempo_etapa"][e] = quart(v) | dict(soma=float(v.sum()))
tempo_total = tempo[ETAPAS + ["Outras"]].sum(axis=1)
RES["tempo_total_aluno"] = quart(tempo_total.values)

# ----------------------------------------------------------------------------------
# 6. Marcos por estudante (tempos absolutos) e status derivado por fim de dia
# ----------------------------------------------------------------------------------
m = {a: {} for a in ALUNOS}
first_entry = ev[ev.tipo == "aluno_entrou"].groupby("aluno").ts.min()
for a in ALUNOS:
    m[a]["entrou"] = first_entry.get(a, alunos.set_index("id").entrou_em.get(a))
pa_of = {a: p for p, a in pa2al.items()}
for r in pa.itertuples():
    a = r.aluno_id
    v = vers[vers.pa_id == r.pa_id].sort_values("numero")
    m[a]["v1"] = v.salva_em.min()
    m[a]["v2"] = v.salva_em.iloc[1] if len(v) > 1 else pd.NaT
    f = fbk.merge(vers[["vid", "pa_id"]], on="vid")
    f = f[f.pa_id == r.pa_id]
    m[a]["fb"] = f.recebido_em.min() if len(f) else pd.NaT
    m[a]["envio"] = v[v.finalizada == 1].salva_em.min() if (v.finalizada == 1).any() else pd.NaT
    pr = v[v.validacao == "pronta"]
    m[a]["liberado"] = pd.to_datetime(pr.validada_em).min() if len(pr) else pd.NaT
    n = nar[nar.pa_id == r.pa_id]
    m[a]["narr"] = n.criada_em.min() if len(n) else pd.NaT
    cc = cenas[cenas.nid.isin(n.nid)]
    m[a]["cena1"] = cc.criada_em.min() if len(cc) else pd.NaT
    ex = exe[exe.nid.isin(n.nid)]
    m[a]["exec"] = ex.rodada_em.min() if len(ex) else pd.NaT
    rf = refl[(refl.pa_id == r.pa_id) & refl.concluida_em.notna()]
    m[a]["aval"] = pd.to_datetime(rf.concluida_em).min() if len(rf) else pd.NaT  # já convertida para UTC-3 no laço acima
dl = ev[ev.tipo == "download"].groupby("aluno").ts.min()
for a in ALUNOS:
    m[a]["down"] = dl.get(a, pd.NaT)
M = pd.DataFrame(m).T.loc[ALUNOS]
for c in M.columns:
    M[c] = pd.to_datetime(M[c])
MARCOS = [("entrou", "Entrou"), ("v1", "1ª versão"), ("v2", "2ª versão"), ("fb", "Feedback da IA"),
          ("envio", "Enviou à professora"), ("liberado", "Texto liberado"), ("narr", "Criou narrativa"),
          ("cena1", "Criou 1ª cena"), ("exec", "Executou a história"), ("down", "Baixou"), ("aval", "Concluiu avaliação")]
RES["funil"] = {lab_: int(M[k].notna().sum()) for k, lab_ in MARCOS}

# fim de cada encontro (corte: 23:59 do dia) para o status derivado
CORTE = {e: pd.Timestamp(d + " 23:59:59") for d, e in DIAS.items()}


def status_em(a, t):
    """Status derivado (espelha calcular_status) com o que existia no instante t."""
    mm = M.loc[a]
    if pd.isna(mm["entrou"]) or mm["entrou"] > t:
        return "Ainda não entrou"
    if pd.notna(mm["exec"]) and mm["exec"] <= t and pd.notna(mm["cena1"]) and mm["cena1"] <= t:
        return "Executou a história"
    if pd.notna(mm["cena1"]) and mm["cena1"] <= t:
        return "Remidiando (com cena)"
    v = vers[(vers.pa_id == pa_of[a]) & (vers.salva_em <= t)]
    dec = v[v.validacao.notna() & (v.validada_em <= t)]
    if (dec.validacao == "pronta").any():
        return "Texto liberado"
    pend = v[(v.finalizada == 1) & ~v.index.isin(dec.index)]
    ult = dec.numero.max() if len(dec) else -1
    if len(pend) and (pend.numero > ult).any():
        return "Aguardando liberação"
    return "Escrevendo (Etapa 1)"


STAT_ORD = ["Ainda não entrou", "Escrevendo (Etapa 1)", "Aguardando liberação", "Texto liberado", "Remidiando (com cena)", "Executou a história"]
ST = pd.DataFrame({e: [status_em(a, CORTE[e]) for a in ALUNOS] for e in (1, 2, 3, 4)}, index=ALUNOS)
RES["status_fim_encontro"] = {e: ST[e].value_counts().reindex(STAT_ORD, fill_value=0).to_dict() for e in ST.columns}

# presença por encontro
pres = ev[(ev.tipo == "aluno_entrou")].groupby(["E", "enc"]).size().unstack(fill_value=0).reindex(index=[lab[a] for a in ALUNOS], columns=[1, 2, 3, 4], fill_value=0)
PRES = (pres > 0).astype(int)
RES["presenca"] = {e: int(PRES[e].sum()) for e in PRES.columns}
RES["freq_presenca"] = Counter(PRES.sum(axis=1)).most_common()
RES["freq_presenca"] = {int(k): int(v) for k, v in RES["freq_presenca"]}

# ----------------------------------------------------------------------------------
# 7. Métricas por estudante (não textuais)
# ----------------------------------------------------------------------------------
S = pd.DataFrame(index=ALUNOS)
S["E"] = [lab[a] for a in ALUNOS]
S["versoes"] = [fp[a][0] for a in ALUNOS]
S["palavras_ult"] = [fp[a][1] for a in ALUNOS]
S["fb"] = [fp[a][2] for a in ALUNOS]
S["orient"] = [fp[a][3] for a in ALUNOS]
nn = nar.assign(aluno=nar.nid.map(nid2al))
cc = cenas.assign(aluno=cenas.nid.map(nid2al))
S["cenas"] = cc.groupby("aluno").size().reindex(ALUNOS).fillna(np.nan)
S.loc[nn.aluno.unique(), "cenas"] = S.loc[nn.aluno.unique(), "cenas"].fillna(0)
S["execucoes"] = exe.assign(aluno=exe.nid.map(nid2al)).groupby("aluno").size().reindex(ALUNOS)
S.loc[nn.aluno.unique(), "execucoes"] = S.loc[nn.aluno.unique(), "execucoes"].fillna(0)
ce = esc.merge(cenas[["id", "nid"]].rename(columns={"id": "cena_id"}), on="cena_id")
S["escolhas"] = ce.assign(aluno=ce.nid.map(nid2al)).groupby("aluno").size().reindex(ALUNOS)
S.loc[nn.aluno.unique(), "escolhas"] = S.loc[nn.aluno.unique(), "escolhas"].fillna(0)
# personagens por narrativa: evento personagem_criado (totais líquidos exigiriam texto): usa contagem do csv-03
S = S.merge(uso[["rotulo", "personagens", "falas"]].rename(columns={"rotulo": "E"}), on="E", how="left").set_index(S.index)
S["min_E1"] = [tempo.loc[S.loc[a, "E"], "Etapa 1 Texto"] if S.loc[a, "E"] in tempo.index else 0 for a in ALUNOS]
S["min_E2"] = [tempo.loc[S.loc[a, "E"], "Etapa 2 Remidiação"] if S.loc[a, "E"] in tempo.index else 0 for a in ALUNOS]
S["min_E1"] = S["min_E1"].replace(0, np.nan)  # sem relatório na Etapa 1 = sem medida (não é zero)
S["min_E3"] = [tempo.loc[S.loc[a, "E"], "Etapa 3 Player"] if S.loc[a, "E"] in tempo.index else 0 for a in ALUNOS]
S["min_total"] = [tempo_total.get(S.loc[a, "E"], 0) for a in ALUNOS]
RANK = {k: i for i, (k, _) in enumerate(MARCOS)}
S["marco_max"] = [max([RANK[k] for k, _ in MARCOS if pd.notna(M.loc[a, k])]) for a in ALUNOS]
S["enc_presentes"] = [int(PRES.loc[S.loc[a, "E"]].sum()) for a in ALUNOS]
S.to_csv(OUT / "estudantes_metricas.csv", index=False)
RES["metricas"] = {c: quart(S[c].dropna().values) for c in ["versoes", "palavras_ult", "fb", "orient", "cenas", "personagens", "escolhas", "falas", "execucoes"]}
RES["metricas_narr_cenas_n"] = int(S.cenas.notna().sum())
RES["escolhas_zero"] = int((S.escolhas.dropna() == 0).sum())


# ==================================================================================
# FIGURAS
# ==================================================================================
# ---------- D1: diagrama do pipeline (SVG escrito à mão) ----------
def tabela_n(t):
    return con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]


NT = {t: tabela_n(t) for t in ["eventos", "versoes_texto", "feedbacks_ia", "orientacoes_professora", "narrativas",
                               "cenas", "escolhas", "execucoes", "reflexoes", "alunos", "sessoes_criativas"]}
RES["registros_por_tabela"] = NT
N_TEL = int((ev.tipo == "telemetria_cliente").sum())


def fig_pipeline():
    W, H = 1200, 635
    f = lambda n: f"{n:,}".replace(",", ".")
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" role="img" aria-labelledby="t d" font-family="DejaVu Sans, Helvetica, Arial, sans-serif">',
         '<title id="t">Esquema de dados e pipeline de análise</title>',
         '<desc id="d">Cinco fontes de dados fluem por extração somente leitura, anonimização, cálculo de métricas e geração de tabelas e figuras; colunas de texto de estudantes nunca são lidas.</desc>',
         '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#374151"/></marker></defs>',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>']

    def box(x, y, w, h, title, lines, color, fill):
        o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{fill}" stroke="{color}" stroke-width="1.6"/>')
        o.append(f'<text x="{x+12}" y="{y+22}" font-size="13" font-weight="700" fill="{color}">{title}</text>')
        for i, ln in enumerate(lines):
            o.append(f'<text x="{x+12}" y="{y+42+i*16}" font-size="11" fill="#1f2937">{ln}</text>')

    def arrow(x1, y1, x2, y2, dash=False):
        d = ' stroke-dasharray="5 4"' if dash else ""
        o.append(f'<path d="M{x1} {y1} L{x2} {y2}" stroke="#374151" stroke-width="1.6" fill="none"{d} marker-end="url(#a)"/>')

    # coluna de fontes
    o.append(f'<text x="20" y="28" font-size="12" font-weight="700" fill="#6b7280">FONTES</text>')
    box(20, 40, 290, 118, "F1 Banco do piloto (somente leitura)", [f"{f(NT['eventos'])} eventos de {ev.tipo.nunique()} tipos (metadados)",
        f"{f(N_TEL)} relatórios de rede do navegador", f"{NT['versoes_texto']} versões, {NT['feedbacks_ia']} feedbacks, {NT['orientacoes_professora']} orientações",
        f"{NT['narrativas']} narrativas, {NT['cenas']} cenas, {NT['execucoes']} execuções, {NT['reflexoes']} avaliações", f"{NT['alunos']} estudantes, {NT['sessoes_criativas']} sessões; 4 encontros + 7 semanas"], VIO, "#f5f3ff")
    box(20, 172, 290, 76, "F2 Telemetria agregada e CSV derivados", ["02-telemetria-agregada.md, csv-01 a csv-07", "usados para conferência cruzada dos totais"], ESM, "#ecfdf5")
    box(20, 262, 290, 76, "F3 Avaliação objetiva do feedback", ["07-avaliacao-feedback-ia.csv", "13 respostas de 3 participantes autorizados"], VIO, "#f5f3ff")
    box(20, 352, 290, 90, "F4 Repositório (histórico do software)", ["git log (commits por tipo), ADRs, testes", "automatizados, contagem de achados"], AMB, "#fffbeb")
    box(20, 456, 290, 76, "F5 Registros de campo (4 encontros)", ["achados com identificador por encontro", "painel de monitoramento (só citado)"], AMB, "#fffbeb")
    # etapas
    xs = [360, 560, 760, 960]
    titles = ["1 Extração", "2 Anonimização", "3 Métricas", "4 Saídas"]
    body = [
        ["SQLite aberto com", "mode=ro (uri); python3 -I", "SELECT só de colunas", "numéricas, ids, tipos,", "carimbos e categorias", "Texto de estudantes:", "nunca selecionado"],
        ["ids de estudante em", "rótulos E1 a E22", "datas viram encontro 1 a 4", "e minutos desde o início", "UTC convertido em UTC-3", "sem nomes, codinomes,", "escola ou município"],
        ["marcos e funil; tempo", "ativo por etapa", "matriz de transição", "ECDF, percentis, KM", "bootstrap (10.000)", "Spearman e Wilson", "git: commits e testes"],
        ["15 figuras PNG (200 dpi)", "e este diagrama SVG", "tabelas em Markdown", "CSV agregados em", "08-saidas/", "script único e", "reprodutível"],
    ]
    cols = [VIO, G1, ESM, AMB]
    fills = ["#f5f3ff", "#f3f4f6", "#ecfdf5", "#fffbeb"]
    o.append('<text x="360" y="28" font-size="12" font-weight="700" fill="#6b7280">PIPELINE</text>')
    for x, t, b, c, fl in zip(xs, titles, body, cols, fills):
        box(x, 80, 180, 190, t, b, c, fl)
    for i in range(3):
        arrow(xs[i] + 180, 175, xs[i + 1], 175)
    for y in (99, 210, 300, 397, 494):
        arrow(310, y, 358, 175 if y < 320 else 190, dash=False)
    # barreira de privacidade
    o.append(f'<rect x="346" y="62" width="840" height="226" rx="12" fill="none" stroke="{AMB}" stroke-width="1.4" stroke-dasharray="7 5"/>')
    o.append(f'<text x="360" y="304" font-size="11" fill="#92400e">Barreira de privacidade: processamento local no cofre, sem rede; saem apenas agregados e rótulos anônimos</text>')
    # saídas para o artigo
    box(360, 340, 580, 100, "Produtos para o artigo (Seção 6.4)", ["Esquema e pipeline (D1) | Distribuições (D2 a D4) | Funil, fluxo e retenção (D5 a D8)",
        "Processo: transições, trajetórias e intervalos (D9 a D11) | Desempenho técnico (D12 a D14)",
        "Associações exploratórias (D15) | Evolução do software (D16) | Tabelas D1 a D11"], ESM, "#ecfdf5")
    arrow(1050, 270, 800, 340)
    box(960, 340, 230, 100, "Controles", ["Totais conferidos com o dossiê", "(6.606; 2.928; 101; 41)", "Semente fixa (bootstrap, rótulos)", "Banco nunca alterado"], G1, "#f9fafb")
    # legenda
    o.append(f'<text x="20" y="590" font-size="11" fill="#374151">Cores: violeta = escrita e IA; esmeralda = computação e métricas; âmbar = mediação humana e histórico do software; cinza = infraestrutura.</text>')
    o.append(f'<text x="20" y="612" font-size="11" fill="#374151">Linhas contínuas = fluxo de dados; tracejada âmbar = fronteira de privacidade (nenhuma coluna de texto de estudante cruza a fronteira).</text>')
    o.append('</svg>')
    (FIG / "figD1-pipeline.svg").write_text("\n".join(o), encoding="utf-8")


fig_pipeline()

# ---------- D2: eventos por tipo e por encontro ----------
def fig_eventos():
    fig, ax = plt.subplots(1, 2, figsize=(11, 5.4), gridspec_kw={"width_ratios": [1.15, 1]})
    top = TIPOS.head(20)[::-1]
    ax[0].barh(range(len(top)), top.values, color=[COLC[CAT[t]] for t in top.index])
    ax[0].set_yticks(range(len(top)))
    ax[0].set_yticklabels(top.index, fontsize=7.5)
    ax[0].set_xscale("log")
    for i, v in enumerate(top.values):
        ax[0].text(v * 1.08, i, f"{v:,}".replace(",", "."), va="center", fontsize=7)
    ax[0].set_xlabel("Eventos (escala logarítmica)")
    ax[0].set_title("A. Os 20 tipos mais frequentes (de 56)")
    ax[0].set_xlim(1, 12000)
    ax[0].grid(axis="y", visible=False)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in COLC.values()]
    ax[0].legend(handles, list(COLC.keys()), loc="lower right", fontsize=7)
    sub = ev[ev.tipo != "telemetria_cliente"].copy()
    sub["g"] = sub.enc.fillna(0).astype(int)
    tab = sub.groupby(["g", "cat"]).size().unstack(fill_value=0).reindex(columns=[c for c in COLC if c in sub.cat.unique()], fill_value=0)
    tab = tab.reindex([1, 2, 3, 4, 0])
    bottom = np.zeros(len(tab))
    for c in tab.columns:
        ax[1].bar(range(len(tab)), tab[c].values, bottom=bottom, color=COLC[c], label=c, width=0.7)
        bottom += tab[c].values
    for i, t in enumerate(bottom):
        ax[1].text(i, t + 25, f"{int(t):,}".replace(",", "."), ha="center", fontsize=8)
    ax[1].set_xticks(range(5))
    ax[1].set_xticklabels(["Encontro 1", "Encontro 2", "Encontro 3", "Encontro 4", "Entre aulas"], fontsize=8)
    ax[1].set_ylabel("Eventos de uso (sem telemetria)")
    ax[1].set_title("B. Eventos por encontro e categoria")
    ax[1].grid(axis="x", visible=False)
    fig.tight_layout()
    savefig(fig, "figD2-eventos.png")
    tab.assign(total=tab.sum(axis=1)).to_csv(OUT / "eventos_por_encontro_categoria.csv")
    RES["eventos_sem_telemetria_por_encontro"] = {int(k): int(v) for k, v in tab.sum(axis=1).items()}
    RES["eventos_categoria"] = tab.sum().astype(int).to_dict()


fig_eventos()


# ---------- D3: tempo ativo por etapa ----------
def fig_tempo():
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.6), gridspec_kw={"width_ratios": [1.25, 1]})
    cols = [VIO, ESM, "#10b981", "#0f766e"]
    data = [tempo[e][tempo[e] > 0].values for e in ETAPAS]
    bp = ax[0].boxplot(data, orientation="vertical", widths=0.5, patch_artist=True, showfliers=False,
                       medianprops=dict(color="black", linewidth=1.6), whiskerprops=dict(color=G2), capprops=dict(color=G2))
    for p, c in zip(bp["boxes"], cols):
        p.set_facecolor(c)
        p.set_alpha(0.35)
        p.set_edgecolor(c)
    for i, d in enumerate(data):
        ax[0].scatter(np.full(len(d), i + 1) + RNG.uniform(-0.17, 0.17, len(d)), d, s=16, color=cols[i], edgecolor="white", linewidth=0.4, zorder=3)
        q1, md, q3 = np.quantile(d, [.25, .5, .75])
        ax[0].text(i + 1.36, md, f"mediana {fmt(md,0)}\nQ1–Q3 {fmt(q1,0)}–{fmt(q3,0)}\nn={len(d)}", fontsize=7, va="center")
    ax[0].set_xticks(range(1, 5))
    ax[0].set_xticklabels(["Etapa 1\nTexto", "Etapa 2\nRemidiação", "Etapa 3\nPlayer", "Etapa 4\nConclusão"])
    ax[0].set_ylabel("Minutos com a aba visível, por estudante")
    ax[0].set_xlim(0.5, 5.0)
    ax[0].set_title("A. Tempo ativo por etapa (encontros 2 a 4)")
    ax[0].grid(axis="x", visible=False)
    te = tempo_enc.reindex([2, 3, 4])
    bottom = np.zeros(3)
    for e, c in zip(ETAPAS + ["Outras"], cols + [G3]):
        ax[1].bar(range(3), te[e].values, bottom=bottom, color=c, label=e.replace("Etapa ", "E"), width=0.6)
        bottom += te[e].values
    for i, t in enumerate(bottom):
        ax[1].text(i, t + 15, f"{int(t)}", ha="center", fontsize=8)
    ax[1].set_xticks(range(3))
    ax[1].set_xticklabels(["Encontro 2", "Encontro 3", "Encontro 4"])
    ax[1].set_ylabel("Minutos-estudante")
    ax[1].set_title("B. Composição do tempo por encontro")
    ax[1].legend(loc="upper left", ncol=1, fontsize=7)
    ax[1].grid(axis="x", visible=False)
    fig.tight_layout()
    savefig(fig, "figD3-tempo-etapas.png")
    te.assign(total=te.sum(axis=1)).to_csv(OUT / "tempo_por_encontro_etapa.csv")
    RES["tempo_enc"] = te.astype(int).to_dict()
    # bootstrap das medianas
    RES["tempo_boot"] = {e: dict(zip(("med", "lo", "hi"), boot_ci(tempo[e][tempo[e] > 0].values))) for e in ETAPAS}


fig_tempo()


# ---------- D4: distribuições de uso e estrutura ----------
def fig_dist():
    fig, axs = plt.subplots(2, 3, figsize=(11, 6))
    spec = [("versoes", "Versões do texto por estudante", VIO, 22, 1), ("fb", "Feedbacks da IA por estudante", VIO, 22, 1),
            ("orient", "Orientações da professora por estudante", AMB, 22, 1),
            ("cenas", "Cenas por narrativa", ESM, 17, 2), ("personagens", "Personagens por narrativa", ESM, 17, 1),
            ("escolhas", "Escolhas por narrativa", ESM, 17, 1)]
    for ax, (c, ttl, col, n, w) in zip(axs.ravel(), spec):
        v = S[c].dropna().astype(int).values
        mx = v.max()
        if c == "cenas":
            bins = np.arange(0, 56, 4) 
        else:
            bins = np.arange(0, mx + 2) - 0.5
        ax.hist(v, bins=bins, color=col, alpha=0.85, edgecolor="white")
        md = np.median(v)
        ax.axvline(md, color="black", ls="--", lw=1)
        ax.text(0.97, 0.93, f"mediana {fmt(md)}\nQ1–Q3 {fmt(np.quantile(v,.25))}–{fmt(np.quantile(v,.75))}\nn={len(v)}", transform=ax.transAxes, ha="right", va="top", fontsize=7.5)
        ax.set_title(ttl, fontsize=9)
        ax.set_ylabel("Estudantes" if c in ("versoes", "cenas") else "")
        ax.grid(axis="x", visible=False)
    fig.suptitle("Distribuições do uso do ciclo (linha tracejada = mediana)", fontsize=10.5, fontweight="bold", y=1.0)
    fig.tight_layout()
    savefig(fig, "figD4-distribuicoes.png")


fig_dist()


# ---------- D5: funil com IC de Wilson ----------
def fig_funil():
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    labs = [l for _, l in MARCOS]
    ks = [int(M[k].notna().sum()) for k, _ in MARCOS]
    colr = [VIO, VIO, VIO, VIO, AMB, AMB, ESM, ESM, "#10b981", "#0f766e", "#0f766e"]
    rows = []
    for i, (k, lb) in enumerate(zip(ks, labs)):
        p, lo, hi = wilson(k, 22)
        rows.append((lb, k, p, lo, hi))
        ax.barh(i, p * 100, color=colr[i], alpha=0.85, height=0.62)
        ax.plot([lo * 100, hi * 100], [i, i], color="black", lw=1.2)
        ax.plot([lo * 100] * 2, [i - .12, i + .12], color="black", lw=1.2)
        ax.plot([hi * 100] * 2, [i - .12, i + .12], color="black", lw=1.2)
        ax.text(103, i, f"{k}/22 ({p*100:.0f}%)", va="center", fontsize=8)
    ax.set_yticks(range(len(labs)))
    ax.set_yticklabels(labs)
    ax.invert_yaxis()
    ax.set_xlim(0, 125)
    ax.set_xticks(range(0, 101, 20))
    ax.set_xlabel("% dos 22 estudantes que alcançaram o marco (barras de erro: IC95% de Wilson)")
    ax.set_title("Funil de marcos do Ciclo de Remidiação Ativa ao fim do piloto")
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    savefig(fig, "figD5-funil.png")
    RES["funil_wilson"] = {r[0]: dict(k=r[1], p=r[2], lo=r[3], hi=r[4]) for r in rows}
    RES["conversao_passo"] = {labs[i]: ks[i] / ks[i - 1] for i in range(1, len(ks))}


fig_funil()


# ---------- D6: Sankey entre encontros ----------
STCOL = {"Executou a história": "#0f766e", "Remidiando (com cena)": ESM, "Texto liberado": AMB_L,
         "Aguardando liberação": AMB, "Escrevendo (Etapa 1)": VIO_L, "Ainda não entrou": G4}
SK_ORD = ["Executou a história", "Remidiando (com cena)", "Texto liberado", "Aguardando liberação", "Escrevendo (Etapa 1)", "Ainda não entrou"]


def fig_sankey():
    fig, ax = plt.subplots(figsize=(11, 5.8))
    xs = [0, 1, 2, 3]
    nw = 0.045
    gap = 0.8
    # posições dos nós por coluna (altura em número de estudantes)
    pos = {}
    for e in (1, 2, 3, 4):
        cnt = ST[e].value_counts()
        y = 0.0
        for s_ in SK_ORD:
            h = int(cnt.get(s_, 0))
            pos[(e, s_)] = [y, h, y]  # base, altura, cursor de saída; cursor de entrada separado
            y += h + (gap if h > 0 else 0)
    tot_h = max(max(v[0] + v[1] for k, v in pos.items() if k[0] == e) for e in (1, 2, 3, 4))
    inc = {k: v[0] for k, v in pos.items()}
    out = {k: v[0] for k, v in pos.items()}
    for e in (1, 2, 3):
        fl = Counter(zip(ST[e], ST[e + 1]))
        # ordenar para minimizar cruzamentos
        for (a, b), n in sorted(fl.items(), key=lambda kv: (SK_ORD.index(kv[0][0]), SK_ORD.index(kv[0][1]))):
            y0 = out[(e, a)]
            y1 = inc[(e + 1, b)]
            out[(e, a)] += n
            inc[(e + 1, b)] += n
            x0, x1 = xs[e - 1] + nw, xs[e] - nw
            xm = (x0 + x1) / 2
            verts = [(x0, y0), (xm, y0), (xm, y1), (x1, y1), (x1, y1 + n), (xm, y1 + n), (xm, y0 + n), (x0, y0 + n), (x0, y0)]
            codes = [MPath.MOVETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4, MPath.LINETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4, MPath.CLOSEPOLY]
            ax.add_patch(PathPatch(MPath(verts, codes), facecolor=STCOL[a], edgecolor="white", lw=0.4, alpha=0.55 if a != "Ainda não entrou" else 0.5))
            if n >= 2:
                ax.text(xm, (y0 + y0 + n) / 2 * 0.5 + (y1 + y1 + n) / 2 * 0.5, str(n), ha="center", va="center", fontsize=7, color="black")
    for (e, s_), (base, h, _) in pos.items():
        if h == 0:
            continue
        ax.add_patch(Rectangle((xs[e - 1] - nw, base), 2 * nw, h, facecolor=STCOL[s_], edgecolor="white"))
        ha, dx = ("right", -nw - 0.02) if e == 1 else ("left", nw + 0.02) if e == 4 else ("center", 0)
        if e in (1, 4):
            ax.text(xs[e - 1] + dx, base + h / 2, f"{s_} ({h})", ha=ha, va="center", fontsize=8)
        else:
            ax.text(xs[e - 1], base + h / 2, str(h), ha="center", va="center", fontsize=8, fontweight="bold")
    ax.set_xlim(-0.95, 3.95)
    ax.set_ylim(tot_h + 1, -1.2)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"Fim do encontro {e}" for e in (1, 2, 3, 4)])
    ax.set_yticks([])
    ax.grid(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="x", length=0)
    # legenda
    hs = [plt.Rectangle((0, 0), 1, 1, color=STCOL[s_]) for s_ in SK_ORD]
    ax.legend(hs, SK_ORD, loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.17), fontsize=8)
    ax.set_title("Fluxo dos 22 estudantes entre os encontros, pela situação derivada ao fim de cada dia")
    fig.tight_layout()
    savefig(fig, "figD6-sankey.png")
    trans = {}
    for e in (1, 2, 3):
        trans[f"{e}->{e+1}"] = {f"{a} > {b}": int(n) for (a, b), n in Counter(zip(ST[e], ST[e + 1])).items()}
    RES["fluxos"] = trans
    ST.rename(columns=lambda c: f"fim_enc{c}").assign(E=[lab[a] for a in ALUNOS]).to_csv(OUT / "status_por_encontro.csv", index=False)


fig_sankey()


# ---------- D7: presença e situação por estudante (retenção entre encontros) ----------
def fig_presenca():
    order = sorted(ALUNOS, key=lambda a: (-S.loc[a, "marco_max"], -S.loc[a, "enc_presentes"], int(lab[a][1:])))
    fig, ax = plt.subplots(1, 2, figsize=(11, 6.2), gridspec_kw={"width_ratios": [1.3, 1]})
    for i, a in enumerate(order):
        for j, e in enumerate((1, 2, 3, 4)):
            c = STCOL[ST.loc[a, e]]
            ax[0].add_patch(Rectangle((j, i), 0.92, 0.88, facecolor=c, edgecolor="white"))
            dark = ST.loc[a, e] in ("Executou a história", "Remidiando (com cena)", "Aguardando liberação")
            mc = "white" if dark else "black"
            if PRES.loc[lab[a], e]:
                ax[0].plot(j + 0.46, i + 0.44, "o", color=mc, ms=4.5)
            else:
                ax[0].plot(j + 0.46, i + 0.44, "x", color=mc, ms=4.5, mew=1)
    ax[0].set_xlim(0, 4)
    ax[0].set_ylim(len(order), 0)
    ax[0].set_xticks([j + 0.46 for j in range(4)])
    ax[0].set_xticklabels([f"Enc. {e}" for e in (1, 2, 3, 4)])
    ax[0].set_yticks([i + 0.44 for i in range(len(order))])
    ax[0].set_yticklabels([lab[a] for a in order], fontsize=7.5)
    ax[0].grid(False)
    ax[0].set_title("A. Presença (círculo) ou ausência (x) e situação ao fim do dia")
    hs = [plt.Rectangle((0, 0), 1, 1, color=STCOL[s_]) for s_ in SK_ORD]
    ax[0].legend(hs, SK_ORD, loc="upper center", bbox_to_anchor=(0.5, -0.06), ncol=2, fontsize=7)
    # B: retenção: P(presente em k+1 | presente em k), e presença condicional
    pr = PRES.values
    ks = [pr[:, j].sum() for j in range(4)]
    cond = [(pr[:, j] & pr[:, j + 1]).sum() for j in range(3)]
    ax[1].bar(range(4), ks, color=VIO, alpha=0.85, width=0.55, label="Presentes")
    for j in range(4):
        ax[1].text(j, ks[j] + 0.4, f"{ks[j]}/22", ha="center", fontsize=8)
    ax[1].plot(range(1, 4), cond, "o--", color=AMB, label="Presentes no encontro anterior e neste")
    for j, c_ in enumerate(cond):
        ax[1].text(j + 1.08, c_ - 1.5, str(c_), color=AMB, fontsize=8)
    ax[1].set_xticks(range(4))
    ax[1].set_xticklabels([f"Enc. {e}" for e in (1, 2, 3, 4)])
    ax[1].set_ylim(0, 29)
    ax[1].set_ylabel("Estudantes")
    ax[1].set_title("B. Presença por encontro")
    ax[1].legend(fontsize=7.5, loc="upper center", ncol=1)
    ax[1].grid(axis="x", visible=False)
    fig.tight_layout()
    savefig(fig, "figD7-presenca.png")
    RES["presenca_condicional"] = {f"{j+1}->{j+2}": dict(ambos=int(cond[j]), de=int(ks[j])) for j in range(3)}
    RES["presenca_condicional_pct"] = {f"{j+1}->{j+2}": cond[j] / ks[j] for j in range(3)}


fig_presenca()


# ---------- D8: Kaplan-Meier em minutos de tela acumulados ----------
def km(times, events):
    times = np.asarray(times, float)
    events = np.asarray(events, bool)
    ut = np.unique(times[events])
    S_, var, out = 1.0, 0.0, [(0.0, 1.0, 1.0, 1.0, len(times))]
    g = 0.0
    for t in ut:
        n = (times >= t).sum()
        d = ((times == t) & events).sum()
        S_ *= 1 - d / n
        if n > d:
            g += d / (n * (n - d))
        se = S_ * np.sqrt(g)
        out.append((t, S_, max(0, S_ - 1.96 * se), min(1, S_ + 1.96 * se), int(n)))
    return out


def fig_km():
    vis = tel_all[tel_all.vis].copy()
    vis = vis.sort_values("ts")
    cum = {}
    for E_, d in vis.groupby("E"):
        cum[E_] = d.ts.values
    tot = {E_: len(v) for E_, v in cum.items()}
    # relógio de tela só existe a partir do encontro 2; eventos antes disso contam como tempo 0
    first_tel = vis.ts.min()

    def tela_ate(E_, t):
        if pd.isna(t):
            return np.nan
        if E_ not in cum:
            return 0.0
        return float((cum[E_] <= np.datetime64(t)).sum())

    spec = [("cena1", "Criou a 1ª cena", ESM), ("exec", "Executou a história", "#0f766e")]
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.6), gridspec_kw={"width_ratios": [1.4, 1]})
    resk = {}
    for k, ttl, c in spec:
        T, Ev = [], []
        for a in ALUNOS:
            E_ = lab[a]
            if pd.notna(M.loc[a, k]):
                T.append(tela_ate(E_, M.loc[a, k]))
                Ev.append(True)
            else:
                T.append(float(tot.get(E_, 0)))
                Ev.append(False)
        T = np.array(T)
        Ev = np.array(Ev)
        curve_full = km(T, Ev)
        curve = [x for x in curve_full if x[4] >= 5]  # exibe só enquanto houver >= 5 em risco
        tcut = max(x[0] for x in curve)
        t_ = [x[0] for x in curve] + [tcut]
        s_ = [1 - x[1] for x in curve] + [1 - curve[-1][1]]
        lo_ = [1 - x[3] for x in curve] + [1 - curve[-1][3]]
        hi_ = [1 - x[2] for x in curve] + [1 - curve[-1][2]]
        ax[0].step(t_, s_, where="post", color=c, lw=2, label=f"{ttl} ({int(Ev.sum())} de 22)")
        ax[0].fill_between(t_, lo_, hi_, step="post", color=c, alpha=0.12)
        cens = T[(~Ev) & (T <= tcut)]
        ax[0].plot(cens, [s_[np.searchsorted(t_, t, side="right") - 1] for t in cens], "|", color=c, ms=7, alpha=0.8)
        # mediana KM = menor t com S(t)<=0.5
        med = next((t for t, v in zip(t_, s_) if v >= 0.5), None)
        def at(t0):
            i = np.searchsorted(t_, t0, side="right") - 1
            return float(s_[i]), float(lo_[i]), float(hi_[i])
        resk[k] = dict(eventos=int(Ev.sum()), censurados=int((~Ev).sum()), mediana_min=med, tcut=float(tcut),
                       em60=at(60), em90=at(90), em_corte=at(tcut),
                       t_zero=int(((T == 0) & Ev).sum()), prob_final=float(s_[-1]), lo=float(lo_[-1]), hi=float(hi_[-1]))
    ax[0].set_xlabel("Minutos de tela acumulados desde o encontro 2 (curva mostrada enquanto há 5 ou mais em risco)")
    ax[0].set_ylabel("Proporção que já alcançou o marco (1 − S(t))")
    ax[0].set_ylim(-0.02, 1.02)
    ax[0].set_title("A. Kaplan-Meier com IC95% (Greenwood); | = censura")
    ax[0].legend(loc="upper left", fontsize=8)
    # B: distribuição do total de minutos de tela (n=22) com a marca dos que não alcançaram
    tt = np.array([tot.get(lab[a], 0) for a in ALUNOS])
    ax[1].hist(tt, bins=np.arange(0, 200, 20), color=G3, edgecolor="white")
    ax[1].set_xlabel("Minutos de tela totais por estudante (encontros 2 a 4)")
    ax[1].set_ylabel("Estudantes")
    ax[1].set_title("B. Exposição observada (n=22)")
    ax[1].grid(axis="x", visible=False)
    fig.tight_layout()
    savefig(fig, "figD8-kaplan-meier.png")
    RES["km"] = resk


fig_km()


# ---------- D9: matriz de transição entre tipos de evento ----------
SYM = {}
for t in TIPOS.index:
    if t in ("aluno_entrou",): s_ = "Entrada"
    elif t == "versao_salva": s_ = "Salvar versão"
    elif t == "feedback_solicitado": s_ = "Pedir feedback IA"
    elif t == "feedback_recebido": s_ = "Receber feedback IA"
    elif t == "texto_finalizado": s_ = "Enviar à professora"
    elif t in ("texto_validado", "orientacao_adicionada", "revisao_professora_vista"): s_ = "Mediação da professora"
    elif t.startswith("divisao"): s_ = "Dividir em cenas"
    elif t in ("cena_criada", "cena_editada", "cena_removida", "previa_cena"): s_ = "Cenas (criar/editar)"
    elif t.startswith("personagem"): s_ = "Personagens"
    elif t.startswith("escolha") or t.startswith("condicional") or t.startswith("variavel"): s_ = "Escolhas e lógica"
    elif t.startswith("player") or t in ("narrativa_executada", "assistir_agora", "mapa_aberto"): s_ = "Player"
    elif t in ("trilha_passo", "trilha_clique", "tour_concluido", "intro_fechada", "memoria_modal", "narrativa_iniciada", "corrigir_texto"): s_ = "Trilha e apoio"
    elif t in ("inatividade_alertada", "retorno_alertado", "travado_alerta"): s_ = "Alerta de andaime"
    elif t == "download": s_ = "Baixar"
    else: s_ = None
    SYM[t] = s_
ev["sym"] = ev.tipo.map(SYM)
SYM_ORD = ["Entrada", "Salvar versão", "Pedir feedback IA", "Receber feedback IA", "Enviar à professora", "Mediação da professora",
           "Alerta de andaime", "Trilha e apoio", "Dividir em cenas", "Cenas (criar/editar)", "Personagens", "Escolhas e lógica", "Player", "Baixar"]
seqd = ev[(ev.sym.notna()) & ev.E.notna() & ev.enc.notna()].sort_values(["E", "enc", "ts", "id"])


def fig_transicoes():
    cnt = np.zeros((len(SYM_ORD), len(SYM_ORD)))
    pers = np.zeros(len(SYM_ORD))
    tot_ev = np.zeros(len(SYM_ORD))
    gaps = []
    seqs = {}
    for (E_, enc_), d in seqd.groupby(["E", "enc"]):
        s_ = d.sym.tolist()
        t_ = d.ts.tolist()
        seqs[(E_, enc_)] = s_
        for i in range(len(s_) - 1):
            a, b = SYM_ORD.index(s_[i]), SYM_ORD.index(s_[i + 1])
            tot_ev[a] += 1
            if a == b:
                pers[a] += 1
            else:
                cnt[a, b] += 1
            gaps.append((s_[i], s_[i + 1], (t_[i + 1] - t_[i]).total_seconds()))
    row = cnt.sum(axis=1, keepdims=True)
    P = np.divide(cnt, row, out=np.zeros_like(cnt), where=row > 0)
    fig, ax = plt.subplots(1, 2, figsize=(12, 5.8), gridspec_kw={"width_ratios": [1.9, 1]})
    im = ax[0].imshow(P, cmap=matplotlib.colors.LinearSegmentedColormap.from_list("v", ["#ffffff", VIO_L, VIO]), vmin=0, vmax=1)
    for i in range(len(SYM_ORD)):
        for j in range(len(SYM_ORD)):
            if cnt[i, j] > 0:
                ax[0].text(j, i, f"{int(cnt[i,j])}", ha="center", va="center", fontsize=6.5, color="white" if P[i, j] > .55 else "black")
    ax[0].set_xticks(range(len(SYM_ORD)))
    ax[0].set_xticklabels(SYM_ORD, rotation=60, ha="right", fontsize=7.5)
    ax[0].set_yticks(range(len(SYM_ORD)))
    ax[0].set_yticklabels(SYM_ORD, fontsize=7.5)
    ax[0].set_xlabel("Evento seguinte")
    ax[0].set_ylabel("Evento de origem")
    ax[0].grid(False)
    ax[0].set_title("A. Transições entre ações distintas (cor = proporção na linha; número = contagem)")
    cb = fig.colorbar(im, ax=ax[0], fraction=0.035, pad=0.02)
    cb.set_label("Proporção da linha")
    pp = pers / np.maximum(tot_ev, 1)
    ok = tot_ev >= 20
    idx = [i for i in np.argsort(-pp) if ok[i]]
    ax[1].barh(range(len(idx)), pp[idx] * 100, color=[VIO if SYM_ORD[i] in SYM_ORD[:7] else ESM for i in idx], alpha=0.85)
    for k, i in enumerate(idx):
        ax[1].text(pp[i] * 100 + 1, k, f"{pp[i]*100:.0f}% (n={int(tot_ev[i])})", va="center", fontsize=7)
    ax[1].set_yticks(range(len(idx)))
    ax[1].set_yticklabels([SYM_ORD[i] for i in idx], fontsize=7.5)
    ax[1].invert_yaxis()
    ax[1].set_xlim(0, 125)
    ax[1].set_xlabel("% das vezes em que a mesma ação se repete em seguida")
    ax[1].set_title("B. Persistência por ação")
    ax[1].grid(axis="y", visible=False)
    fig.tight_layout()
    savefig(fig, "figD9-transicoes.png")
    pd.DataFrame(cnt, index=SYM_ORD, columns=SYM_ORD).astype(int).to_csv(OUT / "matriz_transicao_contagens.csv")
    top = sorted(((SYM_ORD[i], SYM_ORD[j], int(cnt[i, j]), float(P[i, j])) for i in range(len(SYM_ORD)) for j in range(len(SYM_ORD)) if cnt[i, j] > 0), key=lambda x: -x[2])[:14]
    RES["transicoes_top"] = top
    RES["persistencia"] = {SYM_ORD[i]: float(pp[i]) for i in range(len(SYM_ORD)) if tot_ev[i] > 0}
    RES["n_transicoes"] = int(cnt.sum() + pers.sum())
    return gaps, seqs


GAPS, SEQS = fig_transicoes()


# ---------- D10: linhas do tempo por etapa (telemetria) ----------
ECOL = {"Etapa 1 Texto": VIO, "Etapa 2 Remidiação": ESM, "Etapa 3 Player": "#10b981", "Etapa 4 Conclusão": "#0f766e", "Outras": G3}


def fig_linhas_tempo():
    fig, axs = plt.subplots(3, 1, figsize=(11, 10), gridspec_kw={"height_ratios": [14, 19, 15]})
    res = {}
    for ax, e in zip(axs, (2, 3, 4)):
        d = tel_win[tel_win.enc == e].copy()
        d["min"] = ((d.ts - JAN[e]["ini"]).dt.total_seconds() // 60).astype(int)
        order = d.groupby("E")["min"].min().sort_values().index.tolist()
        for i, E_ in enumerate(order):
            for r in d[d.E == E_].itertuples():
                col = ECOL[r.etapa] if r.vis else "#ffffff"
                ax.add_patch(Rectangle((r.min, i), 1, 0.85, facecolor=col, edgecolor=(G2 if not r.vis else "none"), lw=0.4 if not r.vis else 0, hatch=None))
        fb = ev[(ev.tipo == "feedback_solicitado") & (ev.enc == e)]
        for r in fb.itertuples():
            if r.E in order:
                ax.plot(r.t_min + 0.5, order.index(r.E) + 0.42, marker="D", ms=4.5, color=AMB, mec="black", mew=0.5, zorder=5)
        fn = ev[(ev.tipo == "texto_finalizado") & (ev.enc == e)]
        for r in fn.itertuples():
            if r.E in order:
                ax.plot(r.t_min + 0.5, order.index(r.E) + 0.42, marker="*", ms=8, color="white", mec="black", mew=0.6, zorder=5)
        ex = ev[(ev.tipo == "narrativa_executada") & (ev.enc == e)]
        for r in ex.itertuples():
            if r.E in order:
                ax.plot(r.t_min + 0.5, order.index(r.E) + 0.42, marker="^", ms=4.5, color="white", mec="black", mew=0.6, zorder=5)
        ax.set_xlim(0, JAN[e]["dur"] + 14)
        ax.axvline(JAN[e]["dur"] if e != 1 else 45, color=G1, ls=":", lw=1)
        ax.set_ylim(len(order), -0.2)
        ax.set_yticks([i + 0.42 for i in range(len(order))])
        ax.set_yticklabels(order, fontsize=7)
        ax.set_title(f"Encontro {e} (n={len(order)} navegadores)", loc="left", fontsize=9)
        ax.grid(axis="y", visible=False)
        res[e] = len(order)
    axs[-1].set_xlabel("Minutos desde o início do encontro (1 relatório de telemetria por minuto; célula branca = aba oculta; linha pontilhada = encerramento registrado)")
    hs = [plt.Rectangle((0, 0), 1, 1, color=ECOL[k]) for k in ECOL] + [
        plt.Line2D([], [], marker="D", color=AMB, mec="black", ls="", ms=5), plt.Line2D([], [], marker="*", color="white", mec="black", ls="", ms=8),
        plt.Line2D([], [], marker="^", color="white", mec="black", ls="", ms=5)]
    axs[0].legend(hs, ["Etapa 1 Texto", "Etapa 2 Remidiação", "Etapa 3 Player", "Etapa 4 Conclusão", "Outras", "Pedido de feedback da IA", "Texto enviado", "Execução da história"],
                  ncol=4, loc="lower center", bbox_to_anchor=(0.5, 1.12), fontsize=7.5)
    fig.tight_layout()
    savefig(fig, "figD10-linhas-do-tempo.png")
    RES["linhas_tempo_navegadores"] = res


fig_linhas_tempo()


# ---------- D11: intervalos entre eventos e entre marcos ----------
def fig_intervalos():
    g = pd.DataFrame(GAPS, columns=["a", "b", "s"])
    g["grupo"] = g.a.map(lambda x: "Etapa 1 (texto e feedback)" if x in ("Entrada", "Salvar versão", "Pedir feedback IA", "Receber feedback IA", "Enviar à professora", "Mediação da professora") else
                         "Etapa 2 (remidiação)" if x in ("Trilha e apoio", "Dividir em cenas", "Cenas (criar/editar)", "Personagens", "Escolhas e lógica") else
                         "Etapa 3 e 4 (player e baixar)" if x in ("Player", "Baixar") else "Alerta de andaime")
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.7), gridspec_kw={"width_ratios": [1.1, 1]})
    cm = {"Etapa 1 (texto e feedback)": VIO, "Etapa 2 (remidiação)": ESM, "Etapa 3 e 4 (player e baixar)": "#10b981", "Alerta de andaime": "#92400e"}
    res = {}
    for k, c in cm.items():
        x = np.sort(g[g.grupo == k].s.clip(lower=0.5).values)
        ax[0].step(x, np.arange(1, len(x) + 1) / len(x), where="post", color=c, lw=1.8, label=f"{k} (n={len(x)})")
        res[k] = dict(n=int(len(x)), med=float(np.median(x)), p90=float(np.quantile(x, .9)), acima240=float((x > 240).mean()))
    ax[0].set_xscale("log")
    ax[0].axvline(240, color=G1, ls=":", lw=1)
    ax[0].text(260, 0.55, "limiar do alerta\nde inatividade (4 min)", fontsize=7, color=G1)
    ax[0].set_xlabel("Intervalo até a ação seguinte do mesmo estudante, dentro do encontro (s, escala log)")
    ax[0].set_ylabel("Proporção acumulada (ECDF)")
    ax[0].set_title("A. Tempo entre ações consecutivas")
    ax[0].legend(loc="upper left", fontsize=7.5)
    ax[0].set_xlim(0.5, 1e4)
    RES["intervalos_acao"] = res
    # B: intervalos entre marcos, dentro do mesmo dia
    def dif(a, b, lim=240):
        x = (M[b] - M[a]).dt.total_seconds() / 60
        x = x[(x.notna())]
        return x[(x >= 0) & (x <= lim)].values, int(x.notna().sum())
    pares = [("entrou", "v1", "Entrada → 1ª versão"), ("narr", "cena1", "Narrativa → 1ª cena"), ("cena1", "exec", "1ª cena → 1ª execução"),
             ("exec", "down", "1ª execução → baixar"), ("down", "aval", "Baixar → avaliação")]
    data, labs_, ns, tot = [], [], [], []
    rb = {}
    for a, b, l in pares:
        x, n_all = dif(a, b)
        if len(x) >= 3:
            data.append(x)
            labs_.append(l)
            ns.append(len(x))
            tot.append(n_all)
            rb[l] = dict(n=len(x), de=n_all, **{k: v for k, v in quart(x).items() if k in ("med", "q1", "q3", "min", "max")})
    bp = ax[1].boxplot(data, orientation="horizontal", patch_artist=True, showfliers=False, widths=0.55,
                       medianprops=dict(color="black", lw=1.5))
    for p_ in bp["boxes"]:
        p_.set_facecolor(ESM_L)
        p_.set_edgecolor(ESM)
    for i, d in enumerate(data):
        ax[1].scatter(d, np.full(len(d), i + 1) + RNG.uniform(-0.15, 0.15, len(d)), s=12, color=ESM, edgecolor="white", lw=0.3, zorder=3)
    ax[1].set_yticks(range(1, len(data) + 1))
    ax[1].set_yticklabels([f"{l}\n(n={n} de {t})" for l, n, t in zip(labs_, ns, tot)], fontsize=7.5)
    ax[1].invert_yaxis()
    ax[1].set_xlabel("Minutos (somente intervalos de até 4 h, isto é, dentro do mesmo encontro)")
    ax[1].set_title("B. Intervalos entre marcos")
    ax[1].grid(axis="y", visible=False)
    fig.tight_layout()
    savefig(fig, "figD11-intervalos.png")
    RES["intervalos_marcos"] = rb


fig_intervalos()


# ---------- D12: rede, ECDF ----------
def fig_rede_ecdf():
    d = tel_win[tel_win.n > 0].copy()
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.4), sharey=True)
    cmap = {2: VIO_L, 3: ESM, 4: AMB}
    res = {}
    for e in (2, 3, 4):
        x = d[d.enc == e]
        for a_, col, ttl in ((ax[0], "p50_ms", "p50"), (ax[1], "p95_ms", "p95")):
            v = np.sort(x[col].values)
            a_.step(v, np.arange(1, len(v) + 1) / len(v), where="post", color=cmap[e] if e != 2 else VIO, lw=1.9, label=f"Encontro {e} (n={len(v)} relatórios)")
        pp = x.p95_ms.values
        pb = x.p50_ms.values
        res[e] = dict(relatorios=int(len(x)), p50_p50=float(np.median(pb)), p50_p90=float(np.quantile(pb, .9)), p95_p50=float(np.median(pp)),
                      p95_p90=float(np.quantile(pp, .9)), p95_p95=float(np.quantile(pp, .95)), p95_p99=float(np.quantile(pp, .99)), p95_max=float(pp.max()),
                      acima2s=int((pp > 2000).sum()), acima5s=int((pp > 5000).sum()), chamadas=int(x.n.sum()), falhas=int(x.falhas.sum()))
    for a_, ttl in zip(ax, ("A. Latência mediana (p50) por relatório", "B. Latência p95 por relatório")):
        a_.set_xscale("log")
        a_.set_xlabel("Milissegundos (escala log)")
        a_.set_title(ttl)
    ax[1].axvline(2000, color=G1, ls=":", lw=1)
    ax[1].text(2100, 0.1, "2 s", fontsize=7.5)
    ax[0].set_ylabel("Proporção acumulada (ECDF)")
    ax[0].legend(loc="lower right", fontsize=7.5)
    fig.tight_layout()
    savefig(fig, "figD12-rede-ecdf.png")
    # Kruskal-Wallis por navegador (mediana do p95 por navegador-encontro)
    nav = d.groupby(["enc", "E"]).p95_ms.median().reset_index()
    grp = [nav[nav.enc == e].p95_ms.values for e in (2, 3, 4)]
    H, pH = stats.kruskal(*grp)
    N_ = sum(len(g_) for g_ in grp)
    eps2 = (H - 3 + 1) / (N_ - 3)
    res["kw"] = dict(H=float(H), p=float(pH), eps2=float(eps2), n=[len(g_) for g_ in grp])
    # taxa de falha com Wilson
    tot_calls = int(d.n.sum())
    tot_fail = int(d.falhas.sum())
    res["falha"] = dict(calls=tot_calls, fail=tot_fail, wilson=wilson(tot_fail, tot_calls))
    res["p95_navegador"] = quart(nav.p95_ms.values)
    RES["rede"] = res


fig_rede_ecdf()


# ---------- D13: séries de 1 minuto ----------
def fig_serie_rede():
    d = tel_win.copy()
    d["min"] = ((d.ts - JAN[0 + 2]["ini"]).dt.total_seconds() // 60).astype(int)
    fig, axs = plt.subplots(3, 1, figsize=(11, 8.6))
    res = {}
    for ax, e in zip(axs, (2, 3, 4)):
        x = d[d.enc == e].copy()
        x["min"] = ((x.ts - JAN[e]["ini"]).dt.total_seconds() // 60).astype(int)
        agg = x.groupby("min").agg(nav=("E", "nunique"), p95med=("p95_ms", "median"), p95max=("p95_ms", "max"), falhas=("falhas", "sum"), calls=("n", "sum"))
        agg = agg.reindex(range(0, int(agg.index.max()) + 1))
        ax2 = ax.twinx()
        ax2.bar(agg.index, agg.nav.fillna(0), color=G4, width=0.9, zorder=0)
        ax2.set_ylim(0, 40)
        ax2.set_ylabel("Navegadores ativos", color=G2, fontsize=8)
        ax2.spines["right"].set_visible(True)
        ax2.grid(False)
        ax.set_zorder(ax2.get_zorder() + 1)
        ax.patch.set_visible(False)
        ax.plot(agg.index, agg.p95max, ".", color=AMB, ms=4, label="p95 máximo entre navegadores")
        ax.plot(agg.index, agg.p95med, "-", color=VIO, lw=1.6, label="p95 mediano entre navegadores")
        fl = agg[agg.falhas > 0]
        ax.plot(fl.index, [3 * 10 ** 4 * 1.0] * len(fl), "v", color="#b91c1c", ms=5, label="minuto com falha sem resposta")
        ax.set_yscale("log")
        ax.set_ylim(150, 8e4)
        ax.axhline(2000, color=G1, ls=":", lw=1)
        ax.set_ylabel("Latência (ms, log)")
        ax.set_title(f"Encontro {e}", loc="left", fontsize=9)
        ax.set_xlim(-1, JAN[e]["dur"] + 12)
        res[e] = dict(min_ativos=int((agg.nav > 0).sum()), pico_navegadores=int(agg.nav.max()), min_p95max_gt2s=int((agg.p95max > 2000).sum()),
                      min_p95med_gt1s=int((agg.p95med > 1000).sum()), min_falha=int((agg.falhas > 0).sum()), p95med_mediana=float(agg.p95med.median()))
        pk = agg.p95max.idxmax()
        res[e]["pico_p95max"] = float(agg.p95max.max())
        res[e]["min_do_pico"] = int(pk)
        ax.set_zorder(5)
    axs[0].legend(loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.4), fontsize=7.5)
    axs[-1].set_xlabel("Minutos desde o início do encontro (janelas de 1 minuto; linha pontilhada = 2 s)")
    fig.tight_layout()
    savefig(fig, "figD13-serie-1min.png")
    RES["serie1min"] = res


fig_serie_rede()


# ---------- D14: IA, local x nuvem ----------
def fig_ia():
    f = ev[ev.tipo == "feedback_recebido"].copy()
    f["s"] = [c["latencia_ms"] / 1000 for c in f.ctx]
    loc = np.sort(f[f.enc == 1].s.values)
    nuv = np.sort(f[f.enc != 1].s.values)
    sq = ev[ev.tipo == "feedback_solicitado"].copy()
    sq["fila"] = [c["fila"] for c in sq.ctx]
    # pico de pedidos em 5 min por encontro
    pico = {}
    for e, d in sq.groupby("enc"):
        t = np.sort(d.t_min.values)
        pico[int(e)] = int(max(((t >= x) & (t < x + 5)).sum() for x in t))
    f["aluno"] = [vid2al.get(c["versao_id"]) for c in f.ctx]
    # Sorteios por pedido (primeira versão do script), mantidos só para não alterar a sequência de RNG dos demais
    boot_ci(loc); boot_ci(nuv)
    RNG.integers(0, len(loc), (BOOT, len(loc))); RNG.integers(0, len(nuv), (BOOT, len(nuv)))
    # Reamostragem por estudante (pedidos do mesmo estudante não são independentes)
    gl, gn = f[f.enc == 1].aluno.values, f[f.enc != 1].aluno.values
    xl, xn = f[f.enc == 1].s.values, f[f.enc != 1].s.values
    mL, loL, hiL, bsL = boot_cluster(xl, gl)
    mN, loN, hiN, bsN = boot_cluster(xn, gn)
    ratio = bsN / bsL
    diff = bsN - bsL
    RES["ia_cluster"] = dict(estudantes_local=int(len(np.unique(gl))), estudantes_nuvem=int(len(np.unique(gn))),
                             pedidos_por_estudante_local=sorted(Counter(gl).values(), reverse=True),
                             pedidos_por_estudante_nuvem=sorted(Counter(gn).values(), reverse=True),
                             ic_por_pedido_local=boot_ci(loc, n=2000)[1:], ic_por_pedido_nuvem=boot_ci(nuv, n=2000)[1:])
    U, pU = stats.mannwhitneyu(nuv, loc, alternative="two-sided")
    rbc = 1 - 2 * U / (len(nuv) * len(loc))  # correlação bisserial por postos (sinal: nuvem > local -> negativo; invertemos)
    rbc = -rbc
    fig, ax = plt.subplots(1, 3, figsize=(12, 4.3), gridspec_kw={"width_ratios": [1.2, 1, 1.1]})
    for v, c, l in ((loc, VIO, f"Local, encontro 1 (n={len(loc)})"), (nuv, AMB, f"Nuvem, encontros 2 a 4 (n={len(nuv)})")):
        ax[0].step(v, np.arange(1, len(v) + 1) / len(v), where="post", color=c, lw=2, label=l)
        eps = np.sqrt(np.log(2 / 0.05) / (2 * len(v)))
        y = np.arange(1, len(v) + 1) / len(v)
        ax[0].fill_between(v, np.clip(y - eps, 0, 1), np.clip(y + eps, 0, 1), step="post", color=c, alpha=0.12)
    ax[0].set_xlabel("Tempo de geração do feedback (s)")
    ax[0].set_ylabel("ECDF (banda DKW 95%)")
    ax[0].set_title("A. Distribuição do tempo de geração")
    ax[0].legend(loc="lower right", fontsize=7.5)
    ax[1].errorbar([0], [mL], yerr=[[mL - loL], [hiL - mL]], fmt="o", color=VIO, capsize=5, ms=8)
    ax[1].errorbar([1], [mN], yerr=[[mN - loN], [hiN - mN]], fmt="o", color=AMB, capsize=5, ms=8)
    ax[1].text(0.08, mL, f"{fmt(mL)} s\n[{fmt(loL)}; {fmt(hiL)}]", fontsize=8, va="center")
    ax[1].text(1.08, mN, f"{fmt(mN)} s\n[{fmt(loN)}; {fmt(hiN)}]", fontsize=8, va="center")
    ax[1].set_xlim(-0.5, 1.9)
    ax[1].set_xticks([0, 1])
    ax[1].set_xticklabels(["Local", "Nuvem"])
    ax[1].set_ylabel("Mediana (s) e IC95% (bootstrap por estudante)")
    ax[1].set_title("B. Medianas")
    ax[1].grid(axis="x", visible=False)
    ax[2].hist(ratio, bins=40, color=G3, edgecolor="white")
    rm, rlo, rhi = float(np.median(ratio)), float(np.quantile(ratio, .025)), float(np.quantile(ratio, .975))
    ax[2].axvline(rm, color="black", lw=1.4)
    ax[2].axvspan(rlo, rhi, color=ESM, alpha=0.15)
    ax[2].set_xlabel("Razão das medianas nuvem/local (10.000 reamostras de estudantes)")
    ax[2].set_ylabel("Frequência")
    ax[2].set_title(f"C. Razão: {fmt(rm)} [{fmt(rlo)}; {fmt(rhi)}]")
    ax[2].grid(axis="x", visible=False)
    fig.tight_layout()
    savefig(fig, "figD14-ia-local-nuvem.png")
    por_enc = {}
    for e, d in f.groupby("enc"):
        k_est = int(d.aluno.nunique())
        b_ = boot_cluster(d.s.values, d.aluno.values)[:3] if k_est >= 5 else (float(np.median(d.s.values)), None, None)
        por_enc[int(e)] = quart(d.s.values) | dict(boot=b_, estudantes=k_est)
    RES["ia"] = dict(local=quart(loc) | dict(boot=(mL, loL, hiL), p95=float(np.quantile(loc, .95))),
                     nuvem=quart(nuv) | dict(boot=(mN, loN, hiN), p95=float(np.quantile(nuv, .95))),
                     razao=(rm, rlo, rhi), diff=(float(np.median(diff)), float(np.quantile(diff, .025)), float(np.quantile(diff, .975))),
                     U=float(U), pU=float(pU), rbc=float(rbc), todos=quart(np.concatenate([loc, nuv])),
                     por_enc=por_enc, pico5min=pico, fila_max=int(sq.fila.max()), fila_zero=int((sq.fila == 0).sum()), n_pedidos=int(len(sq)),
                     sem_sugestao_prof=None)
    sg = [c["segundos"] for c in ev[ev.tipo == "orientacao_ia_sugerida"].ctx]
    RES["ia"]["sugestao_professora"] = quart(sg)
    RES["ia"]["sobreposicao_cloud_max_local"] = float((nuv < loc.max()).mean())


fig_ia()

# ---------- D15: associações exploratórias (Spearman, bootstrap IC95%) ----------
def fig_spearman():
    pares = [("min_E1", "versoes", "Min. Etapa 1 × nº de versões", "todos"), ("min_E1", "marco_max", "Min. Etapa 1 × marco alcançado", "todos"),
             ("min_total", "marco_max", "Min. de tela totais × marco alcançado", "todos"), ("versoes", "marco_max", "Nº de versões × marco alcançado", "todos"),
             ("fb", "versoes", "Feedbacks da IA × nº de versões", "todos"), ("orient", "marco_max", "Orientações recebidas × marco alcançado", "todos"),
             ("enc_presentes", "marco_max", "Encontros presentes × marco alcançado", "todos"),
             ("min_E2", "cenas", "Min. Etapa 2 × cenas", "narr"), ("min_E2", "execucoes", "Min. Etapa 2 × execuções", "narr"),
             ("cenas", "execucoes", "Cenas × execuções", "narr"), ("escolhas", "execucoes", "Escolhas × execuções", "narr")]
    rows = []
    for x, y, l, sub in pares:
        d = S if sub == "todos" else S[S.cenas.notna()]
        d = d[[x, y]].dropna().astype(float)
        r, p = stats.spearmanr(d[x], d[y])
        n = len(d)
        bs = []
        for _ in range(BOOT):
            i = RNG.integers(0, n, n)
            a, b = d[x].values[i], d[y].values[i]
            if np.ptp(a) == 0 or np.ptp(b) == 0:
                continue
            bs.append(stats.spearmanr(a, b)[0])
        lo, hi = np.quantile(bs, [.025, .975])
        rows.append(dict(par=l, n=n, rho=float(r), lo=float(lo), hi=float(hi), p=float(p), x=x, y=y))
    R = pd.DataFrame(rows)
    fig = plt.figure(figsize=(11, 5.6))
    gs = fig.add_gridspec(2, 2, width_ratios=[1.5, 1])
    ax = fig.add_subplot(gs[:, 0])
    for i, r in R.iterrows():
        c = VIO if i < 7 else ESM
        ax.plot([r.lo, r.hi], [i, i], color=c, lw=2)
        ax.plot(r.rho, i, "o", color=c, ms=7)
        ax.text(1.08, i, f"{fmt(r.rho,2)} [{fmt(r.lo,2)}; {fmt(r.hi,2)}]  n={r.n}", va="center", fontsize=7.5)
    ax.axvline(0, color=G1, lw=1)
    for v in (-.5, -.3, .3, .5):
        ax.axvline(v, color=G3, ls=":", lw=0.8)
    ax.set_yticks(range(len(R)))
    ax.set_yticklabels(R.par, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(-1, 1.0)
    ax.set_xlabel("Correlação de Spearman (ρ) com IC95% por bootstrap de pares; linhas pontilhadas: |ρ| = 0,3 e 0,5")
    ax.set_title("A. Associações exploratórias (11 pares, sem ajuste; descritivas)")
    ax.grid(axis="y", visible=False)
    for k, (x, y, xl, yl) in enumerate([("min_E1", "versoes", "Minutos na Etapa 1", "Versões do texto"), ("min_E2", "cenas", "Minutos na Etapa 2", "Cenas na narrativa")]):
        a2 = fig.add_subplot(gs[k, 1])
        d = S[[x, y]].dropna().astype(float)
        a2.scatter(d[x] + RNG.uniform(-.8, .8, len(d)), d[y], s=22, color=VIO if k == 0 else ESM, edgecolor="white", lw=0.4)
        a2.set_xlabel(xl)
        a2.set_ylabel(yl)
        r_ = stats.spearmanr(d[x], d[y])[0]
        a2.set_title(f"{'B' if k == 0 else 'C'}. ρ = {fmt(r_,2)} (n={len(d)})", fontsize=9)
    fig.tight_layout()
    savefig(fig, "figD15-spearman.png")
    R.to_csv(OUT / "spearman.csv", index=False)
    RES["spearman"] = R.to_dict("records")


fig_spearman()


# ---------- D16: evolução do software entre encontros ----------
def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True).stdout


def fig_evolucao():
    cortes = [("até o encontro 1", "2026-09-18 00:00"), ("encontros 1 a 2", "2026-09-25 00:00"), ("encontros 2 a 3", "2026-10-02 00:00"),
              ("encontros 3 a 4", "2026-11-06 00:00"), ("pós-encontro 4", "2026-11-08 00:00")]
    shas = [git("rev-list", "-1", f"--before={d}", "main").strip() for _, d in cortes]
    tipos = ["feat", "fix", "docs", "test", "refactor", "outros"]
    por_fase = []
    prev = None
    for sh in shas:
        rng = sh if prev is None else f"{prev}..{sh}"
        subj = git("log", "--format=%s", rng).splitlines()
        c = Counter()
        for s_ in subj:
            m_ = re.match(r"^(feat|fix|docs|test|refactor)(\(|:|!)", s_)
            c[m_.group(1) if m_ else "outros"] += 1
        por_fase.append(c)
        prev = sh
    cum = [int(git("rev-list", "--count", sh).strip()) for sh in shas]
    adrs = [len([l for l in git("ls-tree", "-r", "--name-only", sh, "docs/adr").splitlines() if re.search(r"/\d{4}-", l)]) for sh in shas]
    tests = [len(git("grep", "-h", "def test_", sh, "--", "backend/tests").splitlines()) for sh in shas]
    achados = [15, 7, 19, 8]  # C2.x a C5.x: registros de campo 2 a 5 (encontros 1 a 4); contagem por identificador único
    fig, ax = plt.subplots(2, 2, figsize=(11, 7.4))
    fases = [c for c, _ in cortes]
    colt = {"feat": ESM, "fix": AMB, "docs": VIO, "test": "#10b981", "refactor": G2, "outros": G4}
    bottom = np.zeros(5)
    for t in tipos:
        v = np.array([c[t] for c in por_fase])
        ax[0, 0].bar(range(5), v, bottom=bottom, color=colt[t], label=t, width=0.7)
        bottom += v
    for i, t in enumerate(bottom):
        ax[0, 0].text(i, t + 3, int(t), ha="center", fontsize=8)
    ax[0, 0].set_xticks(range(5))
    ax[0, 0].set_xticklabels([f.replace(" ", "\n", 1) for f in fases], fontsize=7.5)
    ax[0, 0].set_ylabel("Commits na fase")
    ax[0, 0].set_title("A. Commits por fase e tipo (convenção)")
    ax[0, 0].legend(ncol=3, fontsize=7)
    ax[0, 0].grid(axis="x", visible=False)
    for a_, v, ttl, col in ((ax[0, 1], adrs, "B. ADRs acumuladas", AMB), (ax[1, 0], tests, "C. Testes automatizados (backend)", ESM)):
        a_.plot(range(5), v, "o-", color=col, lw=2)
        for i, y in enumerate(v):
            a_.text(i, y + (max(v) - min(v)) * 0.04 + 0.2, str(y), ha="center", fontsize=8)
        a_.set_xticks(range(5))
        a_.set_xticklabels(["Antes\nenc. 1", "Antes\nenc. 2", "Antes\nenc. 3", "Antes\nenc. 4", "Fecha-\nmento"], fontsize=7.5)
        a_.set_title(ttl)
        a_.set_ylim(min(v) * 0.9, max(v) * 1.08)
    ax[1, 1].bar(range(4), achados, color=VIO, width=0.6)
    for i, v in enumerate(achados):
        ax[1, 1].text(i, v + 0.3, str(v), ha="center", fontsize=8)
    ax[1, 1].set_xticks(range(4))
    ax[1, 1].set_xticklabels([f"Encontro {i}" for i in range(1, 5)])
    ax[1, 1].set_ylabel("Achados registrados")
    ax[1, 1].set_title("D. Achados de campo por encontro")
    ax[1, 1].grid(axis="x", visible=False)
    fig.tight_layout()
    savefig(fig, "figD16-evolucao-software.png")
    RES["evolucao"] = dict(fases=fases, commits_fase=[dict(c) for c in por_fase], commits_cum=cum, adrs=adrs, testes=tests, achados=achados)


fig_evolucao()


# ==================================================================================
# CONCILIAÇÕES, AUDITORIA DO BOOTSTRAP E VARREDURA DOS CAMPOS DE CONTEXTO (v2 revisada)
# ==================================================================================
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def enc_dia(ts):
    """Encontro (1 a 4) do carimbo; 0 = entre aulas."""
    return ts.dt.strftime("%Y-%m-%d").map(DIAS).fillna(0).astype(int)


def balde(t):
    """Posição temporal de um carimbo: durante um encontro ou entre dois encontros."""
    d = t.strftime("%Y-%m-%d")
    if d in DIAS:
        return f"durante o encontro {DIAS[d]}"
    ds = sorted(DIAS)
    for i in range(len(ds) - 1):
        if ds[i] < d < ds[i + 1]:
            return f"entre os encontros {i + 1} e {i + 2}"
    return "antes do encontro 1" if d < ds[0] else "depois do encontro 4"


def auditoria_bootstrap():
    """Reexecuta o IC95% da mediana com outra semente e compara com Q1, Q3 e com o IC exato (por postos)."""
    rng = np.random.default_rng(99)
    vetores = []
    for c, l in [("versoes", "Versões do texto"), ("palavras_ult", "Palavras da última versão"), ("fb", "Feedbacks da IA"),
                 ("orient", "Orientações da professora"), ("cenas", "Cenas por narrativa"), ("personagens", "Personagens por narrativa"),
                 ("falas", "Falas por narrativa"), ("escolhas", "Escolhas por narrativa"), ("execucoes", "Execuções por narrativa")]:
        vetores.append((l, S[c].dropna().values.astype(float)))
    for e in ETAPAS:
        vetores.append((f"Minutos em tela: {e}", tempo[e][tempo[e] > 0].values.astype(float)))
    rows = []
    for l, x in vetores:
        x = np.sort(x)
        n = len(x)
        med, lo, hi = boot_ci(x, n=BOOT)  # mesma função da tabela (continua o fluxo RNG)
        idx = rng.integers(0, n, size=(BOOT, n))
        m2 = np.median(x[idx], axis=1)
        lo2, hi2 = float(np.quantile(m2, .025)), float(np.quantile(m2, .975))
        q1, q3 = float(np.quantile(x, .25)), float(np.quantile(x, .75))
        # IC exato, livre de distribuição, por postos: P(Bin(n,1/2) <= l-1) <= 2,5%
        k = 0
        while k + 1 <= n and stats.binom.cdf(k, n, .5) <= .025:
            k += 1
        lin, uin = (k, n + 1 - k) if k >= 1 else (None, None)
        cob = 1 - 2 * stats.binom.cdf(k - 1, n, .5) if k >= 1 else float("nan")
        rows.append(dict(medida=l, n=n, valores_distintos=int(len(np.unique(x))), mediana=med, ic_boot_tabela_lo=lo, ic_boot_tabela_hi=hi,
                         ic_boot_reexec_lo=lo2, ic_boot_reexec_hi=hi2, q1=q1, q3=q3,
                         ic_exato_lo=float(x[lin - 1]) if lin else None, ic_exato_hi=float(x[uin - 1]) if uin else None,
                         postos_exatos=f"{lin}-{uin}" if lin else "–", cobertura_exata=cob,
                         ic_igual_q1q3=bool(np.isclose(lo, q1) and np.isclose(hi, q3)),
                         ic_igual_q1q3_reexec=bool(np.isclose(lo2, q1) and np.isclose(hi2, q3)),
                         ic_igual_exato=bool(lin and np.isclose(lo, x[lin - 1]) and np.isclose(hi, x[uin - 1]))))
    A = pd.DataFrame(rows)
    A.to_csv(OUT / "auditoria-bootstrap.csv", index=False)
    RES["auditoria_bootstrap"] = A.to_dict("records")
    return A


def varredura_contexto():
    """Chaves JSON do contexto de cada tipo de evento, tipos de valor e comprimentos (sem emitir valores)."""
    info = {}

    def visita(tipo, prefixo, v):
        if isinstance(v, dict):
            for k_, x in v.items():
                visita(tipo, f"{prefixo}.{k_}" if prefixo else k_, x)
        elif isinstance(v, list):
            e = info.setdefault((tipo, prefixo + "[]"), dict(n=0, tipos=Counter(), maxlen=0, distintos=set(), uuid=0, strs=0, com_espaco=0, maxitens=0))
            e["n"] += 1
            e["tipos"]["lista"] += 1
            e["maxitens"] = max(e["maxitens"], len(v))
            for x in v:
                if isinstance(x, (dict, list)):
                    visita(tipo, prefixo + "[]", x)
                elif isinstance(x, str):
                    ee = info.setdefault((tipo, prefixo + "[]#texto"), dict(n=0, tipos=Counter(), maxlen=0, distintos=set(), uuid=0, strs=0, com_espaco=0, maxitens=0))
                    ee["n"] += 1; ee["tipos"]["str"] += 1; ee["strs"] += 1
                    ee["maxlen"] = max(ee["maxlen"], len(x)); ee["distintos"].add(x)
                    ee["com_espaco"] += int(" " in x)
        else:
            e = info.setdefault((tipo, prefixo), dict(n=0, tipos=Counter(), maxlen=0, distintos=set(), uuid=0, strs=0, com_espaco=0, maxitens=0))
            e["n"] += 1
            e["tipos"][type(v).__name__] += 1
            if isinstance(v, str):
                e["strs"] += 1
                e["maxlen"] = max(e["maxlen"], len(v))
                e["distintos"].add(v)
                e["uuid"] += int(bool(UUID_RE.match(v)))
                e["com_espaco"] += int(" " in v)

    for t_, c_ in zip(ev.tipo, ev.ctx):
        visita(t_, "", c_)
    PROIB = {"texto", "conteudo", "content", "fala", "falas", "proposta", "observacoes"}
    rows = []
    for (t_, k_), e in sorted(info.items()):
        eh_id = k_.endswith("_id")
        rows.append(dict(tipo=t_, chave=k_, n=e["n"], tipos="/".join(f"{a}:{b}" for a, b in sorted(e["tipos"].items())),
                         comprimento_max=e["maxlen"] if e["strs"] else "", itens_max=e["maxitens"] or "",
                         natureza=("identificador" if eh_id else "valor fechado" if e["strs"] else "número/booleano/lista"),
                         uuid_em_todos=(e["uuid"] == e["strs"] if eh_id and e["strs"] else ""),
                         valores_distintos=(len(e["distintos"]) if e["strs"] and not eh_id else ""),
                         strings_com_espaco=e["com_espaco"] if e["strs"] else ""))
    V = pd.DataFrame(rows)
    V.to_csv(OUT / "varredura-contexto.csv", index=False)
    nid = V[V.natureza == "valor fechado"]
    ids = V[V.natureza == "identificador"]
    LIM = 36  # comprimento de um UUID; qualquer string maior ou com espaço seria suspeita
    res = dict(eventos=int(len(ev)), tipos=int(ev.tipo.nunique()), pares_tipo_chave=int(len(V)),
               chaves_distintas=int(V.chave.nunique()),
               chaves_proibidas_presentes=sorted({k for k in V.chave if k.split(".")[-1].replace("[]", "").replace("#texto", "") in PROIB}),
               ids_total=int(len(ids)), ids_todos_uuid=bool((ids.uuid_em_todos == True).all()),  # noqa: E712
               maior_string_de_id=int(ids.comprimento_max.max()),
               valores_fechados_chaves=int(len(nid)), valores_fechados_comprimento_max=int(nid.comprimento_max.max()),
               valores_fechados_cardinalidade_max=int(nid.valores_distintos.max()),
               strings_com_espaco_total=int(pd.to_numeric(V.strings_com_espaco, errors="coerce").fillna(0).sum()),
               strings_acima_do_limiar=int(((pd.to_numeric(V.comprimento_max, errors="coerce").fillna(0)) > LIM).sum()),
               limiar=LIM, tipos_sem_contexto=sorted(t for t in ev.tipo.unique() if not any(k[0] == t for k in info)))
    # chave de maior comprimento entre os valores fechados (nome da chave apenas)
    j = nid.comprimento_max.astype(int).idxmax()
    res["valores_fechados_chave_maior"] = f"{nid.loc[j, 'tipo']}.{nid.loc[j, 'chave']}"
    RES["varredura"] = res
    return V


def conciliacoes():
    C = {}
    # (a) avaliações
    r = refl.copy()
    r["enc_criada"] = enc_dia(r.criada_em)
    r["enc_concl"] = enc_dia(r.concluida_em.dropna()).reindex(r.index)
    C["avaliacoes"] = dict(registros=int(len(r)), concluidas=int(r.concluida_em.notna().sum()), nao_concluidas=int(r.concluida_em.isna().sum()),
                           estudantes_com_registro=int(r.pa_id.nunique()),
                           criadas_por_encontro={int(k): int(v) for k, v in r.enc_criada.value_counts().sort_index().items()},
                           concluidas_por_encontro={int(k): int(v) for k, v in r.enc_concl.dropna().value_counts().sort_index().items()},
                           evento_reflexao_concluida=int((ev.tipo == "reflexao_concluida").sum()))
    # (b) cenas
    cc_ = ev[ev.tipo == "cena_criada"]
    rm_ = ev[ev.tipo == "cena_removida"]
    por_n = pd.DataFrame({"criadas": cc_.ctx.map(lambda c: c["narrativa_id"]).value_counts(),
                          "removidas_evento": rm_.ctx.map(lambda c: c["narrativa_id"]).value_counts(),
                          "existentes": cenas.groupby("nid").size()}).fillna(0).astype(int)
    por_n["saldo"] = por_n.criadas - por_n.removidas_evento - por_n.existentes
    disc = por_n[por_n.saldo != 0]
    C["cenas"] = dict(na_tabela=int(len(cenas)), criadas_evento=int(len(cc_)), removidas_evento=int(len(rm_)), saldo_eventos=int(len(cc_) - len(rm_)),
                      diferenca=int(len(cenas) - (len(cc_) - len(rm_))), narrativas_com_diferenca=int(len(disc)),
                      diferenca_por_narrativa=[int(-v) for v in disc.saldo.values],
                      todas_as_cenas_da_tabela_tem_evento_de_criacao=bool(cenas.id.isin({c["cena_id"] for c in cc_.ctx}).all()),
                      criadas_por_encontro={int(k): int(v) for k, v in enc_dia(cc_.ts).value_counts().sort_index().items()},
                      removidas_por_encontro={int(k): int(v) for k, v in enc_dia(rm_.ts).value_counts().sort_index().items()})
    # (c) divisão em cenas
    d = ev[ev.tipo.isin(["divisao_aberta", "divisao_concluida", "divisao_cancelada"])].copy()
    d["nid"] = d.ctx.map(lambda c: c["narrativa_id"])
    d = d.sort_values("ts")
    seq_apos = Counter(); seq_antes_c = Counter(); seq_antes_k = Counter()
    for _, g in d.groupby("nid"):
        t_ = g.tipo.tolist()
        for i, x in enumerate(t_):
            if x == "divisao_aberta":
                seq_apos[t_[i + 1] if i + 1 < len(t_) else "nada"] += 1
            if x == "divisao_concluida":
                seq_antes_c[t_[i - 1] if i > 0 else "inicio"] += 1
            if x == "divisao_cancelada":
                seq_antes_k[t_[i - 1] if i > 0 else "inicio"] += 1
    # cancelamentos espúrios: gravados a menos de 3 s de uma conclusão seguinte ou de outro cancelamento anterior
    espurio = Counter(); verdadeiro = Counter()
    for _, g in d.groupby("nid"):
        t_, ts_, en_ = g.tipo.tolist(), g.ts.tolist(), g.enc.tolist()
        for i, x in enumerate(t_):
            if x != "divisao_cancelada":
                continue
            seg_depois = (ts_[i + 1] - ts_[i]).total_seconds() if i + 1 < len(t_) and t_[i + 1] in ("divisao_concluida", "divisao_cancelada") else None
            seg_antes = (ts_[i] - ts_[i - 1]).total_seconds() if i > 0 and t_[i - 1] == "divisao_cancelada" else None
            if (seg_depois is not None and seg_depois < 3) or (seg_antes is not None and seg_antes < 3):
                espurio[int(en_[i])] += 1
            else:
                verdadeiro[int(en_[i])] += 1
    C["divisao_cancelamentos"] = dict(espurios=dict(espurio), verdadeiros=dict(verdadeiro), espurios_total=int(sum(espurio.values())),
                                      verdadeiros_total=int(sum(verdadeiro.values())),
                                      reaberta_sem_desfecho=int(seq_apos.get("divisao_aberta", 0)),
                                      aberturas_por_encontro={int(k): int(v) for k, v in d[d.tipo == "divisao_aberta"].enc.value_counts().items()},
                                      conclusoes_por_encontro={int(k): int(v) for k, v in d[d.tipo == "divisao_concluida"].enc.value_counts().items()})
    C["divisao"] = dict(abertas=int((d.tipo == "divisao_aberta").sum()), concluidas=int((d.tipo == "divisao_concluida").sum()),
                        canceladas=int((d.tipo == "divisao_cancelada").sum()), apos_aberta=dict(seq_apos),
                        concluida_precedida_por=dict(seq_antes_c), cancelada_precedida_por=dict(seq_antes_k),
                        por_encontro={int(k): v for k, v in d.assign(e=enc_dia(d.ts)).groupby("e").tipo.value_counts().unstack(fill_value=0).to_dict("index").items()})
    # (d) telemetria: universos
    tt = ev[(ev.tipo == "telemetria_cliente")]
    C["telemetria"] = dict(total_banco=int(len(tt)), na_janela_mais_10min=int(len(tel_win)), fora_da_janela=int(len(tel_all) - len(tel_win)),
                           por_encontro_banco={int(k): int(v) for k, v in tt.enc.value_counts().sort_index().items()},
                           por_encontro_janela={int(k): int(v) for k, v in tel_win.enc.value_counts().sort_index().items()},
                           com_chamadas_janela={int(k): int((pd.to_numeric(g.n) > 0).sum()) for k, g in tel_win.groupby("enc")},
                           chamadas_banco={int(k): int(pd.to_numeric(g.n).sum()) for k, g in tt.assign(n=tt.ctx.map(lambda c: c["n"])).groupby("enc")},
                           chamadas_janela={int(k): int(pd.to_numeric(g.n).sum()) for k, g in tel_win.groupby("enc")},
                           offline=int((~tel_all.ctx.map(lambda c: c["online"])).sum()))
    # cobertura da telemetria: ações do estudante com relatório a menos de 120 s; lacunas dentro de cada navegador-encontro
    acoes = ev[(ev.tipo != "telemetria_cliente") & ev.aluno.notna() & ev.enc.isin([2, 3, 4])]
    relat = tel_all.groupby(["aluno", "enc"]).ts.apply(lambda s: np.sort(s.values.astype("datetime64[s]").astype(np.int64))).to_dict()
    com, sem = 0, 0
    for r_ in acoes.itertuples():
        arr = relat.get((r_.aluno, r_.enc))
        t0 = np.datetime64(r_.ts, "s").astype(np.int64)
        ok = arr is not None and len(arr) and np.min(np.abs(arr - t0)) <= 120
        com += int(ok); sem += int(not ok)
    span, nrep = 0, 0
    for (a_, e_), arr in relat.items():
        span += (arr.max() - arr.min()) / 60 + 1
        nrep += len(arr)
    C["telemetria"]["cobertura"] = dict(acoes_avaliadas=int(com + sem), com_relatorio_a_120s=int(com), sem_relatorio=int(sem),
                                        navegadores_encontro=int(len(relat)), relatorios=int(nrep), minutos_entre_primeiro_e_ultimo=int(round(span)),
                                        razao_relatorios_minutos=float(nrep / span))
    # (e) entradas
    en = ev[ev.tipo == "aluno_entrou"]
    C["entradas"] = {int(e): dict(eventos=int(len(g)), distintos=int(g.aluno.nunique())) for e, g in en.groupby("enc")}
    # (f) pedidos de ajuda
    C["ajuda"] = {t: {int(k): int(v) for k, v in enc_dia(ev[ev.tipo == t].ts).value_counts().sort_index().items()} for t in ("ajuda_solicitada", "ajuda_atendida")}
    # (g) intervalos entre marcos: universo único (mesmo encontro, até 4 h) e sem restrição
    def par(a, b):
        x = (M[b] - M[a]).dt.total_seconds() / 60
        x = x[x.notna()]
        r1 = x[(x >= 0) & (x <= 240)]
        return dict(com_ambos=int(len(x)), n_ate_4h=int(len(r1)), mediana_ate_4h=float(r1.median()) if len(r1) else None,
                    q1=float(r1.quantile(.25)) if len(r1) else None, q3=float(r1.quantile(.75)) if len(r1) else None,
                    n_mais_de_4h=int((x > 240).sum()), n_negativos=int((x < 0).sum()),
                    mediana_sem_corte=float(x[x >= 0].median()) if len(x[x >= 0]) else None, n_sem_corte=int((x >= 0).sum()))
    C["marcos"] = {"narr_cena1": par("narr", "cena1"), "cena1_exec": par("cena1", "exec"), "entrou_v1": par("entrou", "v1")}
    # (h) cronologia da Etapa 2
    C["cronologia"] = dict(
        narrativas_criadas_por_encontro={int(k): int(v) for k, v in enc_dia(nar.criada_em).value_counts().sort_index().items()},
        estudantes_com_cena_primeira_vez_por_encontro={int(k): int(v) for k, v in enc_dia(M.cena1.dropna()).value_counts().sort_index().items()} if False else {},
        execucoes_por_encontro={int(k): int(v) for k, v in enc_dia(exe.rodada_em).value_counts().sort_index().items()},
        primeira_liberacao=dict(Counter(balde(t) for t in M.liberado.dropna())),
        primeiro_envio=dict(Counter(balde(t) for t in M.envio.dropna())),
        primeira_cena=dict(Counter(balde(t) for t in M.cena1.dropna())),
        primeira_execucao=dict(Counter(balde(t) for t in M.exec.dropna())),
        status_fim={e: RES["status_fim_encontro"][e] for e in (1, 2, 3, 4)})
    # (i) alerta de inatividade
    al = ev[ev.tipo == "inatividade_alertada"].copy()
    al["resp"] = al.ctx.map(lambda c: c["resposta"]); al["vazio"] = al.ctx.map(lambda c: c["texto_vazio"])
    en2 = en[["aluno", "ts", "enc"]]
    deltas, sem_versao = [], 0
    vs = ev[ev.tipo == "versao_salva"].copy()
    vs["aluno"] = vs.ctx.map(lambda c: vid2al.get(c["versao_id"]))
    for r_ in al.itertuples():
        e_ = en2[(en2.aluno == r_.aluno) & (en2.enc == r_.enc) & (en2.ts <= r_.ts)]
        deltas.append((r_.ts - e_.ts.max()).total_seconds() / 60 if len(e_) else np.nan)
        sem_versao += int(vs[(vs.aluno == r_.aluno) & (vs.enc == r_.enc) & (vs.ts <= r_.ts)].empty)
    dl = np.array(deltas, float)
    C["inatividade"] = dict(total=int(len(al)), por_encontro={int(k): int(v) for k, v in al.enc.value_counts().sort_index().items()},
                            respostas={int(e): dict(Counter(g.resp)) for e, g in al.groupby("enc")}, respostas_total=dict(Counter(al.resp)),
                            tela_em_branco={int(e): int(g.vazio.sum()) for e, g in al.groupby("enc")}, estudantes={int(e): int(g.aluno.nunique()) for e, g in al.groupby("enc")},
                            minutos_do_limiar=sorted({int(c["minutos"]) for c in al.ctx}),
                            desde_ultima_entrada=dict(n=int(np.isfinite(dl).sum()), mediana=float(np.nanmedian(dl)), q1=float(np.nanquantile(dl, .25)), q3=float(np.nanquantile(dl, .75)),
                                                      ate_6min=int((dl <= 6).sum()), ate_10min=int((dl <= 10).sum()), mais_de_20min=int((dl > 20).sum())),
                            sem_versao_salva_antes=int(sem_versao))
    # (j) alerta de travamento e tempo de Etapa 2
    tr = ev[ev.tipo == "travado_alerta"].copy(); tr["passo"] = tr.ctx.map(lambda c: c["passo"])
    t2 = {int(e): int(tempo_enc.loc[e, "Etapa 2 Remidiação"]) for e in tempo_enc.index if e in (3, 4)}
    C["travado"] = {int(e): dict(alertas=int(len(g)), estudantes=int(g.aluno.nunique()), passos=dict(Counter(g.passo)),
                                 min_estudante_etapa2=t2.get(int(e)), por_100_min=float(100 * len(g) / t2[int(e)]) if t2.get(int(e)) else None)
                    for e, g in tr.groupby("enc")}
    # (k) guarda de conteúdo e estado dos feedbacks
    C["guarda"] = dict(feedbacks_linhas=int(len(fbk)), por_status=dict(Counter(fbk.status)), versoes_distintas=int(fbk.vid.nunique()),
                       solicitados=int((ev.tipo == "feedback_solicitado").sum()), recebidos=int((ev.tipo == "feedback_recebido").sum()),
                       eventos_reprovado_guarda=int((ev.tipo == "feedback_reprovado_guarda").sum()),
                       eventos_com_guarda_ou_reprov_no_nome=int(ev.tipo.str.contains("guarda|reprov|erro").sum()),
                       modelos=dict(Counter(fbk.modelo)), eval_count_disponivel=False)
    # (l) downloads e execuções
    dd = ev[ev.tipo == "download"]
    C["downloads"] = dict(total=int(len(dd)), por_tipo=dict(Counter(dd.ctx.map(lambda c: c["o_que"]))), por_encontro={int(k): int(v) for k, v in dd.enc.value_counts().items()})
    RES["conc"] = C
    return C


auditoria_bootstrap()
varredura_contexto()
conciliacoes()


# ==================================================================================
# TABELAS (Markdown) e resultados
# ==================================================================================
def md(df, idx=False):
    d = df.reset_index() if idx else df
    h = "| " + " | ".join(str(c) for c in d.columns) + " |\n|" + "|".join(["---"] * len(d.columns)) + "|\n"
    return h + "\n".join("| " + " | ".join(str(v) for v in r) + " |" for r in d.itertuples(index=False)) + "\n"


def br(x, nd=1):
    return fmt(x, nd)


def tab_all():
    T = {}
    # D1 fontes
    T["D1"] = md(pd.DataFrame([
        ["F1 Banco do piloto (somente leitura)", f"{NT['eventos']} eventos de {ev.tipo.nunique()} tipos (inclui {N_TEL} relatórios de telemetria do navegador); {NT['versoes_texto']} versões; {NT['feedbacks_ia']} feedbacks; {NT['orientacoes_professora']} orientações; {NT['narrativas']} narrativas; {NT['cenas']} cenas; {NT['escolhas']} escolhas; {NT['execucoes']} execuções; {NT['reflexoes']} avaliações; {NT['alunos']} estudantes", "4 encontros em 7 semanas (telemetria: encontros 2 a 4)"],
        ["F2 Dossiê de telemetria e CSV derivados", "totais usados para conferência cruzada (6.606; 2.928; 101; 41)", "mesmo período"],
        ["F3 Avaliação objetiva do feedback", "13 respostas de 3 participantes autorizados (verificações de script)", "encontros 1 a 3"],
        ["F4 Repositório de código", "commits, ADRs e testes automatizados por fase (git)", "antes do encontro 1 até o fechamento"],
        ["F5 Registros de campo", "contagem de achados com identificador por encontro", "encontros 1 a 4"]],
        columns=["Fonte", "Registros", "Período"]))
    # D2 descritiva
    rows = []
    for c, l in [("versoes", "Versões do texto por estudante"), ("palavras_ult", "Palavras da última versão"), ("fb", "Feedbacks da IA por estudante"),
                 ("orient", "Orientações da professora por estudante"), ("cenas", "Cenas por narrativa"), ("personagens", "Personagens por narrativa"),
                 ("falas", "Falas por narrativa"), ("escolhas", "Escolhas por narrativa"), ("execucoes", "Execuções do player por narrativa")]:
        v = S[c].dropna().values
        q_ = quart(v)
        mb = boot_ci(v)
        rows.append([l, q_["n"], br(q_["med"]), f"{br(mb[1])}–{br(mb[2])}", f"{br(q_['q1'])}–{br(q_['q3'])}", br(q_["iqr"]), f"{br(q_['min'],0)}–{br(q_['max'],0)}"])
    T["D2"] = md(pd.DataFrame(rows, columns=["Medida", "n", "Mediana", "IC95% da mediana (bootstrap)", "Q1–Q3", "IIQ", "Mín–máx"]))
    # D3 tempo
    rows = []
    for e in ETAPAS:
        q_ = RES["tempo_etapa"][e]
        b = RES["tempo_boot"][e]
        rows.append([e, q_["n"], br(q_["med"], 0), f"{br(b['lo'],0)}–{br(b['hi'],0)}", f"{br(q_['q1'],0)}–{br(q_['q3'],0)}", f"{br(q_['min'],0)}–{br(q_['max'],0)}", int(q_["soma"])])
    T["D3"] = md(pd.DataFrame(rows, columns=["Etapa", "n (passaram pela etapa)", "Mediana (min)", "IC95% da mediana", "Q1–Q3", "Mín–máx", "Total (min-estudante)"]))
    # D4 funil
    rows = []
    ks = list(RES["funil"].items())
    for i, (l, k) in enumerate(ks):
        w = wilson(k, 22)
        rows.append([l, f"{k}/22", f"{w[0]*100:.0f}", f"{w[1]*100:.0f}–{w[2]*100:.0f}", "–" if (i == 0 or l in ("Feedback da IA", "Enviou à professora")) else f"{k/ks[i-1][1]*100:.0f}"])
    T["D4"] = md(pd.DataFrame(rows, columns=["Marco", "Estudantes", "%", "IC95% de Wilson (%)", "% do marco anterior"]))
    # D5 retenção e KM
    rows = []
    for j in range(3):
        k = f"{j+1}->{j+2}"
        r = RES["presenca_condicional"][k]
        w = wilson(r["ambos"], r["de"])
        rows.append([f"Encontro {j+1} para {j+2}", f"{r['ambos']}/{r['de']}", f"{w[0]*100:.0f}", f"{w[1]*100:.0f}–{w[2]*100:.0f}"])
    T["D5a"] = md(pd.DataFrame(rows, columns=["Transição", "Presentes em ambos / presentes no primeiro", "%", "IC95% de Wilson (%)"]))
    km_ = RES["km"]
    rows = []
    for k, l in (("cena1", "Criar a 1ª cena"), ("exec", "Executar a história")):
        r = km_[k]
        f_ = lambda t: f"{t[0]*100:.0f} [{t[1]*100:.0f}; {t[2]*100:.0f}]"
        rows.append([l, f"{r['eventos']} de 22", r["censurados"], f_(r["em60"]), f_(r["em90"]), f"{f_(r['em_corte'])} aos {r['tcut']:.0f} min", "não atingida" if r["mediana_min"] is None else f"{r['mediana_min']:.0f}"])
    T["D5b"] = md(pd.DataFrame(rows, columns=["Marco", "Eventos", "Censurados", "% aos 60 min [IC95%]", "% aos 90 min [IC95%]", "% no ponto de corte [IC95%]", "Mediana KM (min)"]))
    # D6 transições
    T["D6"] = md(pd.DataFrame([[a, b, n, f"{p*100:.0f}"] for a, b, n, p in RES["transicoes_top"]], columns=["Origem", "Destino", "Transições", "% da linha de origem"]))
    # D7 rede
    rows = []
    for e in (2, 3, 4):
        r = RES["rede"][e]
        rows.append([f"Encontro {e}", r["relatorios"], r["chamadas"], r["falhas"], br(r["p50_p50"], 0), br(r["p95_p50"], 0), br(r["p95_p90"], 0), br(r["p95_p95"], 0), br(r["p95_p99"], 0), r["acima2s"], r["acima5s"]])
    T["D7"] = md(pd.DataFrame(rows, columns=["Encontro", "Relatórios com chamadas", "Chamadas", "Falhas", "p50 mediano (ms)", "p95: mediana", "p95: p90", "p95: p95", "p95: p99", "Rel. p95 > 2 s", "Rel. p95 > 5 s"]))
    # D8 IA (IC95% por bootstrap de ESTUDANTES; sem IC quando há menos de 5 estudantes)
    ia = RES["ia"]
    rows = []
    icx = lambda b: "não estimado (< 5 estudantes)" if b[1] is None else f"{br(b[1])}–{br(b[2])}"
    est = {"Local (encontro 1)": RES["ia_cluster"]["estudantes_local"], "Nuvem (encontros 2 a 4)": RES["ia_cluster"]["estudantes_nuvem"], "Todos": 16}
    for l, d in (("Local (encontro 1)", ia["local"]), ("Nuvem (encontros 2 a 4)", ia["nuvem"]), ("Todos", None)):
        if d is None:
            q_ = ia["todos"]
            rows.append([l, q_["n"], est[l], br(q_["med"]), "–", f"{br(q_['q1'])}–{br(q_['q3'])}", f"{br(q_['min'])}–{br(q_['max'])}"])
        else:
            rows.append([l, d["n"], est[l], br(d["med"]), icx(d["boot"]), f"{br(d['q1'])}–{br(d['q3'])}", f"{br(d['min'])}–{br(d['max'])}"])
    for e, d in sorted(ia["por_enc"].items()):
        rows.append([f"Encontro {e}", d["n"], d["estudantes"], br(d["med"]), icx(d["boot"]), f"{br(d['q1'])}–{br(d['q3'])}", f"{br(d['min'])}–{br(d['max'])}"])
    T["D8"] = md(pd.DataFrame(rows, columns=["Ambiente", "Pedidos", "Estudantes", "Mediana (s)", "IC95% da mediana (bootstrap por estudante)", "Q1–Q3", "Mín–máx"]))
    # D9 Spearman
    T["D9"] = md(pd.DataFrame([[r["par"], r["n"], br(r["rho"], 2), f"{br(r['lo'],2)}; {br(r['hi'],2)}", ("pequeno" if abs(r["rho"]) < .3 else "moderado" if abs(r["rho"]) < .5 else "grande"), br(r["p"], 3)] for r in RES["spearman"]],
                           columns=["Par de variáveis", "n", "ρ de Spearman", "IC95% (bootstrap)", "Magnitude (Cohen)", "p (descritivo, sem ajuste)"]))
    # D10 evolução
    ev_ = RES["evolucao"]
    rows = []
    for i, f_ in enumerate(ev_["fases"]):
        c = ev_["commits_fase"][i]
        rows.append([f_, sum(c.values()), c.get("feat", 0), c.get("fix", 0), c.get("docs", 0), ev_["commits_cum"][i], ev_["adrs"][i], ev_["testes"][i], (ev_["achados"][i - 1] if i > 0 else "–")])
    T["D10"] = md(pd.DataFrame(rows, columns=["Fase", "Commits na fase", "feat", "fix", "docs", "Commits acumulados", "ADRs acumuladas", "Testes acumulados", "Achados do encontro anterior (a tratar na fase)"]))
    (OUT / "tabelas.md").write_text("\n\n".join(f"### {k}\n{v}" for k, v in T.items()), encoding="utf-8")


tab_all()
S.drop(columns=["E"]).assign(E=S["E"]).to_csv(OUT / "estudantes_metricas.csv", index=False)


def apendice():
    L = []
    # A13 descritiva completa
    rows = []
    for c, l in [("versoes", "Versões do texto"), ("palavras_ult", "Palavras da última versão"), ("fb", "Feedbacks da IA"), ("orient", "Orientações da professora"),
                 ("cenas", "Cenas"), ("personagens", "Personagens"), ("falas", "Falas"), ("escolhas", "Escolhas"), ("execucoes", "Execuções")]:
        q_ = RES["metricas"][c]
        rows.append([l, q_["n"], br(q_["mean"], 2), br(q_["sd"], 2), br(q_["med"]), br(q_["q1"], 2), br(q_["q3"], 2), br(q_["min"], 0), br(q_["max"], 0)])
    for e in ETAPAS + ["Outras"]:
        q_ = RES["tempo_etapa"][e]
        rows.append([f"Minutos em tela: {e}", q_["n"], br(q_["mean"], 2), br(q_["sd"], 2), br(q_["med"]), br(q_["q1"], 2), br(q_["q3"], 2), br(q_["min"], 0), br(q_["max"], 0)])
    L.append("## A13. Estatística descritiva completa (usada nas Figuras D3 e D4)\n\nUnidade: estudante (22) para a Etapa 1 e narrativa (17) para a Etapa 2; minutos em tela apenas entre os estudantes que passaram pela etapa. DP: desvio-padrão amostral.\n\n" + md(pd.DataFrame(rows, columns=["Medida", "n", "Média", "DP", "Mediana", "Q1", "Q3", "Mín", "Máx"])))
    # A14 matriz de transição
    mt = pd.read_csv(OUT / "matriz_transicao_contagens.csv", index_col=0)
    L.append("## A14. Matriz de transição entre ações (contagens; linha = ação de origem, coluna = ação seguinte; repetições da mesma ação excluídas)\n\nSequências por estudante dentro de cada encontro, ordenadas por horário, sobre os eventos atribuíveis a estudante (n total de pares consecutivos = " + str(RES["n_transicoes"]) + ", dos quais as repetições da mesma ação aparecem na persistência).\n\n" + md(mt.reset_index().rename(columns={"index": "Origem \\ destino"})))
    # A15 fluxos
    rows = []
    for e in (1, 2, 3, 4):
        rows.append([f"Fim do encontro {e}"] + [RES["status_fim_encontro"][e][k] for k in STAT_ORD])
    t1 = md(pd.DataFrame(rows, columns=["Momento"] + STAT_ORD))
    fl = []
    for k, d in RES["fluxos"].items():
        for kk, n in sorted(d.items(), key=lambda x: -x[1]):
            a, b = kk.split(" > ")
            fl.append([f"{k.split('->')[0]} para {k.split('->')[1]}", a, b, n])
    L.append("## A15. Situação derivada ao fim de cada encontro e fluxos entre encontros (Figuras D6 e D7)\n\nSituação reconstruída com os carimbos de tempo (função de status do sistema), com corte no fim do dia do encontro. Difere em até 1 estudante do quadro A2 pela inclusão da categoria \"ainda não entrou\" e pelo tratamento de devoluções.\n\n" + t1 + "\n" + md(pd.DataFrame(fl, columns=["Encontros", "De", "Para", "Estudantes"])))
    # A16 rede
    rows = []
    for e in (2, 3, 4):
        r = RES["rede"][e]
        sr = RES["serie1min"][e]
        rows.append([f"Encontro {e}", r["relatorios"], br(r["p50_p50"], 0), br(r["p50_p90"], 0), br(r["p95_p50"], 0), br(r["p95_p90"], 0), br(r["p95_p95"], 0), br(r["p95_max"], 0), sr["min_ativos"], sr["pico_navegadores"], sr["min_p95max_gt2s"], sr["min_p95med_gt1s"], sr["min_falha"]])
    kw = RES["rede"]["kw"]
    fa = RES["rede"]["falha"]
    L.append("## A16. Rede do navegador: percentis por relatório e janelas de 1 minuto (Figuras D12 e D13)\n\nPercentis calculados sobre os relatórios com ao menos uma chamada, dentro da janela do encontro mais 10 min. Colunas de minutos: minutos com relatório; pico de navegadores simultâneos; minutos em que o maior p95 excedeu 2 s; minutos em que o p95 mediano entre navegadores excedeu 1 s; minutos com ao menos uma falha sem resposta.\n\n" + md(pd.DataFrame(rows, columns=["Encontro", "Relatórios", "p50: mediana (ms)", "p50: p90", "p95: mediana", "p95: p90", "p95: p95", "p95: máximo", "Min. com relatório", "Pico de navegadores", "Min. p95 máx. > 2 s", "Min. p95 med. > 1 s", "Min. com falha"])) + f"\nTaxa de falha sem resposta: {fa['fail']} em {fa['calls']} chamadas ({fa['wilson'][0]*100:.2f}%; IC95% de Wilson {fa['wilson'][1]*100:.2f} a {fa['wilson'][2]*100:.2f}%). Kruskal-Wallis sobre a mediana do p95 por navegador-encontro (n = {kw['n']}): H = {br(kw['H'],1)}, p = {kw['p']:.5f}, epsilon-quadrado = {br(kw['eps2'],2)} (exploratório; unidades repetidas entre encontros).\n")
    # A17 intervalos
    rows = [[k, v["n"], br(v["med"]), br(v["p90"]), f"{v['acima240']*100:.0f}"] for k, v in RES["intervalos_acao"].items()]
    rows2 = [[k, f"{v['n']} de {v['de']}", br(v["med"]), f"{br(v['q1'])}–{br(v['q3'])}", f"{br(v['min'])}–{br(v['max'])}"] for k, v in RES["intervalos_marcos"].items()]
    L.append("## A17. Intervalos entre ações e entre marcos (Figura D11)\n\n" + md(pd.DataFrame(rows, columns=["Grupo de ação de origem", "Pares", "Mediana (s)", "p90 (s)", "% acima de 4 min"])) + "\nIntervalos entre marcos, restritos a até 4 h (mesmo encontro):\n\n" + md(pd.DataFrame(rows2, columns=["Intervalo", "n (de com ambos os marcos)", "Mediana (min)", "Q1–Q3", "Mín–máx"])))
    # A18 commits
    ev_ = RES["evolucao"]
    rows = []
    for i, f_ in enumerate(ev_["fases"]):
        c = ev_["commits_fase"][i]
        rows.append([f_] + [c.get(t, 0) for t in ("feat", "fix", "docs", "test", "refactor", "outros")] + [sum(c.values())])
    L.append("## A18. Commits por fase e tipo (Figura D16)\n\nTipos pela convenção de mensagem (Conventional Commits); \"outros\" reúne mensagens sem prefixo. As fases são delimitadas pelo início de cada dia de encontro.\n\n" + md(pd.DataFrame(rows, columns=["Fase", "feat", "fix", "docs", "test", "refactor", "outros", "Total"])))
    (OUT / "apendice-A13-A18.md").write_text("\n".join(L), encoding="utf-8")


apendice()
(OUT / "resultados.json").write_text(json.dumps(RES, default=str, ensure_ascii=False, indent=1), encoding="utf-8")
print("ok: figuras em", FIG)
