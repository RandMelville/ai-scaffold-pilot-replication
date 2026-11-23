# Replication material: refinement of a teacher-mediated AI writing scaffold in a public-school pilot

Material that supports the aggregated results of the paper "Designing a Teacher-Mediated AI Writing Scaffold for Interactive Narratives: Local Deployment and Iterative Refinement in a Public-School Pilot".

## What is here

| Folder | Content |
|---|---|
| `data/` | Aggregated, content-free tables of the paper's Supplementary Material (CSV): timeline of the four lessons (S1), technical performance per lesson (S2), feedback queue per lesson (S3) and the matrix of the 49 field findings with decision, change and outcome (S4). |
| `bench/` | Bench-test and capacity-sweep scripts (synthetic load of 30 simulated students with fictitious texts, network and dependability probes) and their results, including the runs that were discarded and why (see `bench/README.md`). |
| `analysis/` | `08-analise-dados.py`, the single script that regenerates the aggregated tables and figures from the closing database. It opens the database read-only (`mode=ro`), selects no column with student text, labels students only as E1 to E22 and emits no calendar dates. Paths are set by the environment variables `PILOT_ROOT`, `PILOT_DB` and `PLATFORM_REPO`. The inputs are not public (see below), so the script is shared for inspection, not for re-execution by third parties. |
| `guard/` | The code of the deterministic guard on the model's output, the versioned system prompt and the tests that verify the guard. These are copies from the platform at the release tag `v1.0.0-piloto`; they are not runnable on their own. |
| `deployment/` | The script that starts the local configuration (compute unit and network unit). |

## What is not here, and why

- **No student data.** The pilot involved minors. The approved protocol and the consent forms restrict the use of students' texts and records to the research team, so raw data (texts, event records, the closing database) and the per-student outputs of the analysis script are not shared. The aggregated tables used in the paper are in `data/`.
- **Not the platform.** The platform code is in its own repository, frozen at the release tag `v1.0.0-piloto`.
- **No licence yet.** A licence will be added at the final deposit.

## Reading the bench results

`bench/resultados/` holds one `.jsonl` per run (one line per simulated request) and a `.resumo.json` with the summary. The folders `v1-contaminado`, `v2-gpu-invalido` and `v3-cache-entre-repeticoes` keep runs that were invalid, each for a documented reason, so that the correction of the procedure can be audited. Code comments and some file names are in Portuguese.
