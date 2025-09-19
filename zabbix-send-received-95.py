#!/usr/bin/env python3
import os
import re
import requests
import statistics
from datetime import datetime, timedelta, timezone
from calendar import monthrange
from collections import defaultdict

# =======================
# CONFIG
# =======================
ZABBIX_URL = "https://zabbix.toledofibra.net.br/api_jsonrpc.php"
AUTH_TOKEN = os.getenv("ZABBIX_TOKEN", "827be10800577e595a26a0cad5ccac7976d8b459f27adf593182fceb18a2ee69")

# Liste aqui todos os hosts que deseja consultar
HOSTS = [
    "router-edge-for",       # antigo
    "rj-cdn-dc-aux-01",      # novo
    # "outro-host-aqui",
]

# Padrões de label (substrings) para buscar nas interfaces de TODOS os hosts
# Ex.: "transit-EdgeUno" e "Peering"
LABEL_PATTERNS = [
    "transit-EdgeUno",
    "Peering",
    # "outro-label",
]

# Se True: últimos 30 dias; se False: mês anterior fechado
ULTIMOS_30_DIAS = False

# SSL do requests
VERIFY_SSL = False
HTTP_TIMEOUT = 60

# Impressões opcionais
PRINT_TOTAL = False          # total em Bytes do período (opcional)
PRINT_P95   = True           # 95º percentil

# =======================
# Tempo
# =======================
def intervalo_mes_anterior_utc(now_utc: datetime):
    ano = now_utc.year if now_utc.month > 1 else now_utc.year - 1
    mes = now_utc.month - 1 if now_utc.month > 1 else 12
    inicio = datetime(ano, mes, 1, 0, 0, 0, tzinfo=timezone.utc)
    fim = datetime(ano, mes, monthrange(ano, mes)[1], 23, 59, 59, tzinfo=timezone.utc)
    return int(inicio.timestamp()), int(fim.timestamp())

def intervalo_ultimos_30_dias_utc(now_utc: datetime):
    return int((now_utc - timedelta(days=30)).timestamp()), int(now_utc.timestamp())

# =======================
# API
# =======================
def zabbix_api(method, params):
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "auth": AUTH_TOKEN, "id": 1}
    r = requests.post(ZABBIX_URL, json=payload, verify=VERIFY_SSL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]

# =======================
# Helpers
# =======================
def format_bps(bps: float) -> str:
    units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]
    v = float(bps)
    i = 0
    while v >= 1000 and i < len(units) - 1:
        v /= 1000.0
        i += 1
    return f"{v:.2f} {units[i]}"

def format_total_bytes(num_bytes: float) -> str:
    gb = num_bytes / (1024 ** 3)
    if gb >= 1024:
        return f"{gb/1024:.2f} TB"
    return f"{gb:.2f} GB"

def percentile(values, p):
    if not values:
        return None
    vals = sorted(values)
    k = (len(vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return vals[f]
    return vals[f] + (vals[c] - vals[f]) * (k - f)

def fetch_trend_avgs(itemid, t_from, t_till):
    """Retorna listas (avgs, mins, maxs) apenas para buckets com num>0."""
    trends = zabbix_api("trend.get", {
        "output": ["clock", "num", "value_avg", "value_min", "value_max"],
        "itemids": itemid,
        "time_from": t_from,
        "time_till": t_till,
        "sortfield": "clock",
        "sortorder": "ASC",
    })
    if not trends:
        return [], [], []
    buckets = [t for t in trends if int(t.get("num", 0)) > 0]
    if not buckets:
        return [], [], []
    avgs = [float(t["value_avg"]) for t in buckets]  # bits/s
    mins = [float(t["value_min"]) for t in buckets]
    maxs = [float(t["value_max"]) for t in buckets]
    return avgs, mins, maxs

def listar_itens_ifaces(hostname):
    """
    Retorna:
      - idx_in:  dict { ifIndex: item_in }
      - idx_out: dict { ifIndex: item_out }
      - name_by_idx: dict { ifIndex: nome_do_item (um dos nomes, útil p/ imprimir) }
      - names_all: dict { ifIndex: nome_composto_mais_descritivo } (IN priorizado, OUT fallback)
    """
    # Itens IN (HC)
    items_in = zabbix_api("item.get", {
        "output": ["itemid", "name", "key_"],
        "host": hostname,
        "search": {"key_": "net.if.in[ifHCInOctets."},
        "searchWildcardsEnabled": True,
        "sortfield": "name",
        "limit": 10000
    })
    # Itens OUT (HC)
    items_out = zabbix_api("item.get", {
        "output": ["itemid", "name", "key_"],
        "host": hostname,
        "search": {"key_": "net.if.out[ifHCOutOctets."},
        "searchWildcardsEnabled": True,
        "sortfield": "name",
        "limit": 10000
    })

    def idx_from_key(key_):
        # Ex.: net.if.in[ifHCInOctets.12] -> 12
        m = re.search(r'\.(\d+)\]$', key_)
        return int(m.group(1)) if m else None

    idx_in, idx_out, name_by_idx, names_all = {}, {}, {}, {}

    for it in items_in:
        idx = idx_from_key(it["key_"])
        if idx is not None:
            idx_in[idx] = it
            name_by_idx.setdefault(idx, it["name"])
            names_all[idx] = it["name"]

    for it in items_out:
        idx = idx_from_key(it["key_"])
        if idx is not None:
            idx_out[idx] = it
            name_by_idx.setdefault(idx, it["name"])
            names_all.setdefault(idx, it["name"])

    return idx_in, idx_out, name_by_idx, names_all

def indices_por_label_patterns(names_all, patterns):
    """
    Retorna dict { pattern: set([ifIndex,...]) } com todos os ifIndex cujo nome
    contém cada pattern (case-insensitive).
    """
    result = {p: set() for p in patterns}
    for idx, nm in names_all.items():
        nm_l = nm.lower()
        for p in patterns:
            if p.lower() in nm_l:
                result[p].add(idx)
    return result

# =======================
# Main
# =======================
def main():
    if not VERIFY_SSL:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    now = datetime.now(timezone.utc)
    if ULTIMOS_30_DIAS:
        time_from, time_till = intervalo_ultimos_30_dias_utc(now)
    else:
        time_from, time_till = intervalo_mes_anterior_utc(now)

    print(f"Período: {datetime.utcfromtimestamp(time_from)} UTC até {datetime.utcfromtimestamp(time_till)} UTC\n")

    # Processa cada host
    for host in HOSTS:
        print(f"==================== {host} ====================")

        try:
            idx_in, idx_out, name_by_idx, names_all = listar_itens_ifaces(host)
        except Exception as e:
            print(f"  Falha ao listar itens do host: {e}\n")
            continue

        if not names_all:
            print("  Nenhuma interface encontrada (verifique template/LLD/credenciais SNMP).")
            print()
            continue

        # Encontra todos os índices que batem com cada label pattern
        match_map = indices_por_label_patterns(names_all, LABEL_PATTERNS)

        # Monta chaves necessárias (IN/OUT) para todos os índices encontrados
        all_indices = sorted(set().union(*match_map.values()))
        if not all_indices:
            print("  Nenhuma interface combinou com os labels fornecidos:")
            for p in LABEL_PATTERNS:
                print(f"    - {p}")
            print("\n  Interfaces disponíveis (amostra):")
            shown = 0
            for i, (idx, nm) in enumerate(sorted(names_all.items())):
                print(f"    ifIndex {idx:>4}: {nm}")
                shown += 1
                if shown >= 30:
                    print("    ... (lista truncada)")
                    break
            print()
            continue

        keys_needed = []
        key_to_meta = {}
        for idx in all_indices:
            kin = f"net.if.in[ifHCInOctets.{idx}]"
            kout = f"net.if.out[ifHCOutOctets.{idx}]"
            keys_needed.extend([kin, kout])
            # label de exibição = nome real do item do Zabbix (mais amigável)
            label = names_all.get(idx, f"ifIndex {idx}")
            key_to_meta[kin]  = (idx, "IN",  label)
            key_to_meta[kout] = (idx, "OUT", label)

        # Resolve itemids que existem de fato
        items = zabbix_api("item.get", {
            "output": ["itemid", "name", "key_", "units"],
            "host": host,
            "filter": {"key_": keys_needed},
        })
        found = {it["key_"]: it for it in items}
        missing = [k for k in keys_needed if k not in found]
        if missing:
            # Não aborta; apenas avisa
            print("  Aviso: alguns itens não encontrados/estão desabilitados:")
            for m in missing[:10]:
                print(f"    - {m}")
            if len(missing) > 10:
                print(f"    ... (+{len(missing)-10} itens)")
            print()

        # Agregador por interface
        by_if = {}
        for key, meta in key_to_meta.items():
            if key not in found:
                idx, direc, label = meta
                by_if.setdefault(idx, {"label": label, "IN": None, "OUT": None})
                continue

            idx, direc, label = meta
            it = found[key]
            itemid = it["itemid"]

            avgs, mins, maxs = fetch_trend_avgs(itemid, time_from, time_till)
            by_if.setdefault(idx, {"label": label, "IN": None, "OUT": None})

            if not avgs:
                by_if[idx][direc] = None
                continue

            media_dir = statistics.mean(avgs)
            min_dir   = min(mins)
            max_dir   = max(maxs)
            p95_dir   = percentile(avgs, 95.0) if PRINT_P95 else None
            total_bits = sum(a * 3600 for a in avgs)

            by_if[idx][direc] = {
                "media": media_dir,
                "min": min_dir,
                "max": max_dir,
                "p95": p95_dir,
                "total_bytes": total_bits / 8.0,
            }

        # Impressão organizada por pattern e por interface
        # Primeiro, criamos um mapa pattern -> lista de idx (ordenados)
        for pattern in LABEL_PATTERNS:
            indices = sorted(match_map.get(pattern, []))
            if not indices:
                continue
            print(f"--- Label contém: \"{pattern}\" ---")
            for idx in indices:
                entry = by_if.get(idx, {"label": names_all.get(idx, f"ifIndex {idx}"), "IN": None, "OUT": None})
                label = entry["label"]
                din = entry["IN"]
                dout = entry["OUT"]

                print(f"[ifIndex {idx}] {label}")

                if din:
                    print(f"  Received (IN):")
                    print(f"    Média: {format_bps(din['media'])}")
                    print(f"    Mín/Máx horário: {format_bps(din['min'])} | {format_bps(din['max'])}")
                    if PRINT_P95 and din['p95'] is not None:
                        print(f"    95º percentil: {format_bps(din['p95'])}")
                    if PRINT_TOTAL:
                        print(f"    Total (opcional): {format_total_bytes(din['total_bytes'])}")
                else:
                    print("  Received (IN): sem dados no período ou item ausente.")

                if dout:
                    print(f"  Send (OUT):")
                    print(f"    Média: {format_bps(dout['media'])}")
                    print(f"    Mín/Máx horário: {format_bps(dout['min'])} | {format_bps(dout['max'])}")
                    if PRINT_P95 and dout['p95'] is not None:
                        print(f"    95º percentil: {format_bps(dout['p95'])}")
                    if PRINT_TOTAL:
                        print(f"    Total (opcional): {format_total_bytes(dout['total_bytes'])}")
                else:
                    print("  Send (OUT): sem dados no período ou item ausente.")

                if din and dout:
                    media_sum = din['media'] + dout['media']
                    print(f"  Agregado (IN+OUT):")
                    print(f"    Média: {format_bps(media_sum)}")
                    if PRINT_P95 and din['p95'] is not None and dout['p95'] is not None:
                        print(f"    95º percentil (aprox.): {format_bps(din['p95'] + dout['p95'])}")
                print()
        print()  # linha em branco entre hosts

if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        print(f"Erro HTTP/Conexão: {e}")
    except RuntimeError as e:
        print(f"Erro na API do Zabbix: {e}")
    except Exception as e:
        print(f"Falha inesperada: {e}")
