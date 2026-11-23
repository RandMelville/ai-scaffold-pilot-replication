#!/usr/bin/env bash
# Varredura na MAQUINA REAL do piloto (ADR-0027): o Mac do pesquisador, com
# Metal e a mesma configuracao que a plataforma usa (OLLAMA_NUM_GPU=-1,
# OLLAMA_NUM_THREAD=8, config.py). A varredura.sh original emula o mini-PC
# (--num-gpu 0 --num-thread 2), que nao sera implantado; fica como registro.
#
#   ./varredura-mac.sh
#
# Tres repeticoes por configuracao no pico; a chegada distribuida roda uma vez,
# porque no Mac ela e o caso facil e custa a janela inteira de relogio.
set -u

AQUI="$(cd "$(dirname "$0")" && pwd)"
RAIZ="$(cd "$AQUI/../.." && pwd)"
PY="$RAIZ/backend/.venv/bin/python"
PORTA=8011
API="http://127.0.0.1:$PORTA"
CODIGO="CARGA-8A-WXXJ"
DB="$AQUI/experimento.db"
GPU=-1     # Metal, como na plataforma
THR=8      # OLLAMA_NUM_THREAD do config.py

cd "$AQUI"

vivos=$(pgrep -f "carga.py" | wc -l | tr -d ' ')
if [ "$vivos" -ne 0 ]; then
  echo "ABORTANDO: ha $vivos gerador(es) de carga vivo(s)."; pgrep -fl "carga.py"; exit 1
fi
echo "maquina: $(sysctl -n machdep.cpu.brand_string) | $(sysctl -n hw.memsize | awk '{print $1/1073741824 " GB"}')"
echo "ollama: $(curl -s http://localhost:11434/api/version)"

parar_backend() { lsof -ti tcp:$PORTA 2>/dev/null | xargs kill -9 2>/dev/null || true; sleep 1; }

subir_backend() {
  parar_backend
  ( cd "$RAIZ/backend" && DATABASE_URL="sqlite:///$DB" LLM_PROVIDER=ollama \
      OLLAMA_NUM_THREAD=$THR OLLAMA_NUM_GPU=$GPU OLLAMA_MODEL=qwen2.5:3b-instruct \
      nohup ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 \
      --port $PORTA --log-level warning > "$AQUI/uvicorn-mac.log" 2>&1 & )
  for _ in $(seq 1 20); do
    curl -s -m 3 "$API/api/oficinas/$CODIGO" >/dev/null 2>&1 && { echo "backend no ar"; return 0; }
    sleep 1
  done
  echo "backend NAO subiu"; return 1
}

rodar() {  # rodar <rotulo> <reps> <args...>
  local rot="$1" reps="$2"; shift 2
  for r in $(seq 1 "$reps"); do
    $PY carga.py --label "${rot}-r${r}" --variant "$((r - 1))" --timeout 900 "$@" >/dev/null 2>&1
    echo "  ${rot}-r${r}  $(date +%H:%M:%S)"
  done
}

echo "== 1. pico sincronizado (nivel 1, direto no Ollama) =="
for n in 5 15 30; do
  rodar "mac-pico-$(printf %02d "$n")" 3 --target ollama --students "$n" --pattern burst --num-gpu $GPU --num-thread $THR
done

echo "== 2. chegada distribuida em 240 s (nivel 1) =="
rodar "mac-espalhado-240s" 1 --target ollama --students 30 --pattern spread --window 240 --num-gpu $GPU --num-thread $THR

echo "== 3. fim a fim pela plataforma, com sonda (nivel 2) =="
subir_backend || exit 1
rodar mac-sonda-sem-carga 1 --target ollama --students 1 --pattern spread --window 40 --num-gpu $GPU --num-thread $THR --probe --api "$API" --code "$CODIGO"
rodar mac-sonda-pela-app 2 --target platform --students 30 --pattern burst --probe --api "$API" --code "$CODIGO"
parar_backend

echo "== FIM $(date +%H:%M:%S) =="
