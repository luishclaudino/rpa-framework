# Modelo de configuração do projeto ${project_name}.
# Copie para .env (arquivo NÃO versionado) e ajuste os valores.
RPA3003_ENV=dev
RPA3003_LOG_LEVEL=INFO
# RPA3003_LOG_USER=robo01
# RPA3003_LOG_RETENTION_DAYS=30

# HashiCorp Vault (obrigatório quando ENV_CONFIG define vault_secret_path)
# RPA3003_VAULT_URL=https://vault.empresa.local:8200
# RPA3003_VAULT_AUTH_METHOD=approle
# RPA3003_VAULT_ROLE_ID=
# RPA3003_VAULT_SECRET_ID=
# RPA3003_VAULT_MOUNT_POINT=secret

# Somente dev: arquivo local de segredos (proibido em prd)
# RPA3003_VAULT_LOCAL_SECRETS_FILE=C:/rpa/segredos/${project_name}.secrets.json
