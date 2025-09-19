#!/usr/bin/env python3
import os
import re
import requests

# =======================
# CONFIG
# =======================
ZABBIX_URL = "https://zabbix.toledofibra.net.br/api_jsonrpc.php"
AUTH_TOKEN = os.getenv("ZABBIX_TOKEN", "827be10800577e595a26a0cad5ccac7976d8b459f27adf593182fceb18a2ee69")

# Hosts para testar
HOSTS = [
    "router-edge-for",
    "rj-cdn-dc-aux-01",
]

# Substrings de label para validar correspondência (opcional)
LABEL_PATTERNS = [
    "transit-EdgeUno",
    "Peering",
]

VERIFY_SSL = False
HTTP_TIMEOUT = 60
MAX_SHOW = 40  # quantos ifIndex listar por host (para não poluir)

# =======================
# Zabbix API helper
# =======================
def zabbix_api(method, params):
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "auth": AUTH_TOKEN, "id": 1}
    r = requests.post(ZABBIX_URL, json=payload, verify=VERIFY_SSL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]

def idx_from_key(key_):
    # net.if.in[ifHCInOctets.12] -> 12
    m = re.search(r'\.(\d+)\]$', key_)
    return int(m.group(1)) if m else None

def listar_labels(hostname):
    # pega itens IN
    items_in = zabbix_api("item.get", {
        "output": ["itemid", "name", "key_"],
        "host": hostname,
        "search": {"key_": "net.if.in[ifHCInOctets."},
        "searchWildcardsEnabled": True,
        "sortfield": "name",
        "limit": 10000
    })
    # pega itens OUT
    items_out = zabbix_api("item.get", {
        "output": ["itemid", "name", "key_"],
        "host": hostname,
        "search": {"key_": "net.if.out[ifHCOutOctets."},
        "searchWildcardsEnabled": True,
        "sortfield": "name",
        "limit": 10000
    })

    names_all = {}
    for it in items_in:
        idx = idx_from_key(it["key_"])
        if idx is not None:
            names_all[idx] = it["name"]
    for it in items_out:
        idx = idx_from_key(it["key_"])
        if idx is not None:
            # mantém o nome IN se já existir; OUT serve de fallback
            names_all.setdefault(idx, it["name"])

    return names_all, items_in, items_out

def main():
    if not VERIFY_SSL:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    for host in HOSTS:
        print(f"\n==================== {host} ====================")
        try:
            # Confirma se o host existe
            hosts = zabbix_api("host.get", {"output": ["hostid","host"], "filter": {"host": [host]}})
            if not hosts:
                print("  Host não existe na API (verifique o nome exato do host).")
                continue

            names_all, items_in, items_out = listar_labels(host)

            print(f"  Itens IN encontrados:  {len(items_in)}")
            print(f"  Itens OUT encontrados: {len(items_out)}")
            print(f"  ifIndexes distintos:   {len(names_all)}")

            if not names_all:
                print("  Nenhuma interface encontrada (Template/LLD/SNMP?).")
                continue

            # Mostra amostra de labels por ifIndex
            print("  Amostra de labels (ifIndex → Nome do item):")
            shown = 0
            for idx in sorted(names_all.keys()):
                print(f"    {idx:>4} → {names_all[idx]}")
                shown += 1
                if shown >= MAX_SHOW:
                    print("    ... (lista truncada)")
                    break

            # Teste de match por patterns (opcional)
            if LABEL_PATTERNS:
                print("\n  Checagem por substrings (case-insensitive):")
                for p in LABEL_PATTERNS:
                    p_low = p.lower()
                    casados = [idx for idx, nm in names_all.items() if p_low in nm.lower()]
                    print(f'    "{p}": {len(casados)} interface(s) casando')
                    if casados:
                        for idx in sorted(casados)[:10]:
                            print(f"      - {idx:>4} → {names_all[idx]}")
                        if len(casados) > 10:
                            print(f"      ... (+{len(casados)-10})")

        except requests.exceptions.RequestException as e:
            print(f"  Erro HTTP/Conexão: {e}")
        except RuntimeError as e:
            print(f"  Erro na API do Zabbix: {e}")
        except Exception as e:
            print(f"  Falha inesperada: {e}")

if __name__ == "__main__":
    main()
