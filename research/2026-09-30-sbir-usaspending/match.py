import sqlite3, json, urllib.request, random
db=sqlite3.connect("sbir.db"); random.seed(7)
rows=[]
for y in range(2015,2026):
    rows+=db.execute('select Contract,UEI,Company,"Award Amount","Award Year",Phase from awards where Agency like "%Aeronautics%" and "Award Year"=? order by random() limit 10',(str(y),)).fetchall()
def search(ids):
    body={"filters":{"award_ids":ids,"award_type_codes":["A","B","C","D"],"time_period":[{"start_date":"2007-10-01","end_date":"2026-09-30"}]},
          "fields":["Award ID","Recipient Name","Recipient UEI","Award Amount","Awarding Agency","generated_internal_id"],"limit":100}
    req=urllib.request.Request("https://api.usaspending.gov/api/v2/search/spending_by_award/",data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req,timeout=60))["results"]
hits={}
for i in range(0,len(rows),25):
    for r in search([x[0] for x in rows[i:i+25]]):
        hits.setdefault(r["Award ID"],[]).append(r)
m=ue=nasa=0; miss=[]; amt=[]
for c,u,co,a,y,ph in rows:
    h=hits.get(c)
    if not h: miss.append((y,c,co)); continue
    m+=1; r=h[0]
    nasa+= "Aeronautics" in (r["Awarding Agency"] or "")
    ue+= (r["Recipient UEI"] or "").upper()==(u or "").upper() and bool(u)
    if r["Award Amount"] and a: amt.append(r["Award Amount"]/float(a))
print(f"sample {len(rows)} NASA awards 2015-2025 | matched by contract#=Award ID: {m} | awarding agency NASA: {nasa} | UEI agrees: {ue} (SBIR rows with UEI: {sum(1 for x in rows if x[1].strip())})")
print("multi-hit IDs:", sum(1 for v in hits.values() if len(v)>1))
amt.sort(); print("USAspending/SBIR amount ratio: median %.2f, p10 %.2f, p90 %.2f"%(amt[len(amt)//2],amt[len(amt)//10],amt[9*len(amt)//10]))
print("unmatched:", miss)
