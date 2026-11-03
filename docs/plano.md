# Arquitetura atual do ecossistema

## Componentes

| Componente | Responsabilidade |
| --- | --- |
| `scan_maps.py` | Varredura por vertical e local; deduplicação por FTID |
| `classify.py` / `qualify.py` | Classificação do link, critérios e descartes |
| `store.py` | Índice JSON local de prospects, CID e ID do card; escrita atômica |
| `prospect_cycle.py` | Busca em lotes de até cinco combinações quando falta oferta |
| `trello.py` | Cliente da API limitado ao board configurado |
| `watcher.py` | Admissão de até cinco cards por ciclo, gates, produção e recuperação |
| `production.py` | ZIP, Claude, QA desktop/mobile e empacotamento |

O Trello é a interface de operação. O JSON local é um índice técnico, não um
segundo painel de gestão. O Google Sheets foi removido após importar os dados
anteriores; o antigo `site-patterns/` já havia sido removido por decisão do
usuário. `playbook-LP.md` é a regra de operação e conteúdo.

## Estado de um prospect

1. **Qualificado:** aparece em `prospects.json`; card ainda não criado.
2. **Coleta de ativos:** card criado, espera Prompt, Cores, ZIP e duas marcas
   manuais.
3. **Em produção:** Claude Code iniciado pelo watcher; checklist automático
   marcado.
4. **QA técnico:** produção falhou, foi interrompida ou o QA encontrou uma pendência.
5. **Pronto para envio:** site e ZIP gerados e anexados. Revisão final e deploy
   são feitos manualmente.
6. **Contato feito / Negociação / Fechado / Descartado:** gestão comercial.

## Dados e recuperação

O CID/FTID é a chave de deduplicação e o ID do card é armazenado localmente.
Runs `raw.jsonl`, `classified.jsonl` e `prospects.jsonl` preservam a trilha da
prospecção. `prospects.json` deve ser incluído no backup local; não vai para o
Git por conter dados de contato. Checklists legados são removidos dos cards
na migração.

Um card com ID conhecido mas ausente da listagem de cards abertos nunca é
recriado automaticamente. O operador deve verificar se foi arquivado ou
excluído. Uma falha de produção só é repetida depois que o card volta a
`Coleta de ativos`.

Scanner e watcher têm locks de processo independentes. Ambos gravam o índice
sob um lock curto compartilhado. O scanner salva um cursor após cada lote
concluído; um lote que falha é repetido, mas os registros já coletados são
importados. O watcher recupera cards interrompidos em `Em produção` para
`QA técnico`, sem consumir Claude novamente.

## Condições para ativar a tarefa agendada

- Claude Code CLI instalado, autenticado e acessível como `claude`.
- `python -m prospector.cli check` passa.
- Um card de teste com `Prompt:`, `Cores:`, ZIP `assets/` e dois checklists
  manuais passa por Coleta → Em produção → Pronto para envio.
- O QA local gera capturas desktop/mobile; o site e o ZIP são revisados.
- A migração de cards existentes mantém no máximo três checklists por card.

Cards arquivados em Descartado foram removidos do Trello por decisão do usuário;
o watcher ignora cards que estejam nessa lista.

O agendador Windows permanece desligado durante a reestruturação. Habilitar
com `start.bat` somente após essa validação. O watcher não publica sites nem
faz contato comercial.
