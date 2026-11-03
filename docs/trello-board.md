# Board Trello do ecossistema

O board é o painel operacional. O watcher cria um card por prospect qualificado
e o associa ao CID no índice local `prospector/data/prospects.json`. Nenhuma
planilha participa do fluxo. O contrato completo está em `playbook-LP.md`.

## Listas

`Prospects qualificados` e `Triagem` continuam para cards antigos; cards novos
entram diretamente em `Coleta de ativos`. Depois seguem `Em produção` →
`Pronto para envio` ou `QA técnico`. `Contato feito`, `Negociação`, `Fechado`
e `Descartado` são etapas comerciais. O watcher respeita descarte e não
recria cards arquivados.

## Descrição

```text
Prompt:
<direção do usuário para a LP>

Cores:
<paleta/referências>

Dados do prospecto:
<campos coletados no Maps e na classificação>
```

O usuário escreve Prompt e Cores. O watcher preenche os dados do prospecto e
mantém o CID no índice local. A descrição inteira é fornecida ao Claude.

## Três checklists

| Checklist | Quem marca | Significado |
| --- | --- | --- |
| Imagens/assets para a pagina anexadas | Usuário | Um ZIP com `assets/` foi anexado e conferido |
| Cores para a landing page definidas | Usuário | `Cores:` está preenchido |
| Landing page em produção | Watcher | Execução do Claude foi iniciada |

O watcher move para `Pronto para envio` após a criação e QA técnico local. Não existe
checklist `Finalizado`. Para poupar o limite do Claude, o watcher executa as
operações mecânicas no Trello. Checklists antigos são removidos na migração.
