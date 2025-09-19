#!/usr/bin/env python3
import os
import re
import requests

# =======================
# CONFIG
# =======================
ZABBIX_URL = "https://zabbix.toledofibra.net.br/api_jsonrpc.php"
AUTH_TOKEN = os.getenv("ZABBIX_TOKEN", "827be10800577e595a26a0cad5ccac7976d8b459f27adf593182fceb18a2ee69")

HOSTS = [
    "router-edge-for",
    "rj-cdn-dc-aux-01",
    # "outros-hosts-aqui",
]

# Substrings para testar (busca em NAME e em TAGS)
LABEL_PATTERNS = [
    "transit-EdgeUno",
    "Peering",
]

VERIFY_SSL = False
HTTP_TIMEOUT = 60
MAX_SHOW = 40   # quantos itens detalhar por host
RAW_SHOW  = 10  # dump inicial de itens crus por família

# Famílias de chaves a procurar (SNMP e Agent)
KEY_FAMILIES = [
    "net.if.in[",  "net.if.out[",
    "ifHCInOctets[", "ifHCOutOctets[",
    "ifInOctets[",   "ifOutOctets[",
]

# =======================
# API helper
# =======================
def api(method, params):
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "auth": AUTH_TOKEN, "id": 1}
    r = requests.post(ZABBIX_URL, json=payload, verify=VERIFY_SSL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]

def idx_from_key(key_):
    # "[12]" ou ".12]"
    m = re.search(r'\[(\d+)\]$', key_)
    if m:
        return int(m.group(1))
    m2 = re.search(r'\.(\d+)\]$', key_)
    return int(m2.group(1)) if m2 else None

def coletar_itens_iface_com_tags(host):
    items_all = []
    raw_debug = []
    for fam in KEY_FAMILIES:
        items = api("item.get", {
            "output": ["itemid", "name", "key_"],
            "host": host,
            "search": {"key_": fam},
            "searchWildcardsEnabled": True,
            "limit": 10000,
            "sortfield": "name",
            "selectTags": ["tag", "value"],   # <- pega TAGS
        })
        items_all.extend(items)
        raw_debug.extend((it.get("key_",""), it.get("name","")) for it in items[:RAW_SHOW])

    # dedup por itemid só por segurança
    seen = set()
    dedup = []
    for it in items_all:
        iid = it.get("itemid")
        if iid not in seen:
            seen.add(iid)
            dedup.append(it)
    return dedup, raw_debug

def tags_to_str(tags):
    if not tags:
        return "-"
    return ", ".join(f"{t.get('tag','')}: {t.get('value','')}" for t in tags)

def match_patterns(name, tags, patterns):
    textblocks = [name or ""]
    if tags:
        textblocks.extend([t.get("tag",""), t.get("value","")] for t in tags)
        # flatten
        flat = []
        for x in textblocks:
            if isinstance(x, list):
                flat.extend(x)
            else:
                flat.append(x)
        text = " | ".join(flat).lower()
    else:
        text = (name or "").lower()

    matches = {}
    for p in patterns:
        pl = p.lower()
        matches[p] = (pl in text)
    return matches

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
            hh = api("host.get", {"output": ["hostid","host"], "filter": {"host": [host]}})
            if not hh:
                print("  Host não existe (nome difere do Zabbix).")
                continue

            items, raw = coletar_itens_iface_com_tags(host)
            print(f"  Itens (interfaces in/out) encontrados: {len(items)}")

            if raw:
                print("  Amostra RAW (key_ → name):")
                for key_, nm in raw[:RAW_SHOW]:
                    print(f"    {key_}  ->  {nm}")
                if len(raw) > RAW_SHOW:
                    print(f"    ... (+{len(raw)-RAW_SHOW} itens)")

            if not items:
                print("  Nenhum item retornado. Verifique template/LLD/SNMP.")
                # ajuda extra: mostrar templates e interfaces
                try:
                    info = api("host.get", {
                        "output": ["hostid"],
                        "selectParentTemplates": ["templateid","name"]
                    })
                    if info:
                        tpl = info[0].get("parentTemplates", [])
                        if tpl:
                            print("  Templates vinculados:")
                            for t in tpl:
                                print(f"    - {t.get('name')}")
                    hifs = api("hostinterface.get", {
                        "output": ["type","useip","ip","dns","port","details"],
                        "hostids": [hh[0]["hostid"]]
                    })
                    if hifs:
                        print("  Interfaces do host (1=Agent,2=SNMP,3=IPMI,4=JMX):")
                        for i in hifs:
                            print(f"    - type={i.get('type')} ip={i.get('ip')} dns={i.get('dns')} port={i.get('port')}")
                except Exception:
                    pass
                continue

            # Detalha primeiros itens (key, name, tags)
            print("\n  Detalhe (amostra):")
            for it in items[:MAX_SHOW]:
                key_ = it.get("key_", "")
                name = it.get("name","")
                tags = it.get("tags", [])
                idx = idx_from_key(key_) or "-"
                print(f"    ifIndex={idx:>4} | key={key_}")
                print(f"       name: {name}")
                print(f"       tags: {tags_to_str(tags)}")

            # Match por substring em NAME+TAGS
            if LABEL_PATTERNS:
                print("\n  Match por substrings (busca em NAME e TAGS):")
                counters = {p: 0 for p in LABEL_PATTERNS}
                examples = {p: [] for p in LABEL_PATTERNS}

                for it in items:
                    name = it.get("name","")
                    tags = it.get("tags", [])
                    m = match_patterns(name, tags, LABEL_PATTERNS)
                    for p, ok in m.items():
                        if ok:
                            counters[p] += 1
                            if len(examples[p]) < 10:
                                idx = idx_from_key(it.get("key_","")) or "-"
                                examples[p].append(f"{idx} → {name} | {tags_to_str(tags)}")

                for p in LABEL_PATTERNS:
                    print(f'    "{p}": {counters[p]} item(ns) casando')
                    for ex in examples[p]:
                        print(f"       - {ex}")

        except requests.exceptions.RequestException as e:
            print(f"  Erro HTTP/Conexão: {e}")
        except RuntimeError as e:
            print(f"  Erro na API do Zabbix: {e}")
        except Exception as e:
            print(f"  Falha inesperada: {e}")

if __name__ == "__main__":
    main()
