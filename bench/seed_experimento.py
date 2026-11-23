#!/usr/bin/env python3
"""Semeia um banco SEPARADO com uma turma de 30 para o experimento de carga.

Não toca no banco de desenvolvimento. O caminho vem de ``--db`` (padrão:
``experimento.db`` nesta pasta) e é exportado como ``DATABASE_URL`` antes de
importar o app, que é como a ``config.py`` lê a configuração.

Cria: uma oficina rotulada como experimento, 30 codinomes na folha-chave
(ADR-0017: a entrada só aceita codinomes da lista) e um projeto **aberto**
(ADR-0016: um por vez; só o aberto é visível ao aluno). Grava
``oficina.json`` com o código e os codinomes, que é o que o ``carga.py`` lê.

Idempotente: rodar de novo não duplica.

Uso:
    ../../backend/.venv/bin/python seed_experimento.py
    ../../backend/.venv/bin/python seed_experimento.py --students 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1] / "backend"

NAME = "Oficina de Experimento de Carga (sintética)"
SCHOOL = "Escola de Teste (sintética)"
GRADE = "8º ano"
SCHOOL_TAG = "CARGA"
GRADE_TAG = "8A"
PROJECT_TITLE = "Fábula do experimento de capacidade"
PROPOSAL = (
    "Proposta sintética usada apenas para medir a capacidade do appliance sob "
    "carga de turma. Não é atividade pedagógica e não contém dado de aluno."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(HERE / "experimento.db"))
    parser.add_argument("--students", type=int, default=30)
    parser.add_argument("--out", default=str(HERE / "oficina.json"))
    args = parser.parse_args()

    # Precisa vir ANTES de importar o app: a Settings lê o ambiente na importação.
    os.environ["DATABASE_URL"] = f"sqlite:///{args.db}"
    sys.path.insert(0, str(BACKEND))

    from alembic import command  # noqa: E402
    from alembic.config import Config  # noqa: E402

    alembic_cfg = Config(str(BACKEND / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{args.db}")
    command.upgrade(alembic_cfg, "head")

    from sqlalchemy import select  # noqa: E402

    from app.db import SessionLocal  # noqa: E402
    from app.models.oficina import Oficina  # noqa: E402
    from app.models.projeto import Projeto  # noqa: E402
    from app.services.oficinas import criar_oficina, gerar_codinomes  # noqa: E402
    from app.services.projetos import criar_projeto  # noqa: E402

    db = SessionLocal()
    try:
        workshop = db.scalar(select(Oficina).where(Oficina.nome == NAME))
        if workshop is None:
            workshop = criar_oficina(
                db,
                nome=NAME,
                escola=SCHOOL,
                serie=GRADE,
                sigla_escola=SCHOOL_TAG,
                sigla_serie=GRADE_TAG,
            )

        current = list(workshop.codinomes_permitidos or [])
        if len(current) < args.students:
            gerar_codinomes(
                db, oficina=workshop, quantidade=args.students - len(current)
            )
            db.refresh(workshop)

        project = db.scalar(select(Projeto).where(Projeto.oficina_id == workshop.id))
        if project is None:
            project = criar_projeto(
                db,
                oficina_id=workshop.id,
                titulo=PROJECT_TITLE,
                proposta=PROPOSAL,
            )

        payload = {
            "db": args.db,
            "codigo": workshop.codigo,
            "projeto_id": project.id,
            "aberto": project.aberto_em is not None,
            "codinomes": list(workshop.codinomes_permitidos or []),
        }
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if not payload["aberto"]:
            print(
                "\nAVISO: o projeto não está aberto, o aluno não o enxerga. "
                "Abra pela visão da professora antes de rodar o nível 2.",
                file=sys.stderr,
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
