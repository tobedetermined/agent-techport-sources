import sqlite3, json, urllib.request, random, re
db=sqlite3.connect("sbir.db")
TYPES=["A","B","C","D","02","03","04","05"]
def search(ids):
    body={"filters":{"award_ids":ids,"award_type_codes":TYPES[:4],"time_period":[{"start_date":"2007-10-01","end_date":"2026-09-30"}]},"fields":["Award ID","Recipient UEI","Awarding Agency"],"limit":100}
    out=[]
    for codes in (TYPES[:4],TYPES[4:]):
        body["filters"]["award_type_codes"]=codes
        req=urllib.request.Request("https://api.usaspending.gov/api/v2/search/spending_by_award/",data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
        out+=json.load(urllib.request.urlopen(req,timeout=90))["results"]
    return out
def variants(c):
    c=c.strip(); v=[c, c.replace("-","")]
    m=re.match(r"^\d?([A-Z]\d{2}[A-Z]{2}\d{6})",c)          # NIH: 2R44AG050454-02 -> R44AG050454
    if m: v.append(m.group(1))
    return list(dict.fromkeys(v))
AG={"DoD":"Department of Defense","DOE":"Department of Energy","NSF":"National Science Foundation","HHS":"Department of Health and Human Services"}
for k,a in AG.items():
    rows=db.execute('select Contract,UEI from awards where Agency=? and "Award Year" between "2015" and "2025" order by random() limit 30',(a,)).fetchall()
    allv=sorted({v for c,_ in rows for v in variants(c)})
    hits={}
    for i in range(0,len(allv),40):
        for r in search(allv[i:i+40]): hits.setdefault(r["Award ID"],[]).append(r)
    found=raw=ue=multi=0; how={}; miss=[]
    for c,u in rows:
        vs=variants(c); got=[(v,hits[v]) for v in vs if v in hits]
        if not got: miss.append(c); continue
        found+=1; v,h=got[0]; how[vs.index(v)]=how.get(vs.index(v),0)+1
        multi+=len(h)>1; ue+=any((x["Recipient UEI"] or "")==u.strip() for x in h)
    print(f"{k}: {found}/30 matched | via variant idx {how} (0=raw,1=no-dashes,2=NIH core) | multi-hit {multi} | UEI agrees {ue} | misses {miss[:6]}")
