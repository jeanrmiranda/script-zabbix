#!/usr/bin/env python3
import os
import requests
import statistics
from datetime import datetime, timedelta, timezone
from calendar import monthrange

# =======================
# CONFIG
# =======================
ZABBIX_URL = "https://zbx-dev.edgeuno.net/api_jsonrpc.php"
AUTH_TOKEN = os.getenv("ZABBIX_TOKEN", "dcfa7ffd192f750ce9f6d6182553805936f2697b3acde0d10c5afe5118409b5c")

# Hosts e ifName por host (as chaves dos itens usam ifName, não ifIndex)
HOSTS_IFINDEX = {
    "edge1.eze1.edgeuno.net": ['ae814', 'ae33', 'ae2'],
    "router-edge-spo": ['11', '4', '8'],
}

# Se True: usa últimos 30 dias; se False: mês anterior fechado
ULTIMOS_30_DIAS = True

# SSL do requests
VERIFY_SSL = False
HTTP_TIMEOUT = 60

# Impressões opcionais
PRINT_TOTAL = False          # total em Bytes do período (opcional)
PRINT_P95   = True           # mostrar 95º percentil

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

def sanitize_item_name(name: str) -> str:
    """Tenta deixar o nome da interface mais limpo."""
    if not name:
        return name
    for sep in (": Bits", ": Inbound", ": Outbound", ": Receive", ": Transmit", " - In", " - Out"):
        if sep in name:
            return name.split(sep, 1)[0]
    return name

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

    for host, ifindexes in HOSTS_IFINDEX.items():
        print(f"==================== {host} ====================")

        # Monta todas as chaves IN/OUT necessárias para este host (usando ifName, sem aspas)
        keys_needed = []
        meta_list = []  # (key, ifname, dir)

        for raw_idx in ifindexes:
            ifname = str(raw_idx)  # garante string (evita misturar int/str)
            kin = f"SnmpInterfaceInTraffic[{ifname}]"
            kout = f"SnmpInterfaceOutTraffic[{ifname}]"
            keys_needed.extend([kin, kout])
            meta_list.append((kin, ifname, "IN"))
            meta_list.append((kout, ifname, "OUT"))

        # Resolve itemids
        items = zabbix_api("item.get", {
            "output": ["itemid", "name", "key_", "units"],
            "host": host,
            "filter": {"key_": keys_needed},
        })
        found = {it["key_"]: it for it in items}
        missing = [k for k in keys_needed if k not in found]
        if missing:
            print("  Itens não encontrados no host:", missing)
            print("  Verifique se o ifName no HOSTS_IFINDEX corresponde exatamente ao que está no key.\n")

        # Agregador por interface (usa ifName como chave, sempre string)
        by_if = {}  # ifName -> { "name": <auto>, "IN": {...} | None, "OUT": {...} | None }

        for key, ifname, direc in meta_list:
            it = found.get(key)
            if not it:
                by_if.setdefault(ifname, {"name": None, "IN": None, "OUT": None})
                continue

            itemid = it["itemid"]
            item_name = sanitize_item_name(it.get("name") or "")

            if ifname not in by_if:
                by_if[ifname] = {"name": item_name, "IN": None, "OUT": None}
            else:
                if not by_if[ifname].get("name"):
                    by_if[ifname]["name"] = item_name

            avgs, mins, maxs = fetch_trend_avgs(itemid, time_from, time_till)
            if not avgs:
                continue

            media_dir = statistics.mean(avgs)
            min_dir   = min(mins)
            max_dir   = max(maxs)
            p95_dir   = percentile(avgs, 95.0) if PRINT_P95 else None
            total_bits = sum(a * 3600 for a in avgs)  # somatório por bucket de 1h

            by_if[ifname][direc] = {
                "media": media_dir,
                "min": min_dir,
                "max": max_dir,
                "p95": p95_dir,
                "total_bytes": total_bits / 8.0,
            }

        # Impressão organizada por interface
        if not by_if:
            print("  Nenhum item processado.\n")
            continue

        for ifname in sorted(by_if.keys()):  # todas as chaves são str
            entry = by_if[ifname]
            name = entry.get("name") or f"{ifname}"
            din = entry["IN"]
            dout = entry["OUT"]
            print(f"[ifName {ifname}] {name}")

            if din:
                print(f"  Received (IN):")
                print(f"    Média: {format_bps(din['media'])}")
                print(f"    Mín/Máx horário: {format_bps(din['min'])} | {format_bps(din['max'])}")
                if PRINT_P95 and din['p95'] is not None:
                    print(f"    95º percentil: {format_bps(din['p95'])}")
                if PRINT_TOTAL:
                    print(f"    Total (opcional): {format_total_bytes(din['total_bytes'])}")
            else:
                print("  Received (IN): sem dados no período.")

            if dout:
                print(f"  Send (OUT):")
                print(f"    Média: {format_bps(dout['media'])}")
                print(f"    Mín/Máx horário: {format_bps(dout['min'])} | {format_bps(dout['max'])}")
                if PRINT_P95 and dout['p95'] is not None:
                    print(f"    95º percentil: {format_bps(dout['p95'])}")
                if PRINT_TOTAL:
                    print(f"    Total (opcional): {format_total_bytes(dout['total_bytes'])}")
            else:
                print("  Send (OUT): sem dados no período.")

            if din and dout:
                media_sum = din['media'] + dout['media']
                print(f"  Agregado (IN+OUT):")
                print(f"    Média: {format_bps(media_sum)}")
                if PRINT_P95 and din['p95'] is not None and dout['p95'] is not None:
                    print(f"    95º percentil (aprox.): {format_bps(din['p95'] + dout['p95'])}")
            print()

if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        print(f"Erro HTTP/Conexão: {e}")
    except RuntimeError as e:
        print(f"Erro na API do Zabbix: {e}")
    except Exception as e:
        print(f"Falha inesperada: {e}")
