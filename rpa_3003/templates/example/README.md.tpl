# ${project_title}

Projeto RPA `${project_name}` criado em ${created_at} por ${author} com rpa_3003 ${framework_version}.

## Execução

```powershell
rpa3003 run ${project_name} --env dev
rpa3003 run ${project_name} --env prd --timeout 3600
python main.py --env dev
```

## Estrutura

| Caminho | Conteúdo |
|---|---|
| `main.py` | Orquestrador (não altere sem necessidade) |
| `global_vars.py` | Constantes e `ENV_CONFIG` por ambiente; credenciais via Vault |
| `workflows/dev/` | Steps em desenvolvimento |
| `workflows/prd/` | Steps de produção |
| `data/input/`, `data/output/`, `data/images/` | Entradas, saídas e imagens de referência |
| `logs/` | Logs diários e `screenshots/` |

## Configuração

1. Copie `.env.example` para `.env` e ajuste as variáveis (o `.env` não é versionado).
2. Em `global_vars.py`, ajuste `ENV_CONFIG` (URL, navegador, caminho do segredo no Vault).
3. Para produção, solicite à equipe de segurança o segredo em `rpa/${project_name}/prd/sistema` e o AppRole da máquina.

## Promoção de dev para prd

1. Valide a alteração em `dev`.
2. Copie os arquivos alterados de `workflows/dev/` para `workflows/prd/`, ajustando a docstring.
3. Execute `rpa3003 validate ${project_name}` e submeta para revisão de código.

## Códigos de saída

`0` sucesso, `1` falha técnica, `2` uso inválido, `3` configuração, `4` regra de negócio, `5` timeout, `6` já em execução, `130` interrompido.
