#!/usr/bin/env python3
"""Gerador de carga de turma para o appliance RemidiAção (ADR-0002).

Mede o que o aluno sente: o tempo entre pedir o feedback e o feedback ficar
pronto, sob o pico sincronizado de uma turma de 30. A ADR-0001 fixou ~2,9 s por
resposta e 10 s como limiar de sala de aula, mas esse número é de UMA chamada
isolada; em CPU a inferência é serializada e a fila cobra o resto.

Dois níveis de medição:

* ``--target ollama``      bate direto no ``/api/chat``. Mede a capacidade pura
  de serving, sem a plataforma no caminho. Não precisa de seed nem de banco, o
  que o torna portátil entre substratos (Docker local, caixa capada no Railway,
  mini-PC de verdade). Aproveita as métricas que o próprio Ollama devolve
  (``eval_count``/``eval_duration``) para reportar tokens/s reais.

* ``--target platform``    percorre o caminho real do aluno: entrar na oficina,
  salvar versão, pedir feedback e fazer polling até o estado terminal. Captura o
  efeito do ``BackgroundTasks`` (``routes/projetos.py``), que despacha cada
  pedido no threadpool sem consumidor limitado nem controle de admissão.

Padrões de chegada:

* ``burst``     todos no instante zero. É o pior caso realista: a professora diz
  "agora peçam o feedback" e 30 Chromebooks disparam juntos.
* ``spread``    chegadas uniformes numa janela (``--window``).
* ``poisson``   intervalos exponenciais com a mesma média da janela.

Saída: um JSONL com uma linha por pedido e um JSON de resumo. A análise fica em
``analise.py`` para que a coleta nunca dependa de decisão estatística.

Exemplos:

    # capacidade pura, turma inteira de uma vez
    python carga.py --target ollama --students 30 --pattern burst \
        --num-thread 8 --label local-8thr

    # fim a fim pela plataforma
    python carga.py --target platform --students 30 \
        --api http://localhost:8000 --code SCHOOL-8A-XXXX --label e2e-local
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - orientação de ambiente
    sys.exit("httpx não encontrado. Rode: pip install httpx")

from textos import text_for

# Prompt do piloto. Importa do backend quando disponível para não haver duas
# verdades; se o experimento rodar isolado (outra máquina, sem o pacote), cai
# numa cópia local marcada como tal.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
    from app.utils.prompts import KOCH_SYSTEM, koch_user_prompt  # type: ignore

    PROMPT_SOURCE = "backend/app/utils/prompts.py"
except Exception:  # pragma: no cover - execução fora do repo
    from prompt_fallback import KOCH_SYSTEM, koch_user_prompt  # type: ignore

    PROMPT_SOURCE = "prompt_fallback.py (cópia local)"


@dataclass
class Record:
    """Uma requisição de feedback, do ponto de vista do aluno."""

    student: int
    sent_at: float  # segundos desde o início da rodada
    finished_at: float
    latency_s: float
    status: str  # ok | error | timeout
    error: str = ""
    # nível 1 (Ollama)
    total_duration_s: Optional[float] = None
    load_duration_s: Optional[float] = None
    prompt_eval_count: Optional[int] = None
    prompt_eval_duration_s: Optional[float] = None
    eval_count: Optional[int] = None
    eval_duration_s: Optional[float] = None
    tokens_per_s: Optional[float] = None
    # nível 2 (plataforma)
    polls: Optional[int] = None
    scheduled_at: Optional[float] = None
    # A resposta cumpriu o contrato de 2 chaves (ADR-0019)? None = não checado.
    # HTTP 200 não é sucesso: o consumidor a jusante precisa do JSON válido.
    contract_ok: Optional[bool] = None
    # Segundo poller do navegador real (GET /projetos/{id}/texto a cada 10 s).
    # É requisição de aluno SEM inferência: se travar, a tela dele congela.
    mediation_polls: Optional[int] = None
    mediation_failures: Optional[int] = None
    mediation_max_s: Optional[float] = None


@dataclass
class ProbeRecord:
    """Uma sondagem de "a plataforma ainda responde?" durante a carga.

    Bate num endpoint barato (validação do código da oficina: uma consulta, sem
    IA, sem autenticação) enquanto a turma pede feedback. É o que mede se a
    carga de IA **fica contida** ou se contamina o resto da aplicação. A ADR-0002
    afirma que "a LLM nunca degrada a plataforma (desacoplamento por fila)", e
    esta sonda é o teste dessa afirmação.
    """

    at: float  # segundos desde o início da rodada
    latency_s: float
    status: str  # ok | error
    error: str = ""


async def probe_loop(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    t0: float,
    stop: asyncio.Event,
) -> list[ProbeRecord]:
    """Sonda o endpoint barato até a carga terminar."""
    url = f"{args.api}/api/oficinas/{args.code}"
    records: list[ProbeRecord] = []
    while not stop.is_set():
        start = time.perf_counter()
        try:
            response = await client.get(url, timeout=args.probe_timeout)
            response.raise_for_status()
            records.append(
                ProbeRecord(
                    at=round(start - t0, 3),
                    latency_s=round(time.perf_counter() - start, 4),
                    status="ok",
                )
            )
        except (httpx.HTTPError, ValueError) as exc:
            records.append(
                ProbeRecord(
                    at=round(start - t0, 3),
                    latency_s=round(time.perf_counter() - start, 4),
                    status="error",
                    error=str(exc)[:200],
                )
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=args.probe_interval)
        except asyncio.TimeoutError:
            pass
    return records


async def model_placement(client: httpx.AsyncClient, ollama: str) -> dict:
    """Onde o modelo esta carregado: CPU, GPU ou dividido.

    Existe por causa de um erro real (29/07/2026): as primeiras medicoes foram
    tomadas num Mac onde o Ollama usa Metal, entao rodaram **100% na GPU**,
    enquanto o appliance alvo e um mini-PC **sem acelerador**. O sintoma que
    deveria ter delatado isso foi ``num_thread`` nao alterar nada. Agora a
    colocacao vai gravada no resumo de cada rodada, para nao depender de memoria.
    """
    try:
        resposta = await client.get(f"{ollama}/api/ps", timeout=10)
        resposta.raise_for_status()
        modelos = resposta.json().get("models") or []
    except (httpx.HTTPError, ValueError):
        return {"placement": "desconhecido"}
    if not modelos:
        return {"placement": "nenhum modelo carregado"}
    m = modelos[0]
    total, vram = m.get("size") or 0, m.get("size_vram") or 0
    if not total:
        return {"placement": "desconhecido"}
    fracao = vram / total
    rotulo = "100% CPU" if fracao < 0.01 else (
        "100% GPU" if fracao > 0.99 else f"{round(100 * fracao)}% GPU"
    )
    return {"placement": rotulo, "size_bytes": total, "size_vram_bytes": vram}


def _ns_to_s(value: Any) -> Optional[float]:
    """Ollama reporta durações em nanossegundos."""
    if value is None:
        return None
    return round(float(value) / 1e9, 4)


def arrival_delays(n: int, pattern: str, window: float, rng: random.Random) -> list[float]:
    """Atrasos de chegada, em segundos, por aluno."""
    if pattern == "burst":
        return [0.0] * n
    if pattern == "spread":
        if n == 1:
            return [0.0]
        step = window / (n - 1)
        return [i * step for i in range(n)]
    if pattern == "poisson":
        # Mesma taxa média da janela: lambda = n / window.
        rate = n / window if window > 0 else float(n)
        delays, t = [], 0.0
        for _ in range(n):
            t += rng.expovariate(rate)
            delays.append(round(t, 4))
        return delays
    raise ValueError(f"padrão de chegada desconhecido: {pattern}")


# ──────────────────────────────────────────────────────────────────────────────
# Nível 1: Ollama direto
# ──────────────────────────────────────────────────────────────────────────────


async def run_ollama_student(
    idx: int,
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    t0: float,
    delay: float,
) -> Record:
    """Um aluno pedindo feedback direto ao modelo."""
    await asyncio.sleep(delay)
    sent = time.perf_counter() - t0
    payload: dict[str, Any] = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": KOCH_SYSTEM},
            {"role": "user", "content": koch_user_prompt(text_for(idx, args.variant))},
        ],
        "stream": False,
        "format": "json",
    }
    # num_gpu = 0 forca CPU-only, que e a condicao do appliance alvo (mini-PC
    # sem acelerador). Sem isso, num Mac o Ollama usa Metal e a medicao descreve
    # um sistema que ninguem vai implantar.
    opcoes: dict[str, Any] = {"num_gpu": args.num_gpu}
    if args.num_thread > 0:
        opcoes["num_thread"] = args.num_thread
    payload["options"] = opcoes

    try:
        response = await client.post(
            f"{args.ollama}/api/chat", json=payload, timeout=args.timeout
        )
        response.raise_for_status()
        body = response.json()
    except httpx.TimeoutException as exc:
        done = time.perf_counter() - t0
        return Record(idx, sent, done, round(done - sent, 3), "timeout", str(exc))
    except (httpx.HTTPError, ValueError) as exc:
        done = time.perf_counter() - t0
        return Record(idx, sent, done, round(done - sent, 3), "error", str(exc))

    done = time.perf_counter() - t0
    eval_count = body.get("eval_count")
    eval_duration = _ns_to_s(body.get("eval_duration"))
    tokens_per_s = None
    if eval_count and eval_duration:
        tokens_per_s = round(eval_count / eval_duration, 2)

    # Contrato de 2 chaves (ADR-0019): status 200 com JSON inválido é falha para
    # quem consome a resposta, e precisa aparecer na medição.
    contract_ok = False
    try:
        conteudo = json.loads(body["message"]["content"])
        contract_ok = (
            isinstance(conteudo, dict)
            and isinstance(conteudo.get("pontos_fortes"), str)
            and isinstance(conteudo.get("perguntas_reflexivas"), list)
        )
    except (KeyError, TypeError, ValueError):
        contract_ok = False

    return Record(
        student=idx,
        sent_at=round(sent, 3),
        finished_at=round(done, 3),
        latency_s=round(done - sent, 3),
        status="ok",
        total_duration_s=_ns_to_s(body.get("total_duration")),
        load_duration_s=_ns_to_s(body.get("load_duration")),
        prompt_eval_count=body.get("prompt_eval_count"),
        prompt_eval_duration_s=_ns_to_s(body.get("prompt_eval_duration")),
        eval_count=eval_count,
        eval_duration_s=eval_duration,
        tokens_per_s=tokens_per_s,
        contract_ok=contract_ok,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Nível 2: plataforma fim a fim
# ──────────────────────────────────────────────────────────────────────────────


async def enter_workshop(
    client: httpx.AsyncClient, api: str, code: str, nickname: str
) -> tuple[str, str]:
    """Entra na oficina e devolve ``(token, oficina_id)``."""
    response = await client.post(
        f"{api}/api/oficinas/{code}/entrar", json={"pseudonimo": nickname}, timeout=30
    )
    response.raise_for_status()
    body = response.json()
    workshop = body.get("oficina") or {}
    return body["token"], workshop.get("id", "")


async def mediation_loop(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    project_id: str,
    headers: dict,
    stop: asyncio.Event,
) -> list[float]:
    """O segundo poller que o navegador do aluno mantém aberto.

    A SPA roda DOIS temporizadores na etapa de texto: o do feedback, a cada
    ``POLL_INTERVALO_MS`` = 2000 ms, e este, ``INTERVALO_MEDIACAO_MS`` = 10000 ms,
    que recarrega ``GET /api/projetos/{id}/texto`` para ver orientação nova da
    professora (``frontend/src/components/student/TextPhase.vue`` e
    ``stores/project.ts``). Sem ele, a mistura de requisições medida não é a que
    a turma gera.

    Devolve as latências observadas. Elas valem por si: é uma requisição REAL de
    aluno, sem inferência, então travamento aqui é a tela do próprio aluno
    congelando, não uma sonda sintética.
    """
    latencias: list[float] = []
    url = f"{args.api}/api/projetos/{project_id}/texto"
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=args.mediation_interval)
            return latencias  # parou durante a espera
        except asyncio.TimeoutError:
            pass
        inicio = time.perf_counter()
        try:
            resposta = await client.get(url, headers=headers, timeout=args.probe_timeout)
            resposta.raise_for_status()
            latencias.append(round(time.perf_counter() - inicio, 4))
        except (httpx.HTTPError, ValueError):
            latencias.append(-round(time.perf_counter() - inicio, 4))  # negativo = falha
    return latencias


async def run_platform_student(
    idx: int,
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    t0: float,
    delay: float,
    project_id: str,
    token: str,
) -> Record:
    """Um aluno salvando versão, pedindo feedback e aguardando ficar pronto.

    Reproduz os DOIS pollers que o navegador real mantém: o do feedback e o da
    mediação (ver ``mediation_loop``). A tarefa de mediação é sempre encerrada,
    inclusive em erro, e suas latências vão no registro.
    """
    await asyncio.sleep(delay)
    headers = {"Authorization": f"Bearer {token}"}
    parar_mediacao = asyncio.Event()
    tarefa_mediacao = asyncio.create_task(
        mediation_loop(client, args, project_id, headers, parar_mediacao)
    )
    sent = time.perf_counter() - t0
    polls = 0
    estado = "timeout"
    erro = f"não ficou pronto em {args.timeout}s"
    scheduled: Optional[float] = None

    try:
        version = await client.post(
            f"{args.api}/api/projetos/{project_id}/versoes",
            json={"texto": text_for(idx, args.variant)},
            headers=headers,
            timeout=60,
        )
        version.raise_for_status()
        version_id = version.json()["id"]

        request = await client.post(
            f"{args.api}/api/versoes/{version_id}/feedback", headers=headers, timeout=60
        )
        request.raise_for_status()
        scheduled = round(time.perf_counter() - t0, 3)

        # Polling até o estado terminal, no mesmo intervalo da SPA.
        deadline = time.perf_counter() + args.timeout
        while time.perf_counter() < deadline:
            await asyncio.sleep(args.poll_interval)
            polls += 1
            current = await client.get(
                f"{args.api}/api/versoes/{version_id}/feedback",
                headers=headers,
                timeout=60,
            )
            current.raise_for_status()
            situacao = current.json().get("status")
            if situacao == "pronto":
                estado, erro = "ok", ""
                break
            if situacao == "erro":
                estado = "error"
                erro = "feedback marcado como erro pelo backend"
                break
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        estado, erro = "error", str(exc)
    finally:
        parar_mediacao.set()
        try:
            mediacao = await tarefa_mediacao
        except Exception:  # noqa: BLE001 - instrumentação nunca derruba a medição
            mediacao = []

    done = time.perf_counter() - t0
    sucessos = [x for x in mediacao if x >= 0]
    return Record(
        student=idx,
        sent_at=round(sent, 3),
        finished_at=round(done, 3),
        latency_s=round(done - sent, 3),
        status=estado,
        error=erro,
        polls=polls,
        scheduled_at=scheduled,
        mediation_polls=len(mediacao),
        mediation_failures=sum(1 for x in mediacao if x < 0),
        mediation_max_s=round(max(sucessos), 4) if sucessos else None,
    )


async def prepare_platform(
    client: httpx.AsyncClient, args: argparse.Namespace
) -> tuple[str, list[str]]:
    """Entra com N alunos e resolve o projeto aberto. Devolve (projeto, tokens).

    Os codinomes vêm do ``oficina.json`` gerado pelo ``seed_experimento.py``: a
    entrada só aceita codinomes da folha-chave (ADR-0017), então pseudônimo
    inventado é rejeitado com 400.
    """
    workshop_file = Path(args.workshop_file)
    if not workshop_file.is_file():
        raise SystemExit(
            f"{workshop_file} não encontrado. Rode antes: "
            f"python seed_experimento.py --students {args.students}"
        )
    workshop = json.loads(workshop_file.read_text(encoding="utf-8"))
    codenames = workshop.get("codinomes") or []
    if len(codenames) < args.students:
        raise SystemExit(
            f"a folha-chave tem {len(codenames)} codinomes e o teste pede "
            f"{args.students}. Rode: python seed_experimento.py "
            f"--students {args.students}"
        )
    if not args.code:
        args.code = workshop["codigo"]

    tokens: list[str] = []
    for idx in range(args.students):
        token, _ = await enter_workshop(client, args.api, args.code, codenames[idx])
        tokens.append(token)

    headers = {"Authorization": f"Bearer {tokens[0]}"}
    response = await client.get(
        f"{args.api}/api/meus-projetos", headers=headers, timeout=30
    )
    response.raise_for_status()
    projects = response.json()
    if not projects:
        raise SystemExit(
            "nenhum projeto aberto na oficina. Crie um projeto pela visão da "
            "professora antes de rodar o nível 2."
        )
    return projects[0]["id"], tokens


# ──────────────────────────────────────────────────────────────────────────────


def _percentiles(values: list[float]) -> dict[str, Optional[float]]:
    """p50/p95/máx de uma lista já ordenável (vazia devolve None)."""
    ordered = sorted(values)

    def pct(p: float) -> Optional[float]:
        if not ordered:
            return None
        k = max(0, math.ceil(p * len(ordered)) - 1)
        return round(ordered[k], 4)

    return {
        "p50": pct(0.50),
        "p95": pct(0.95),
        "max": round(ordered[-1], 4) if ordered else None,
    }


def summarize(records: list[Record], args: argparse.Namespace, wall: float) -> dict:
    """Resumo da rodada. Percentis sem dependência externa."""
    ok = [r for r in records if r.status == "ok"]
    latencies = sorted(r.latency_s for r in ok)

    def pct(p: float) -> Optional[float]:
        if not latencies:
            return None
        k = max(0, math.ceil(p * len(latencies)) - 1)
        return round(latencies[k], 3)

    throughputs = [r.tokens_per_s for r in ok if r.tokens_per_s]
    threshold = args.threshold
    within = [x for x in latencies if x <= threshold]
    # O denominador é a TURMA, não os que deram certo: um aluno que recebeu erro
    # não foi atendido dentro do limiar, foi não-atendido. Contar só os OK
    # inflaria a métrica justamente nas configurações que falham.
    served_pct = round(100 * len(within) / args.students, 1) if args.students else None
    contract_ok = sum(1 for r in ok if r.contract_ok)
    contract_checked = sum(1 for r in ok if r.contract_ok is not None)

    return {
        "label": args.label,
        "target": args.target,
        "students": args.students,
        "pattern": args.pattern,
        "window_s": args.window,
        "model": args.model,
        "num_thread": args.num_thread,
        "num_gpu": args.num_gpu,
        "variant": args.variant,
        "prompt_source": PROMPT_SOURCE,
        "wall_clock_s": round(wall, 3),
        "ok": len(ok),
        "errors": sum(1 for r in records if r.status == "error"),
        "timeouts": sum(1 for r in records if r.status == "timeout"),
        "latency_p50": pct(0.50),
        "latency_p90": pct(0.90),
        "latency_p95": pct(0.95),
        "latency_max": round(latencies[-1], 3) if latencies else None,
        "latency_mean": round(sum(latencies) / len(latencies), 3) if latencies else None,
        "threshold_s": threshold,
        "within_threshold_pct": served_pct,
        "within_threshold_count": len(within),
        "class_size": args.students,
        "contract_ok": contract_ok,
        "contract_checked": contract_checked,
        "mediation_polls": sum(r.mediation_polls or 0 for r in records) or None,
        "mediation_failures": sum(r.mediation_failures or 0 for r in records) or None,
        "mediation_max_s": max(
            (r.mediation_max_s for r in records if r.mediation_max_s is not None),
            default=None,
        ),
        "poll_interval_s": args.poll_interval,
        "mediation_interval_s": args.mediation_interval,
        "completions_per_min": round(60 * len(ok) / wall, 2) if wall > 0 else None,
        "tokens_per_s_mean": round(sum(throughputs) / len(throughputs), 2)
        if throughputs
        else None,
    }


async def main_async(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    delays = arrival_delays(args.students, args.pattern, args.window, rng)
    limits = httpx.Limits(
        max_connections=args.students + 10, max_keepalive_connections=args.students + 10
    )

    async with httpx.AsyncClient(limits=limits) as client:
        if args.target == "platform":
            print(f"preparando {args.students} alunos na oficina {args.code}…")
            project_id, tokens = await prepare_platform(client, args)
            print(f"projeto aberto: {project_id}")
            t0 = time.perf_counter()
            tasks = [
                run_platform_student(
                    i, client, args, t0, delays[i], project_id, tokens[i]
                )
                for i in range(args.students)
            ]
        else:
            t0 = time.perf_counter()
            tasks = [
                run_ollama_student(i, client, args, t0, delays[i])
                for i in range(args.students)
            ]

        print(
            f"disparando {args.students} alunos · padrão={args.pattern} · "
            f"alvo={args.target}…"
        )
        stop = asyncio.Event()
        probe_task = (
            asyncio.create_task(probe_loop(client, args, t0, stop))
            if args.probe
            else None
        )
        records: list[Record] = await asyncio.gather(*tasks)
        wall = time.perf_counter() - t0
        # Depois da carga: o modelo esta carregado, então a consulta é válida.
        placement = await model_placement(client, args.ollama)
        print(f"colocação do modelo: {placement.get('placement')}")
        probes: list[ProbeRecord] = []
        if probe_task is not None:
            stop.set()
            probes = await probe_task

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = args.label or f"{args.target}-{args.students}-{args.pattern}"

    jsonl = out_dir / f"{stamp}.jsonl"
    with jsonl.open("w", encoding="utf-8") as handle:
        for record in sorted(records, key=lambda r: r.student):
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    summary = summarize(records, args, wall)
    summary.update(placement)
    if args.probe:
        probe_path = out_dir / f"{stamp}.probe.jsonl"
        with probe_path.open("w", encoding="utf-8") as handle:
            for probe in probes:
                handle.write(json.dumps(asdict(probe), ensure_ascii=False) + "\n")
        ok_probes = [p.latency_s for p in probes if p.status == "ok"]
        stats = _percentiles(ok_probes)
        summary["probe_samples"] = len(probes)
        summary["probe_errors"] = sum(1 for p in probes if p.status == "error")
        summary["probe_p50"] = stats["p50"]
        summary["probe_p95"] = stats["p95"]
        summary["probe_max"] = stats["max"]

    (out_dir / f"{stamp}.resumo.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nbruto: {jsonl}")
    return 0 if summary["ok"] == args.students else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Carga de turma para o appliance RemidiAção",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target", choices=("ollama", "platform"), default="ollama",
        help="nível 1 (modelo) ou nível 2 (plataforma fim a fim)",
    )
    parser.add_argument("--students", type=int, default=30, help="alunos simultâneos")
    parser.add_argument(
        "--pattern", choices=("burst", "spread", "poisson"), default="burst"
    )
    parser.add_argument(
        "--window", type=float, default=60.0,
        help="janela de chegada em segundos (spread/poisson)",
    )
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--model", default="qwen2.5:3b-instruct")
    parser.add_argument(
        "--num-thread", type=int, default=0,
        help="threads de CPU por inferência (0 = Ollama decide). Ver config.py",
    )
    parser.add_argument(
        "--num-gpu", type=int, default=0,
        help="camadas na GPU. 0 = CPU-only, que é a condição do appliance alvo. "
             "Só use valor maior se quiser medir DE PROPÓSITO uma caixa com "
             "acelerador, e nesse caso diga isso no artigo",
    )
    parser.add_argument(
        "--code", default="", help="código da oficina (padrão: o do oficina.json)"
    )
    parser.add_argument(
        "--workshop-file", default="oficina.json",
        help="saída do seed_experimento.py (código + folha-chave de codinomes)",
    )
    parser.add_argument(
        "--probe", action="store_true",
        help="sonda um endpoint barato durante a carga: mede se a IA contamina "
             "o resto da plataforma (exige --api e código da oficina)",
    )
    parser.add_argument("--probe-interval", type=float, default=1.0)
    parser.add_argument(
        "--probe-timeout", type=float, default=120.0,
        help="teto da sonda. DEVE ser bem maior que qualquer timeout do servidor "
             "(pool do SQLAlchemy = 30 s), senão a indisponibilidade é censurada "
             "pelo cliente e a duração real não é medida (erro de 29/07/2026)",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=2.0,
        help="intervalo do polling de feedback. Padrão 2,0 s para casar com a "
             "SPA (POLL_INTERVALO_MS em frontend/src/stores/project.ts)",
    )
    parser.add_argument(
        "--mediation-interval", type=float, default=10.0,
        help="intervalo do segundo poller do navegador, que recarrega o texto "
             "para ver orientação nova. Padrão 10,0 s para casar com a SPA "
             "(INTERVALO_MEDIACAO_MS em components/student/TextPhase.vue). "
             "0 desliga",
    )
    parser.add_argument(
        "--timeout", type=float, default=600.0, help="teto por aluno, em segundos"
    )
    parser.add_argument(
        "--threshold", type=float, default=10.0,
        help="limiar de sala de aula em segundos (ADR-0001)",
    )
    parser.add_argument("--label", default="", help="rótulo da configuração")
    parser.add_argument("--out-dir", default="resultados")
    parser.add_argument(
        "--variant", type=int, default=0,
        help="variante do texto. Repetições DEVEM usar variantes distintas, "
             "senão o cache de prefixo do servidor esquenta e a 2a e 3a rodadas "
             "medem prefill quente (erro medido em 29/07/2026)",
    )
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    # O código da oficina pode vir do seed; só é obrigatório informá-lo quando
    # não há oficina.json (ex.: sondar uma caixa remota já semeada).
    if not args.code and (args.target == "platform" or args.probe):
        workshop_file = Path(args.workshop_file)
        if workshop_file.is_file():
            args.code = json.loads(workshop_file.read_text(encoding="utf-8"))["codigo"]
        else:
            sys.stderr.write(
                f"informe --code ou gere {workshop_file} com seed_experimento.py\n"
            )
            return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
