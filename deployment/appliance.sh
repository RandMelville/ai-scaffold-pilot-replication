#!/usr/bin/env bash
# Sobe a plataforma no MODO APPLIANCE, como ela vai rodar na escola:
# um endereço só na rede local, IA local de verdade (Ollama), sem nuvem.
#
#   ./scripts/appliance.sh            # dados de teste, banco próprio
#   ./scripts/appliance.sh --demo     # + oficina fictícia pronta (seed)
#   ./scripts/appliance.sh --piloto   # BANCO NO COFRE CIFRADO (dado real, só codinomes)
#
# Para parar: Ctrl+C. Sem --piloto, o banco fica em backend/appliance-teste.db e
# pode ser apagado à vontade: NÃO é o cofre, e não deve receber dado real.
#
# Modo --piloto (ADR-0027): o banco vive em /Volumes/RemidiAcao-Cofre, um volume
# APFS cifrado montado só durante a sessão, FORA de qualquer pasta sincronizada
# (Documents e Desktop vão para o iCloud nesta máquina). Criar uma vez, no
# Terminal, com senha própria guardada no papel junto da folha-chave:
#
#   hdiutil create -size 4g -type SPARSE -fs APFS -encryption AES-256 \
#       -volname RemidiAcao-Cofre ~/RemidiAcao-Cofre
#
# Montar antes de cada sessão (duplo clique em ~/RemidiAcao-Cofre.sparseimage ou
# `hdiutil attach ~/RemidiAcao-Cofre.sparseimage`); desmontar ao fim (ejetar).
# O script recusa subir se o cofre não estiver montado, faz cópia de segurança do
# banco no início e no fim da sessão (dentro do próprio cofre, pasta backups/),
# desliga o DEBUG e gera o código de equipe na primeira vez (fica no cofre).
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORTA="${PORTA:-8000}"
MODELO="qwen2.5:3b-instruct"
# Pedidos simultâneos no servidor Ollama (ADR-0025, medido em 17/set/2026 na
# máquina do piloto): 1 = um por vez (22 respostas/min, p95 de 77 s com 30 alunos
# em pico); 4 = 50 respostas/min, p95 de 36 s; 8 piora (o contexto de 4096 é
# dividido entre os slots e a decodificação cai para 10-15 tok/s).
PARALELO="${OLLAMA_NUM_PARALLEL:-4}"
OLLAMA_BIN="$(command -v ollama || echo /Applications/Ollama.app/Contents/Resources/ollama)"

echo "→ Ollama"
if curl -fsS -m 5 http://localhost:11434/api/version >/dev/null 2>&1; then
  # Já há um servidor (normalmente o Ollama.app). Ele não lê OLLAMA_NUM_PARALLEL
  # desta shell: lê o launchctl setenv, mas só se for encerrado POR COMPLETO e
  # reaberto (o "quit" do menu costuma deixar o "ollama serve" filho vivo, e ele
  # segue com a configuração antiga; visto em 17/set). Confere o que o runner
  # está usando e avisa se não for o valor do piloto.
  curl -s http://localhost:11434/api/chat -d "{\"model\":\"$MODELO\",\"messages\":[{\"role\":\"user\",\"content\":\"oi\"}],\"stream\":false}" >/dev/null 2>&1 || true
  NP="$(pgrep -fl llama-server 2>/dev/null | grep -o -- '-np [0-9]*' | head -1 | awk '{print $2}')"
  if [ -n "$NP" ] && [ "$NP" != "$PARALELO" ]; then
    echo "  AVISO: o servidor Ollama em execução atende $NP pedido(s) por vez; o piloto pede $PARALELO."
    echo "  Para corrigir:  launchctl setenv OLLAMA_NUM_PARALLEL $PARALELO"
    echo "                  pkill -f 'ollama serve'; pkill -f llama-server; open -a Ollama"
    echo "  ou feche o Ollama.app por completo e rode este script de novo, que ele sobe o servidor certo."
  fi
else
  echo "  subindo o servidor Ollama com OLLAMA_NUM_PARALLEL=$PARALELO"
  ( OLLAMA_NUM_PARALLEL="$PARALELO" nohup "$OLLAMA_BIN" serve > /tmp/remidiacao-ollama.log 2>&1 & )
  for _ in $(seq 1 20); do
    curl -fsS -m 3 http://localhost:11434/api/version >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsS -m 5 http://localhost:11434/api/version >/dev/null 2>&1 || { echo "  Ollama não subiu (veja /tmp/remidiacao-ollama.log)"; exit 1; }
fi
# Sem cano com grep -q aqui: sob 'pipefail', o grep fecha o cano no primeiro
# acerto, o 'ollama list' morre de SIGPIPE e o modelo presente seria reportado
# como ausente.
LISTA="$(ollama list 2>/dev/null || true)"
case "$LISTA" in
  *"$MODELO"*) ;;
  *) echo "  Modelo $MODELO não está baixado. Rode: ollama pull $MODELO"; exit 1 ;;
esac
echo "  ok, com $MODELO"

echo "→ Interface (build)"
# Rebuild SEMPRE. O appliance serve o build, não o código-fonte: pular isto faz
# você testar a versão anterior e atribuir ao sistema um defeito já corrigido.
# Custa poucos segundos.
( cd "$RAIZ/frontend" && npm run build >/dev/null 2>&1 )
echo "  ok, build novo em frontend/dist"

echo "→ Banco"
cd "$RAIZ/backend"
COFRE="/Volumes/RemidiAcao-Cofre"
if [ "${1:-}" = "--piloto" ]; then
  if [ ! -d "$COFRE" ] || ! mount | grep -q " $COFRE "; then
    echo "  COFRE NÃO MONTADO. Monte ~/RemidiAcao-Cofre.sparseimage (duplo clique) e rode de novo."
    echo "  Se ainda não existe, crie uma vez (veja o cabeçalho deste script)."
    exit 1
  fi
  DB="$COFRE/remidiacao.db"
  mkdir -p "$COFRE/backups"
  # Cópia de segurança consistente ANTES de tocar no banco (condição de início, ADR-0027).
  if [ -f "$DB" ]; then
    sqlite3 "$DB" ".backup '$COFRE/backups/remidiacao-$(date +%Y%m%d-%H%M%S)-inicio.db'"
    echo "  cópia de segurança de início feita em $COFRE/backups/"
  fi
  export DATABASE_URL="sqlite:///$DB"
  export DEBUG=false
  # Código de acesso da equipe (professora/pesquisador): gerado uma vez, guardado no cofre.
  if [ ! -f "$COFRE/equipe.codigo" ]; then
    # Sem cano: sob 'pipefail', tr | head morre de SIGPIPE e o set -e derruba o script.
    .venv/bin/python -c "import secrets; print(''.join(secrets.choice('ABCDEFGHJKMNPQRSTUVWXYZ23456789') for _ in range(10)), end='')" > "$COFRE/equipe.codigo"
    echo "  código de equipe gerado pela primeira vez: $(cat "$COFRE/equipe.codigo")  ← anote no papel"
  fi
  export EQUIPE_ACESSO_CODIGO="$(cat "$COFRE/equipe.codigo")"
  .venv/bin/alembic upgrade head >/dev/null
  echo "  ok, banco no COFRE: $DB (dado real, só codinomes; nunca sai do volume cifrado)"
  # Ao encerrar (Ctrl+C), cópia de segurança de fim de sessão.
  trap 'sqlite3 "$DB" ".backup '"'"'$COFRE/backups/remidiacao-$(date +%Y%m%d-%H%M%S)-fim.db'"'"'" && echo "cópia de segurança de fim de sessão feita. Agora EJETE o cofre."' EXIT
else
  export DATABASE_URL="sqlite:///./appliance-teste.db"
  .venv/bin/alembic upgrade head >/dev/null
  echo "  ok, appliance-teste.db (banco de TESTE, não é o cofre)"
  if [ "${1:-}" = "--demo" ]; then
    echo "→ Dados fictícios"
    DEMO_MODE=true .venv/bin/python -m app.scripts.seed
  fi
fi

# Todos os endereços IPv4 da máquina, um por linha: na sala, o que vale é o da
# unidade de rede (cabo no roteador, reserva 10.0.0.10), não o do Wi-Fi que
# porventura dê internet ao Mac. Listar todos evita imprimir o errado.
IPS="$(ifconfig 2>/dev/null | awk '/inet / && $2 != "127.0.0.1" && $2 !~ /^169\.254/ {print $2}' | tr '\n' ' ')"
IP="${IPS:-127.0.0.1}"
cat <<FIM

  ┌─────────────────────────────────────────────────────────
  │  RemidiAção no ar, modo appliance
  │
  │  Neste computador:   http://localhost:$PORTA
  │  Na rede local:      porta $PORTA em: $IP
  │                      (na sala, use o endereço do roteador: http://10.0.0.10:$PORTA)
  │
  │  IA: $MODELO, local, sem nuvem
  │  Ctrl+C para parar
  └─────────────────────────────────────────────────────────

FIM

export LLM_PROVIDER=ollama
# Consumidores da fila de feedback = pedidos simultâneos no Ollama (ADR-0025).
export FEEDBACK_WORKERS="$PARALELO"
export FRONTEND_DIST="$RAIZ/frontend/dist"
# `caffeinate -i` segura o sono ocioso enquanto o servidor roda: na sala, a máquina
# suspender no meio da sessão derruba a plataforma para a turma inteira, e a ADR-0027
# lista isso como passo de procedimento justamente porque o appliance deixou de ser um
# aparelho dedicado. Melhor o script garantir do que depender de lembrar.
if [ "${1:-}" = "--piloto" ]; then
  # Sem exec: o trap de cópia de segurança precisa rodar depois do servidor cair.
  caffeinate -i .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORTA" || true
else
  exec caffeinate -i .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORTA"
fi
