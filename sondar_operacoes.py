"""
Sondagem 3 — fechar lacunas.

a) Quais operações existem em /quantitativos? Testa palavras candidatas.
b) Qual é o nome correto do filtro de data em /rotas?
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


def get_curto(path, params, label):
    url = f"{BASE_URL}{path}"
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    except requests.RequestException as e:
        print(f"  [{label}] ERRO de rede: {e}")
        return None
    if r.status_code != 200:
        print(f"  [{label}] status={r.status_code}")
        return None
    try:
        d = r.json()
    except ValueError:
        print(f"  [{label}] não-JSON")
        return None
    if isinstance(d, list):
        return len(d), d
    if isinstance(d, dict) and "data" in d:
        return d.get("total_items", len(d["data"])), d["data"]
    return None


print("=" * 60)
print("a) Mapear operações em /quantitativos")
print("=" * 60)
candidatos = [
    "Compra", "Saida", "Saída", "Baixa", "Devolucao", "Devolução",
    "Transferencia", "Transferência", "Locacao", "Locação", "Estorno",
    "Entrada", "Manutencao", "Manutenção", "Ajuste",
    "Estornar", "Faturamento", "Recebimento", "Venda",
]

operacoes_validas = {}
for op in candidatos:
    res = get_curto(
        "/cadastros/equipamentos/quantitativos",
        {"first": "1", "operacao": op},
        op,
    )
    if res:
        n, _ = res
        operacoes_validas[op] = n
        print(f"  {op:20s} → {n} registros")

# Salvar resultado
Path(SAIDA / f"{TS}_operacoes_validas.json").write_text(
    json.dumps(operacoes_validas, ensure_ascii=False, indent=2)
)
print(f"\nResumo salvo em amostras_api/{TS}_operacoes_validas.json")

print()
print("=" * 60)
print("b) Filtros de data em /rotas — testar nomes candidatos")
print("=" * 60)
hoje = datetime.now()
data_recente = (hoje - timedelta(days=30)).strftime("%Y-%m-%d")
data_br = (hoje - timedelta(days=30)).strftime("%d/%m/%Y")

testes_rota = [
    ({"page_size": "1", "data": data_recente}, "data"),
    ({"page_size": "1", "data_de": data_recente, "data_ate": hoje.strftime("%Y-%m-%d")}, "data_de/ate"),
    ({"page_size": "1", "dt_de": data_recente, "dt_ate": hoje.strftime("%Y-%m-%d")}, "dt_de/ate"),
    ({"page_size": "1", "dtinicial": data_recente, "dtfinal": hoje.strftime("%Y-%m-%d")}, "dtinicial"),
    ({"page_size": "1", "data_inicio": data_recente, "data_fim": hoje.strftime("%Y-%m-%d")}, "data_inicio/fim"),
    ({"page_size": "1", "data_lib_de": data_recente, "data_lib_ate": hoje.strftime("%Y-%m-%d")}, "data_lib_de"),
    ({"page_size": "1", "dt_preventrega_de": data_recente, "dt_preventrega_ate": hoje.strftime("%Y-%m-%d")}, "dt_preventrega_de"),
    ({"page_size": "1", "data_inicial": data_br, "data_final": hoje.strftime("%d/%m/%Y")}, "data_inicial (formato BR)"),
    ({"page_size": "1", "sort_by": "desc(data)"}, "sort_by desc(data)"),
    ({"page_size": "1", "sort_by": "desc(id_liberacao)"}, "sort_by desc(id_liberacao)"),
]

resultados_rota = {}
for params, label in testes_rota:
    url = f"{BASE_URL}/rotas"
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    except Exception as e:
        print(f"  [{label}] ERRO: {e}")
        continue
    status = r.status_code
    snippet = ""
    if status == 200:
        try:
            d = r.json()
            total = d.get("total_items")
            data_field = d.get("data", [])
            primeira_data = data_field[0].get("data") if data_field else None
            snippet = f"total={total}, primeira_data={primeira_data}"
        except Exception:
            snippet = "JSON inválido"
    else:
        try:
            txt = r.json()
            snippet = json.dumps(txt)[:120]
        except Exception:
            snippet = r.text[:120]
    print(f"  [{label}] status={status}  {snippet}")
    resultados_rota[label] = {"status": status, "snippet": snippet}

Path(SAIDA / f"{TS}_filtros_rotas.json").write_text(
    json.dumps(resultados_rota, ensure_ascii=False, indent=2)
)
print(f"\nResumo salvo em amostras_api/{TS}_filtros_rotas.json")
