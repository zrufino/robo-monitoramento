"""
Sondagem 2 — perguntas específicas que sobraram da primeira rodada.

1. Quantitativos só tem operação 'Compra' ou existem outras (Saída, Baixa, etc)?
2. Quantos itens TOTAIS existem em /equipamentos? (dimensionar o robô)
3. Como funcionam filtros de data em /rotas?
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.dosistemas.com.br/qualy/api"
TOKEN = os.getenv("DOSISTEMAS_TOKEN")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

SAIDA = Path("amostras_api")
SAIDA.mkdir(exist_ok=True)

TS = datetime.now().strftime("%Y%m%d_%H%M%S")


def salvar(nome, dados):
    caminho = SAIDA / f"{nome}.json"
    with caminho.open("w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    return caminho


def get(nome, path, params=None, mostrar_amostra=False):
    url = f"{BASE_URL}{path}"
    print(f"\nGET {path}  params={params or {}}")
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=120)
    except requests.RequestException as exc:
        print(f"  ERRO: {exc}")
        return None
    print(f"  status: {resp.status_code}")
    try:
        dados = resp.json()
    except ValueError:
        dados = {"raw_text": resp.text}
    caminho = salvar(nome, dados)
    if isinstance(dados, list):
        print(f"  itens: {len(dados)}")
    elif isinstance(dados, dict):
        if "total_items" in dados:
            print(f"  total_items: {dados.get('total_items')}")
        if "data" in dados and isinstance(dados["data"], list):
            print(f"  itens em data: {len(dados['data'])}")
    print(f"  salvo: {caminho}")
    return dados


# ============================================================
# PERGUNTA 1 — Quantitativos: existem outras operações além de Compra?
# ============================================================
print("=" * 60)
print("PERGUNTA 1 — Existem outras operações em /quantitativos?")
print("=" * 60)

# 1a. Sem ordenação (default)
get(f"{TS}_q1a_quantitativos_default", "/cadastros/equipamentos/quantitativos",
    {"first": "50"})

# 1b. Ordem ascendente por data (registros mais antigos)
get(f"{TS}_q1b_quantitativos_asc", "/cadastros/equipamentos/quantitativos",
    {"first": "50", "sort_by": "asc(data)"})

# 1c. Filtro por item conhecido com muita movimentação (MARTELO ROMP. 5KG)
get(f"{TS}_q1c_quantitativos_por_codigo", "/cadastros/equipamentos/quantitativos",
    {"first": "100", "codigo": "001.001"})

# 1d. Tenta filtrar por operacao (se a API aceitar)
get(f"{TS}_q1d_quantitativos_saida", "/cadastros/equipamentos/quantitativos",
    {"first": "20", "operacao": "Saida"})

get(f"{TS}_q1e_quantitativos_baixa", "/cadastros/equipamentos/quantitativos",
    {"first": "20", "operacao": "Baixa"})

# ============================================================
# PERGUNTA 2 — Quantos itens TOTAIS existem em /equipamentos?
# ============================================================
print()
print("=" * 60)
print("PERGUNTA 2 — Dimensionamento de /equipamentos")
print("=" * 60)

# 2a. Tentar paginar e ver se vem metadado de total
get(f"{TS}_q2a_equip_page1", "/cadastros/equipamentos",
    {"first": "1", "page": "1"})

# 2b. Só itens de patrimônio (tipo=IT) — pra dimensionar o que realmente importa
get(f"{TS}_q2b_equip_tipo_IT", "/cadastros/equipamentos",
    {"first": "5", "tipo": "IT"})

# 2c. Só grupos (tipo=GR) — visão de alto nível
get(f"{TS}_q2c_equip_tipo_GR", "/cadastros/equipamentos",
    {"first": "100", "tipo": "GR"})

# 2d. Só subgrupos (tipo=SG)
get(f"{TS}_q2d_equip_tipo_SG", "/cadastros/equipamentos",
    {"first": "5", "tipo": "SG"})

# ============================================================
# PERGUNTA 3 — Como filtrar /rotas por data?
# ============================================================
print()
print("=" * 60)
print("PERGUNTA 3 — Filtros de data em /rotas")
print("=" * 60)

hoje = datetime.now()
data_30d = (hoje - timedelta(days=30)).strftime("%Y-%m-%d")
data_7d = (hoje - timedelta(days=7)).strftime("%Y-%m-%d")
hoje_str = hoje.strftime("%Y-%m-%d")

# 3a. Tentativa com parâmetros comuns
get(f"{TS}_q3a_rotas_data_inicial", "/rotas",
    {"page_size": "10", "data_inicial": data_30d, "data_final": hoje_str})

# 3b. Outro formato comum
get(f"{TS}_q3b_rotas_dt", "/rotas",
    {"page_size": "10", "dt_inicial": data_7d, "dt_final": hoje_str})

# 3c. Filtro por operacao Saída
get(f"{TS}_q3c_rotas_saida", "/rotas",
    {"page_size": "10", "operacao": "Saída"})

# 3d. Última página pra ver se tem rota recente
print("\n--- Buscando última página (mais recente) de /rotas ---")
get(f"{TS}_q3d_rotas_ultima_pagina", "/rotas",
    {"page_size": "10", "page": "3855"})

print()
print("=" * 60)
print("Fim. Veja os JSONs em amostras_api/ com prefixo", TS)
print("=" * 60)
