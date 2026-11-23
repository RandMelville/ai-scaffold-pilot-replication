"""Carga de trabalho sintética: 30 textos distintos de 8º ano, um por aluno.

Não são dados de aluno. São fábulas curtas escritas para este experimento, com
os problemas de coesão e coerência que o feedback de Koch (ADR-0019) procura:
repetição lexical, pronome ambíguo, conectivo contraditório, marcador temporal
inconsistente.

POR QUE 30 E NÃO 6 (correção de 29/07/2026): a primeira versão ciclava 6 textos
base com sufixo. Dentro de uma rodada isso dava 30 strings distintas, mas ENTRE
rodadas as mesmas 30 se repetiam, e o servidor de modelo servia prefixo do cache.
Medido: o prefill caiu para 0,041 s em 449 tokens de prompt, ou seja, o prompt
inteiro vinha do cache, e o tempo de serviço medido ficou otimista. Agora há um
texto distinto por aluno, e cada um **começa** com uma frase diferente, porque o
cache é de prefixo: variação no fim do texto não o invalida.

O prompt de sistema (~250 tokens) continua compartilhado e cacheado, e isso é
fiel: em produção ele é o mesmo para a turma toda.
"""

from __future__ import annotations

TEXTS: list[str] = [
    """A raposa e o corvo esperto
Era uma vez uma raposa que estava com muita fome. A raposa andou pela floresta e
a raposa viu um corvo com um queijo no bico. A raposa pensou que ia conseguir o
queijo fácil. Então ela falou que ele era o pássaro mais bonito da floresta e que
ele devia cantar. Mas o corvo já tinha ouvido essa história na escola dele. Ele
olhou para baixo e não abriu o bico. Depois de um tempo a raposa foi embora com
fome. No dia seguinte, que era o mesmo dia, o corvo contou para os amigos e todos
acharam engraçado. Ele aprendeu que elogio demais esconde alguma coisa.""",
    """O menino que corria demais
Tinha um menino chamado Téo que corria muito rápido. Téo corria para a escola,
Téo corria no recreio e Téo corria na volta para casa. Um dia a professora falou
que ele devia prestar atenção nas coisas. Porém ele achou a ideia boa e continuou
correndo do mesmo jeito. Numa terça de manhã ele estava correndo e não viu um
buraco na calçada. Ele caiu e machucou o joelho. Enquanto estava sentado no chão,
ele reparou que tinha uma figueira enorme na rua que ele passava todo dia e nunca
tinha visto. Ontem ele voltou a correr, mas agora ele para de vez em quando.""",
    """A tartaruga que queria voar
Uma tartaruga chamada Dona Rita morava perto de um lago. Ela via os pássaros
voando e ficava pensando nisso o tempo todo. Ela pediu ajuda para uma águia e a
águia disse que ia carregar ela. A águia pegou a tartaruga e subiu bem alto. Lá
em cima ela viu o lago pequenininho e as árvores pequenininhas e ficou feliz. Mas
ela também ficou com medo porque estava muito longe do chão. Quando desceu, falou
para as amigas que não queria mais voar, queria só saber como era. As amigas não
acreditaram nela. Contudo ela não ligou, porque sabia o que tinha visto.""",
    """O rio que mudou de lugar
No lugar onde meu avô morava tinha um rio que passava atrás das casas. As
crianças brincavam no rio e as famílias pegavam água no rio. Aí construíram uma
fábrica perto do rio. A água ficou escura e ninguém mais entrava. Meu avô disse
que o rio não foi embora, ele continuou lá, quem foi embora foram as pessoas.
Anos depois um grupo de moradores começou a limpar as margens todo sábado.
Primeiro tiraram o lixo grande. Semana passada plantaram mudas na beira. Hoje dá
para ver alguns peixes, mas ainda não dá para nadar.""",
    """A luz da vila
Na vila onde a Zaira morava a luz caía toda vez que chovia forte. Quando a luz
caía a vila inteira ficava escura e as pessoas acendiam velas. A Zaira tinha doze
anos e gostava de mexer com as coisas. Ela juntou uma bateria velha, uns fios e
uma lâmpada pequena que achou no ferro-velho. Ela montou uma luminária que acendia
sozinha quando a luz caía. A vizinha pediu uma igual. Depois o vizinho da esquina
pediu outra. No fim do mês ela já tinha feito onze e ensinou três meninas a
montarem também. A chuva continua derrubando a luz, entretanto agora a vila não
fica no escuro.""",
    """O cachorro que guardava a padaria
O Bilu era um cachorro que ficava na porta da padaria do seu Nelson. O Bilu não
era o cachorro do seu Nelson, o Bilu era um cachorro de rua que escolheu ficar
ali. De manhã cedo ele já estava sentado esperando abrir. Ele latia quando alguém
estranho chegava perto das caixas de pão. Um dia entrou um homem correndo e pegou
o dinheiro do caixa. O Bilu correu atrás dele e latiu até a esquina. O homem
deixou o dinheiro cair na calçada e fugiu. Desde esse dia o seu Nelson colocou uma
placa escrito gerente perto da porta.""",
    """Meu vizinho conserta tudo
O senhor Aparício mora do outro lado do muro e ele conserta tudo. Ele conserta
ventilador, ele conserta rádio antigo e ele conserta bicicleta. A garagem dele é
cheia de peça pequena guardada em pote de sorvete. Eu levei um fone que tinha
parado de funcionar de um lado. Ele olhou, cortou o fio e soldou na frente de mim
explicando cada passo. Levou seis minutos. Depois ele falou que jogar fora é fácil
e entender é difícil. Mas eu fiquei pensando nisso a semana toda e agora eu abro
as coisas antes de jogar fora.""",
    """A menina que não gostava de segunda-feira
A Ivone odiava segunda-feira. Na segunda ela acordava com sono, na segunda a aula
parecia mais longa e na segunda o ônibus atrasava. Uma segunda de manhã ela
resolveu contar quantas coisas boas aconteciam no dia. Ela anotou num caderninho
pequeno. Ela contou o cachorro do portão, contou o pão de queijo da cantina e
contou uma piada do Caio. Deu onze coisas. Na terça ela contou de novo e deu nove.
Então ela descobriu que o problema não era a segunda, o problema era ela não estar
olhando.""",
    """O time da rua de baixo
Nossa rua sempre perdia do time da rua de baixo. Eles eram mais altos e treinavam
mais. A gente jogava do mesmo jeito todo sábado e perdia do mesmo jeito. Aí o
Dedé falou para a gente parar de chutar de longe. A gente começou a tocar a bola
mais perto e esperar o erro deles. No primeiro sábado perdemos de dois a um, o que
já era bom. No sábado seguinte empatamos. Ninguém ficou mais alto nesse mês, só
mudou o que a gente fazia com a bola.""",
    """A árvore da praça
Na praça perto da minha casa tem uma árvore muito velha. Minha mãe brincava
embaixo dessa árvore quando ela era criança. O tronco dela é largo e cheio de nome
escrito com canivete. No ano passado a prefeitura ia cortar a árvore para fazer
estacionamento. As pessoas do bairro fizeram uma fila em volta e não saíram. Um
jornal foi lá e mostrou na televisão. A árvore ficou. Agora tem uma placa dizendo
que ela é patrimônio, mas quem salvou ela foi a fila de gente.""",
    """O caderno esquecido no ônibus
Eu esqueci meu caderno de matemática no ônibus na quarta de manhã. Nesse caderno
tinha toda a matéria da prova e tinha os exercícios que a professora corrigiu. Eu
chorei quando percebi. Minha tia falou para eu ir na garagem da empresa perguntar.
O motorista chamado Reginaldo tinha guardado numa caixa junto com dois guarda-chuva
e um casaco. Ele nem sabia de quem era. Eu peguei meu caderno de volta e a prova
foi na sexta. Desde então eu escrevo meu nome e meu telefone em tudo.""",
    """A galinha que contava
No quintal da minha avó tinha uma galinha que a gente chamava de Contadora. Ela
botava um ovo por dia e ela sempre botava no mesmo canto do galinheiro. Um dia o
ovo não estava lá. Minha avó falou que galinha não erra lugar, quem erra é gente
que não procura. A gente procurou e achou o ovo atrás do tanque, escondido no
mato. Tinha um gato novo no quintal e a galinha estava escondendo. Ela não sabia
contar, mas sabia esconder melhor que a gente sabia procurar.""",
    """O celular do meu irmão
Meu irmão mais velho ganhou um celular novo e ele não deixava ninguém pegar. Ele
levava o celular para o banheiro e ele dormia com o celular embaixo do travesseiro.
Uma noite o celular caiu atrás da cama e ficou lá dois dias. Nesses dois dias ele
jogou bola comigo, ele ajudou minha mãe na louça e ele assistiu um filme inteiro
sem pausar. Quando achou o celular ele ficou muito feliz. Só que ele contou depois
que aqueles dois dias foram bons e ele não entendeu bem o porquê.""",
    """A feira de sábado
Todo sábado tem feira na minha rua e todo sábado o seu Válter grita o preço da
manga. A voz dele é a mais alta da feira inteira. Eu perguntei por que ele grita
tanto se todo mundo já sabe onde é a banca dele. Ele riu e falou que não grita
para vender, grita porque gosta. Depois ele me deu uma manga e falou para eu
gritar junto. Eu gritei baixinho e ficou ruim. Ele falou que a gente aprende a
gritar do jeito que a gente é.""",
    """Quando faltou água
Faltou água no prédio por quatro dias em janeiro. No primeiro dia foi engraçado e
todo mundo brincou no grupo do zap. No segundo dia ninguém brincou mais. A gente
descia com balde até a torneira da garagem que ainda tinha pressão. A dona Cleide
do quinto andar não conseguia descer por causa do joelho. Aí três meninos do
segundo andar começaram a subir balde para ela e para mais duas pessoas idosas.
Voltou a água no quinto dia. Os meninos continuaram subindo compra para a dona
Cleide até hoje.""",
    """A prova de português
A professora avisou da prova com duas semanas de antecedência. Eu estudei na noite
anterior porque eu sempre estudo na noite anterior. Na prova tinha um texto grande
para interpretar e eu li rápido demais. Eu respondi o que eu achei que o texto
dizia e não o que ele dizia. Tirei cinco. A professora escreveu do lado que eu
tinha entendido o assunto e não o texto. Na prova seguinte eu li duas vezes antes
de escrever qualquer coisa e tirei oito, com o mesmo tanto de estudo.""",
    """O carrinho de pipoca
Na frente da escola tem um carrinho de pipoca que abre às cinco da tarde. O homem
do carrinho chama Osvaldo e ele conhece o nome de quase todo aluno. Ele sabe quem
gosta de sal e quem gosta de doce sem precisar perguntar. Um dia ele não apareceu
e ninguém sabia explicar. Ficou uma semana sem carrinho e a saída da escola ficou
estranhamente silenciosa. Ele voltou depois de operar a vista. A fila no dia da
volta deu a volta no muro.""",
    """A bicicleta emprestada
O Juninho me emprestou a bicicleta dele por uma tarde. A bicicleta era vermelha e
tinha um adesivo de banda no quadro. Eu andei até a pracinha e deixei encostada no
poste enquanto eu tomava água. Quando voltei o pneu de trás estava furado. Eu não
sabia se tinha sido eu ou se já estava assim. Eu levei na borracharia e paguei com
o dinheiro do meu lanche. Contei tudo para ele. Ele falou que o pneu já estava
ruim, mas que gostou de eu ter contado antes de ele perguntar.""",
    """O gato que escolheu a casa
Apareceu um gato cinza na nossa varanda em junho. Ninguém chamou esse gato e
ninguém deu comida no começo. Ele ficou três dias sentado olhando pela janela.
Minha mãe falou que não ia ter gato nessa casa. No quarto dia ela colocou um
pratinho de leite escondido. No sétimo dia o gato dormia no sofá. Hoje o gato tem
nome, tem coleira e tem uma caminha que minha mãe costurou. Ela ainda diz que não
queria gato.""",
    """A festa junina da escola
A festa junina desse ano quase não aconteceu porque choveu na semana toda. A
diretora falou que ia cancelar. Aí o pessoal do grêmio pediu para fazer no
ginásio. O ginásio é menor e não caberia todas as barracas. Então a gente cortou
as barracas de nove para cinco e juntou as comidas parecidas numa só. Ficou
apertado e ficou quente. Mesmo assim foi a festa mais cheia dos últimos anos,
porque quem foi ficou até o fim em vez de circular.""",
    """Meu primeiro dia de aula nova
Eu mudei de escola no meio do ano e eu não conhecia ninguém. No primeiro dia eu
sentei no fundo e não falei com pessoa nenhuma. No segundo dia uma menina chamada
Perla perguntou se eu queria a metade do sanduíche dela. Eu não estava com fome,
porém eu aceitei. A gente ficou conversando o recreio inteiro sobre um jogo que as
duas jogavam. Hoje ela é minha melhor amiga da escola. Metade de um sanduíche
resolveu um problema que eu achei que ia durar o ano.""",
    """O muro pintado
Tinha um muro grande e cinza do lado da quadra e sempre aparecia pichação nele. O
zelador pintava de cinza de novo e em uma semana voltava a pichação. Isso durou
uns dois anos. Aí a professora de artes propôs pintar um mural com a turma. A
gente pintou peixe, onda e uma baleia grande no meio. Faz oito meses e ninguém
pichou em cima. O muro continua igual de grande, só deixou de ser cinza.""",
    """A avó e o telefone
Minha avó aprendeu a usar videochamada aos setenta e nove anos. No começo ela
segurava o telefone de um jeito que a gente só via o teto. Ela desligava sem
querer e ela ligava de novo achando que tinha travado. Meu primo escreveu num
papel os três passos e colou na parede da cozinha dela. Hoje ela liga para as
irmãs dela no Ceará toda quinta. Ela ainda segura errado às vezes, mas agora ela
sabe consertar sozinha.""",
    """O campeonato de tabuada
Na quinta série a professora fazia campeonato de tabuada na sexta-feira. Eu era
muito ruim e eu sempre caía na primeira rodada. Eu decorei do jeito que dava e
continuei caindo. Aí meu pai me mostrou que sete vezes oito é a mesma coisa que
oito vezes sete, o que cortava metade do que eu precisava decorar. Depois ele
mostrou que nove vezes qualquer coisa dá para fazer na mão. Eu não ganhei o
campeonato, mas eu passei da terceira rodada e isso já foi festa.""",
    """A horta da escola
A gente fez uma horta no canteiro atrás da cozinha da escola. Plantamos alface,
plantamos cebolinha e plantamos tomate. A alface nasceu rápido e a cebolinha
nasceu rápido também. O tomate não nasceu de jeito nenhum. A gente regava todo dia
e mesmo assim nada. Depois a merendeira falou que aquele canto pega sol só de
manhã e tomate quer sol o dia inteiro. Mudamos o tomate para o outro lado do muro
e em três semanas apareceu o primeiro.""",
    """O ônibus das seis
Eu pego o ônibus das seis da manhã e nesse ônibus tem sempre as mesmas pessoas. Um
homem de terno que dorme em pé, duas moças de uniforme de hospital e um senhor com
uma pasta de couro. Ninguém conversa e ninguém precisa. Um dia o senhor da pasta
não subiu e as duas moças se olharam preocupadas. Ele voltou na semana seguinte com
um gesso no braço. As moças bateram palma quando ele subiu. Depois voltou tudo ao
silêncio de sempre, só que um silêncio diferente.""",
    """A chuva de granizo
Caiu granizo na cidade em outubro e foi a primeira vez que eu vi. As pedras eram
do tamanho de bola de gude e batiam no telhado igual pipoca. Durou uns cinco
minutos e parou de repente. Depois a rua ficou branca como se tivesse nevado.
Todo mundo saiu para pegar as pedras com a mão. Meu vizinho perdeu a claraboia do
banheiro e a árvore da esquina perdeu quase toda folha. Ficou bonito e ficou caro
ao mesmo tempo.""",
    """O livro que ninguém pegava
Na biblioteca da escola tem um livro de capa marrom que ninguém pegava nunca. A
ficha dele estava vazia desde dois mil e dezenove. Eu peguei por curiosidade e por
pena. O livro conta a história de uma menina que atravessa o sertão procurando o
pai. Eu li em dois dias e chorei no fim. Eu contei para quatro pessoas da minha
turma. Agora a ficha do livro tem sete nomes e ele fica sempre emprestado.""",
    """A mudança de casa
A gente mudou de casa em fevereiro e eu tive que dividir quarto com minha irmã. Eu
reclamei muito antes da mudança. Eu fiz até uma lista dos motivos e mostrei para
minha mãe. No começo foi ruim mesmo, porque ela dorme tarde e eu durmo cedo. A
gente combinou um horário de luz apagada e ela passou a usar fone. Hoje eu não
troco mais, porque a gente conversa no escuro antes de dormir e isso não existia
quando cada uma tinha o seu quarto.""",
    """O relógio do meu pai
Meu pai tem um relógio de pulso que era do pai dele. O relógio não funciona e
nunca funcionou desde que eu nasci. Ele usa mesmo assim, todo dia, no braço
esquerdo. Eu perguntei por que usar relógio parado. Ele falou que o relógio não
serve para ver a hora, serve para lembrar de quem deu. Depois ele mostrou onde
tinha um risco no vidro e contou a história do risco. Eu nunca mais achei estranho
um relógio parado no braço de alguém.""",
]

MAX_ALUNOS = len(TEXTS)


def text_for(index: int, variant: int = 0) -> str:
    """Devolve o texto do aluno ``index``, na variante ``variant``.

    Não cicla: o experimento tem 30 alunos e 30 textos. Ciclar reintroduziria o
    reuso de prefixo que contaminou a primeira medição (ver docstring do módulo).

    POR QUE A VARIANTE (correção de 29/07/2026, segunda ocorrência do mesmo erro):
    textos distintos por aluno resolvem o cache DENTRO de uma rodada, mas as
    repetições reusavam os mesmos 30 textos, então o servidor respondia do cache
    a partir da segunda. Medido: prefill mediano de 1,210 s na primeira rodada
    contra 0,027 s na segunda e 0,026 s na terceira. Numa turma real o prefill é
    sempre frio, porque cada aluno escreve o próprio texto uma vez, então a
    primeira rodada era a única válida.

    A variante altera a PRIMEIRA linha, e isso é deliberado: o cache é de
    prefixo, então mudar o fim do texto não o invalidaria. O prompt de sistema
    continua compartilhado e cacheado, que é o regime fiel à produção.
    """
    if index >= MAX_ALUNOS:
        raise ValueError(
            f"há {MAX_ALUNOS} textos distintos e o aluno {index} foi pedido. "
            "Acrescente textos em vez de ciclar, senão o cache de prefixo do "
            "servidor de modelo falseia o tempo de serviço."
        )
    texto = TEXTS[index]
    if variant <= 0:
        return texto
    titulo, _, corpo = texto.partition("\n")
    return f"{titulo} (versão {variant + 1})\n{corpo}"
