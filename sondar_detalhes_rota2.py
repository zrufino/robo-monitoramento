"""Sondagem 2 — /rotas/details exige tipo_ficha."""

import json
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
BASE_URL = "https://api.dosistemas.com.br/qualy/api"
TOKEN = os.environ["DOSISTEMAS_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
SAIDA = Path("amostras_api")
TS = datetime.now().strftime("%Y%m%d_%H%M%S")


def sonda(label, path, params):
    url = f"{BASE_URL}{path}"
    print(f"\n[{label}]  params={params}")
    r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    print(f"  status: {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        d = {"raw_text": r.text[:500]}
    (SAIDA / f"{TS}_{label}.json").write_text(json.dumps(d, ensure_ascii=False, indent=2))
    if isinstance(d, list):
        print(f"  lista: {len(d)} itens")
        if d and isinstance(d[0], dict):
            print(f"  chaves do 1º: {sorted(d[0].keys())}")
    elif isinstance(d, dict):
        if "data" in d and isinstance(d["data"], list):
            print(f"  data: {len(d['data'])} itens")
            if d["data"]:
                print(f"  chaves do 1º: {sorted(d['data'][0].keys())}")
        else:
            print(f"  chaves: {sorted(d.keys())}")
            if "raw_text" in d:
                print(f"  texto: {d['raw_text'][:200]}")


sonda("a_details_contrato_libera", "/rotas/details",
      {"tipo_ficha": "Contrato", "id_liberacao": 32599})

sonda("b_details_contrato_origem", "/rotas/details",
      {"tipo_ficha": "Contrato", "id_origem": 12872})

sonda("c_details_so_tipo_ficha", "/rotas/details",
      {"tipo_ficha": "Contrato"})

sonda("d_details_id", "/rotas/details",
      {"tipo_ficha": "Contrato", "id": 32599})

print(f"\nFim. Prefixo {TS} em amostras_api/")
