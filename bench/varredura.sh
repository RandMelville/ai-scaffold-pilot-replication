#!/usr/bin/env bash
# Simulacao de turma no appliance. Tres repeticoes por configuracao.
#
#   ./varredura.sh            # tudo que nao exige mexer no codigo
#
# Nao inclui a medicao "antes da correcao": essa vive em dependabilidade.sh,
# separada de proposito, porque exige reverter o feedback.py temporariamente.
set -u

AQUI="$(cd "$(dirname "$0")" && pwd)"
RAIZ="$(cd "$AQUI/../.." && pwd)"
PY="$RAIZ/backend/.venv/bin/python"
PORTA=8011
API="http://127.0.0.1:$PORTA"
CODIGO="CARGA-8A-WXXJ"
DB="$AQUI/experimento.db"

cd "$AQUI"

vivos=$(pgrep -f "carga.py" | wc -l | tr -d ' ')
if [ "$vivos" -ne 0 ]; then
  echo "ABORTANDO: ha $vivos gerador(es) de carga vivo(s). Medicao seria contaminada."
  pgrep -fl "carga.py"
  exit 1
fi
echo "nenhum gerador de carga concorrente; iniciando"

parar_backend() { lsof -ti tcp:$PORTA 2>/dev/null | xargs kill -9 2>/dev/null || true; sleep 1; }

subir_backend() {
  parar_backend
  ( cd "$RAIZ/backend" && DATABASE_URL="sqlite:///$DB" LLM_PROVIDER=ollama \
      OLLAMA_NUM_THREAD=2 OLLAMA_NUM_GPU=0 OLLAMA_MODEL=qwen2.5:3b-instruct \
      nohup ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 \
      --port $PORTA --log-level warning > "$AQUI/uvicorn.log" 2>&1 & )
  for _ in $(seq 1 20); do
    curl -s -m 3 "$API/api/oficinas/$CODIGO" >/dev/null 2>&1 && { echo "backend no ar"; return 0; }
    sleep 1
  done
  echo "backend NAO subiu"; return 1
}

rodar() {  # rodar <rotulo> <args...>
  local rot="$1"; shift
  for r in 1 2 3; do
    # variante = numero da repeticao: sem isso, r2 e r3 medem cache quente
    $PY carga.py --label "${rot}-r${r}" --variant "$((r - 1))" --timeout 900 "$@" >/dev/null 2>&1
    echo "  ${rot}-r${r}"
  done
}

echo "== 1. turma chegando de uma vez (pico sincronizado) =="
for n in 5 15 30; do
  rodar "pico-$(printf %02d "$n")" --target ollama --students "$n" --pattern burst --num-gpu 0 --num-thread 2
done

echo "== 2. turma chegando distribuida =="
for w in 240 360; do
  rodar "espalhado-${w}s" --target ollama --students 30 --pattern spread --window "$w" --num-gpu 0 --num-thread 2
done

echo "== 3. a carga de IA respeita a fronteira? (sonda) =="
subir_backend || exit 1
rodar sonda-sem-carga  --target ollama   --students 1  --pattern spread --window 40 --num-gpu 0 --num-thread 2 --probe --api "$API" --code "$CODIGO"
rodar sonda-fora-da-app --target ollama  --students 30 --pattern burst  --num-gpu 0 --num-thread 2 --probe --api "$API" --code "$CODIGO"
rodar sonda-pela-app-depois --target platform --students 30 --pattern burst --probe --api "$API" --code "$CODIGO"
parar_backend

echo "== FIM =="
