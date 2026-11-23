# Capacidade do appliance sob carga de turma

Experimento de dimensionamento do appliance "RemidiAção-in-a-Box" (ADR-0002),
com o modelo do piloto (ADR-0001) e o prompt validado (ADR-0019).

## A pergunta

A ADR-0001 fixou **~2,9 s por resposta** e adotou **10 s como limiar de sala de
aula**. Esse número é de **uma chamada isolada**. O piloto tem **30 alunos, um
Chromebook cada**, na rede Wi-Fi do próprio appliance, e a professora pede o
feedback para a turma ao mesmo tempo. A pergunta que este experimento responde é:

> quantos alunos simultâneos um appliance atende dentro do limiar de 10 s, e o
> que muda esse número?

A resposta define o hardware a comprar. Hoje ela não existe: a ADR-0002 lista
concorrência como risco a mitigar **antes** do piloto, e a ADR-0001 registra que
a inferência em CPU é serializada.

## Resultados válidos: máquina real do piloto (17/09/2026)

A ADR-0027 mudou o alvo: a unidade de computação é o Mac do pesquisador (Apple M5, 24 GB,
Metal), não um mini-PC. A varredura foi re-rodada nessa condição (`varredura-mac.sh`, rótulos
`resultados/mac-*`), e a segunda passada mediu `OLLAMA_NUM_PARALLEL` (`mac-np4-*`, `mac-np8-*`).
Tabela consolidada, decisão e o defeito de threadpool que a medição revelou: ADR-0025, seção
"Medição na máquina real". Em resumo: um pedido por vez dá 23 respostas/min (p95 de 77 s com
30 em pico); `-np 4` dá 50/min (p95 de 36 s); `-np 8` piora. A fila com worker dedicado zerou
os erros de plataforma sob pico.

Para reproduzir: `./varredura-mac.sh` (baseline) e `./varredura-mac-paralelo.sh 4`. O
Ollama.app só lê `OLLAMA_NUM_PARALLEL` do `launchctl setenv` se for encerrado por completo
(`pkill -f 'ollama serve'; pkill -f llama-server`) e reaberto; o "quit" do menu deixa o
servidor filho vivo com a configuração antiga.

## Resultados da emulação do mini-PC: NENHUM válido (29/07/2026, histórico)

Três gerações de coleta foram invalidadas, e os motivos ficam registrados porque
cada um é uma lição de método:

| pasta | por que não vale |
|---|---|
| `resultados/exploratorio/` | rodada única por configuração, sem repetição |
| `resultados/v1-contaminado/` | só 6 textos, então o servidor respondia do cache de prompt; e um gerador de carga zumbi de um experimento abortado rodou por cima da coleta |
| `resultados/v2-gpu-invalido/` | rodou com o modelo **100% na GPU** (Metal), enquanto o appliance alvo é um mini-PC sem acelerador |

O terceiro é o mais instrutivo. O sintoma que deveria ter denunciado estava à
vista: `num_thread` em 0, 4 e 8 dava o mesmo p95. A leitura correta não era "a
máquina é insensível a threads", era "a inferência não está na CPU". Por isso o
`carga.py` agora força `--num-gpu 0` por padrão e **grava a colocação do modelo
em cada resumo**, consultando o `/api/ps` do próprio Ollama.

Efeito medido de rodar de fato em CPU, texto inédito: o processamento do prompt
sai de 0,02 s para cerca de 1,9 s, e o tempo de serviço por aluno vai de ~2,5 s
para ~4 s. A decodificação quase não muda (52 contra 55 tokens/s), porque com um
pedido por vez ela é limitada por banda de memória, não por compute.

A coleta válida ainda não foi feita.

## Onde parei (29/07/2026, 22h57)

A coleta válida foi interrompida a pedido, com 4 de 24 rodadas feitas. Para
retomar, basta `./varredura.sh`: ela já está configurada na forma correta e
sobrescreve os rótulos existentes.

**Configuração que a varredura usa hoje, e por que cada escolha:**

| parâmetro | valor | motivo |
|---|---|---|
| `--num-gpu 0` | CPU apenas | o appliance não tem acelerador; sem isso o Mac usa Metal |
| `--num-thread 2` | 2 threads | emula caixa de 2 núcleos (cenário do meio de Malveira et al., SBESC 2025) **e** usa ~1/5 da máquina: pico de 199% de CPU contra 1000% possíveis, load 1,69 |
| `--variant` = nº da repetição | 0, 1, 2 | sem isso a 2ª e a 3ª rodada medem cache de prompt quente |
| `--poll-interval 2` | 2 s | igual à SPA (`POLL_INTERVALO_MS`) |
| `--mediation-interval 10` | 10 s | igual à SPA, confirmado por calibração com navegador real |
| `--probe-timeout 120` | 120 s | acima de qualquer timeout de servidor, senão a queda é censurada pelo cliente |
| pico | 5, 15, 30 alunos | 3 pontos bastam para a forma |
| janelas | 240 s, 360 s | capacidade medida a 2 threads é ~8,8/min, então 30 alunos exigem ≥205 s |

Custo de relógio para completar as 24: cerca de 65 minutos, dos quais 32 são as
duas janelas de chegada distribuída, que por construção duram no mínimo o
tamanho da janela.

**Primeiro dado válido já coletado:** 5 alunos em pico nessa caixa emulada dão
p50 de 21,1 s e p95 de 34,1 s. Cinco alunos já estouram o limiar de 10 s por três
vezes, o que converge com a conclusão de Malveira et al. de que sem acelerador
essa classe de hardware é impraticável para uso interativo.

**Falta, além de completar a varredura:** a medição *antes* da correção de
isolamento, que exige reverter `backend/app/services/feedback.py` por alguns
minutos (`dependabilidade.sh` faz isso com `trap`, mas **não o mate com `pkill`**,
porque o `trap` não dispara e o arquivo fica revertido). E, opcionalmente, a
emulação de rede (`rede.sh`, exige `sudo`), que só é necessária se o artigo for
reivindicar a cadeia completa incluindo o caminho de rede.

## Os dois níveis de medição

**Nível 1 (`--target ollama`)** bate direto no `/api/chat`. Mede a capacidade de
serving sem a plataforma no caminho. Não precisa de banco nem de seed, então
roda igual em qualquer substrato (Docker local, caixa capada no Railway,
mini-PC). Aproveita `eval_count`/`eval_duration` do próprio Ollama para reportar
tokens/s reais.

**Nível 2 (`--target platform`)** percorre o caminho do aluno: entrar na oficina,
salvar versão, pedir feedback, polling até o estado terminal. Existe para medir
uma patologia específica da implementação atual: `routes/projetos.py:232` agenda
via `BackgroundTasks`, e como `processar_feedback` é síncrono cada pedido cai no
threadpool do anyio. **Não há consumidor limitado, nem FIFO, nem controle de
admissão**: 30 alunos geram até 30 chamadas concorrentes ao Ollama, cada uma
segurando uma thread por até `OLLAMA_TIMEOUT` (240 s). O `config.py:79` já
documenta o efeito desse regime: 48 threads na cota deram 0,1 tok/s contra
42 tok/s com 8 threads, colapso de ~100×.

## Como rodar

O interpretador é o venv do backend (Python 3.12 com httpx):

```bash
cd experiments/capacidade-appliance
PY=../../backend/.venv/bin/python

# nível 1: pico sincronizado da turma
$PY carga.py --target ollama --students 30 --pattern burst --label mac-30-burst

# nível 1: chegada distribuída em 150 s
$PY carga.py --target ollama --students 30 --pattern spread --window 150 \
    --label mac-30-spread150

# nível 1: cota de threads explícita (ver OLLAMA_NUM_THREAD em config.py)
$PY carga.py --target ollama --students 30 --num-thread 8 --label caixa-8thr

# nível 2: fim a fim (exige backend no ar, LLM_PROVIDER=ollama e projeto ABERTO)
$PY carga.py --target platform --students 30 --api http://localhost:8000 \
    --code SCHOOL-8A-XXXX --label e2e-30-burst
```

Saída em `resultados/<label>.jsonl` (uma linha por pedido) e
`resultados/<label>.resumo.json` (percentis e taxas). A análise fica separada da
coleta de propósito: `analise.py` consolida vários resumos numa tabela.

## Substratos

1. **Mac nativo.** Repetível e rápido, serve de controle. **Otimista**: núcleo
   Apple Silicon é bem mais rápido que o do mini-PC alvo.
2. **Docker com cota.** `--cpus` e `--memory` dão teto de núcleos e de RAM. Não
   emulam um processador mais lento: continua sendo núcleo Apple Silicon, só que
   menos deles. Serve para a curva de escalonamento, não para prever o alvo.
3. **Railway capado (x86).** É o substrato representativo da classe de hardware
   alvo (ADR-0018 já mediu ~10 tok/s a 4 vCPU e o colapso por thrashing de
   threads). Sem dados reais de aluno, então é uso permitido.

Por isso o resultado do artigo **não** é "o mini-PC X aguenta N alunos", e sim um
**modelo de dimensionamento** parametrizado pela vazão de decodificação medida:
qualquer pessoa mede um número na máquina dela e lê a capacidade na curva.

## Limitação registrada

Trinta Chromebooks associados ao Wi-Fi de um único mini-PC é um gargalo
**separado** do de computação. O payload de texto é pequeno e não deve limitar,
mas a capacidade de associação de um adaptador comum com 30 clientes não é
medida aqui. Fica declarada como limitação, com recomendação de AP externo.

## Arquivos

- `carga.py`: gerador de carga (níveis 1 e 2, três padrões de chegada)
- `textos.py`: carga de trabalho sintética (não são dados de aluno)
- `prompt_fallback.py`: cópia do prompt para rodar fora do repo
- `analise.py`: consolida os resumos numa tabela comparativa
- `resultados/`: saída bruta e resumos
