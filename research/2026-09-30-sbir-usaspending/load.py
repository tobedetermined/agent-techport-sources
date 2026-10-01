import csv, sqlite3, sys, time, os
csv.field_size_limit(sys.maxsize)
DROP = {"Contact Name","Contact Title","Contact Phone","Contact Email","PI Name","PI Title","PI Phone","PI Email","RI POC Name","RI POC Phone"}
t=time.time()
if os.path.exists("sbir.db"): os.remove("sbir.db")
db=sqlite3.connect("sbir.db")
with open("award_data.csv", newline="", encoding="utf-8", errors="replace") as f:
    r=csv.reader(f); hdr=next(r)
    keep=[i for i,h in enumerate(hdr) if h not in DROP]
    cols=[hdr[i] for i in keep]
    db.execute("CREATE TABLE awards (%s)" % ",".join('"%s" TEXT'%c for c in cols))
    bad=0; n=0; batch=[]
    for row in r:
        if len(row)!=len(hdr): bad+=1; continue
        batch.append([row[i] for i in keep]); n+=1
        if len(batch)>=20000: db.executemany("INSERT INTO awards VALUES (%s)"%",".join("?"*len(cols)),batch); batch=[]
    db.executemany("INSERT INTO awards VALUES (%s)"%",".join("?"*len(cols)),batch)
db.commit()
print(f"columns in file: {len(hdr)}, kept: {len(cols)}; rows loaded: {n}; malformed rows skipped: {bad}; load time {time.time()-t:.0f}s; db size {os.path.getsize('sbir.db')/1e6:.0f} MB")
q=lambda s: db.execute(s).fetchall()
print("\n-- agency values (top 15):"); [print(f"  {c:>7}  {a}") for a,c in q('select Agency,count(*) c from awards group by 1 order by 2 desc limit 15')]
N='Agency like "%Aeronautics%"'
print("\n-- NASA:", q(f'select count(*) from awards where {N}')[0][0])
print("  with abstract:", q(f'select count(*) from awards where {N} and trim(Abstract)<>""')[0][0])
print("  with UEI:", q(f'select count(*) from awards where {N} and trim(UEI)<>""')[0][0])
print("  award year range:", q(f'select min("Award Year"),max("Award Year") from awards where {N} and "Award Year"<>""')[0])
print("  by program/phase:", q(f'select Program,Phase,count(*) from awards where {N} group by 1,2 order by 1,2'))
print("  branch values:", q(f'select Branch,count(*) from awards where {N} group by 1 order by 2 desc limit 8'))
print("\n-- ALL: with abstract", q('select count(*) from awards where trim(Abstract)<>""')[0][0], "| with UEI", q('select count(*) from awards where trim(UEI)<>""')[0][0])
print("-- phase values:", q('select Phase,count(*) from awards group by 1'))
print("-- state sample:", q('select State,count(*) from awards group by 1 order by 2 desc limit 5'))
print("-- award date > 2026-09-30:", q('select count(*) from awards where "Proposal Award Date">"2026-09-30"')[0][0], "| NASA:", q(f'select count(*) from awards where {N} and "Proposal Award Date">"2026-09-30"')[0][0])
print("-- award date blank/bad format:", q('select count(*) from awards where "Proposal Award Date" not glob "[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]"')[0][0])
print("-- dup (agency tracking #, contract) NASA:", q(f'select count(*) from (select "Agency Tracking Number",Contract,count(*) c from awards where {N} group by 1,2 having c>1)')[0][0])
print("-- NASA amount total ($M):", round(q(f'select sum(cast("Award Amount" as real)) from awards where {N}')[0][0]/1e6))
