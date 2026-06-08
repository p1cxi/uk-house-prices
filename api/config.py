import os

DB_CONFIG = {
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': int(os.getenv('POSTGRES_PORT', 5432)),
    'dbname': os.getenv('POSTGRES_DB', 'house_prices'),
    'user': os.getenv('POSTGRES_USER', 'prices'),
    'password': os.getenv('POSTGRES_PASSWORD'),
}

# llama.cpp server (OpenAI-compatible API). LLM_HOST should point at the
# server root; the client appends /v1/chat/completions.
LLM_HOST = os.getenv('LLM_HOST', 'http://localhost:8080')
LLM_MODEL = os.getenv('LLM_MODEL', 'Qwen3-8B-Q4_K_M.gguf')

# Dedicated least-privilege connection for the /ask analytics agent. Same host/db
# as DB_CONFIG but a read-only role (agent_ro) — see postgres/init/02_agent_readonly.sql.
# Every DB access the LLM can influence (typed tools AND run_sql) uses this.
AGENT_DB_CONFIG = {
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': int(os.getenv('POSTGRES_PORT', 5432)),
    'dbname': os.getenv('POSTGRES_DB', 'house_prices'),
    'user': os.getenv('AGENT_DB_USER', 'agent_ro'),
    'password': os.getenv('AGENT_DB_PASSWORD'),
}

# EPC (Energy Performance Certificate) data. Initial full load is a one-time manual bulk
# download (GOV.UK One Login) into EPC_DIR; quarterly top-ups use the developer API with
# HTTP Basic auth (email + API key) — kept ONLY in gitignored .env (repo is public).
EPC_DIR = os.getenv('EPC_DIR', './epc_data')
EPC_API_BASE = os.getenv('EPC_API_BASE', 'https://epc.opendatacommunities.org/api/v1/domestic/search')
EPC_API_EMAIL = os.getenv('EPC_API_EMAIL')
EPC_API_KEY = os.getenv('EPC_API_KEY')

# /ask agent loop tuning.
AGENT_MAX_STEPS = int(os.getenv('AGENT_MAX_STEPS', 3))        # max tool calls per question
AGENT_MAX_ROWS = int(os.getenv('AGENT_MAX_ROWS', 25))        # rows per observation fed back to LLM
AGENT_SQL_ROW_LIMIT = int(os.getenv('AGENT_SQL_ROW_LIMIT', 500))  # hard LIMIT for run_sql
AGENT_TOOL_MODE = os.getenv('AGENT_TOOL_MODE', 'structured')  # structured | native
