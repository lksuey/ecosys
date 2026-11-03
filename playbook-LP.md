# Playbook — ecossistema de prospecção e landing pages

Este documento descreve o fluxo vigente. O Google Sheets e a biblioteca de
`site-patterns/` foram retirados da arquitetura. O Trello é o painel de trabalho;
`prospector/data/prospects.json` é o índice técnico local entre o CID do Maps e o
ID do card. A criação de texto, design e código da landing page é feita por uma
execução individual do Claude Code para cada card pronto.

## 1. Visão geral

```
Google Maps → classificar → qualificar → índice JSON → card Trello
                                                      ↓
                                   Prompt + Cores + dados do prospecto
                                   ZIP com assets/ + 2 checklists humanos
                                                      ↓
                                    watcher inicia Claude Code uma vez por versão dos insumos
                                                      ↓
                     site local + QA desktop/mobile + ZIP no card
                                                      ↓
                                        Pronto para envio
```

O watcher de cards roda a cada cinco minutos e o scanner a cada vinte, em
tarefas Windows independentes. Cada busca percorre no máximo cinco combinações;
o cursor avança apenas após um lote completo. Nenhuma sessão interativa do
Claude precisa permanecer aberta. A máquina precisa estar ligada, com Claude
Code CLI instalado e autenticado.

Dentro de um mesmo tick, o watcher primeiro resolve as ações rápidas (criar
card, mover de lista, checklists) para todos os prospects e só depois inicia
o Claude Code dos cards prontos — em até cinco execuções simultâneas (uma
por card, `ThreadPoolExecutor`). Se você preencher vários cards de uma vez,
o próximo tick os produz ao mesmo tempo, não um atrás do outro. Um lock de
arquivo continua impedindo dois ticks simultâneos; o índice JSON aceita
escritas concorrentes das threads porque cada uma grava só os campos do seu
próprio prospect.

Os dois processos têm locks próprios liberados pelo sistema operacional após
uma queda. O índice JSON tem um lock de escrita compartilhado: cada processo
grava só os registros ou campos alterados, evitando perder mudanças do outro.
O watcher obtém os checklists do board em uma leitura agrupada por ciclo e só
faz chamadas individuais quando precisa criar, corrigir ou marcar um item.

## 2. Prospecção

`categorias.yaml` define as verticais e termos; `bairros.yaml` define a cobertura;
`exclusoes.yaml` define denylist, classes de link e corte de avaliações. Venda de
roupa infantil não é vertical — depende de estoque/catálogo e checkout dentro do
site, modelo diferente do 1:1 que este ecossistema prospecta; reforçado também
por categoria do Maps em `categorias_denylist`. Aluguel (fantasia infantil,
roupa de festa infantil) continua permitido como vertical própria, por ser 1:1.
Prospects qualificados por outras verticais (ex.: vestuário adulto, esporte,
shopping) que a varredura antiga classificou como "moda-infantil" só por causa
do termo de busca não são descartados automaticamente — a vertical do índice é
só a origem da busca, não uma reclassificação confiável do negócio; a decisão de
descartar continua manual (§3, único ato de autoria do usuário no funil). O scanner
usa Playwright/Chrome para ler Google Maps em Teresina e grava `raw.jsonl` por run.
`classify.py` normaliza telefone, bairro e verifica domínios próprios por HTTP.
`qualify.py` exige classe que indica ausência de site próprio e ao menos cinco
avaliações. Negócios com site, fora de Teresina ou na denylist não geram card.

O ledger `seen-places.json` evita reprocessar o mesmo FTID; o índice
`prospects.json` guarda os qualificados e o vínculo CID → card. O watcher cria
card apenas para um CID sem vínculo. A importação inicial das runs locais e dos
IDs antigos do Sheets já foi feita; o Sheets não é consultado em ticks futuros.
Se um card vinculado sumir da listagem de cards abertos, o watcher **não cria
outro**: ele pode estar arquivado ou removido. A reconciliação deve ser manual.

O watcher admite até cinco cards por ciclo e mantém no máximo 40 em **Coleta de
ativos**, **Em produção** ou **QA técnico**. Cards ainda em **Prospects
qualificados** ou **Triagem**, além de prospects locais sem card, contam como
oferta antes de iniciar uma busca nova. **Pronto para envio** e etapas comerciais
não ocupam capacidade. O scanner pode encontrar mais prospects que o teto;
eles aguardam no índice local até existir capacidade de admissão.

## 3. Card do Trello

Cada prospect qualificado tem um card. O watcher cria cards novos diretamente em
**Coleta de ativos**. O título é `<nome> — <bairro>`. A descrição tem esta ordem:

```text
Prompt:
<o usuário escreve o briefing e a direção do site>

Cores:
<cores, códigos hex ou referências cromáticas>

Dados do prospecto:
- CID: ...
- FTID: ...
- Nome: ...
- Vertical: ...
- Categoria no Maps: ...
- Endereço: ...
- Bairro: ...
- Telefone/WhatsApp: ...
- Instagram: ...
- Link informado como site: ...
- Classificação do link: ...
- Nota no Maps: ...
- Número de avaliações: ...
- Latitude/Longitude: ...
- Ficha do Maps: ...
- Data da coleta: ...
- Slug: ...
```

O watcher preenche os dados conhecidos e deixa `Cores:` em branco. `Prompt:` já
nasce com um bloco fixo de requisitos do ecossistema (ver abaixo) — o usuário
escreve a direção específica do negócio depois desse bloco, sem apagá-lo. Dados
ausentes são marcados como "não informado"; o Claude não deve interpretá-los
como fatos. O prompt começa em `Prompt:` e termina antes de `Cores:`; as cores
terminam antes de `Dados do prospecto:`. Os três cabeçalhos devem permanecer em
linhas próprias.

### Requisitos fixos de toda landing page

Todo card novo já nasce com este bloco no início de `Prompt:` (constante
`PROMPT_BASELINE` em `watcher.py`), e o executor reforça os mesmos três pontos
como não-negociáveis em `production._instruction()` — valem mesmo que o
usuário edite ou apague o bloco do card:

1. **Mínimo de 15 animações diferentes** ao longo da página, com variedade
   real (não repetir o mesmo efeito 15 vezes): transição ao rolar, blur
   progressivo, movimento de fundo/parallax, efeito de "digitação" em texto,
   fade/slide, hover states, etc.
2. **Nenhum emoji como ícone**, em lugar nenhum da página — sempre SVG/PNG
   reais (ícone de biblioteca aberta, ou logo oficial como WhatsApp/e-mail
   quando fizer sentido).
3. **Botão flutuante de WhatsApp com a animação de "revelação circular" do
   botão oficial "Open app"** da página de redirecionamento do WhatsApp
   (wa.me/`#action-button._9u4i`): `position: relative` + `overflow: hidden`
   no botão; um `::after` circular atrás do texto (`z-index: -1`) em repouso
   com `transform: scale(1) translateY(67px)`; ao ativar (hover no desktop,
   classe equivalente via JS no mobile, já que toque não tem hover), anima
   para `transform: scale(1.47) translateY(0)` com
   `transition: transform 1s cubic-bezier(.66,0,.34,1)`, revelando uma cor de
   preenchimento diferente enquanto o texto muda de cor com delay de `.5s`.
   É transição de revelação ao interagir, não um pulso em loop.

O ponto 3 reproduz a mecânica do botão oficial de redirecionamento do WhatsApp: uma
transição de revelação ao interagir, não um pulso em loop.

### Checklists — exatamente três

1. **Imagens/assets para a pagina anexadas** — manual, item "ZIP de assets
   anexado e conferido". O usuário marca após anexar e conferir o ZIP.
2. **Cores para a landing page definidas** — manual, item "Cores preenchidas
   na descrição". O usuário marca após preencher `Cores:`.
3. **Landing page em produção** — automático, item "Produção iniciada". O
   watcher marca quando o Claude começa. Nenhuma chamada adicional do Claude
   ao MCP é gasta com isso.

Não existe checklist "Finalizado": o card em **Pronto para envio** representa a
conclusão da construção. Os checklists legados são removidos na migração. O
watcher nunca marca os dois checklists humanos.

## 4. ZIP de entrada

O Trello limita cada anexo a 10 MB; um card com mais mídia pode anexar **vários
ZIPs de insumos**, cada um contendo somente a pasta `assets/` na raiz — o watcher
baixa todos os ZIPs anexados (exceto o ZIP de saída do próprio site) e mescla o
conteúdo de `assets/` de todos num único diretório antes de acionar o Claude. Em
caso de nome de arquivo repetido entre ZIPs, o anexo mais recente prevalece.
Exemplo de um ZIP:

```text
insumos.zip
└── assets/
    ├── logo.png
    ├── fachada.jpg
    ├── produto-01.webp
    └── video-01.mp4
```

São aceitos JPG, JPEG, PNG, WebP, GIF, MP4 e MOV, inclusive subpastas dentro de
`assets/`. Pelo menos um dos ZIPs precisa ter ao menos uma imagem ou vídeo. Fotos
dos sites vêm somente desses ZIPs; imagens do Maps não entram como fallback. O
watcher ignora o ZIP de saída `<slug>-<8 primeiros caracteres do ID do card>.zip`,
limita quantidade e tamanho descompactado somados de todos os ZIPs do card e
rejeita caminhos fora de `assets/`. Um novo ZIP, a remoção de um ZIP ou edição do
prompt altera a impressão digital do trabalho (calculada sobre a descrição e o
conjunto de IDs de todos os ZIPs de insumos anexados). Se o mesmo card voltar
para Coleta com os mesmos insumos já produzidos, o watcher o devolve a **Pronto
para envio** sem chamar Claude novamente. Para refazer, edite o Prompt ou
anexe/troque um ZIP.

Para vídeos, o executor extrai alguns frames candidatos. O Claude pode escolher
um bom frame para compor a página; o MP4 original é preservado. Dependendo do
plano Trello, o limite por anexo pode exigir reduzir o tamanho do ZIP antes do
upload. Não use fotos ou marcas d'água sem ter direito de uso.

## 5. Condições para iniciar o Claude

O watcher só inicia a produção se **todas** forem verdadeiras:

- card em **Coleta de ativos**;
- `Prompt:` preenchido;
- `Cores:` preenchido;
- os dois checklists manuais marcados;
- ZIP de entrada anexado e validado, contendo `assets/`;
- comando `claude` disponível no PATH e autenticado.

Ao iniciar, o watcher move o card para **Em produção** e marca o terceiro
checklist. Claude Code roda em modo não interativo (`claude -p`) em um diretório
de trabalho por prospect. Ele lê a descrição inteira e os assets, produz copy,
HTML, CSS e JS e escreve o site. A integração com o Trello continua sob controle
do watcher; o Claude não precisa de chamadas MCP para atualizar checklists ou
listas. O executor restringe as ferramentas do Claude a leitura e edição de
arquivos e não habilita ferramentas MCP nessa sessão.

O site deve ser único para o negócio, responsivo, acessível e coerente com os
assets e cores. Não há templates de layout obrigatórios. O Claude pode usar os
frames extraídos de vídeo. O principal arquivo é `index.html`; arquivos de
mídia ficam em `assets/`. Dados que não estiverem confirmados devem ser
sinalizados em `Handoff.md`, sem inventar preços, horários, nomes de produtos,
clientes ou avaliações. CTAs para WhatsApp usam apenas número confirmado.

## 6. Saída, QA e estados

O trabalho temporário fica em `prospector/data/jobs/<slug>-<8 primeiros caracteres do ID do card>/`. Após o
Claude terminar, o QA verifica título, `<h1>`, viewport, copy mínima, arquivos
locais, uso dos assets e âncoras. Ele abre a página em navegador headless em
desktop e mobile, verifica imagens quebradas, erros JavaScript e rolagem
horizontal e gera `_qa/desktop.png`, `_qa/mobile.png` e `_qa/report.md`.
Esse QA não substitui revisão humana dos fatos, do visual, dos links externos
ou de aparelhos reais.

Se o QA passar, a versão nova vai para `Landing Pages/<slug>-<8 primeiros caracteres do ID do card>/`, a
anterior é guardada em `prospector/data/jobs/<slug>-<8 primeiros caracteres do ID do card>/previous-*`, o ZIP é anexado ao card e
o watcher move para **Pronto para envio**. Essa lista quer dizer "construção
feita", não "site publicado". Publicação, domínio real, Open Graph final,
preview social e contato comercial continuam manuais.

Falha do Claude, execução interrompida ou QA pendente leva a **QA técnico** com comentário
explicando o erro. Para tentar de novo, corrigir o insumo e mover o card de
volta a **Coleta de ativos**. Erro no ZIP ou ausência do CLI mantém o card em
Coleta e gera aviso uma única vez por condição. Cards em Descartado, Fechado e
Pronto para envio não são reconstruídos automaticamente.

## 7. Regras de conteúdo e publicação

- O site é uma proposta de prospecção antes do primeiro contato com o negócio.
- Usar informação pública como ponto de partida; apresentar inferências como
  pendentes, não como fatos confirmados.
- Não copiar literalmente avaliações de terceiros nem inventar depoimentos.
- Não remover marca d'água de terceiro sem autorização.
- Confirmar grafia oficial, endereço e WhatsApp antes de apresentar ao cliente.
- Antes de publicar, conferir manualmente acessibilidade, visual mobile,
  links, HTTPS, SEO local, URL canonical, OG image física 1200×630 e preview
  real no WhatsApp/Facebook. A URL do domínio só é conhecida nessa etapa.
- O deploy pode continuar por Netlify Drop, mas não é executado pelo watcher.

## 8. Operação e recuperação

Instalar dependências: `python -m pip install -r prospector/requirements.txt`,
`python -m playwright install chromium`; instalar/autenticar Claude Code CLI.
`python -m prospector.cli check` valida as configurações. `watch --slug X`
filtra o tick por slug sem iniciar nova prospecção; `watch` faz um tick completo.
`prospect` roda um lote do scanner. `start.bat` habilita as duas tarefas Windows;
`stop.bat` desabilita ambas. `start.bat` exige o comando `claude` no PATH antes
de criar as tarefas. O fluxo completo com Claude Code depende do CLI instalado e autenticado; a ativação
das tarefas agendadas só deve ocorrer depois de um card de teste passar de ponta a ponta.

Fazer backup de `prospector/data/prospects.json` e `seen-places.json`; sem eles,
um cartão com descrição editada pode perder a reconciliação CID → card.
`runs/*/prospects.jsonl` permite reconstruir os dados de prospecção, mas o
vínculo final com o card deve ser recuperado do backup do índice ou do Trello.
Segredos ficam apenas em `prospector/.secrets/` e nunca entram no Git.
