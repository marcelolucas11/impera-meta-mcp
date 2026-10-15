"""Graph API falsa para os testes do painel: dois tokens válidos, cada um com as próprias contas."""
import json, random
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse
import httpx

HOJE = date.today(); D1 = HOJE - timedelta(days=45)
CONTAS = {
    "TOKPRIMOS": ("relatorios-primos", [("111111111111111", "PRIMOS BM01 CA 01", 1)]),
    "TOKDONA": ("sysuser-donaco", [("687018494504975", "DONA CO BM01 CA 02", 1), ("113392661530740", "DONA CO BM01 CA 03", 3)]),
}
random.seed(11)
FACTS = {}
for tok, (_, contas) in CONTAS.items():
    for acc, nome, _st in contas:
        ads = [(f"{acc[:6]}00000000000{i}1", f"[VENDAS] cpa_{600 + i * 100} aberto v{i}", "OUTCOME_SALES", f"{acc[:6]}00000000000{i}2", f"aberto 18-65 #{i}",
                f"{acc[:6]}00000000000{i}3", f"video_carro_0{i}", "ARCHIVED" if i == 2 else "ACTIVE") for i in (1, 2, 3)]
        fs = []; d = D1
        while d <= HOJE:
            for ad in ads:
                for a in ("18-24", "25-34", "35-44"):
                    for g in ("female", "male"):
                        for p in ("facebook", "instagram"):
                            imp = random.randint(300, 1200); buys = random.randint(0, 5)
                            fs.append(dict(ad=ad, day=d, age=a, gender=g, plat=p, spend=round(random.uniform(8, 55), 2), imp=imp,
                                           people=set(random.sample(range(90000), k=int(imp * .8))), cl=random.randint(4, 40), buys=buys, val=buys * random.choice([20, 25, 40])))
            d += timedelta(days=1)
        FACTS[acc] = (ads, fs)
VISTO = []

def err(status, code, msg): return httpx.Response(status, json={"error": {"message": msg, "code": code}})

def agg(facts, level, daily, bks, since, until, acc):
    groups = {}
    for f in facts:
        key = []
        if level in ("campaign", "adset", "ad"): key.append(f["ad"][0])
        if level in ("adset", "ad"): key.append(f["ad"][3])
        if level == "ad": key.append(f["ad"][5])
        if daily: key.append(f["day"])
        for b in bks: key.append({"age": f["age"], "gender": f["gender"], "publisher_platform": f["plat"]}[b])
        groups.setdefault(tuple(key), []).append(f)
    rows = []
    for key in sorted(groups, key=str):
        fs = groups[key]; ad = fs[0]["ad"]; imp = sum(x["imp"] for x in fs); reach = len(set().union(*(x["people"] for x in fs)))
        buys = sum(x["buys"] for x in fs); val = sum(x["val"] for x in fs)
        row = {"account_id": acc, "spend": f"{sum(x['spend'] for x in fs):.2f}", "impressions": str(imp), "reach": str(reach), "frequency": f"{imp / reach:.6f}",
               "clicks": "9", "inline_link_clicks": str(sum(x["cl"] for x in fs)),
               "date_start": (fs[0]["day"] if daily else since).isoformat(), "date_stop": (fs[0]["day"] if daily else until).isoformat()}
        if buys:
            row["actions"] = [{"action_type": t, "value": str(buys)} for t in ("purchase", "omni_purchase")]
            row["action_values"] = [{"action_type": t, "value": f"{val:.2f}"} for t in ("purchase", "omni_purchase")]
        if level in ("campaign", "adset", "ad"): row.update(campaign_id=ad[0], campaign_name=ad[1], objective=ad[2])
        if level in ("adset", "ad"): row.update(adset_id=ad[3], adset_name=ad[4])
        if level == "ad": row.update(ad_id=ad[5], ad_name=ad[6])
        for b in bks: row[b] = {"age": fs[0]["age"], "gender": fs[0]["gender"], "publisher_platform": fs[0]["plat"]}[b]
        rows.append(row)
    return rows

def handler(req):
    assert req.method == "GET"
    tok = req.headers.get("authorization", "").removeprefix("Bearer "); u = urlparse(str(req.url)); q = parse_qs(u.query)
    p = u.path.split("/", 2)[2]; VISTO.append((tok, p))
    if tok not in CONTAS: return err(400, 190, "Invalid OAuth access token - Cannot parse access token")
    dono, contas = CONTAS[tok]; ids = {c[0] for c in contas}
    if p == "me": return httpx.Response(200, json={"id": "9", "name": dono})
    if p == "me/permissions": return httpx.Response(200, json={"data": [{"permission": "ads_read", "status": "granted"}, {"permission": "business_management", "status": "granted"}]})
    if p == "me/adaccounts":
        return httpx.Response(200, json={"data": [{"account_id": a, "name": n, "account_status": st, "currency": "BRL", "timezone_name": "America/Bahia", "business": {"id": "5", "name": "BM " + dono}} for a, n, st in contas]})
    if not p.startswith("act_"): return err(400, 100, "Unsupported get request")
    acc = p.split("/")[0][4:]
    if acc not in ids: return err(403, 200, "(#200) Ad account owner has NOT grant ads_read permission")
    nome = next(n for a, n, _ in contas if a == acc); ads, facts = FACTS[acc]
    if p == f"act_{acc}": return httpx.Response(200, json={"account_id": acc, "name": nome, "currency": "BRL", "timezone_name": "America/Bahia"})
    if p == f"act_{acc}/insights":
        tr = json.loads(q["time_range"][0]); since, until = date.fromisoformat(tr["since"]), date.fromisoformat(tr["until"])
        level = q["level"][0]; daily = q["time_increment"][0] == "1"; bks = q["breakdowns"][0].split(",") if q.get("breakdowns") else []
        statuses = None
        for f in json.loads(q.get("filtering", ["[]"])[0]):
            if f["field"].endswith(".effective_status"): statuses = set(f["value"])
        fs = [f for f in facts if since <= f["day"] <= until]
        if level == "ad": fs = [f for f in fs if (f["ad"][7] in statuses if statuses else f["ad"][7] != "ARCHIVED")]
        rows = agg(fs, level, daily, bks, since, until, acc)
        limit = int(q.get("limit", ["25"])[0]); off = int(q.get("after", ["0"])[0])
        body = {"data": rows[off:off + limit], "paging": {"cursors": {"after": str(off + limit)}}}
        if off + limit < len(rows): body["paging"]["next"] = "x"
        return httpx.Response(200, json=body)
    if p == f"act_{acc}/campaigns": return httpx.Response(200, json={"data": [{"id": a[0], "name": a[1], "objective": a[2], "effective_status": "ACTIVE", "daily_budget": "50000"} for a in ads]})
    if p == f"act_{acc}/adsets": return httpx.Response(200, json={"data": [{"id": a[3], "name": a[4], "campaign_id": a[0], "effective_status": "ACTIVE", "bid_amount": "600", "bid_strategy": "COST_CAP"} for a in ads]})
    return err(400, 100, "Unsupported get request")
