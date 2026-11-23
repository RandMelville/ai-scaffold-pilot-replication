#!/usr/bin/env bash
# T4: a carga de IA respeita a fronteira? (sonda de disponibilidade)
#
#   ./t4_sonda.sh [repeticoes]      # padrao 2
#
# Mede o caminho de computacao COM a plataforma no ar, sondando um endpoint
# barato uma vez por segundo durante a carga. Nao inclui a condicao "antes da
# correcao": essa vive em dependabilidade.sh, separada de proposito, porque
# exige reverter o feedback.py.
#
# Aponta para a instancia FIXADA do Ollama (porta 11435, NUM_PARALLEL=1), que
# e a mesma usada em T1, T2 e T3. Sem isso a condicao muda entre blocos.
set -u

AQUI="$(cd "$(dirname "$0")" && pwd)"
RAIZ="$(cd "$AQUI/../.." && pwd)"
PY="$RAIZ/backend/.venv/bin/python"
PORTA=8011
API="http://127.0.0.1:$PORTA"
OLLAMA="http://127.0.0.1:11435"
CODIGO="CARGA-8A-WXXJ"
DB="$AQUI/experimento.db"
REPS="${1:-2}"

cd "$AQUI"

vivos=$(pgrep -f "carga.py" | wc -l | tr -d ' ')
if [ "$vivos" -ne 0 ]; then
  echo "ABORTANDO: ha $vivos gerador(es) de carga vivo(s)."
  pgrep -fl "carga.py"
  exit 1
fi

if ! curl -s -m 3 "$OLLAMA/api/version" >/dev/null; then
  echo "ABORTANDO: instancia fixada do Ollama nao responde em $OLLAMA"
  exit 1
fi

parar_backend() { lsof -ti tcp:$PORTA 2>/dev/null | xargs kill -9 2>/dev/null || true; sleep 1; }

subir_backend() {
  parar_backend
  ( cd "$RAIZ/backend" && DATABASE_URL="sqlite:///$DB" LLM_PROVIDER=ollama \
      OLLAMA_HOST="$OLLAMA" OLLAMA_NUM_THREAD=2 OLLAMA_NUM_GPU=0 \
      OLLAMA_MODEL=qwen2.5:3b-instruct \
      nohup ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 \
      --port $PORTA --log-level warning > "$AQUI/uvicorn-t4.log" 2>&1 & )
  for _ in $(seq 1 20); do
    curl -s -m 3 "$API/api/oficinas/$CODIGO" >/dev/null 2>&1 && { echo "backend no ar"; return 0; }
    sleep 1
  done
  echo "backend NAO subiu; ver uvicorn-t4.log"; return 1
}

subir_backend || exit 1

# Variantes a partir de 10 para nao colidir com T1 (3), T2 (4) e T3 (5, 6).
v=10
for r in $(seq 1 "$REPS"); do
  echo "== repeticao $r de $REPS =="

  echo "  t4-baseline-r${r} (sem carga)"
  $PY carga.py --target ollama --ollama "$OLLAMA" --students 1 --pattern spread \
      --window 40 --num-gpu 0 --num-thread 2 --variant $v --probe --api "$API" \
      --code "$CODIGO" --timeout 900 --label "t4-baseline-r${r}" >/dev/null 2>&1
  v=$((v + 1))

  echo "  t4-nivel1-r${r} (30 em pico, IA FORA da plataforma)"
  $PY carga.py --target ollama --ollama "$OLLAMA" --students 30 --pattern burst \
      --num-gpu 0 --num-thread 2 --variant $v --probe --api "$API" \
      --code "$CODIGO" --timeout 900 --label "t4-nivel1-r${r}" >/dev/null 2>&1
  v=$((v + 1))

  echo "  t4-nivel2-depois-r${r} (30 em pico, PELA plataforma, apos a correcao)"
  $PY carga.py --target platform --students 30 --pattern burst \
      --probe --api "$API" --code "$CODIGO" --timeout 900 \
      --variant $v --label "t4-nivel2-depois-r${r}" >/dev/null 2>&1
  v=$((v + 1))
done

parar_backend
echo "== FIM T4 =="
