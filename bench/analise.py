#!/usr/bin/env python3
"""Consolida os resumos das rodadas numa tabela comparativa.

A coleta (``carga.py``) não decide nada de estatística: ela grava o bruto e um
resumo por rodada. Esta análise junta os resumos e imprime a tabela que vai para
o artigo, em Markdown ou CSV.

Uso:
    python analise.py                      # tabela markdown de resultados/
    python analise.py --format csv         # csv para plotar
    python analise.py --dir outra/pasta
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

# Colunas na ordem em que interessam ao artigo.
COLUMNS: list[tuple[str, str]] = [
    ("label", "configuração"),
    ("students", "alunos"),
    ("pattern", "chegada"),
    ("num_thread", "threads"),
    ("latency_p50", "p50 (s)"),
    ("latency_p95", "p95 (s)"),
    ("latency_max", "máx (s)"),
    ("within_threshold_pct", "≤ limiar (%)"),
    ("completions_per_min", "vazão (/min)"),
    ("tokens_per_s_mean", "tok/s"),
    ("errors", "erros"),
    ("timeouts", "timeouts"),
]


def load_summaries(directory: Path) -> list[dict]:
    """Lê todos os ``*.resumo.json`` da pasta, ordenados por nome."""
    summaries = []
    for path in sorted(directory.glob("*.resumo.json")):
        try:
            summaries.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"aviso: ignorando {path.name} ({exc})", file=sys.stderr)
    return summaries


def render_markdown(rows: list[dict]) -> str:
    headers = [label for _, label in COLUMNS]
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        cells = []
        for key, _ in COLUMNS:
            value = row.get(key)
            cells.append("n/d" if value is None else str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_csv(rows: list[dict]) -> str:
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=[key for key, _ in COLUMNS])
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key, _ in COLUMNS})
    return buffer.getvalue()


def aggregate(rows: list[dict]) -> list[dict]:
    """Agrega repetições: rótulos ``base-r1``, ``base-r2``… viram uma linha.

    Reporta a **média** e a **faixa** (mín a máx) das métricas que interessam.
    Uma execução por configuração não sustenta p95: sem dispersão, o revisor não
    tem como saber se o número é típico ou sorte.
    """
    grupos: dict[str, list[dict]] = {}
    for row in rows:
        base = re.sub(r"-r\d+$", "", str(row.get("label", "")))
        grupos.setdefault(base, []).append(row)

    agregado = []
    for base, itens in sorted(grupos.items()):
        linha = dict(itens[0])
        linha["label"] = base
        linha["n_execucoes"] = len(itens)
        for key in ("latency_p50", "latency_p95", "within_threshold_pct",
                    "completions_per_min", "tokens_per_s_mean"):
            valores = [i[key] for i in itens if i.get(key) is not None]
            if not valores:
                linha[key] = None
                continue
            media = sum(valores) / len(valores)
            linha[key] = round(media, 2)
            if len(valores) > 1:
                linha[f"{key}_faixa"] = f"{min(valores):.1f}–{max(valores):.1f}"
        linha["errors"] = sum(i.get("errors") or 0 for i in itens)
        linha["timeouts"] = sum(i.get("timeouts") or 0 for i in itens)
        agregado.append(linha)
    return agregado


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="resultados")
    parser.add_argument("--format", choices=("markdown", "csv"), default="markdown")
    parser.add_argument(
        "--agregar", action="store_true",
        help="junta repetições (rótulos terminados em -rN) com média e faixa",
    )
    args = parser.parse_args()

    directory = Path(args.dir)
    if not directory.is_dir():
        print(f"pasta não encontrada: {directory}", file=sys.stderr)
        return 1

    rows = load_summaries(directory)
    if not rows:
        print(f"nenhum resumo em {directory}", file=sys.stderr)
        return 1

    if args.agregar:
        rows = aggregate(rows)

    print(render_csv(rows) if args.format == "csv" else render_markdown(rows))

    # O limiar é o critério de aceitação da ADR-0001: destacar quem não passa.
    failing = [r for r in rows if (r.get("within_threshold_pct") or 0) < 95]
    if failing:
        print(
            f"\n{len(failing)} de {len(rows)} configurações ficam abaixo de 95% "
            f"dos alunos dentro do limiar de sala de aula.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
