import gzip, json, re, threading, time, urllib.parse, urllib.request, sqlite3, os, csv, math, asyncio, sys, shutil, tempfile, concurrent.futures
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from datetime import datetime, timezone, timedelta
from pathlib import Path

APP_VERSION="1.5.12"
GITHUB_OWNER="Chupalupaa"
GITHUB_REPO="albion-market-assistant"
UPDATE_APP_URL=f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/albion_market_assistant.py"
UPDATE_LAUNCHER_URL=f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/START_ALBION_MARKET_ASSISTANT.bat"
OVERRIDES_FILE_NAME="manual_price_overrides.json"
BASE_URL="https://west.albion-online-data.com"
ITEMS_URL="https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
RECIPES_URL="https://raw.githubusercontent.com/vkorne-web/albion-market/main/recipes.json"

CITIES=["Bridgewatch","Martlock","Thetford","Fort Sterling","Lymhurst","Caerleon"]
FLIP_SELL_LOCATIONS=CITIES+["Black Market"]
CRAFT_CITIES=CITIES+["Brecilien"]

GEAR_MARKERS=("_HEAD_","_ARMOR_","_SHOES_","_MAIN_","_2H_","_OFF_","_BAG","_CAPE")
EXCLUDES=("QUEST","TOKEN","JOURNAL","FURNITURE","UNIQUE_UNLOCK","SKIN_","VANITY","MOUNT")

SETUP_FEE=0.025
PREMIUM_SALES_TAX=0.04
NONPREMIUM_SALES_TAX=0.08

# Current Royal City crafting RRR assumptions.
# Bonus applies only when the selected crafting city matches the item's specialty city.
BASE_PRODUCTION_BONUS=18.0
SPECIALTY_PRODUCTION_BONUS=15.0
FOCUS_PRODUCTION_BONUS=59.0

def production_bonus_to_rrr(bonus_pct):
    return bonus_pct/(100.0+bonus_pct) if bonus_pct>0 else 0.0

ICON_URL="https://render.albiononline.com/v1/item/{uid}.png?quality=1&size=64"
APP_DIR=os.path.dirname(os.path.abspath(__file__))
ICON_DIR=os.path.join(APP_DIR,"item_icons")
RECIPES_CACHE=os.path.join(APP_DIR,"recipes_cache.json")
SETTINGS_FILE=os.path.join(APP_DIR,"app_settings.json")
HISTORY_DAYS=14
DB=os.path.join(APP_DIR,"albion_market_cache.db")
ITEMS_CACHE=os.path.join(APP_DIR,"items_cache.json")
FLIP_HISTORY_CACHE=os.path.join(APP_DIR,"flip_history_cache.json")
ITEMS_CACHE_SECONDS=24*3600
HISTORY_CACHE_SECONDS=30*60
FLIP_WORKERS=8

SELL_LOCATIONS=CRAFT_CITIES+["Black Market"]
WATCHLIST_FILE=os.path.join(APP_DIR,"watchlist.json")
SESSION_FILE=os.path.join(APP_DIR,"session_history.json")
OVERRIDES_FILE=os.path.join(APP_DIR,OVERRIDES_FILE_NAME)
NATS_URL="nats://public:thenewalbiondata@nats.albion-online-data.com:4222"
NATS_TOPIC="marketorders.deduped"
LOCATION_MAP={
    "7":"Thetford","1002":"Lymhurst","2004":"Bridgewatch",
    "3005":"Caerleon","3010":"Martlock","4002":"Fort Sterling",
    7:"Thetford",1002:"Lymhurst",2004:"Bridgewatch",
    3005:"Caerleon",3010:"Martlock",4002:"Fort Sterling",
}
QUALITY_NAMES={1:"Normal",2:"Good",3:"Outstanding",4:"Excellent",5:"Masterpiece"}


def dbinit():
    con=sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS observations(
      item_id TEXT, city TEXT, sell_price REAL, seen_utc TEXT,
      PRIMARY KEY(item_id,city))""")
    con.execute("""CREATE TABLE IF NOT EXISTS live_orders(
      order_id TEXT, item_id TEXT, city TEXT, quality INTEGER, price INTEGER,
      amount INTEGER, auction_type TEXT, expires TEXT, last_seen TEXT,
      PRIMARY KEY(order_id,city))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_orders_item_city ON live_orders(item_id,city,quality,auction_type,price)")
    con.commit(); con.close()

def cache_observation(uid,city,price,date):
    if not price or not date or date.startswith("0001-"): return
    con=sqlite3.connect(DB)
    con.execute("""INSERT INTO observations(item_id,city,sell_price,seen_utc)
      VALUES(?,?,?,?) ON CONFLICT(item_id,city) DO UPDATE SET
      sell_price=excluded.sell_price,seen_utc=excluded.seen_utc""",(uid,city,price,date))
    con.commit(); con.close()

def get_json(url,timeout=60):
    req=urllib.request.Request(url,headers={"User-Agent":"AlbionMarketAssistant/1.3","Accept-Encoding":"gzip"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        raw=r.read()
        if r.headers.get("Content-Encoding","").lower()=="gzip":
            raw=gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))

def load_recipes():
    try:
        data=get_json(RECIPES_URL,90)
        if isinstance(data,dict) and data:
            try:
                with open(RECIPES_CACHE,"w",encoding="utf-8") as f:json.dump(data,f,separators=(",",":"))
            except:pass
            return data
    except:
        pass
    if os.path.exists(RECIPES_CACHE):
        with open(RECIPES_CACHE,"r",encoding="utf-8") as f:return json.load(f)
    raise RuntimeError("Could not download crafting recipes and no local recipe cache exists.")

def tier(uid):
    m=re.match(r"^T([4-8])_",uid)
    return int(m.group(1)) if m else None

def enchant(uid):
    m=re.search(r"@([1-4])$",uid)
    return int(m.group(1)) if m else 0

def isgear(uid):
    return tier(uid) and not any(x in uid for x in EXCLUDES) and any(x in uid for x in GEAR_MARKERS)

def name_of(x):
    n=x.get("LocalizedNames") or {}
    return n.get("EN-US") or n.get("EN") or x["UniqueName"]

def batches(items,max_chars=2800,max_items=75):
    b=[]; chars=0
    for x in items:
        if b and (len(b)>=max_items or chars+len(x)+1>max_chars):
            yield b; b=[]; chars=0
        b.append(x); chars+=len(x)+1
    if b: yield b

def api_ids(b):
    return ",".join(urllib.parse.quote(x,safe="@_") for x in b)

def prices_for(b,locations):
    q=urllib.parse.urlencode({"locations":",".join(locations),"qualities":"1"})
    return get_json(f"{BASE_URL}/api/v2/stats/prices/{api_ids(b)}.json?{q}")

def prices_for_qualities(b,locations,qualities=(1,2,3,4,5)):
    q=urllib.parse.urlencode({"locations":",".join(locations),"qualities":",".join(str(x) for x in qualities)})
    return get_json(f"{BASE_URL}/api/v2/stats/prices/{api_ids(b)}.json?{q}")

def market_price(row,mode):
    """Return (price,date) for a row and transaction mode."""
    if not row:return (0,None)
    if mode=="Buy Order":
        p=int(row.get("buy_price_max") or 0)
        return ((p+1) if p>0 else 0,row.get("buy_price_max_date"))
    if mode=="Instant Sell":
        return (int(row.get("buy_price_max") or 0),row.get("buy_price_max_date"))
    # Instant Buy / Sell Order
    return (int(row.get("sell_price_min") or 0),row.get("sell_price_min_date"))

def sale_fee(price,premium,sell_mode):
    tax=PREMIUM_SALES_TAX if premium else NONPREMIUM_SALES_TAX
    pct=tax+(SETUP_FEE if sell_mode=="Sell Order" else 0)
    return int(math.ceil(price*pct))

def base_uid(uid):
    return re.sub(r"@[1-4]$","",uid)

def uid_at_enchant(uid,e):
    b=base_uid(uid)
    return b if e==0 else f"{b}@{e}"

def item_category(uid):
    if "_2H_" in uid or "_MAIN_" in uid:return "Weapons"
    if "_HEAD_" in uid or "_ARMOR_" in uid or "_SHOES_" in uid:return "Armor"
    if "_OFF_" in uid:return "Off-hands"
    if "_BAG" in uid:return "Bags"
    if "_CAPE" in uid:return "Capes"
    return "Other"

def upgrade_count(uid):
    if "_2H_" in uid:return 384
    if "_MAIN_" in uid:return 288
    if "_ARMOR_" in uid or "_BAG" in uid:return 192
    if any(x in uid for x in ("_HEAD_","_SHOES_","_OFF_","_CAPE")):return 96
    return 0

def upgrade_material_id(t,e_step):
    kind={1:"RUNE",2:"SOUL",3:"RELIC"}.get(e_step)
    return f"T{t}_{kind}" if kind else None

def confidence_rank(c):
    return {"LOW":1,"MEDIUM":2,"HIGH":3}.get(c,0)

def normalize_location(x):
    if x in LOCATION_MAP:return LOCATION_MAP[x]
    s=str(x)
    if s in LOCATION_MAP:return LOCATION_MAP[s]
    for city in SELL_LOCATIONS:
        if s.lower()==city.lower():return city
    return s

def live_depth(uid,city,quality=1,side="sell",within_pct=0.01,max_hours=6):
    """Observed live NATS depth. This is only as complete as messages seen since the app started."""
    try:
        con=sqlite3.connect(DB)
        cutoff=(datetime.now(timezone.utc)-timedelta(hours=max_hours)).isoformat()
        rows=con.execute("""SELECT price,amount,auction_type FROM live_orders
            WHERE item_id=? AND city=? AND quality=? AND last_seen>=? AND amount>0""",
            (uid,city,quality,cutoff)).fetchall()
        con.close()
        if not rows:return (0,0,0)
        want_offer=(side=="sell")
        filt=[]
        for p,a,typ in rows:
            typ=(typ or "").lower()
            is_offer=("offer" in typ) or typ in ("sell","0")
            if is_offer==want_offer:filt.append((int(p),int(a)))
        if not filt:return (0,0,0)
        best=min(p for p,a in filt) if want_offer else max(p for p,a in filt)
        if want_offer:
            qty=sum(a for p,a in filt if p<=best*(1+within_pct))
        else:
            qty=sum(a for p,a in filt if p>=best*(1-within_pct))
        return (best,qty,len(filt))
    except:
        return (0,0,0)

def history_for(b,locations):
    end=datetime.now(timezone.utc).date()
    start=end-timedelta(days=HISTORY_DAYS)
    q=urllib.parse.urlencode({
        "date":start.isoformat(),"end_date":end.isoformat(),
        "locations":",".join(locations),"qualities":"1","time-scale":"24"
    })
    return get_json(f"{BASE_URL}/api/v2/stats/history/{api_ids(b)}.json?{q}")

def history_for_all_qualities(b,locations):
    end=datetime.now(timezone.utc).date()
    start=end-timedelta(days=HISTORY_DAYS)
    q=urllib.parse.urlencode({
        "date":start.isoformat(),"end_date":end.isoformat(),
        "locations":",".join(locations),"qualities":"1,2,3,4,5","time-scale":"24"
    })
    return get_json(f"{BASE_URL}/api/v2/stats/history/{api_ids(b)}.json?{q}")

def age(s):
    if not s or s.startswith("0001-"): return 10**9
    try:
        d=datetime.fromisoformat(s.replace("Z","+00:00"))
        if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
        return max(0,int((datetime.now(timezone.utc)-d.astimezone(timezone.utc)).total_seconds()/60))
    except:return 10**9

def agetxt(m):
    if m>=10**9:return "?"
    if m<60:return f"{m}m"
    if m<1440:return f"{m//60}h {m%60}m"
    return f"{m//1440}d"

def confidence(source_age,dest_age,vol):
    worst=max(source_age,dest_age)
    if worst<=30 and vol>=10:return "HIGH"
    if worst<=90 and vol>=3:return "MEDIUM"
    return "LOW"

def safe_icon_name(uid):
    return re.sub(r"[^A-Za-z0-9_.@-]","_",uid)+".png"

def download_icon(uid):
    os.makedirs(ICON_DIR,exist_ok=True)
    path=os.path.join(ICON_DIR,safe_icon_name(uid))
    if os.path.exists(path) and os.path.getsize(path)>100:
        return path
    url=ICON_URL.format(uid=urllib.parse.quote(uid,safe="@_"))
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"AlbionMarketAssistant/1.3"})
        with urllib.request.urlopen(req,timeout=15) as r:raw=r.read()
        if len(raw)>100:
            tmp=path+".tmp"
            with open(tmp,"wb") as f:f.write(raw)
            os.replace(tmp,path)
            return path
    except:
        pass
    return None

class App:
    def __init__(self,root):
        dbinit()
        self.root=root
        root.title(f"Albion Market Assistant v{APP_VERSION}")
        root.geometry("1510x840")
        self.style=ttk.Style()
        try:self.style.theme_use("clam")
        except:pass

        self.dark=tk.BooleanVar(value=True)
        self.premium=tk.BooleanVar(value=True)
        self.icon_images={}
        self.craft_icon_images={}
        self.craft_details={}

        self.notebook=ttk.Notebook(root)
        self.notebook.pack(fill="both",expand=True)

        self.dashboard_tab=ttk.Frame(self.notebook)
        self.flips_tab=ttk.Frame(self.notebook)
        self.crafting_tab=ttk.Frame(self.notebook)
        self.refining_tab=ttk.Frame(self.notebook)
        self.watchlist_tab=ttk.Frame(self.notebook)
        self.profile_tab=ttk.Frame(self.notebook)
        self.data_tab=ttk.Frame(self.notebook)
        self.settings_tab=ttk.Frame(self.notebook)
        self.ai_tab=ttk.Frame(self.notebook)
        self.notebook.add(self.dashboard_tab,text="  Dashboard  ")
        self.notebook.add(self.flips_tab,text="  Market Flips  ")
        self.notebook.add(self.crafting_tab,text="  Crafting  ")
        self.notebook.add(self.refining_tab,text="  Refining  ")
        self.notebook.add(self.watchlist_tab,text="  Watchlist  ")
        self.notebook.add(self.profile_tab,text="  My Profile  ")
        self.notebook.add(self.data_tab,text="  Data Health  ")
        self.notebook.add(self.ai_tab,text="  AI Assistant  ")
        self.notebook.add(self.settings_tab,text="  Settings  ")

        self.last_flip_rows=[]
        self.last_craft_rows=[]
        self.last_flip_records=[]
        self.last_craft_records=[]
        self.watchlist=self.load_watchlist()
        self.manual_price_overrides=self.load_manual_overrides()
        self.nats_status=tk.StringVar(value="Live order feed: starting...")
        self.nats_messages=0

        self.build_dashboard_tab()
        self.build_flips_tab()
        self.build_crafting_tab()
        self.build_refining_tab()
        self.build_watchlist_tab()
        self.build_profile_tab()
        self.build_data_tab()
        self.build_ai_tab()
        self.build_settings_tab()
        for tr in (self.tree,self.craft_tree,self.dashboard_tree,self.watch_tree,self.data_tree):
            self.make_tree_sortable(tr)
        self.load_settings()
        self.theme()
        self.update_rrr_preview()
        self.refresh_watchlist_view()
        self.refresh_dashboard()
        self.refresh_data_health()
        self.root.protocol("WM_DELETE_WINDOW",self.on_close)
        threading.Thread(target=self.start_nats_listener,daemon=True).start()

    # ---------- UI ----------


    # ---------- local/manual price state ----------
    def load_manual_overrides(self):
        try:
            with open(OVERRIDES_FILE,"r",encoding="utf-8") as f:
                data=json.load(f)
            return data if isinstance(data,dict) else {}
        except:
            return {}

    def save_manual_overrides(self):
        try:
            with open(OVERRIDES_FILE,"w",encoding="utf-8") as f:
                json.dump(self.manual_price_overrides,f,indent=2)
        except:
            pass

    def material_override_key(self,mid,city):
        return f"mat|{mid}|{city}"

    def output_override_key(self,uid,city):
        return f"out|{uid}|{city}"

    def effective_material_price(self,m,d):
        key=self.material_override_key(m["id"],d["craftcity"])
        if key in self.manual_price_overrides:
            try:return float(self.manual_price_overrides[key]),True
            except:pass
        return float(m.get("market_price",m.get("price",0)) or 0),False

    def craft_semantic_tag(self,d):
        if d.get("profit",0) < 0:return "LOSS"
        if d.get("refresh")=="YES":return "STALE"
        if d.get("profit",0)>0:return "ACTION"
        return "NORMAL"

    def current_rrr_for_record(self,d):
        try:
            if self.use_custom_rrr.get():
                return max(0.0,min(.999,float(self.craft_rrr.get())/100.0))
            daily=float(self.daily_bonus.get().replace("%",""))
            prod=BASE_PRODUCTION_BONUS+daily+(FOCUS_PRODUCTION_BONUS if self.use_focus.get() else 0)
            if d.get("specialty") and d.get("craftcity")==d.get("specialty"):
                prod+=SPECIALTY_PRODUCTION_BONUS
            return production_bonus_to_rrr(prod)
        except:
            return float(d.get("rrr",0))

    def nofocus_rrr_for_record(self,d):
        try:
            daily=float(self.daily_bonus.get().replace("%",""))
            prod=BASE_PRODUCTION_BONUS+daily
            if d.get("specialty") and d.get("craftcity")==d.get("specialty"):
                prod+=SPECIALTY_PRODUCTION_BONUS
            return production_bonus_to_rrr(prod)
        except:return 0.0

    def recalculate_loaded_crafts(self,flash=True):
        """Purely local recalculation. No AODP/network request."""
        if not getattr(self,"last_craft_records",None):return
        try:
            new_runs=max(1,int(float(self.craft_runs.get())))
            station_per=float(self.craft_station_fee.get())
            focus_cost_per=max(0.0,float(self.craft_focus_cost.get()))
        except:
            return
        premium=bool(self.premium.get())
        sell_mode=self.craft_sell_mode.get() if hasattr(self,"craft_sell_mode") else "Sell Order"
        use_focus=bool(self.use_focus.get())
        changed=[]
        for d in self.last_craft_records:
            old_runs=max(1,float(d.get("runs",1)))
            rrr=self.current_rrr_for_record(d)
            raw=0.0;returned=0.0;returnable_raw=0.0
            for m in d.get("materials",[]):
                base_count=float(m.get("count_per_run",float(m.get("count",0))/old_runs))
                price,is_manual=self.effective_material_price(m,d)
                qty=base_count*new_runs
                gross=price*qty
                ret=gross*rrr if m.get("returnable",True) else 0.0
                m["count_per_run"]=base_count
                m["count"]=qty
                m["price"]=price
                m["gross"]=gross
                m["return_value"]=ret
                m["manual"]=is_manual
                raw+=gross;returned+=ret
                if m.get("returnable",True):returnable_raw+=gross

            station=station_per*new_runs
            netmat=raw-returned
            basis=netmat+station

            out_key=self.output_override_key(d["uid"],d["sellcity"])
            output_manual=None
            if out_key in self.manual_price_overrides:
                try:output_manual=float(self.manual_price_overrides[out_key])
                except:output_manual=None

            if output_manual is not None:
                sale_unit=output_manual
                fees_unit=sale_fee(sale_unit,premium,sell_mode)
            else:
                qp=d.get("quality_prices") or []
                sale_unit=sum(float(w)*float(p) for q,w,p,a in qp)
                fees_unit=sum(float(w)*sale_fee(float(p),premium,sell_mode) for q,w,p,a in qp)

            sell=sale_unit*new_runs
            fees=fees_unit*new_runs
            profit=sell-fees-basis
            roi=(profit/basis*100) if basis>0 else 0.0
            total_focus=focus_cost_per*new_runs if use_focus else 0.0
            p10k=(profit/total_focus*10000) if total_focus>0 else None

            extra10k=None;extra_profit=None
            if use_focus and not self.use_custom_rrr.get():
                nr=self.nofocus_rrr_for_record(d)
                nofocus_return=returnable_raw*nr
                extra_profit=returned-nofocus_return
                if total_focus>0:extra10k=extra_profit/total_focus*10000

            d.update({
                "runs":new_runs,"rrr":rrr,"raw":raw,"returned":returned,"netmat":netmat,
                "station":station,"sell":sell,"fees":fees,"profit":profit,"roi":roi,
                "p10k":p10k,"extra10k":extra10k,"extra_profit":extra_profit,
                "days":(new_runs/d["volume"] if d.get("volume",0)>0 else None),
                "focus":use_focus,"focus_total":total_focus,"output_manual":output_manual
            })
            iid=d.get("_iid")
            if iid and self.craft_tree.exists(iid):
                vals=list(self.craft_tree.item(iid,"values"))
                # columns: item,tier,route,runs,craftcity,bonus,rrr,sellcity,raw,returned,net,station,sell,fees,profit,roi,p10k,extra10k,volume,days,depth,ages...
                vals[3]=new_runs
                vals[6]=f"{rrr*100:.1f}%"
                vals[8]=f"{raw:,.0f}";vals[9]=f"{returned:,.0f}";vals[10]=f"{netmat:,.0f}"
                vals[11]=f"{station:,.0f}";vals[12]=f"{sell:,.0f}";vals[13]=f"{fees:,.0f}"
                vals[14]=f"{profit:,.0f}";vals[15]=f"{roi:.1f}%"
                vals[16]=f"{p10k:,.0f}" if p10k is not None else "—"
                vals[17]=f"{extra10k:,.0f}" if extra10k is not None else "—"
                vals[19]=f"{d['days']:.1f}" if d.get("days") is not None else "—"
                self.craft_tree.item(iid,values=vals,tags=("CHANGED",) if flash else (self.craft_semantic_tag(d),))
                changed.append((iid,d))
        # Rank locally without fetching anything.
        ranked=sorted(self.last_craft_records,key=lambda x:(x.get("profit",0),x.get("roi",0)),reverse=True)
        for n,d in enumerate(ranked):
            iid=d.get("_iid")
            if iid and self.craft_tree.exists(iid):self.craft_tree.move(iid,"",n)
        self.last_craft_rows=[list(self.craft_tree.item(d["_iid"],"values")) for d in ranked if d.get("_iid") and self.craft_tree.exists(d["_iid"])]
        focus_text="ON" if use_focus else "OFF"
        self.craft_status.set(f"Updated locally — Focus {focus_text} • {new_runs} runs • no market scan performed.")
        self.update_watchlist_from_scans();self.refresh_dashboard()
        if flash:
            def restore():
                for iid,d in changed:
                    if self.craft_tree.exists(iid):
                        self.craft_tree.item(iid,tags=(self.craft_semantic_tag(d),))
            self.root.after(700,restore)

    def on_local_craft_change(self,event=None):
        self.update_rrr_preview()
        self.recalculate_loaded_crafts(True)

    def refresh_selected_craft_prices(self):
        sels=self.craft_tree.selection()
        if not sels:
            messagebox.showinfo("Refresh prices","Select one or more crafting results first.")
            return
        recs=[self.craft_details.get(i) for i in sels if self.craft_details.get(i)]
        if not recs:return
        def work():
            try:
                ids=set();locs=set()
                for d in recs:
                    ids.add(d["uid"]);locs.add(d["sellcity"]);locs.add(d["craftcity"])
                    for m in d.get("materials",[]):ids.add(m["id"])
                rows=[]
                for b in batches(sorted(ids)):
                    rows.extend(prices_for_qualities(b,sorted(locs)))
                    time.sleep(.12)
                pmap={(r.get("item_id"),r.get("city"),int(r.get("quality") or 1)):r for r in rows}
                for d in recs:
                    for m in d.get("materials",[]):
                        row=pmap.get((m["id"],d["craftcity"],1))
                        p,dt=market_price(row,d.get("buy_mode","Instant Buy"))
                        if p:
                            m["market_price"]=float(p);m["age"]=age(dt)
                    newq=[]
                    oldq=d.get("quality_prices") or [(1,1.0,0,0)]
                    for q,w,oldp,olda in oldq:
                        row=pmap.get((d["uid"],d["sellcity"],q))
                        p,dt=market_price(row,d.get("sell_mode","Sell Order"))
                        if p:newq.append((q,w,float(p),age(dt)))
                        else:newq.append((q,w,oldp,olda))
                    d["quality_prices"]=newq
                    d["mat_age"]=max([m.get("age",0) for m in d.get("materials",[])] or [0])
                    d["out_age"]=max([a for q,w,p,a in newq] or [0])
                    d["refresh"]="YES" if max(d["mat_age"],d["out_age"])>90 else "No"
                self.root.after(0,lambda:self._finish_selected_price_refresh(len(recs)))
            except Exception as e:
                self.root.after(0,lambda:messagebox.showerror("Price refresh failed",str(e)))
        self.craft_status.set(f"Refreshing prices for {len(recs)} selected result(s)...")
        threading.Thread(target=work,daemon=True).start()

    def _finish_selected_price_refresh(self,n):
        self.recalculate_loaded_crafts(False)
        for d in self.last_craft_records:
            iid=d.get("_iid")
            if iid and self.craft_tree.exists(iid):
                vals=list(self.craft_tree.item(iid,"values"))
                vals[21]=agetxt(d.get("mat_age",0));vals[22]=agetxt(d.get("out_age",0));vals[23]=d.get("refresh","No")
                self.craft_tree.item(iid,values=vals,tags=(self.craft_semantic_tag(d),))
        self.craft_status.set(f"Refreshed current AODP prices for {n} selected result(s). Manual price overrides were preserved.")

    def build_dashboard_tab(self):
        f=ttk.Frame(self.dashboard_tab,padding=18);f.pack(fill="both",expand=True)
        ttk.Label(f,text="What Should I Do Right Now?",style="Title.TLabel").pack(anchor="w")
        ttk.Label(f,text="Ranks the best actionable results from your latest Flip and Craft scans. Red/stale results are excluded from the recommended section.").pack(anchor="w",pady=(4,12))
        self.dashboard_summary=tk.StringVar(value="Run a Market Flips scan and/or Crafting scan to populate recommendations.")
        ttk.Label(f,textvariable=self.dashboard_summary,font=("Segoe UI",11,"bold")).pack(anchor="w",pady=(0,8))
        cols=("action","item","setup","profit","roi","sales","depth","confidence")
        self.dashboard_tree=ttk.Treeview(f,columns=cols,show="headings",height=14)
        for c,t,w in [
            ("action","Action",85),("item","Item",290),("setup","Setup",270),("profit","Profit",100),
            ("roi","ROI",75),("sales","14d/day",80),("depth","Live depth",80),("confidence","Confidence",90)]:
            self.dashboard_tree.heading(c,text=t);self.dashboard_tree.column(c,width=w,anchor="e" if c in ("profit","roi","sales","depth") else "w")
        self.dashboard_tree.pack(fill="both",expand=True)
        ttk.Label(f,text="Tip: HIGH/MEDIUM + fresh data + enough sales/depth should beat a giant theoretical profit with LOW confidence.").pack(anchor="w",pady=(10,0))

    def refresh_dashboard(self):
        if not hasattr(self,"dashboard_tree"):return
        for x in self.dashboard_tree.get_children():self.dashboard_tree.delete(x)
        actions=[]
        for d in getattr(self,"last_craft_records",[]):
            if d.get("refresh")=="YES":continue
            score=(confidence_rank(d.get("confidence"))*1_000_000)+(d.get("profit",0))
            setup=f"{d.get('route')} ×{d.get('runs')} • {d.get('craftcity')} → {d.get('sellcity')}"
            actions.append((score,"CRAFT",f"{d.get('name')} ({tier(d.get('uid'))}.{enchant(d.get('uid'))})",setup,d.get("profit",0),d.get("roi",0),d.get("volume",0),d.get("depth",0),d.get("confidence","LOW")))
        for d in getattr(self,"last_flip_records",[]):
            if d.get("refresh")=="YES":continue
            score=(confidence_rank(d.get("confidence"))*1_000_000)+(d.get("profit",0))
            setup=f"{d.get('buycity')} → {d.get('sellcity')}"
            actions.append((score,"FLIP",f"{d.get('name')} ({d.get('tier')})",setup,d.get("profit",0),d.get("roi",0),d.get("volume",0),d.get("depth",0),d.get("confidence","LOW")))
        actions.sort(reverse=True,key=lambda x:x[0])
        for a in actions[:15]:
            _,act,item,setup,profit,roi,sales,depth,conf=a
            tag="ACTION" if profit>0 else "LOSS"
            self.dashboard_tree.insert("","end",tags=(tag,),values=(act,item,setup,f"{profit:,.0f}",f"{roi:.1f}%",f"{sales:.1f}",depth or "—",conf))
        if actions:
            high=sum(1 for x in actions if x[-1]=="HIGH");med=sum(1 for x in actions if x[-1]=="MEDIUM")
            self.dashboard_summary.set(f"{len(actions)} fresh actionable candidates • {high} HIGH • {med} MEDIUM. The table shows the best 15.")
        else:
            self.dashboard_summary.set("No fresh actionable candidates yet. Run scans, or refresh stale markets in-game and scan again.")

    def build_profile_tab(self):
        f=ttk.Frame(self.profile_tab,padding=18);f.pack(fill="both",expand=True)
        ttk.Label(f,text="My Crafting Profile",style="Title.TLabel").pack(anchor="w")
        ttk.Label(f,text="Quality is explicit so the app never invents your spec-dependent probabilities. Enter the quality percentages you actually want modeled.").pack(anchor="w",pady=(4,14))
        self.profile_fce=tk.StringVar(value="0")
        row=ttk.Frame(f);row.pack(anchor="w",fill="x")
        ttk.Label(row,text="Focus Cost Efficiency (reference):").pack(side="left")
        ttk.Entry(row,textvariable=self.profile_fce,width=10).pack(side="left",padx=(6,18))
        ttk.Label(row,text="Focus cost itself remains the exact number you enter on the Crafting tab, because it varies by item/spec.").pack(side="left")
        ttk.Separator(f,orient="horizontal").pack(fill="x",pady=16)
        ttk.Label(f,text="Expected crafted quality mix",font=("Segoe UI",11,"bold")).pack(anchor="w")
        self.quality_vars={}
        qrow=ttk.Frame(f);qrow.pack(anchor="w",pady=(8,4))
        defaults={1:"100",2:"0",3:"0",4:"0",5:"0"}
        for q in range(1,6):
            v=tk.StringVar(value=defaults[q]);self.quality_vars[q]=v
            box=ttk.Frame(qrow);box.pack(side="left",padx=(0,12))
            ttk.Label(box,text=QUALITY_NAMES[q]).pack(anchor="w")
            ttk.Entry(box,textvariable=v,width=7).pack(side="left");ttk.Label(box,text="%").pack(side="left")
        ttk.Button(f,text="NORMAL ONLY",command=self.set_quality_normal).pack(anchor="w",pady=(8,4))
        ttk.Label(f,text="Use 'My quality mix' on the Crafting tab when you want expected sale value across Normal/Good/Outstanding/Excellent/Masterpiece.").pack(anchor="w",pady=(4,0))

    def get_quality_mix(self):
        vals={}
        total=0.0
        for q,v in getattr(self,"quality_vars",{}).items():
            try:x=max(0.0,float(v.get()))
            except:x=0.0
            vals[q]=x;total+=x
        if total<=0:return {q:0 for q in range(1,6)}
        return {q:vals.get(q,0)/total for q in range(1,6)}

    def set_quality_normal(self):
        for q,v in self.quality_vars.items():v.set("100" if q==1 else "0")

    def build_refining_tab(self):
        f=ttk.Frame(self.refining_tab,padding=18);f.pack(fill="both",expand=True)
        ttk.Label(f,text="Refining Calculator",style="Title.TLabel").grid(row=0,column=0,columnspan=12,sticky="w")
        ttk.Label(f,text="Functional market-aware refining calculator. Station fee stays manual, exactly as requested.").grid(row=1,column=0,columnspan=12,sticky="w",pady=(3,14))
        self.ref_type=tk.StringVar(value="Planks");self.ref_tier=tk.StringVar(value="T4");self.ref_enchant=tk.StringVar(value=".0")
        self.ref_city=tk.StringVar(value="Fort Sterling");self.ref_focus=tk.BooleanVar(value=True);self.ref_daily=tk.StringVar(value="0%")
        self.ref_runs=tk.StringVar(value="100");self.ref_station=tk.StringVar(value="0");self.ref_buy_mode=tk.StringVar(value="Instant Buy");self.ref_sell_mode=tk.StringVar(value="Sell Order")
        self.ref_sell_city=tk.StringVar(value="Auto")
        controls=[
            ("Resource",ttk.Combobox(f,textvariable=self.ref_type,values=["Planks","Metal Bars","Cloth","Leather","Stone Blocks"],state="readonly",width=13)),
            ("Tier",ttk.Combobox(f,textvariable=self.ref_tier,values=["T2","T3","T4","T5","T6","T7","T8"],state="readonly",width=6)),
            ("Enchant",ttk.Combobox(f,textvariable=self.ref_enchant,values=[".0",".1",".2",".3",".4"],state="readonly",width=6)),
            ("Refine city",ttk.Combobox(f,textvariable=self.ref_city,values=CRAFT_CITIES,state="readonly",width=13)),
            ("Runs",ttk.Entry(f,textvariable=self.ref_runs,width=8)),
            ("Station fee/run",ttk.Entry(f,textvariable=self.ref_station,width=10)),
        ]
        c=0
        for lab,w in controls:
            ttk.Label(f,text=lab).grid(row=2,column=c,sticky="w",padx=(0,4));w.grid(row=2,column=c+1,sticky="w",padx=(0,12));c+=2
        ttk.Checkbutton(f,text="Use Focus",variable=self.ref_focus).grid(row=3,column=0,sticky="w",pady=(10,0))
        ttk.Label(f,text="Daily bonus").grid(row=3,column=1,sticky="e",pady=(10,0))
        ttk.Combobox(f,textvariable=self.ref_daily,values=["0%","10%","20%"],state="readonly",width=6).grid(row=3,column=2,sticky="w",pady=(10,0))
        ttk.Combobox(f,textvariable=self.ref_buy_mode,values=["Instant Buy","Buy Order"],state="readonly",width=11).grid(row=3,column=3,sticky="w",padx=(8,0),pady=(10,0))
        ttk.Combobox(f,textvariable=self.ref_sell_mode,values=["Sell Order","Instant Sell"],state="readonly",width=11).grid(row=3,column=4,sticky="w",padx=(8,0),pady=(10,0))
        ttk.Label(f,text="Sell city").grid(row=3,column=5,sticky="e",pady=(10,0))
        ttk.Combobox(f,textvariable=self.ref_sell_city,values=["Auto"]+CRAFT_CITIES,state="readonly",width=13).grid(row=3,column=6,sticky="w",pady=(10,0))
        ttk.Button(f,text="CALCULATE",command=self.start_refining).grid(row=3,column=10,columnspan=2,sticky="e",pady=(10,0))
        self.ref_status=tk.StringVar(value="Ready.")
        ttk.Label(f,textvariable=self.ref_status,font=("Segoe UI",11,"bold")).grid(row=4,column=0,columnspan=12,sticky="w",pady=(16,8))
        self.ref_text=tk.Text(f,height=24,width=125,wrap="none")
        self.apply_text_theme(self.ref_text)
        self.ref_text.grid(row=5,column=0,columnspan=12,sticky="nsew")
        f.rowconfigure(5,weight=1);f.columnconfigure(11,weight=1)

    def refining_ids(self,res,t,e):
        mapping={"Planks":("WOOD","PLANKS","Fort Sterling"),"Metal Bars":("ORE","METALBAR","Thetford"),
                 "Cloth":("FIBER","CLOTH","Lymhurst"),"Leather":("HIDE","LEATHER","Martlock"),
                 "Stone Blocks":("ROCK","STONEBLOCK","Bridgewatch")}
        raw,refined,bonus=mapping[res]
        def eid(ti,name,en):
            if en==0:return f"T{ti}_{name}"
            return f"T{ti}_{name}_LEVEL{en}@{en}"
        out=eid(t,refined,e)
        rawid=eid(t,raw,e)
        lower=None
        if t>2:
            lowe=0 if t<=4 else e
            lower=eid(t-1,refined,lowe)
        rawcount={2:1,3:2,4:2,5:3,6:4,7:5,8:5}[t]
        return rawid,lower,out,rawcount,bonus

    def start_refining(self):
        threading.Thread(target=self.calculate_refining,daemon=True).start()

    def calculate_refining(self):
        try:
            res=self.ref_type.get();t=int(self.ref_tier.get()[1:]);e=int(self.ref_enchant.get()[1:])
            if res=="Stone Blocks" and e>0:
                raise RuntimeError("Enchanted stone refining has special multi-output rules. This calculator intentionally blocks it rather than giving you a wrong number.")
            runs=max(1,int(self.ref_runs.get()));station=float(self.ref_station.get())
            rawid,lower,outid,rawcount,bonuscity=self.refining_ids(res,t,e)
            ids=[rawid,outid]+([lower] if lower else [])
            rows=prices_for_qualities(ids,CRAFT_CITIES,(1,))
            p={(r.get("item_id"),r.get("city")):r for r in rows}
            def buy(mid):
                row=p.get((mid,self.ref_city.get()));return market_price(row,self.ref_buy_mode.get())
            rp,rd=buy(rawid)
            if not rp:raise RuntimeError(f"No usable {self.ref_buy_mode.get()} price for {rawid} in {self.ref_city.get()}.")
            lp=0;ld=None
            if lower:
                lp,ld=buy(lower)
                if not lp:raise RuntimeError(f"No usable price for lower-tier refined material {lower} in {self.ref_city.get()}.")
            prod=BASE_PRODUCTION_BONUS+float(self.ref_daily.get().replace("%",""))
            if self.ref_city.get()==bonuscity:prod+=40.0
            if self.ref_focus.get():prod+=FOCUS_PRODUCTION_BONUS
            rrr=production_bonus_to_rrr(prod)
            gross_per=rawcount*rp+lp
            returned_per=gross_per*rrr
            basis=(gross_per-returned_per+station)*runs
            sellcities=CRAFT_CITIES if self.ref_sell_city.get()=="Auto" else [self.ref_sell_city.get()]
            best=None
            for city in sellcities:
                row=p.get((outid,city));sp,sd=market_price(row,self.ref_sell_mode.get())
                if not sp:continue
                fees=sale_fee(sp,self.premium.get(),self.ref_sell_mode.get())*runs
                revenue=sp*runs;profit=revenue-fees-basis;roi=profit/basis*100 if basis else 0
                if best is None or profit>best[0]:best=(profit,roi,city,sp,fees,sd)
            if not best:raise RuntimeError("No usable output price found.")
            profit,roi,city,sp,fees,sd=best
            txt=(
                f"{res} {t}.{e} × {runs}\\n"
                f"Refine city: {self.ref_city.get()}  |  bonus city: {bonuscity}  |  Focus: {'ON' if self.ref_focus.get() else 'OFF'}\\n"
                f"Production bonus: {prod:.1f}%  →  RRR: {rrr*100:.1f}%\\n\\n"
                f"INPUTS PER RUN\\n"
                f"  {rawcount} × {rawid} @ {rp:,.0f} = {rawcount*rp:,.0f}\\n" +
                (f"  1 × {lower} @ {lp:,.0f} = {lp:,.0f}\\n" if lower else "") +
                f"  Gross input cost/run: {gross_per:,.0f}\\n"
                f"  Expected returned value/run: {returned_per:,.0f}\\n"
                f"  Manual station fee/run: {station:,.0f}\\n\\n"
                f"BATCH COST BASIS: {basis:,.0f}\\n"
                f"SELL: {city} via {self.ref_sell_mode.get()} @ {sp:,.0f} × {runs}\\n"
                f"Market fees/tax: {fees:,.0f}\\n"
                f"PROFIT: {profit:,.0f}\\nROI: {roi:.1f}%\\n"
                f"Output price age: {agetxt(age(sd))}\\n"
            )
            self.root.after(0,lambda:self._set_ref_text(txt))
        except Exception as ex:
            self.root.after(0,lambda:messagebox.showerror("Refining error",str(ex)))

    def _set_ref_text(self,txt):
        self.ref_text.config(state="normal");self.ref_text.delete("1.0","end");self.ref_text.insert("1.0",txt);self.ref_text.config(state="disabled")
        self.ref_status.set("Calculation complete.")

    def build_watchlist_tab(self):
        f=ttk.Frame(self.watchlist_tab,padding=18);f.pack(fill="both",expand=True)
        ttk.Label(f,text="Watchlist",style="Title.TLabel").pack(anchor="w")
        ttk.Label(f,text="Add items from Flip or Crafting results. Their last scanned profit/confidence updates automatically.").pack(anchor="w",pady=(4,10))
        cols=("type","item","setup","profit","target","refresh","confidence")
        self.watch_tree=ttk.Treeview(f,columns=cols,show="headings")
        for c,t,w in [("type","Type",70),("item","Item",300),("setup","Setup",300),("profit","Last profit",100),("target","Target profit",100),("refresh","Refresh?",80),("confidence","Confidence",90)]:
            self.watch_tree.heading(c,text=t);self.watch_tree.column(c,width=w,anchor="e" if c in ("profit","target") else "w")
        self.watch_tree.pack(fill="both",expand=True)
        b=ttk.Frame(f);b.pack(fill="x",pady=(8,0))
        ttk.Button(b,text="REMOVE SELECTED",command=self.remove_watch_selected).pack(side="left")
        ttk.Button(b,text="SET TARGET",command=self.set_watch_target).pack(side="left",padx=(6,0))
        ttk.Button(b,text="REFRESH VIEW",command=self.refresh_watchlist_view).pack(side="left",padx=(6,0))

    def load_watchlist(self):
        try:return json.load(open(WATCHLIST_FILE,encoding="utf-8"))
        except:return []

    def save_watchlist(self):
        try:
            with open(WATCHLIST_FILE,"w",encoding="utf-8") as f:json.dump(self.watchlist,f,indent=2)
        except:pass

    def add_record_to_watchlist(self,kind,d):
        if kind=="craft":
            key=f"craft|{d.get('uid')}|{d.get('route')}|{d.get('craftcity')}|{d.get('sellcity')}"
            item={"key":key,"type":"Craft","uid":d.get("uid"),"name":d.get("name"),"setup":f"{d.get('route')} • {d.get('craftcity')} → {d.get('sellcity')}",
                  "profit":d.get("profit",0),"target":0,"refresh":d.get("refresh"),"confidence":d.get("confidence")}
        else:
            key=f"flip|{d.get('uid')}|{d.get('buycity')}|{d.get('sellcity')}"
            item={"key":key,"type":"Flip","uid":d.get("uid"),"name":d.get("name"),"setup":f"{d.get('buycity')} → {d.get('sellcity')}",
                  "profit":d.get("profit",0),"target":0,"refresh":d.get("refresh"),"confidence":d.get("confidence")}
        if not any(x.get("key")==key for x in self.watchlist):self.watchlist.append(item)
        self.save_watchlist();self.refresh_watchlist_view()

    def add_selected_to_watchlist(self,kind):
        if kind=="craft":
            for iid in self.craft_tree.selection():
                d=self.craft_details.get(iid)
                if d:self.add_record_to_watchlist("craft",d)
        else:
            sel=self.tree.selection()
            if not sel:return
            # Match selected displayed item to latest record.
            vals=self.tree.item(sel[0],"values")
            if not vals:return
            for d in self.last_flip_records:
                if f"{d['name']} ({d['tier']})"==vals[0] and d["buycity"]==vals[2] and d["sellcity"]==vals[3]:
                    self.add_record_to_watchlist("flip",d);break

    def refresh_watchlist_view(self):
        if not hasattr(self,"watch_tree"):return
        for x in self.watch_tree.get_children():self.watch_tree.delete(x)
        for i,w in enumerate(self.watchlist):
            tag="STALE" if w.get("refresh")=="YES" else ("ACTION" if w.get("profit",0)>0 else "LOSS")
            self.watch_tree.insert("","end",iid=f"w{i}",tags=(tag,),values=(w.get("type"),w.get("name"),w.get("setup"),f"{w.get('profit',0):,.0f}",f"{w.get('target',0):,.0f}",w.get("refresh","?"),w.get("confidence","?")))

    def update_watchlist_from_scans(self):
        lookup={}
        for d in self.last_craft_records:
            key=f"craft|{d.get('uid')}|{d.get('route')}|{d.get('craftcity')}|{d.get('sellcity')}"
            lookup[key]=d
        for d in self.last_flip_records:
            key=f"flip|{d.get('uid')}|{d.get('buycity')}|{d.get('sellcity')}"
            lookup[key]=d
        changed=False
        for w in self.watchlist:
            d=lookup.get(w.get("key"))
            if d:
                w["profit"]=d.get("profit",0);w["refresh"]=d.get("refresh");w["confidence"]=d.get("confidence");changed=True
        if changed:self.save_watchlist()
        self.refresh_watchlist_view()

    def remove_watch_selected(self):
        idxs=sorted([int(x[1:]) for x in self.watch_tree.selection() if x.startswith("w")],reverse=True)
        for i in idxs:
            if 0<=i<len(self.watchlist):self.watchlist.pop(i)
        self.save_watchlist();self.refresh_watchlist_view()

    def set_watch_target(self):
        sel=self.watch_tree.selection()
        if not sel:return
        import tkinter.simpledialog as sd
        val=sd.askfloat("Target profit","Notify visually when last scanned profit reaches:",minvalue=0)
        if val is None:return
        for iid in sel:
            i=int(iid[1:]);self.watchlist[i]["target"]=val
        self.save_watchlist();self.refresh_watchlist_view()

    def build_data_tab(self):
        f=ttk.Frame(self.data_tab,padding=18);f.pack(fill="both",expand=True)
        ttk.Label(f,text="Data Health & Live Order Depth",style="Title.TLabel").pack(anchor="w")
        ttk.Label(f,text="HTTP prices give broad snapshots. The optional NATS listener builds a local live order cache from new market reports while this app is open.").pack(anchor="w",pady=(4,8))
        ttk.Label(f,textvariable=self.nats_status,font=("Segoe UI",11,"bold")).pack(anchor="w",pady=(0,8))
        self.data_summary=tk.StringVar(value="")
        ttk.Label(f,textvariable=self.data_summary).pack(anchor="w",pady=(0,8))
        ttk.Button(f,text="REFRESH DATA HEALTH",command=self.refresh_data_health).pack(anchor="w",pady=(0,10))
        cols=("city","orders","lastseen")
        self.data_tree=ttk.Treeview(f,columns=cols,show="headings",height=10)
        for c,t,w in [("city","City",160),("orders","Observed live orders",160),("lastseen","Most recent report",220)]:
            self.data_tree.heading(c,text=t);self.data_tree.column(c,width=w)
        self.data_tree.pack(fill="x")
        ttk.Label(f,text="Important: live depth starts empty for a new subscriber. When you load a market page in Albion with AFM/AODP upload enabled, fresh orders should flow into this cache.").pack(anchor="w",pady=(12,0))

    def start_nats_listener(self):
        try:
            import nats
        except Exception:
            self.root.after(0,lambda:self.nats_status.set("Live order feed: nats-py is not installed. Use the included launcher; it installs it automatically."))
            return
        async def runner():
            try:
                nc=await nats.connect(NATS_URL,connect_timeout=5,max_reconnect_attempts=-1)
                self.root.after(0,lambda:self.nats_status.set("Live order feed: CONNECTED — warming local order depth cache."))
                async def cb(msg):
                    try:
                        payload=json.loads(msg.data.decode("utf-8"))
                        orders=payload.get("Orders") if isinstance(payload,dict) else None
                        if orders is None and isinstance(payload,list):orders=payload
                        if orders is None and isinstance(payload,dict) and "ItemTypeId" in payload:orders=[payload]
                        if not orders:return
                        now=datetime.now(timezone.utc).isoformat()
                        con=sqlite3.connect(DB)
                        n=0
                        for o in orders:
                            city=normalize_location(o.get("LocationId"))
                            if city not in SELL_LOCATIONS:continue
                            oid=str(o.get("Id",""))
                            if not oid:continue
                            con.execute("""INSERT INTO live_orders(order_id,item_id,city,quality,price,amount,auction_type,expires,last_seen)
                                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(order_id,city) DO UPDATE SET
                                item_id=excluded.item_id,quality=excluded.quality,price=excluded.price,amount=excluded.amount,
                                auction_type=excluded.auction_type,expires=excluded.expires,last_seen=excluded.last_seen""",
                                (oid,o.get("ItemTypeId"),city,int(o.get("QualityLevel") or 1),int(o.get("UnitPriceSilver") or 0),
                                 int(o.get("Amount") or 0),str(o.get("AuctionType","")),str(o.get("Expires","")),now))
                            n+=1
                        con.commit();con.close()
                        self.nats_messages+=n
                        if n and self.nats_messages%50< n:self.root.after(0,self.refresh_data_health)
                    except:pass
                await nc.subscribe(NATS_TOPIC,cb=cb)
                while True:
                    await asyncio.sleep(60)
            except Exception as e:
                self.root.after(0,lambda:self.nats_status.set(f"Live order feed: reconnecting/unavailable ({e})"))
        try:asyncio.run(runner())
        except:pass

    def refresh_data_health(self):
        if not hasattr(self,"data_tree"):return
        try:
            con=sqlite3.connect(DB)
            total=con.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0]
            rows=con.execute("SELECT city,COUNT(*),MAX(last_seen) FROM live_orders GROUP BY city ORDER BY city").fetchall()
            con.close()
        except:
            total=0;rows=[]
        for x in self.data_tree.get_children():self.data_tree.delete(x)
        for city,count,last in rows:
            la=age(last) if last else 10**9
            self.data_tree.insert("","end",values=(city,count,agetxt(la)))
        self.data_summary.set(f"Local live-order rows: {total:,} • received this run: {self.nats_messages:,} • HTTP history window: {HISTORY_DAYS} days.")

    def combine_selected_shopping_list(self):
        sels=self.craft_tree.selection()
        if not sels:
            messagebox.showinfo("Select crafts","Select one or more Crafting rows first (Ctrl-click for multiple).")
            return
        agg={};cost=0
        for iid in sels:
            d=self.craft_details.get(iid)
            if not d:continue
            for m in d.get("materials",[]):
                key=(m["id"],m["name"],m["price"])
                agg[key]=agg.get(key,0)+m["count"];cost+=m["gross"]
        lines=["COMBINED SHOPPING LIST",""]
        for (mid,nm,price),qty in sorted(agg.items(),key=lambda x:x[0][1]):
            lines.append(f"{qty:g}x {nm} @ {price:,.0f} each")
        lines+=["",f"Upfront material total: {cost:,.0f}"]
        text="\n".join(lines)
        self.root.clipboard_clear();self.root.clipboard_append(text)
        messagebox.showinfo("Copied",f"Combined shopping list for {len(sels)} selected craft(s) copied to clipboard.")

    def log_session(self,kind,d):
        try:
            hist=json.load(open(SESSION_FILE,encoding="utf-8")) if os.path.exists(SESSION_FILE) else []
        except:hist=[]
        hist.append({"time":datetime.now(timezone.utc).isoformat(),"kind":kind,"uid":d.get("uid"),"name":d.get("name"),
                     "profit":d.get("profit"),"roi":d.get("roi"),"setup":d.get("route",""),"status":"planned"})
        try:
            with open(SESSION_FILE,"w",encoding="utf-8") as f:json.dump(hist,f,indent=2)
            messagebox.showinfo("Logged","Saved to session_history.json.")
        except Exception as e:messagebox.showerror("Log error",str(e))

    def save_settings(self):
        data={"dark":self.dark.get(),"premium":self.premium.get(),"craft_city":self.craft_city.get(),"focus":self.use_focus.get(),
              "daily":self.daily_bonus.get(),"buy_mode":getattr(self,"craft_buy_mode",tk.StringVar(value="Instant Buy")).get(),
              "sell_mode":getattr(self,"craft_sell_mode",tk.StringVar(value="Sell Order")).get(),
              "route_mode":getattr(self,"craft_route_mode",tk.StringVar(value="Both")).get(),
              "quality_mode":getattr(self,"craft_quality_mode",tk.StringVar(value="Normal only")).get(),
              "profile_fce":getattr(self,"profile_fce",tk.StringVar(value="0")).get(),
              "quality":{str(q):v.get() for q,v in getattr(self,"quality_vars",{}).items()}}
        try:
            with open(SETTINGS_FILE,"w",encoding="utf-8") as f:json.dump(data,f,indent=2)
        except:pass

    def load_settings(self):
        try:data=json.load(open(SETTINGS_FILE,encoding="utf-8"))
        except:return
        try:
            self.dark.set(bool(data.get("dark",True)));self.premium.set(bool(data.get("premium",True)))
            self.craft_city.set(data.get("craft_city",self.craft_city.get()));self.use_focus.set(bool(data.get("focus",True)))
            self.daily_bonus.set(data.get("daily",self.daily_bonus.get()))
            if hasattr(self,"craft_buy_mode"):self.craft_buy_mode.set(data.get("buy_mode","Instant Buy"))
            if hasattr(self,"craft_sell_mode"):self.craft_sell_mode.set(data.get("sell_mode","Sell Order"))
            if hasattr(self,"craft_route_mode"):self.craft_route_mode.set(data.get("route_mode","Both"))
            if hasattr(self,"craft_quality_mode"):self.craft_quality_mode.set(data.get("quality_mode","Normal only"))
            if hasattr(self,"profile_fce"):self.profile_fce.set(data.get("profile_fce","0"))
            for q,v in data.get("quality",{}).items():
                if int(q) in self.quality_vars:self.quality_vars[int(q)].set(v)
        except:pass
        self.premium_changed()

    def on_close(self):
        self.save_settings();self.save_watchlist();self.save_manual_overrides();self.root.destroy()

    def build_flips_tab(self):
        top=ttk.Frame(self.flips_tab,padding=12); top.pack(fill="x")
        ttk.Label(top,text="Albion Market Assistant",style="Title.TLabel").grid(row=0,column=0,columnspan=7,sticky="w")
        ttk.Label(top,text="Americas • AODP market data • item icons • Premium-aware fees").grid(row=1,column=0,columnspan=7,sticky="w",pady=(0,10))
        ttk.Checkbutton(top,text="Dark mode",variable=self.dark,command=self.theme).grid(row=0,column=7,rowspan=2,sticky="e")

        self.profit=tk.StringVar(value="40000")
        self.roi=tk.StringVar(value="10")
        self.agev=tk.StringVar(value="180")
        # Volume is informational only for flips. Do not hide rare/high-value
        # opportunities (e.g. 7.4 Excellent) just because historical daily volume is low.
        self.vol=tk.StringVar(value="0")
        for i,(lab,var) in enumerate([("Min profit",self.profit),("Min ROI %",self.roi),("Max age (min)",self.agev)]):
            ttk.Label(top,text=lab).grid(row=2,column=i*2,sticky="w",padx=(0,4))
            ttk.Entry(top,textvariable=var,width=11).grid(row=2,column=i*2+1,sticky="w",padx=(0,14))

        filters=ttk.LabelFrame(self.flips_tab,text="Market filters",padding=(10,7));filters.pack(fill="x",padx=12,pady=(0,6))
        self.tiers={}
        ttk.Label(filters,text="Tiers:").grid(row=0,column=0,sticky="w")
        for n,t in enumerate(range(4,9)):
            v=tk.BooleanVar(value=False);self.tiers[t]=v
            ttk.Checkbutton(filters,text=f"T{t}",variable=v).grid(row=0,column=n+1,sticky="w",padx=(2,4))
        self.enchants={}
        ttk.Label(filters,text="Enchant:").grid(row=0,column=6,sticky="w",padx=(14,2))
        for n,e in enumerate(range(5)):
            v=tk.BooleanVar(value=False);self.enchants[e]=v
            ttk.Checkbutton(filters,text=f".{e}",variable=v).grid(row=0,column=7+n,sticky="w",padx=(2,4))

        self.flip_buy_city=tk.StringVar(value="Any")
        self.flip_sell_city=tk.StringVar(value="Any");self.flip_qty=tk.StringVar(value="1")
        self.flip_search=tk.StringVar(value="");self.flip_category=tk.StringVar(value="All")
        ttk.Label(filters,text="Buy city").grid(row=1,column=0,sticky="w",pady=(8,0))
        ttk.Combobox(filters,textvariable=self.flip_buy_city,values=["Any"]+CITIES,state="readonly",width=12).grid(row=1,column=1,columnspan=2,sticky="w",pady=(8,0))
        ttk.Label(filters,text="Sell city").grid(row=1,column=3,sticky="e",padx=(8,3),pady=(8,0))
        ttk.Combobox(filters,textvariable=self.flip_sell_city,values=["Any"]+FLIP_SELL_LOCATIONS,state="readonly",width=12).grid(row=1,column=4,columnspan=2,sticky="w",pady=(8,0))
        ttk.Label(filters,text="Qty").grid(row=1,column=6,sticky="e",padx=(8,3),pady=(8,0))
        ttk.Entry(filters,textvariable=self.flip_qty,width=6).grid(row=1,column=7,sticky="w",pady=(8,0))

        searchrow=ttk.Frame(self.flips_tab,padding=(12,0,12,6));searchrow.pack(fill="x")
        ttk.Label(searchrow,text="Search").pack(side="left")
        ttk.Entry(searchrow,textvariable=self.flip_search,width=24).pack(side="left",padx=(5,8))
        ttk.Label(searchrow,text="Category").pack(side="left")
        ttk.Combobox(searchrow,textvariable=self.flip_category,values=["All","Weapons","Armor","Off-hands","Bags","Capes"],state="readonly",width=11).pack(side="left",padx=(5,0))

        actions=ttk.Frame(self.flips_tab,padding=(12,0,12,7));actions.pack(fill="x")
        self.btn=ttk.Button(actions,text="SCAN MARKET",command=self.start);self.btn.pack(side="left")
        self.flip_refresh_btn=ttk.Button(actions,text="REFRESH SELECTED",command=self.refresh_selected_flip,state="disabled");self.flip_refresh_btn.pack(side="left",padx=(6,0))
        self.flip_recalc_btn=ttk.Button(actions,text="RECALCULATE",command=self.recalculate_loaded_flips,state="disabled");self.flip_recalc_btn.pack(side="left",padx=(6,0))
        self.flip_watch_btn=ttk.Button(actions,text="WATCH SELECTED",command=lambda:self.add_selected_to_watchlist("flip"));self.flip_watch_btn.pack(side="left",padx=(6,0))
        self.flip_export_btn=ttk.Button(actions,text="EXPORT CSV",command=self.export_flips_csv,state="disabled");self.flip_export_btn.pack(side="left",padx=(6,0))
        ttk.Label(actions,text="Double-click a result to verify or override exact in-game prices.").pack(side="right")

        self.status=tk.StringVar(value="Ready. Green = actionable, amber = stale, red = loss/problem, blue = just recalculated locally.")
        ttk.Label(self.flips_tab,textvariable=self.status,padding=(12,4)).pack(fill="x")

        cols=("item","tier","quality","from","buy","buyage","to","sell","sellage","investment","fees","profit","roi","volume","days","depth","refresh")
        self.tree=ttk.Treeview(self.flips_tab,columns=cols,show="tree headings")
        self.tree.heading("#0",text="Icon"); self.tree.column("#0",width=58,minwidth=58,stretch=False,anchor="center")
        heads={"item":"Item","tier":"Tier","quality":"Quality","from":"Buy city","buy":"Buy","buyage":"Buy age","to":"Sell city","sell":"Sell","sellage":"Sell age",
               "investment":"Investment","fees":"Fees","profit":"Profit","roi":"ROI","volume":"14d/day","days":"Days to sell","depth":"Live depth","refresh":"Refresh?"}
        widths={"item":250,"tier":55,"quality":90,"from":95,"buy":85,"buyage":80,"to":95,"sell":85,"sellage":80,"investment":95,"fees":85,"profit":95,"roi":65,"volume":70,"days":80,"depth":75,"refresh":70}
        for c in cols:
            self.tree.heading(c,text=heads[c])
            self.tree.column(c,width=widths[c],anchor="center" if c in ("tier","quality","refresh","buyage","sellage") else ("e" if c in ("buy","sell","investment","fees","profit","roi","volume","days","depth") else "w"))
        tablewrap=ttk.Frame(self.flips_tab)
        tablewrap.pack(fill="both",expand=True,padx=12,pady=(0,12))
        tablewrap.rowconfigure(0,weight=1);tablewrap.columnconfigure(0,weight=1)
        # Treeview was originally created with flips_tab as its parent and then
        # geometry-managed inside tablewrap. Tk widgets cannot be re-parented that way.
        # Create the results widget as an actual child of tablewrap.
        try:self.tree.destroy()
        except:pass
        self.tree=ttk.Treeview(tablewrap,columns=cols,show="tree headings")
        self.tree.heading("#0",text="Icon");self.tree.column("#0",width=58,minwidth=58,stretch=False,anchor="center")
        for c in cols:
            self.tree.heading(c,text=heads[c])
            self.tree.column(c,width=widths[c],anchor="center" if c in ("tier","refresh","confidence","buyage","sellage") else ("e" if c in ("buy","sell","investment","fees","profit","roi","volume","days","depth") else "w"))
        y=ttk.Scrollbar(tablewrap,orient="vertical",command=self.tree.yview)
        x=ttk.Scrollbar(tablewrap,orient="horizontal",command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
        self.tree.bind("<Double-1>",self.open_flip_detail)
        self.tree.bind("<<TreeviewSelect>>",lambda e:self.flip_refresh_btn.config(state="normal" if self.tree.selection() else "disabled"))
        self.tree.grid(row=0,column=0,sticky="nsew")
        y.grid(row=0,column=1,sticky="ns")
        x.grid(row=1,column=0,sticky="ew")

    def build_crafting_tab(self):
        top=ttk.Frame(self.crafting_tab,padding=12); top.pack(fill="x")
        ttk.Label(top,text="Crafting Profit Scanner",style="Title.TLabel").grid(row=0,column=0,columnspan=10,sticky="w")
        ttk.Label(top,text="Live material costs + resource returns + artifacts + sale fees. Double-click a result for the full recipe breakdown.").grid(row=1,column=0,columnspan=10,sticky="w",pady=(0,10))

        self.craft_city=tk.StringVar(value="Bridgewatch")
        self.use_focus=tk.BooleanVar(value=True)
        self.use_custom_rrr=tk.BooleanVar(value=False)
        self.craft_rrr=tk.StringVar(value="43.5")
        self.craft_rrr_preview=tk.StringVar(value="Auto RRR: 43.5% base / 47.9% specialty")
        self.daily_bonus=tk.StringVar(value="0%")
        self.craft_station_fee=tk.StringVar(value="0")
        self.craft_focus_cost=tk.StringVar(value="0")
        self.craft_minprofit=tk.StringVar(value="40000")
        self.craft_minroi=tk.StringVar(value="5")
        self.craft_maxage=tk.StringVar(value="180")
        self.craft_minvol=tk.StringVar(value="1")
        self.craft_sell_city=tk.StringVar(value="Auto")
        self.craft_min_conf=tk.StringVar(value="Any")
        self.specialty_only=tk.BooleanVar(value=False)

        controls=[
            ("Craft city",self.craft_city),
            ("Custom RRR %",self.craft_rrr),
            ("Station fee/craft",self.craft_station_fee),
            ("Min profit",self.craft_minprofit),
            ("Min ROI %",self.craft_minroi),
            ("Max age (min)",self.craft_maxage),
            ("Min vol/day",self.craft_minvol),
        ]

        ttk.Label(top,text="Craft city").grid(row=2,column=0,sticky="w",padx=(0,4))
        citybox=ttk.Combobox(top,textvariable=self.craft_city,values=CRAFT_CITIES,state="readonly",width=15)
        citybox.grid(row=2,column=1,sticky="w",padx=(0,12))

        focus_cb=ttk.Checkbutton(top,text="Use Focus",variable=self.use_focus,command=self.on_local_craft_change)
        focus_cb.grid(row=2,column=2,sticky="w",padx=(0,12))
        ttk.Label(top,textvariable=self.craft_rrr_preview,font=("Segoe UI",10,"bold")).grid(row=2,column=3,columnspan=3,sticky="w",padx=(0,14))
        citybox.bind("<<ComboboxSelected>>",self.on_local_craft_change)

        ttk.Label(top,text="Daily bonus").grid(row=2,column=6,sticky="w",padx=(0,4))
        dailybox=ttk.Combobox(top,textvariable=self.daily_bonus,values=["0%","10%","20%"],state="readonly",width=7)
        dailybox.grid(row=2,column=7,sticky="w",padx=(0,14))
        dailybox.bind("<<ComboboxSelected>>",self.on_local_craft_change)
        ttk.Checkbutton(top,text="Specialty only",variable=self.specialty_only).grid(row=2,column=8,sticky="w")

        ttk.Checkbutton(top,text="Use custom RRR",variable=self.use_custom_rrr,command=self.on_local_craft_change).grid(row=3,column=0,sticky="w",pady=(8,0))
        rrr_entry=ttk.Entry(top,textvariable=self.craft_rrr,width=8);rrr_entry.grid(row=3,column=1,sticky="w",padx=(0,12),pady=(8,0));rrr_entry.bind("<KeyRelease>",self.on_local_craft_change)
        ttk.Label(top,text="Station fee/craft").grid(row=3,column=2,sticky="w",padx=(0,4),pady=(8,0))
        station_entry=ttk.Entry(top,textvariable=self.craft_station_fee,width=10);station_entry.grid(row=3,column=3,sticky="w",padx=(0,12),pady=(8,0));station_entry.bind("<KeyRelease>",self.on_local_craft_change)
        ttk.Label(top,text="Focus cost/craft").grid(row=3,column=4,sticky="w",padx=(0,4),pady=(8,0))
        focus_cost_entry=ttk.Entry(top,textvariable=self.craft_focus_cost,width=8);focus_cost_entry.grid(row=3,column=5,sticky="w",padx=(0,12),pady=(8,0));focus_cost_entry.bind("<KeyRelease>",self.on_local_craft_change)
        ttk.Label(top,text="Sell city").grid(row=3,column=6,sticky="w",padx=(0,4),pady=(8,0))
        ttk.Combobox(top,textvariable=self.craft_sell_city,values=["Auto"]+SELL_LOCATIONS,state="readonly",width=12).grid(row=3,column=7,sticky="w",padx=(0,12),pady=(8,0))

        ttk.Label(top,text="Min profit").grid(row=4,column=0,sticky="w",padx=(0,4),pady=(8,0))
        ttk.Entry(top,textvariable=self.craft_minprofit,width=10).grid(row=4,column=1,sticky="w",padx=(0,12),pady=(8,0))
        ttk.Label(top,text="Min ROI %").grid(row=4,column=2,sticky="w",padx=(0,4),pady=(8,0))
        ttk.Entry(top,textvariable=self.craft_minroi,width=8).grid(row=4,column=3,sticky="w",padx=(0,12),pady=(8,0))
        ttk.Label(top,text="Max age (min)").grid(row=4,column=4,sticky="w",padx=(0,4),pady=(8,0))
        ttk.Entry(top,textvariable=self.craft_maxage,width=8).grid(row=4,column=5,sticky="w",padx=(0,12),pady=(8,0))
        ttk.Label(top,text="Min vol/day").grid(row=4,column=6,sticky="w",padx=(0,4),pady=(8,0))
        ttk.Entry(top,textvariable=self.craft_minvol,width=8).grid(row=4,column=7,sticky="w",padx=(0,12),pady=(8,0))
        ttk.Label(top,text="Min confidence").grid(row=4,column=8,sticky="w",padx=(8,4),pady=(8,0))
        ttk.Combobox(top,textvariable=self.craft_min_conf,values=["Any","MEDIUM+","HIGH"],state="readonly",width=10).grid(row=4,column=9,sticky="w",pady=(8,0))

        # Filters are split across rows so the action buttons never disappear off-screen.
        filterbar=ttk.Frame(self.crafting_tab,padding=(12,0,12,4));filterbar.pack(fill="x")
        ttk.Label(filterbar,text="Tiers:").pack(side="left")
        self.craft_tiers={}
        for t in range(4,9):
            v=tk.BooleanVar(value=False);self.craft_tiers[t]=v
            ttk.Checkbutton(filterbar,text=f"T{t}",variable=v).pack(side="left")
        ttk.Label(filterbar,text="   Enchant:").pack(side="left")
        self.craft_enchants={}
        for e in range(5):
            v=tk.BooleanVar(value=False);self.craft_enchants[e]=v
            ttk.Checkbutton(filterbar,text=f".{e}",variable=v).pack(side="left")

        self.craft_search=tk.StringVar(value="")
        self.craft_category=tk.StringVar(value="All")
        self.craft_runs=tk.StringVar(value="10")
        self.craft_buy_mode=tk.StringVar(value="Instant Buy")
        self.craft_sell_mode=tk.StringVar(value="Sell Order")
        self.craft_route_mode=tk.StringVar(value="Both")
        self.craft_quality_mode=tk.StringVar(value="Normal only")

        ttk.Label(filterbar,text="   Search:").pack(side="left")
        ttk.Entry(filterbar,textvariable=self.craft_search,width=18).pack(side="left")
        ttk.Label(filterbar,text=" Category:").pack(side="left")
        ttk.Combobox(filterbar,textvariable=self.craft_category,values=["All","Weapons","Armor","Off-hands","Bags","Capes"],state="readonly",width=10).pack(side="left",padx=(4,0))

        optionsbar=ttk.Frame(self.crafting_tab,padding=(12,2,12,5));optionsbar.pack(fill="x")
        ttk.Label(optionsbar,text="Runs:").pack(side="left")
        runs_entry=ttk.Entry(optionsbar,textvariable=self.craft_runs,width=6);runs_entry.pack(side="left",padx=(4,12))
        runs_entry.bind("<KeyRelease>",self.on_local_craft_change)
        ttk.Label(optionsbar,text="Route:").pack(side="left")
        ttk.Combobox(optionsbar,textvariable=self.craft_route_mode,values=["Direct","Upgrade","Both"],state="readonly",width=8).pack(side="left",padx=(4,12))
        ttk.Label(optionsbar,text="Buy mats:").pack(side="left")
        ttk.Combobox(optionsbar,textvariable=self.craft_buy_mode,values=["Instant Buy","Buy Order"],state="readonly",width=11).pack(side="left",padx=(4,12))
        ttk.Label(optionsbar,text="Sell output:").pack(side="left")
        ttk.Combobox(optionsbar,textvariable=self.craft_sell_mode,values=["Sell Order","Instant Sell"],state="readonly",width=11).pack(side="left",padx=(4,12))
        ttk.Label(optionsbar,text="Quality:").pack(side="left")
        ttk.Combobox(optionsbar,textvariable=self.craft_quality_mode,values=["Normal only","My quality mix"],state="readonly",width=12).pack(side="left",padx=(4,12))

        actionbar=ttk.Frame(self.crafting_tab,padding=(12,2,12,8));actionbar.pack(fill="x")
        self.craft_btn=ttk.Button(actionbar,text="SCAN CRAFTS",command=self.start_craft_scan)
        self.craft_btn.pack(side="left")
        self.craft_refresh_btn=ttk.Button(actionbar,text="REFRESH SELECTED PRICES",command=self.refresh_selected_craft_prices)
        self.craft_refresh_btn.pack(side="left",padx=(6,0))
        self.craft_export_btn=ttk.Button(actionbar,text="EXPORT CSV",command=self.export_crafts_csv,state="disabled")
        self.craft_export_btn.pack(side="left",padx=(6,0))
        self.craft_shop_btn=ttk.Button(actionbar,text="COMBINE SHOPPING LIST",command=self.combine_selected_shopping_list)
        self.craft_shop_btn.pack(side="left",padx=(6,0))
        self.craft_watch_btn=ttk.Button(actionbar,text="WATCH",command=lambda:self.add_selected_to_watchlist("craft"))
        self.craft_watch_btn.pack(side="left",padx=(6,0))
        ttk.Label(actionbar,text="Focus / Runs / Station Fee / Premium / Daily Bonus recalculate locally — no scan.",font=("Segoe UI",9,"italic")).pack(side="right")

        self.craft_status=tk.StringVar(value="Ready. Focus, Runs, Station Fee, Premium and Daily Bonus recalculate instantly from loaded data.")
        ttk.Label(self.crafting_tab,textvariable=self.craft_status,padding=(12,4)).pack(fill="x")

        cols=("item","tier","route","runs","craftcity","bonus","rrr","sellcity","matcost","returned","netcost","station","sell","fees","profit","roi","p10k","extra10k","volume","days","depth","matage","outage","refresh","confidence")
        self.craft_tree=ttk.Treeview(self.crafting_tab,columns=cols,show="tree headings")
        self.craft_tree.heading("#0",text="Icon");self.craft_tree.column("#0",width=58,minwidth=58,stretch=False,anchor="center")
        heads={
            "item":"Item","tier":"Tier","route":"Route","runs":"Runs","craftcity":"Craft city","bonus":"Bonus?","rrr":"RRR",
            "sellcity":"Best sell city","matcost":"Raw mats","returned":"Returned mats value","netcost":"Net mats",
            "station":"Station","sell":"Sell","fees":"Sell fees","profit":"Profit","roi":"ROI","p10k":"Profit/10k Focus","extra10k":"Extra/10k Focus",
            "volume":"14d/day","days":"Days to sell","depth":"Live depth","matage":"Mats age","outage":"Output age","refresh":"Refresh?","confidence":"Confidence"
        }
        widths={
            "item":240,"tier":55,"route":105,"runs":55,"craftcity":95,"bonus":65,"rrr":65,"sellcity":100,
            "matcost":90,"returned":90,"netcost":90,"station":75,"sell":85,"fees":80,
            "profit":90,"roi":65,"p10k":105,"extra10k":105,"volume":70,"days":80,"depth":75,"matage":75,"outage":75,"refresh":75,"confidence":85
        }
        for c in cols:
            self.craft_tree.heading(c,text=heads[c])
            anchor="center" if c in ("tier","route","runs","bonus","rrr","refresh","confidence") else ("e" if c in ("matcost","returned","netcost","station","sell","fees","profit","roi","p10k","extra10k","volume","days","depth") else "w")
            self.craft_tree.column(c,width=widths[c],anchor=anchor)
        self.craft_tree.bind("<Double-1>",self.show_craft_breakdown)
        cy=ttk.Scrollbar(self.crafting_tab,orient="vertical",command=self.craft_tree.yview)
        cx=ttk.Scrollbar(self.crafting_tab,orient="horizontal",command=self.craft_tree.xview)
        self.craft_tree.configure(yscrollcommand=cy.set,xscrollcommand=cx.set)
        self.craft_tree.pack(side="left",fill="both",expand=True,padx=(12,0),pady=(0,28))
        cy.pack(side="right",fill="y",padx=(0,12),pady=(0,28))
        cx.place(relx=0.01,rely=0.965,relwidth=0.965)


    # ---------- GitHub self updater ----------
    def version_tuple(self,v):
        nums=re.findall(r"\d+",str(v))
        return tuple(int(x) for x in nums[:4]) or (0,)

    def fetch_latest_app(self):
        url=UPDATE_APP_URL+f"?v={int(time.time())}"
        req=urllib.request.Request(url,headers={"User-Agent":f"AlbionMarketAssistant/{APP_VERSION}","Cache-Control":"no-cache, no-store","Pragma":"no-cache"})
        with urllib.request.urlopen(req,timeout=30) as r:data=r.read()
        if len(data)<10000:raise RuntimeError("GitHub application file was unexpectedly small.")
        text=data.decode("utf-8")
        m=re.search(r'^APP_VERSION\s*=\s*["\\\']([^"\\\']+)["\\\']',text,re.M)
        if not m:raise RuntimeError("Could not find APP_VERSION in the GitHub application file.")
        compile(text,"github_albion_market_assistant.py","exec")
        return m.group(1),data

    def check_for_update(self,silent=False):
        if hasattr(self,"update_status"):self.update_status.set(f"Current: {APP_VERSION} • Checking GitHub...")
        def work():
            try:
                latest,new_bytes=self.fetch_latest_app()
                newer=self.version_tuple(latest)>self.version_tuple(APP_VERSION)
                def done():
                    if not newer:
                        self.update_status.set(f"Current: {APP_VERSION} • Latest: {latest} • Up to date")
                        if not silent:messagebox.showinfo("Updates",f"Albion Market Assistant {APP_VERSION} is up to date.")
                        return
                    self.update_status.set(f"Current: {APP_VERSION} • Latest: {latest} • UPDATE AVAILABLE")
                    if silent:return
                    if messagebox.askyesno("Update available",f"Version {latest} is available.\n\nUpdate now?"):
                        self.install_update({"version":latest,"bytes":new_bytes})
                self.root.after(0,done)
            except Exception as e:
                def fail():
                    self.update_status.set(f"Current: {APP_VERSION} • Update check failed")
                    if not silent:messagebox.showerror("Update check failed",str(e))
                self.root.after(0,fail)
        threading.Thread(target=work,daemon=True).start()

    def install_update(self,release):
        if hasattr(self,"update_status"):self.update_status.set(f"Current: {APP_VERSION} • Installing {release['version']}...")
        def work():
            try:
                current=Path(os.path.abspath(__file__))
                new_bytes=release.get("bytes")
                if not new_bytes:
                    latest,new_bytes=self.fetch_latest_app()
                    if latest!=release["version"]:raise RuntimeError("GitHub version changed during update. Check again.")
                text=new_bytes.decode("utf-8")
                compile(text,str(current),"exec")
                m=re.search(r'^APP_VERSION\s*=\s*["\\\']([^"\\\']+)["\\\']',text,re.M)
                if not m or m.group(1)!=release["version"]:raise RuntimeError("Downloaded file version did not match the expected update.")
                backup=current.with_name(current.stem+"_previous"+current.suffix)
                tmp=current.with_suffix(current.suffix+".update")
                tmp.write_bytes(new_bytes);shutil.copy2(current,backup);os.replace(tmp,current)
                try:
                    req=urllib.request.Request(UPDATE_LAUNCHER_URL,headers={"User-Agent":f"AlbionMarketAssistant/{APP_VERSION}"})
                    with urllib.request.urlopen(req,timeout=20) as r:(current.parent/"START_ALBION_MARKET_ASSISTANT.bat").write_bytes(r.read())
                except:pass
                def restart():
                    self.save_settings();self.save_manual_overrides();self.save_watchlist()
                    messagebox.showinfo("Update installed",f"Updated to {release['version']}. The app will restart now.\n\nBackup: {backup.name}")
                    os.execl(sys.executable,sys.executable,str(current))
                self.root.after(0,restart)
            except Exception as e:
                err=str(e)
                self.root.after(0,lambda:self._update_failed(err))
        threading.Thread(target=work,daemon=True).start()

    def _update_failed(self,err):
        self.update_status.set(f"Current: {APP_VERSION} • Update failed: {err}")
        messagebox.showerror("Update failed",f"{err}\n\nYour current version was kept unchanged.")

    def build_settings_tab(self):
        settings=ttk.Frame(self.settings_tab,padding=18);settings.pack(fill="both",expand=True)
        ttk.Label(settings,text="Settings",style="Title.TLabel").pack(anchor="w")
        updatebox=ttk.LabelFrame(settings,text="Updates",padding=12);updatebox.pack(fill="x",anchor="w",pady=(12,8))
        self.update_status=tk.StringVar(value=f"Current: {APP_VERSION} • Automatic GitHub check enabled")
        ttk.Label(updatebox,textvariable=self.update_status,font=("Segoe UI",11,"bold")).pack(side="left")
        ttk.Button(updatebox,text="CHECK FOR UPDATE",command=self.check_for_update).pack(side="right")
        ttk.Label(settings,text="Updates now read APP_VERSION directly from the GitHub program file; version.json is no longer required.").pack(anchor="w",pady=(4,0))
        self.root.after(1800,lambda:self.check_for_update(silent=True))
        ttk.Checkbutton(settings,text="Dark mode",variable=self.dark,command=self.theme).pack(anchor="w",pady=(14,6))
        ttk.Checkbutton(settings,text="Premium active",variable=self.premium,command=self.premium_changed).pack(anchor="w",pady=(2,10))
        self.fee_info=tk.StringVar()
        ttk.Label(settings,textvariable=self.fee_info,font=("Segoe UI",11,"bold")).pack(anchor="w",pady=(2,4))
        ttk.Label(settings,text="Sell-order profit includes the 2.5% setup fee plus the applicable sales tax.").pack(anchor="w")
        ttk.Label(settings,text="Crafting uses instant-buy material prices in the selected crafting city; purchase-side setup fees are not added.").pack(anchor="w",pady=(3,4))
        ttk.Label(settings,text="Auto RRR is calculated from production bonus: Royal City 18%, specialty +15%, Focus +59%, plus optional daily 10%/20% bonus.").pack(anchor="w",pady=(3,4))
        ttk.Label(settings,text="Focus cost/craft is optional and user-entered; if supplied, Crafting shows profit per 10,000 Focus.").pack(anchor="w",pady=(3,12))
        ttk.Label(settings,text="Server: Americas").pack(anchor="w")
        ttk.Label(settings,text="Item icons are downloaded automatically and cached in the item_icons folder.").pack(anchor="w",pady=(4,0))
        ttk.Label(settings,text="Crafting recipes are downloaded once and cached locally in recipes_cache.json.").pack(anchor="w",pady=(4,0))
        ttk.Label(settings,text="Station fee is intentionally manual and will stay manual because player-run station fees vary.").pack(anchor="w",pady=(4,0))
        ttk.Label(settings,text="Preferences, watchlist, manual price overrides, recipes, icons and local market/order caches persist across updates.").pack(anchor="w",pady=(4,0))
        self.premium_changed()


    def make_tree_sortable(self,tree):
        cols=list(tree["columns"])
        for c in cols:
            try:
                label=tree.heading(c,"text")
                tree.heading(c,text=label,command=lambda col=c:self.sort_tree(tree,col,False))
            except:pass

    def sort_tree(self,tree,col,reverse):
        def conv(x):
            s=str(x).replace(",","").replace("%","").replace("—","").strip()
            try:return float(s)
            except:return s.lower()
        rows=[(conv(tree.set(i,col)),i) for i in tree.get_children("")]
        rows.sort(reverse=reverse,key=lambda x:x[0])
        for n,(_,i) in enumerate(rows):tree.move(i,"",n)
        tree.heading(col,command=lambda:self.sort_tree(tree,col,not reverse))

    def apply_text_theme(self,w):
        try:
            dark=self.dark.get()
            w.configure(bg="#252526" if dark else "#ffffff",fg="#f2f2f2" if dark else "#000000",
                        insertbackground="#f2f2f2" if dark else "#000000",selectbackground="#3d5a80")
        except:pass

    def theme(self):
        dark=self.dark.get()
        bg="#1e1e1e" if dark else "#f0f0f0"
        fg="#f2f2f2" if dark else "#000"
        panel="#252526" if dark else "#fff"
        field="#333" if dark else "#fff"
        self.root.configure(bg=bg)
        self.style.configure(".",background=bg,foreground=fg)
        self.style.configure("TFrame",background=bg)
        self.style.configure("TLabel",background=bg,foreground=fg)
        self.style.configure("TNotebook",background=bg,borderwidth=0)
        self.style.configure("TNotebook.Tab",background="#303030" if dark else "#e5e5e5",foreground=fg,padding=(14,7))
        self.style.map("TNotebook.Tab",background=[("selected","#454545" if dark else "#ffffff")],foreground=[("selected",fg)])
        self.style.configure("TCombobox",fieldbackground=field,background=field,foreground=fg,arrowcolor=fg)
        self.style.map("TCombobox",
            fieldbackground=[("readonly",field),("disabled","#2a2a2a" if dark else "#e7e7e7")],
            background=[("readonly",field),("disabled","#2a2a2a" if dark else "#e7e7e7")],
            foreground=[("readonly",fg),("disabled","#aaaaaa" if dark else "#666666")],
            selectbackground=[("readonly",field)],selectforeground=[("readonly",fg)])
        self.root.option_add("*TCombobox*Listbox.background",field)
        self.root.option_add("*TCombobox*Listbox.foreground",fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground","#3d5a80" if dark else "#0078d7")
        self.root.option_add("*TCombobox*Listbox.selectForeground","#ffffff")
        self.style.configure("Title.TLabel",background=bg,foreground=fg,font=("Segoe UI",18,"bold"))
        self.style.configure("Warn.TLabel",background=bg,foreground="#ffcc66" if dark else "#9a6500",font=("Segoe UI",10,"bold"))
        self.style.configure("TEntry",fieldbackground=field,background=field,foreground=fg,insertcolor=fg)
        self.style.map("TEntry",fieldbackground=[("disabled","#2a2a2a" if dark else "#e7e7e7")],foreground=[("disabled","#aaaaaa" if dark else "#666666")])
        self.style.configure("TSpinbox",fieldbackground=field,background=field,foreground=fg,arrowcolor=fg)
        self.style.map("TSpinbox",fieldbackground=[("readonly",field),("disabled","#2a2a2a" if dark else "#e7e7e7")],foreground=[("readonly",fg),("disabled","#aaaaaa" if dark else "#666666")])
        self.style.configure("TCheckbutton",background=bg,foreground=fg)
        self.style.map("TCheckbutton",background=[("active",bg)],foreground=[("active",fg)])
        self.style.configure("TButton",background="#3a3a3a" if dark else "#e1e1e1",foreground=fg)
        self.style.map("TButton",background=[("active","#505050" if dark else "#d0d0d0"),("disabled","#292929" if dark else "#eeeeee")],foreground=[("disabled","#888888" if dark else "#888888")])
        self.style.configure("Horizontal.TScrollbar",background="#444444" if dark else "#d8d8d8",troughcolor=panel,arrowcolor=fg)
        self.style.configure("Vertical.TScrollbar",background="#444444" if dark else "#d8d8d8",troughcolor=panel,arrowcolor=fg)
        self.style.configure("Treeview",background=panel,fieldbackground=panel,foreground=fg,rowheight=54)
        self.style.map("Treeview",background=[("selected","#3d5a80" if dark else "#0078d7")],foreground=[("selected","#fff")])
        self.style.configure("Treeview.Heading",background="#303030" if dark else "#f0f0f0",foreground=fg)

        # New color language:
        # green = actionable/profitable, amber = stale, red = loss/bad, blue = just recalculated locally.
        tags={
            "NORMAL":panel,
            "ACTION":"#183626" if dark else "#dff4e6",
            "STALE":"#4a3c16" if dark else "#fff1c7",
            "LOSS":"#4a2323" if dark else "#f8d7da",
            "CHANGED":"#18364a" if dark else "#d7eef9",
        }
        for tr in [getattr(self,"tree",None),getattr(self,"craft_tree",None),getattr(self,"dashboard_tree",None),getattr(self,"watch_tree",None)]:
            if tr:
                for tag,color in tags.items():tr.tag_configure(tag,background=color)

        for w in [getattr(self,"ref_text",None)]:
            if w:self.apply_text_theme(w)

    def update_rrr_preview(self):
        try:
            if self.use_custom_rrr.get():
                self.craft_rrr_preview.set(f"Custom RRR: {float(self.craft_rrr.get()):.1f}%")
                return
            daily=float(self.daily_bonus.get().replace("%",""))
            base=BASE_PRODUCTION_BONUS+daily+(FOCUS_PRODUCTION_BONUS if self.use_focus.get() else 0)
            spec=base+SPECIALTY_PRODUCTION_BONUS
            br=production_bonus_to_rrr(base)*100
            sr=production_bonus_to_rrr(spec)*100
            focus_text="Focus ON" if self.use_focus.get() else "Focus OFF"
            self.craft_rrr_preview.set(f"{focus_text} • Auto RRR: {br:.1f}% base / {sr:.1f}% specialty")
        except:
            self.craft_rrr_preview.set("RRR preview unavailable")

    def premium_changed(self):
        tax=PREMIUM_SALES_TAX if self.premium.get() else NONPREMIUM_SALES_TAX
        total=SETUP_FEE+tax
        if hasattr(self,"fee_info"):
            self.fee_info.set(f"Current sell-order fees: {SETUP_FEE*100:.1f}% setup + {tax*100:.1f}% sales tax = {total*100:.1f}%")
        if hasattr(self,"status"):
            label="Premium" if self.premium.get() else "No Premium"
            self.status.set(f"Ready — {label}: sell-order fee assumption {total*100:.1f}%.")
        if hasattr(self,"craft_status"):
            self.craft_status.set("Ready. Premium setting is shared with crafting sale-fee calculations.")
        if hasattr(self,"last_craft_records") and self.last_craft_records:self.recalculate_loaded_crafts(True)
        if hasattr(self,"last_flip_records") and self.last_flip_records:self.recalculate_loaded_flips()

    # ---------- icons ----------
    def load_icon_async(self,tree,store,iid,uid):
        def work():
            path=download_icon(uid)
            if not path:return
            def apply():
                try:
                    img=tk.PhotoImage(file=path)
                    store[iid]=img
                    if tree.exists(iid):tree.item(iid,image=img)
                except:
                    pass
            self.root.after(0,apply)
        threading.Thread(target=work,daemon=True).start()

    # ---------- market flip scanner ----------
    def setstatus(self,s):
        self.root.after(0,lambda:self.status.set(s))

    def start(self):
        try:
            if not any(v.get() for v in self.tiers.values()) or not any(v.get() for v in self.enchants.values()):
                messagebox.showerror("Choose filters","Select at least one Tier and one Enchantment before scanning.")
                return
            self.settings=(float(self.profit.get()),float(self.roi.get()),int(self.agev.get()),float(self.vol.get()),bool(self.premium.get()))
            self.flip_scan_buy_city=self.flip_buy_city.get();self.flip_scan_sell_city=self.flip_sell_city.get()
        except:
            messagebox.showerror("Invalid filters","Enter numbers in all four filter boxes.")
            return
        self.btn.config(state="disabled")
        for x in self.tree.get_children():self.tree.delete(x)
        threading.Thread(target=self.scan,daemon=True).start()

    def scan(self):
        try:
            minp,minroi,maxage,minvol,premium=self.settings
            sales_tax=PREMIUM_SALES_TAX if premium else NONPREMIUM_SALES_TAX
            total_sell_fee=SETUP_FEE+sales_tax
            self.setstatus("Loading item catalog...")
            try:
                if os.path.exists(ITEMS_CACHE) and time.time()-os.path.getmtime(ITEMS_CACHE)<ITEMS_CACHE_SECONDS:
                    with open(ITEMS_CACHE,"r",encoding="utf-8") as cf:cat=json.load(cf)
                else:
                    cat=get_json(ITEMS_URL)
                    with open(ITEMS_CACHE,"w",encoding="utf-8") as cf:json.dump(cat,cf,separators=(",",":"))
            except:
                cat=get_json(ITEMS_URL)
            if isinstance(cat,dict):cat=cat.get("items") or cat.get("Items") or list(cat.values())
            st={x for x,v in self.tiers.items() if v.get()}
            se={x for x,v in self.enchants.items() if v.get()}
            names={};ids=[]
            for x in cat:
                if isinstance(x,dict) and x.get("UniqueName"):
                    uid=x["UniqueName"]
                    nm=name_of(x)
                    if isgear(uid) and tier(uid) in st and enchant(uid) in se:
                        if self.flip_search.get().strip() and self.flip_search.get().strip().lower() not in nm.lower() and self.flip_search.get().strip().lower() not in uid.lower():continue
                        if self.flip_category.get()!="All" and item_category(uid)!=self.flip_category.get():continue
                        ids.append(uid);names[uid]=nm
            pp=[];hh=[];bs=list(batches(ids))
            # Only request locations that can participate in the selected route.
            buylocs=CITIES if self.flip_scan_buy_city=="Any" else [self.flip_scan_buy_city]
            selllocs=FLIP_SELL_LOCATIONS if self.flip_scan_sell_city=="Any" else [self.flip_scan_sell_city]
            request_locs=list(dict.fromkeys(buylocs+selllocs))
            # Prices are always fresh. History is cached because 14-day volume changes slowly.
            hist_key="|".join(sorted(ids))+"::"+"|".join(sorted(request_locs))
            hcache={}
            try:
                if os.path.exists(FLIP_HISTORY_CACHE):
                    with open(FLIP_HISTORY_CACHE,"r",encoding="utf-8") as hf:hcache=json.load(hf)
            except:hcache={}
            hc=hcache.get(hist_key,{})
            use_cached_history=bool(hc and time.time()-float(hc.get("time",0))<HISTORY_CACHE_SECONDS)
            if use_cached_history:hh=hc.get("data",[])
            total_jobs=len(bs)+(0 if use_cached_history else len(bs));done_jobs=0
            def price_job(b):return ("p",prices_for_qualities(b,request_locs))
            def hist_job(b):return ("h",history_for_all_qualities(b,request_locs))
            jobs=[]
            with concurrent.futures.ThreadPoolExecutor(max_workers=FLIP_WORKERS) as ex:
                jobs += [ex.submit(price_job,b) for b in bs]
                if not use_cached_history:jobs += [ex.submit(hist_job,b) for b in bs]
                for fut in concurrent.futures.as_completed(jobs):
                    try:
                        typ,data=fut.result()
                        if typ=="p":pp.extend(data)
                        else:hh.extend(data)
                    except:pass
                    done_jobs+=1;self.setstatus(f"Fast market sync... {done_jobs}/{max(1,total_jobs)} requests")
            if not use_cached_history:
                try:
                    hcache={hist_key:{"time":time.time(),"data":hh}}
                    with open(FLIP_HISTORY_CACHE,"w",encoding="utf-8") as hf:json.dump(hcache,hf,separators=(",",":"))
                except:pass
            vm={}
            for h in hh:
                vals=[]
                for p in h.get("data") or []:
                    try:vals.append(float(p.get("item_count",0) or 0))
                    except:pass
                if vals:vm[(h.get("item_id"),h.get("location"),int(h.get("quality") or 1))]=sum(vals)/len(vals)
            by={}
            for r in pp:
                city=r.get("city")
                # A normal city source/destination needs a sell_price_min.
                # Black Market opportunities live on buy_price_max and commonly have no
                # sell_price_min at all. Previously those BM rows were discarded here
                # before the comparison code ever saw them.
                # Keep every returned market row for the requested locations. A row can
                # legitimately have only one side populated; source/destination validation
                # happens below using the side actually needed for that transaction.
                if city in FLIP_SELL_LOCATIONS:
                    uid=r.get("item_id");by.setdefault(uid,[]).append(r)
                    # observations are not required to rank flip results; skip per-row SQLite writes here
            out=[]
            for uid,rows in by.items():
                for s in rows:
                    bp=float(s.get("sell_price_min") or 0);sa=age(s.get("sell_price_min_date"))
                    if not bp or sa>maxage:continue
                    # Black Market is a sell destination, not a normal player market to buy from.
                    if s["city"]=="Black Market":continue
                    if self.flip_scan_buy_city!="Any" and s["city"]!=self.flip_scan_buy_city:continue
                    for d in rows:
                        if s["city"]==d["city"]:continue
                        # Compare the same physical item quality end-to-end.
                        # Quality is automatic: there is intentionally no quality filter.
                        sq=int(s.get("quality") or 1);dq=int(d.get("quality") or 1)
                        if sq!=dq:continue
                        if self.flip_scan_sell_city!="Any" and d["city"]!=self.flip_scan_sell_city:continue
                        # Royal-city destination = list a sell order at sell_price_min.
                        # Black Market destination = sell INTO its highest buy order.
                        # The Black Market is not another player sell-order market.
                        if d["city"]=="Black Market":
                            sp=float(d.get("buy_price_max") or 0);da=age(d.get("buy_price_max_date"))
                            # BM direct sale has sales tax, but no 2.5% sell-order setup fee.
                            sell_fees=sp*sales_tax
                        else:
                            sp=float(d.get("sell_price_min") or 0);da=age(d.get("sell_price_min_date"))
                            sell_fees=sp*total_sell_fee
                        if not sp or da>maxage:continue
                        pr=sp-sell_fees-bp
                        rr=pr/bp*100
                        vv=vm.get((uid,d["city"],dq),0)
                        if pr>=minp and rr>=minroi:
                            refresh="YES" if max(sa,da)>90 else "No"
                            # Do not perform a SQLite live-depth query for every candidate.
                            # That turned broad all-quality scans into thousands of DB opens.
                            # Depth can be refreshed on demand for a selected result.
                            depth=0
                            out.append((pr,rr,uid,names.get(uid,uid),sq,s["city"],d["city"],bp,sp,sell_fees,vv,depth,sa,da,refresh))
            out.sort(reverse=True)
            def show():
                self.last_flip_rows=[];self.last_flip_records=[]
                for idx,(pr,rr,uid,nm,quality,src,dst,bp,sp,sell_fees,vv,depth,sa,da,refresh) in enumerate(out[:500]):
                    t=tier(uid);e=enchant(uid)
                    tag="STALE" if refresh=="YES" else ("ACTION" if pr>0 else "LOSS")
                    iid=self.tree.insert("","end",text="",tags=(tag,),values=(f"{nm} ({t}.{e})",f"{t}.{e}",QUALITY_NAMES.get(quality,str(quality)),src,f"{bp:,.0f}",agetxt(sa),dst,f"{sp:,.0f}",agetxt(da),f"{bp:,.0f}",f"{sell_fees:,.0f}",f"{pr:,.0f}",f"{rr:.1f}%",f"{vv:.1f}",f"{(1/vv):.1f}" if vv>0 else "—",depth if depth else "—",refresh))
                    rec={"type":"Flip","uid":uid,"name":nm,"tier":f"{t}.{e}","quality":quality,"buycity":src,"sellcity":dst,"buy":bp,"sell":sp,"market_buy":bp,"market_sell":sp,"fees":sell_fees,"profit":pr,"roi":rr,"volume":vv,"depth":depth,"buy_age":sa,"sell_age":da,"refresh":refresh,"iid":iid}
                    self.last_flip_records.append(rec)
                    self.last_flip_rows.append([f"{nm} ({t}.{e})",f"{t}.{e}",QUALITY_NAMES.get(quality,str(quality)),src,dst,bp,sp,sell_fees,pr,rr,vv,depth,agetxt(sa),agetxt(da),refresh])
                    if idx<150:self.load_icon_async(self.tree,self.icon_images,iid,uid)
                fee_pct=total_sell_fee*100
                label="Premium" if premium else "No Premium"
                self.status.set(f"Done — {len(out):,} opportunities • {label} sell-order fees {fee_pct:.1f}% • live depth loads on selected refresh.")
                self.tree.xview_moveto(0)
                self.flip_export_btn.config(state="normal" if self.last_flip_rows else "disabled")
                self.flip_recalc_btn.config(state="normal" if self.last_flip_records else "disabled")
                if self.last_flip_records:self.recalculate_loaded_flips(update_status=False)
                self.btn.config(state="normal")
                self.update_watchlist_from_scans()
                self.refresh_dashboard()
            self.root.after(0,show)
        except Exception as e:
            self.root.after(0,lambda:(messagebox.showerror("Scan error",str(e)),self.btn.config(state="normal"),self.status.set("Scan failed.")))

    def flip_override_key(self,uid,city,side):
        return f"flip|{side}|{uid}|{city}"

    def recalculate_loaded_flips(self,update_status=True):
        if not getattr(self,"last_flip_records",None):return
        try:qty=max(1,int(float(self.flip_qty.get())))
        except:qty=1;self.flip_qty.set("1")
        fee_rate=SETUP_FEE+(PREMIUM_SALES_TAX if self.premium.get() else NONPREMIUM_SALES_TAX)
        for d in self.last_flip_records:
            bp=float(self.manual_price_overrides.get(self.flip_override_key(d["uid"],d["buycity"],"buy"),d.get("market_buy",d["buy"])))
            sp=float(self.manual_price_overrides.get(self.flip_override_key(d["uid"],d["sellcity"],"sell"),d.get("market_sell",d["sell"])))
            fees=sp*fee_rate;profit=sp-fees-bp;roi=(profit/bp*100) if bp else 0;investment=bp*qty
            d.update(buy=bp,sell=sp,fees=fees,profit=profit,roi=roi,investment=investment,qty=qty)
            days=(qty/d["volume"]) if d.get("volume",0)>0 else 999
            vals=(f'{d["name"]} ({d["tier"]})',d["tier"],QUALITY_NAMES.get(int(d.get("quality",1)),str(d.get("quality",1))),d["buycity"],f"{bp:,.0f}",agetxt(d.get("buy_age",9999)),d["sellcity"],f"{sp:,.0f}",agetxt(d.get("sell_age",9999)),f"{investment:,.0f}",f"{fees*qty:,.0f}",f"{profit*qty:,.0f}",f"{roi:.1f}%",f'{d.get("volume",0):.1f}',f"{days:.1f}" if days<999 else "—",d.get("depth") or "—",d.get("refresh","No"))
            if self.tree.exists(d["iid"]):self.tree.item(d["iid"],values=vals,tags=("STALE" if d.get("refresh")=="YES" else ("ACTION" if profit>0 else "LOSS"),))
        if update_status:self.status.set(f"Recalculated locally for quantity {qty}. No market scan used.")
        self.flip_recalc_btn.config(state="normal")

    def selected_flip_record(self):
        sel=self.tree.selection()
        if not sel:return None
        iid=sel[0]
        return next((d for d in self.last_flip_records if d.get("iid")==iid),None)

    def open_flip_detail(self,event=None):
        d=self.selected_flip_record()
        if not d:return
        w=tk.Toplevel(self.root);w.title(f'Flip — {d["name"]} ({d["tier"]})');w.geometry("620x390")
        box=ttk.Frame(w,padding=16);box.pack(fill="both",expand=True)
        ttk.Label(box,text=f'{d["name"]} ({d["tier"]})',style="Title.TLabel").grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,12))
        ttk.Label(box,text=f'Buy in {d["buycity"]}').grid(row=1,column=0,sticky="w");buyv=tk.StringVar(value=str(int(d["buy"])))
        ttk.Entry(box,textvariable=buyv,width=18).grid(row=1,column=1,sticky="w")
        ttk.Label(box,text=f'Age: {agetxt(d.get("buy_age",9999))}').grid(row=1,column=2,sticky="w",padx=10)
        ttk.Label(box,text=f'Sell in {d["sellcity"]}').grid(row=2,column=0,sticky="w",pady=8);sellv=tk.StringVar(value=str(int(d["sell"])))
        ttk.Entry(box,textvariable=sellv,width=18).grid(row=2,column=1,sticky="w")
        ttk.Label(box,text=f'Age: {agetxt(d.get("sell_age",9999))}').grid(row=2,column=2,sticky="w",padx=10)
        summary=tk.StringVar()
        def preview(*a):
            try:
                bp=float(buyv.get());sp=float(sellv.get());fee=sp*(SETUP_FEE+(PREMIUM_SALES_TAX if self.premium.get() else NONPREMIUM_SALES_TAX));p=sp-fee-bp;r=p/bp*100 if bp else 0
                summary.set(f"Per unit: cost {bp:,.0f}  •  fees {fee:,.0f}  •  profit {p:,.0f}  •  ROI {r:.1f}%\nVolume/day {d.get('volume',0):.1f}  •  live depth {d.get('depth') or '—'}")
            except:summary.set("Enter valid prices.")
        buyv.trace_add("write",preview);sellv.trace_add("write",preview);preview()
        ttk.Label(box,textvariable=summary,font=("Segoe UI",11,"bold")).grid(row=3,column=0,columnspan=3,sticky="w",pady=14)
        def apply():
            try:
                self.manual_price_overrides[self.flip_override_key(d["uid"],d["buycity"],"buy")]=float(buyv.get())
                self.manual_price_overrides[self.flip_override_key(d["uid"],d["sellcity"],"sell")]=float(sellv.get())
                self.save_manual_overrides();self.recalculate_loaded_flips();w.destroy()
            except:messagebox.showerror("Prices","Enter valid numeric prices.")
        def clear():
            self.manual_price_overrides.pop(self.flip_override_key(d["uid"],d["buycity"],"buy"),None);self.manual_price_overrides.pop(self.flip_override_key(d["uid"],d["sellcity"],"sell"),None)
            self.save_manual_overrides();self.recalculate_loaded_flips();w.destroy()
        ttk.Button(box,text="APPLY MANUAL PRICES",command=apply).grid(row=4,column=0,sticky="w")
        ttk.Button(box,text="CLEAR OVERRIDES",command=clear).grid(row=4,column=1,sticky="w",padx=8)
        ttk.Button(box,text="REFRESH FROM AODP",command=lambda:(w.destroy(),self.refresh_selected_flip())).grid(row=4,column=2,sticky="w")

    def refresh_selected_flip(self):
        d=self.selected_flip_record()
        if not d:return
        self.status.set(f'Refreshing {d["name"]} only...')
        def work():
            try:
                rows=prices_for_qualities([d["uid"]],[d["buycity"],d["sellcity"]],[int(d.get("quality",1))])
                for r in rows:
                    if r.get("city")==d["buycity"] and float(r.get("sell_price_min") or 0)>0:
                        d["market_buy"]=float(r["sell_price_min"]);d["buy_age"]=age(r.get("sell_price_min_date"))
                    if r.get("city")==d["sellcity"] and float(r.get("sell_price_min") or 0)>0:
                        d["market_sell"]=float(r["sell_price_min"]);d["sell_age"]=age(r.get("sell_price_min_date"))
                self.root.after(0,lambda:(self.recalculate_loaded_flips(),self.status.set(f'Refreshed {d["name"]} in {d["buycity"]} and {d["sellcity"]}.')))
            except Exception as e:self.root.after(0,lambda:messagebox.showerror("Refresh selected",str(e)))
        threading.Thread(target=work,daemon=True).start()

    # ---------- crafting scanner ----------
    def set_craft_status(self,s):
        self.root.after(0,lambda:self.craft_status.set(s))

    def start_craft_scan(self):
        try:
            station=float(self.craft_station_fee.get())
            focus_cost=float(self.craft_focus_cost.get())
            runs=max(1,int(self.craft_runs.get()))
            minprofit=float(self.craft_minprofit.get())
            minroi=float(self.craft_minroi.get())
            maxage=int(self.craft_maxage.get())
            minvol=float(self.craft_minvol.get())
            custom=float(self.craft_rrr.get())/100
            if custom<0 or custom>=1:raise ValueError
        except:
            messagebox.showerror("Invalid crafting filters","Enter valid numbers. Runs must be at least 1 and custom RRR must be between 0 and 99.9.")
            return
        st={x for x,v in self.craft_tiers.items() if v.get()}
        se={x for x,v in self.craft_enchants.items() if v.get()}
        if not st or not se:
            messagebox.showerror("No tiers/enchantments","Select at least one tier and enchantment.")
            return
        self.craft_scan_settings={
            "city":self.craft_city.get(),"focus":bool(self.use_focus.get()),
            "custom_on":bool(self.use_custom_rrr.get()),"custom_rrr":custom,
            "station":station,"focus_cost":focus_cost,"runs":runs,
            "minprofit":minprofit,"minroi":minroi,"maxage":maxage,
            "minvol":minvol,"premium":bool(self.premium.get()),"tiers":st,"enchants":se,
            "daily":float(self.daily_bonus.get().replace("%","")),"sell_city":self.craft_sell_city.get(),
            "minconf":self.craft_min_conf.get(),"specialty_only":bool(self.specialty_only.get()),
            "search":self.craft_search.get().strip().lower(),"category":self.craft_category.get(),
            "buy_mode":self.craft_buy_mode.get(),"sell_mode":self.craft_sell_mode.get(),
            "route_mode":self.craft_route_mode.get(),"quality_mode":self.craft_quality_mode.get()
        }
        self.craft_btn.config(state="disabled")
        self.craft_details.clear()
        for x in self.craft_tree.get_children():self.craft_tree.delete(x)
        threading.Thread(target=self.scan_crafts,daemon=True).start()

    def recipe_rrr(self,recipe,settings):
        bonus=(recipe.get("city")==settings["city"])
        if settings["custom_on"]:
            return settings["custom_rrr"],bonus
        prod=BASE_PRODUCTION_BONUS+settings.get("daily",0)
        if bonus:prod+=SPECIALTY_PRODUCTION_BONUS
        if settings["focus"]:prod+=FOCUS_PRODUCTION_BONUS
        return production_bonus_to_rrr(prod),bonus

    def scan_crafts(self):
        try:
            s=self.craft_scan_settings
            self.set_craft_status("Downloading item names and crafting recipes...")
            cat=get_json(ITEMS_URL)
            if isinstance(cat,dict):cat=cat.get("items") or cat.get("Items") or list(cat.values())
            names={}
            for x in cat:
                if isinstance(x,dict) and x.get("UniqueName"):
                    names[x["UniqueName"]]=name_of(x)

            recipes=load_recipes()
            targets={}
            for uid,rec in recipes.items():
                if tier(uid) not in s["tiers"] or enchant(uid) not in s["enchants"] or not isgear(uid):continue
                nm=names.get(uid,uid)
                if s["search"] and s["search"] not in nm.lower() and s["search"] not in uid.lower():continue
                if s["category"]!="All" and item_category(uid)!=s["category"]:continue
                if rec.get("materials"):targets[uid]=rec
            if not targets:
                raise RuntimeError("No crafting recipes matched the selected filters.")

            # Build exact direct and craft+upgrade scenarios.
            scenarios=[]
            for target_uid,target_rec in targets.items():
                te=enchant(target_uid)
                if s["route_mode"] in ("Direct","Both"):
                    scenarios.append({"target":target_uid,"craft_uid":target_uid,"recipe":target_rec,"route":"Direct","upgrade_steps":[]})
                if s["route_mode"] in ("Upgrade","Both") and 1<=te<=3:
                    for start_e in range(0,te):
                        lower=uid_at_enchant(target_uid,start_e)
                        lowrec=recipes.get(lower)
                        if not lowrec or not lowrec.get("materials"):continue
                        steps=list(range(start_e+1,te+1))
                        scenarios.append({"target":target_uid,"craft_uid":lower,"recipe":lowrec,
                                          "route":f"Craft .{start_e}→.{te}","upgrade_steps":steps})
            if not scenarios:
                raise RuntimeError("No direct/upgrade scenarios matched the selected route filter.")

            output_ids=sorted({x["target"] for x in scenarios})
            material_ids={m.get("id") for x in scenarios for m in (x["recipe"].get("materials") or []) if m.get("id")}
            for x in scenarios:
                t=tier(x["target"])
                for step in x["upgrade_steps"]:
                    mid=upgrade_material_id(t,step)
                    if mid:material_ids.add(mid)
            material_ids=sorted(material_ids)
            all_ids=sorted(set(output_ids+material_ids))

            price_rows=[]
            bs=list(batches(all_ids))
            for i,b in enumerate(bs,1):
                self.set_craft_status(f"Downloading current prices... batch {i}/{len(bs)}")
                try:price_rows.extend(prices_for_qualities(b,SELL_LOCATIONS))
                except:pass
                time.sleep(.18)

            hist_rows=[]
            hbs=list(batches(output_ids))
            for i,b in enumerate(hbs,1):
                self.set_craft_status(f"Downloading {HISTORY_DAYS}-day sales activity... batch {i}/{len(hbs)}")
                try:hist_rows.extend(history_for_all_qualities(b,SELL_LOCATIONS))
                except:pass
                time.sleep(.18)

            pmap={}
            for r in price_rows:
                key=(r.get("item_id"),r.get("city"),int(r.get("quality") or 1))
                pmap[key]=r
                if int(r.get("quality") or 1)==1 and (r.get("sell_price_min") or 0)>0:
                    cache_observation(r.get("item_id"),r.get("city"),r.get("sell_price_min"),r.get("sell_price_min_date"))

            # Aggregate all quality histories into a single daily-sales signal.
            sums={};days={}
            for h in hist_rows:
                key=(h.get("item_id"),h.get("location"))
                vals=[]
                for p in h.get("data") or []:
                    try:vals.append(float(p.get("item_count",0) or 0))
                    except:pass
                if vals:
                    sums[key]=sums.get(key,0)+sum(vals)
                    days[key]=max(days.get(key,0),len(vals))
            vm={k:(sums[k]/max(days.get(k,1),1)) for k in sums}

            # User quality mix is intentionally explicit rather than pretending specs are known.
            qmix={1:1.0,2:0,3:0,4:0,5:0}
            if s["quality_mode"]=="My quality mix":
                qmix=self.get_quality_mix()
                if sum(qmix.values())<=0:
                    raise RuntimeError("Your quality mix is empty. Open My Profile and enter percentages that add up to 100.")

            out=[]
            for sc in scenarios:
                uid=sc["target"]; rec=sc["recipe"]; craft_uid=sc["craft_uid"]
                rrr,bonus=self.recipe_rrr(rec,s)
                if s.get("specialty_only") and not bonus:continue

                raw_per=0.0;returned_per=0.0;mat_breakdown=[];mat_worst_age=0;missing=False
                for m in rec.get("materials") or []:
                    mid=m.get("id");count=float(m.get("count") or 0)
                    row=pmap.get((mid,s["city"],1))
                    price,dt=market_price(row,s["buy_mode"])
                    ma=age(dt)
                    if not price or ma>s["maxage"]:
                        missing=True;break
                    market_p=float(price)
                    okey=self.material_override_key(mid,s["city"])
                    manual=okey in self.manual_price_overrides
                    if manual:
                        try:price=float(self.manual_price_overrides[okey])
                        except:price=market_p;manual=False
                    gross=price*count
                    ret_value=gross*rrr if m.get("ret",True) else 0.0
                    raw_per+=gross;returned_per+=ret_value;mat_worst_age=max(mat_worst_age,ma)
                    mat_breakdown.append({"id":mid,"name":names.get(mid,mid),"count_per_run":count,"count":count*s["runs"],
                        "price":price,"market_price":market_p,"manual":manual,"gross":gross*s["runs"],"returnable":bool(m.get("ret",True)),
                        "return_value":ret_value*s["runs"],"age":ma,"kind":"Craft material"})
                if missing:continue

                # Upgrade materials are never returned.
                up_per=0.0
                ucount=upgrade_count(uid)
                for step in sc["upgrade_steps"]:
                    mid=upgrade_material_id(tier(uid),step)
                    row=pmap.get((mid,s["city"],1))
                    price,dt=market_price(row,s["buy_mode"])
                    ma=age(dt)
                    if not price or ma>s["maxage"]:
                        missing=True;break
                    market_p=float(price)
                    okey=self.material_override_key(mid,s["city"])
                    manual=okey in self.manual_price_overrides
                    if manual:
                        try:price=float(self.manual_price_overrides[okey])
                        except:price=market_p;manual=False
                    gross=price*ucount
                    up_per+=gross;raw_per+=gross;mat_worst_age=max(mat_worst_age,ma)
                    mat_breakdown.append({"id":mid,"name":names.get(mid,mid),"count_per_run":ucount,"count":ucount*s["runs"],
                        "price":price,"market_price":market_p,"manual":manual,"gross":gross*s["runs"],"returnable":False,"return_value":0,
                        "age":ma,"kind":"Upgrade material"})
                if missing:continue

                raw=raw_per*s["runs"];returned=returned_per*s["runs"]
                netmat=raw-returned
                station=s["station"]*s["runs"]
                basis=netmat+station
                if basis<=0:continue

                # Calculate the non-focus counterpart for "extra profit created by Focus".
                nofocus_returned=0.0
                if s["focus"] and not s["custom_on"]:
                    ns=dict(s);ns["focus"]=False
                    nr,_=self.recipe_rrr(rec,ns)
                    for m in rec.get("materials") or []:
                        if m.get("ret",True):
                            row=pmap.get((m.get("id"),s["city"],1))
                            price,_=market_price(row,s["buy_mode"])
                            nofocus_returned+=price*float(m.get("count") or 0)*nr*s["runs"]

                best=None
                sell_cities=SELL_LOCATIONS if s.get("sell_city")=="Auto" else [s.get("sell_city")]
                for city in sell_cities:
                    expected_gross=0.0;expected_fees=0.0;out_age=0;quality_prices=[];q_missing=False
                    for q,w in qmix.items():
                        if w<=0:continue
                        row=pmap.get((uid,city,q))
                        mode=s["sell_mode"]
                        price,dt=market_price(row,mode)
                        oa=age(dt)
                        out_key=self.output_override_key(uid,city)
                        if out_key in self.manual_price_overrides:
                            try:price=float(self.manual_price_overrides[out_key])
                            except:pass
                        if not price or oa>s["maxage"]:
                            q_missing=True;break
                        expected_gross+=price*w*s["runs"]
                        expected_fees+=sale_fee(price,s["premium"],mode)*w*s["runs"]
                        out_age=max(out_age,oa)
                        quality_prices.append((q,w,price,oa))
                    if q_missing or not quality_prices:continue
                    vv=vm.get((uid,city),0)
                    if vv<s["minvol"]:continue
                    profit=expected_gross-expected_fees-basis
                    roi=profit/basis*100
                    cand=(profit,roi,city,expected_gross,expected_fees,vv,out_age,quality_prices)
                    if best is None or profit>best[0]:best=cand
                if best is None:continue

                profit,roi,sellcity,sp,fees,vv,out_age,quality_prices=best
                if profit<s["minprofit"] or roi<s["minroi"]:continue
                conf=confidence(mat_worst_age,out_age,vv)
                if confidence_rank(conf)<{"Any":1,"MEDIUM+":2,"HIGH":3}.get(s.get("minconf"),1):continue
                refresh="YES" if max(mat_worst_age,out_age)>90 else "No"
                total_focus=s["focus_cost"]*s["runs"] if s["focus"] else 0
                p10k=(profit/total_focus*10000) if total_focus>0 else None
                extra_profit=(returned-nofocus_returned) if (s["focus"] and not s["custom_on"]) else None
                extra10k=(extra_profit/total_focus*10000) if (extra_profit is not None and total_focus>0) else None
                days_to_sell=(s["runs"]/vv) if vv>0 else None
                _,depth,_=live_depth(uid,sellcity,1,"sell")
                out.append({
                    "profit":profit,"roi":roi,"uid":uid,"name":names.get(uid,uid),"craft_uid":craft_uid,
                    "route":sc["route"],"runs":s["runs"],"craftcity":s["city"],"bonus":bonus,"rrr":rrr,
                    "sellcity":sellcity,"raw":raw,"returned":returned,"netmat":netmat,"station":station,
                    "sell":sp,"fees":fees,"p10k":p10k,"extra10k":extra10k,"volume":vv,"days":days_to_sell,
                    "depth":depth,"mat_age":mat_worst_age,"out_age":out_age,"refresh":refresh,"confidence":conf,
                    "materials":mat_breakdown,"specialty":rec.get("city"),"quality_prices":quality_prices,
                    "buy_mode":s["buy_mode"],"sell_mode":s["sell_mode"],"focus":s["focus"],
                    "custom_rrr":s["custom_on"],"focus_total":total_focus,"extra_profit":extra_profit
                })

            out.sort(key=lambda x:(x["profit"],x["roi"]),reverse=True)

            def show():
                self.last_craft_rows=[];self.last_craft_records=[]
                for idx,d in enumerate(out[:500]):
                    uid=d["uid"];t=tier(uid);e=enchant(uid)
                    tag=self.craft_semantic_tag(d)
                    iid=self.craft_tree.insert("","end",text="",tags=(tag,),values=(
                        f"{d['name']} ({t}.{e})",f"{t}.{e}",d["route"],d["runs"],d["craftcity"],
                        "YES" if d["bonus"] else "No",f"{d['rrr']*100:.1f}%",d["sellcity"],
                        f"{d['raw']:,.0f}",f"{d['returned']:,.0f}",f"{d['netmat']:,.0f}",f"{d['station']:,.0f}",
                        f"{d['sell']:,.0f}",f"{d['fees']:,.0f}",f"{d['profit']:,.0f}",f"{d['roi']:.1f}%",
                        f"{d['p10k']:,.0f}" if d["p10k"] is not None else "—",
                        f"{d['extra10k']:,.0f}" if d["extra10k"] is not None else "—",
                        f"{d['volume']:.1f}",f"{d['days']:.1f}" if d["days"] is not None else "—",
                        d["depth"] if d["depth"] else "—",agetxt(d["mat_age"]),agetxt(d["out_age"]),
                        d["refresh"],d["confidence"]
                    ))
                    d["_iid"]=iid
                    self.craft_details[iid]=d
                    self.last_craft_records.append(d)
                    self.last_craft_rows.append([
                        f"{d['name']} ({t}.{e})",f"{t}.{e}",d["route"],d["runs"],d["craftcity"],
                        "YES" if d["bonus"] else "No",d["rrr"]*100,d["sellcity"],d["raw"],d["returned"],
                        d["netmat"],d["station"],d["sell"],d["fees"],d["profit"],d["roi"],
                        d["p10k"] if d["p10k"] is not None else "",d["extra10k"] if d["extra10k"] is not None else "",
                        d["volume"],d["days"] if d["days"] is not None else "",d["depth"],agetxt(d["mat_age"]),
                        agetxt(d["out_age"]),d["refresh"],d["confidence"]
                    ])
                    if idx<150:self.load_icon_async(self.craft_tree,self.craft_icon_images,iid,uid)

                focus_text="Focus ON" if s["focus"] else "Focus OFF"
                refresh_count=sum(1 for d in out if d["refresh"]=="YES")
                self.craft_status.set(f"Done — {len(out):,} scenarios • {focus_text} • {s['runs']} runs • {HISTORY_DAYS}d sales • {refresh_count} need refresh • {s['buy_mode']} → {s['sell_mode']}.")
                self.craft_tree.xview_moveto(0)
                self.craft_export_btn.config(state="normal" if self.last_craft_rows else "disabled")
                self.craft_btn.config(state="normal")
                self.update_watchlist_from_scans()
                self.refresh_dashboard()
            self.root.after(0,show)

        except Exception as e:
            self.root.after(0,lambda:(messagebox.showerror("Craft scan error",str(e)),self.craft_btn.config(state="normal"),self.craft_status.set("Craft scan failed.")))

    def show_craft_breakdown(self,event=None):
        sel=self.craft_tree.selection()
        if not sel:return
        d=self.craft_details.get(sel[0])
        if not d:return

        win=tk.Toplevel(self.root)
        win.title(f"Recipe Editor — {d['name']} ({tier(d['uid'])}.{enchant(d['uid'])})")
        win.geometry("1040x760")
        frame=ttk.Frame(win,padding=16);frame.pack(fill="both",expand=True)
        ttk.Label(frame,text=f"{d['name']} ({tier(d['uid'])}.{enchant(d['uid'])}) — {d['route']} × {d['runs']}",style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame,text="Double-click any material price to override it. Manual prices update the main Crafting table immediately and survive market refreshes until reset.").pack(anchor="w",pady=(4,10))

        info=tk.StringVar()
        ttk.Label(frame,textvariable=info).pack(anchor="w",pady=(0,8))

        cols=("material","type","count","unit","source","gross","return","net","age")
        tree=ttk.Treeview(frame,columns=cols,show="headings",height=10)
        hs={"material":"Material","type":"Type","count":"Qty","unit":"Unit price","source":"Price source","gross":"Gross","return":"Returned mats","net":"Effective cost","age":"Market age"}
        ws={"material":270,"type":115,"count":65,"unit":95,"source":95,"gross":100,"return":100,"net":100,"age":75}
        for c in cols:
            tree.heading(c,text=hs[c]);tree.column(c,width=ws[c],anchor="e" if c in ("count","unit","gross","return","net") else "w")
        tree.pack(fill="x",pady=(0,8))

        summary=tk.Text(frame,height=15,width=110,wrap="none")
        summary.pack(fill="both",expand=True,pady=(4,8))

        def render():
            for x in tree.get_children():tree.delete(x)
            for idx,m in enumerate(d["materials"]):
                price,manual=self.effective_material_price(m,d)
                net=m.get("gross",0)-m.get("return_value",0)
                label=m["name"]+("" if m.get("returnable",True) else " [NON-RETURNABLE]")
                tree.insert("","end",iid=f"m{idx}",values=(
                    label,m.get("kind","Material"),f"{m['count']:g}",f"{price:,.0f}",
                    "MANUAL" if manual else "AODP",f"{m.get('gross',0):,.0f}",
                    f"{m.get('return_value',0):,.0f}",f"{net:,.0f}",agetxt(m.get("age",0))
                ))
            out_key=self.output_override_key(d["uid"],d["sellcity"])
            out_manual=out_key in self.manual_price_overrides
            out_unit=(float(self.manual_price_overrides[out_key]) if out_manual else (d["sell"]/max(1,d["runs"])))
            focus="ON" if self.use_focus.get() else "OFF"
            info.set(f"Craft {d['craftcity']} • Sell {d['sellcity']} • Focus {focus} • RRR {d['rrr']*100:.1f}% • Output unit price {out_unit:,.0f} ({'MANUAL' if out_manual else 'MARKET MODEL'})")
            text=(
                f"Raw material cost:          {d['raw']:,.0f}\n"
                f"Returned mats value:       -{d['returned']:,.0f}\n"
                f"Effective material cost:    {d['netmat']:,.0f}\n"
                f"Manual station fee total:  +{d['station']:,.0f}\n"
                f"Total cost basis:            {d['netmat']+d['station']:,.0f}\n\n"
                f"Expected sale value:         {d['sell']:,.0f}\n"
                f"Market fees/tax:            -{d['fees']:,.0f}\n"
                f"Net sale proceeds:           {d['sell']-d['fees']:,.0f}\n\n"
                f"PROFIT:                      {d['profit']:,.0f}\n"
                f"ROI:                         {d['roi']:.1f}%\n"
                f"Profit / 10k Focus:          {d['p10k']:,.0f}\n" if d.get("p10k") is not None else
                f"Raw material cost:          {d['raw']:,.0f}\nReturned mats value:       -{d['returned']:,.0f}\n"
                f"Effective material cost:    {d['netmat']:,.0f}\nManual station fee total:  +{d['station']:,.0f}\n"
                f"Total cost basis:            {d['netmat']+d['station']:,.0f}\n\nExpected sale value:         {d['sell']:,.0f}\n"
                f"Market fees/tax:            -{d['fees']:,.0f}\nNet sale proceeds:           {d['sell']-d['fees']:,.0f}\n\n"
                f"PROFIT:                      {d['profit']:,.0f}\nROI:                         {d['roi']:.1f}%\nProfit / 10k Focus:          —\n"
            )
            if d.get("extra10k") is not None:text+=f"Extra profit / 10k Focus:    {d['extra10k']:,.0f}\n"
            summary.config(state="normal");summary.delete("1.0","end");summary.insert("1.0",text);summary.config(state="disabled")
            self.apply_text_theme(summary)

        def edit_material(evt=None):
            selm=tree.selection()
            if not selm:return
            idx=int(selm[0][1:]);m=d["materials"][idx]
            current,_=self.effective_material_price(m,d)
            value=simpledialog.askfloat("Manual material price",f"{m['name']}\nEnter unit price:",initialvalue=current,minvalue=0,parent=win)
            if value is None:return
            self.manual_price_overrides[self.material_override_key(m["id"],d["craftcity"])]=value
            self.save_manual_overrides();self.recalculate_loaded_crafts(True);render()

        def reset_material():
            selm=tree.selection()
            if not selm:return
            idx=int(selm[0][1:]);m=d["materials"][idx]
            self.manual_price_overrides.pop(self.material_override_key(m["id"],d["craftcity"]),None)
            self.save_manual_overrides();self.recalculate_loaded_crafts(True);render()

        def edit_output():
            key=self.output_override_key(d["uid"],d["sellcity"])
            current=float(self.manual_price_overrides.get(key,d["sell"]/max(1,d["runs"])))
            value=simpledialog.askfloat("Manual output price",f"{d['name']} in {d['sellcity']}\nEnter sale price per item:",initialvalue=current,minvalue=0,parent=win)
            if value is None:return
            self.manual_price_overrides[key]=value
            self.save_manual_overrides();self.recalculate_loaded_crafts(True);render()

        def reset_output():
            self.manual_price_overrides.pop(self.output_override_key(d["uid"],d["sellcity"]),None)
            self.save_manual_overrides();self.recalculate_loaded_crafts(True);render()

        def reset_all():
            for m in d["materials"]:
                self.manual_price_overrides.pop(self.material_override_key(m["id"],d["craftcity"]),None)
            self.manual_price_overrides.pop(self.output_override_key(d["uid"],d["sellcity"]),None)
            self.save_manual_overrides();self.recalculate_loaded_crafts(True);render()

        tree.bind("<Double-1>",edit_material)
        b=ttk.Frame(frame);b.pack(fill="x",pady=(4,0))
        ttk.Button(b,text="EDIT SELECTED MATERIAL",command=edit_material).pack(side="left")
        ttk.Button(b,text="RESET SELECTED MATERIAL",command=reset_material).pack(side="left",padx=(6,0))
        ttk.Button(b,text="EDIT OUTPUT PRICE",command=edit_output).pack(side="left",padx=(6,0))
        ttk.Button(b,text="RESET OUTPUT PRICE",command=reset_output).pack(side="left",padx=(6,0))
        ttk.Button(b,text="RESET ALL MANUAL PRICES",command=reset_all).pack(side="left",padx=(6,0))
        ttk.Button(b,text="COPY SHOPPING LIST",command=lambda:self.copy_shopping_list(d)).pack(side="right",padx=(6,0))
        ttk.Button(b,text="CLOSE",command=win.destroy).pack(side="right")
        render()

    def copy_shopping_list(self,d):
        lines=[f"{d['name']} ({tier(d['uid'])}.{enchant(d['uid'])}) — {d.get('route','Direct')} × {d.get('runs',1)} — craft in {d['craftcity']}",""]
        for m in d["materials"]:
            lines.append(f"{m['count']:g}x {m['name']} @ {m['price']:,.0f} each")
        lines += ["",f"Raw mats: {d['raw']:,.0f}",f"Returned mats value: {d['returned']:,.0f}",f"Estimated profit: {d['profit']:,.0f}"]
        text="\n".join(lines)
        self.root.clipboard_clear();self.root.clipboard_append(text)
        messagebox.showinfo("Copied","Shopping list copied to clipboard.")

    # ---------- AI market assistant ----------
    def build_ai_tab(self):
        outer=ttk.Frame(self.ai_tab,padding=16);outer.pack(fill="both",expand=True)
        ttk.Label(outer,text="AI Market Assistant",style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer,text="Uses the market flips and crafting results already loaded in this app. Scan first for the freshest recommendations.").pack(anchor="w",pady=(2,10))

        setup=ttk.LabelFrame(outer,text="OpenAI API",padding=10);setup.pack(fill="x")
        row=ttk.Frame(setup);row.pack(fill="x")
        ttk.Label(row,text="API key").pack(side="left")
        self.ai_api_key=tk.StringVar(value=os.environ.get("OPENAI_API_KEY",""))
        self.ai_key_entry=ttk.Entry(row,textvariable=self.ai_api_key,show="*",width=55)
        self.ai_key_entry.pack(side="left",padx=(8,8))
        ttk.Label(row,text="Model").pack(side="left",padx=(10,4))
        self.ai_model=tk.StringVar(value="gpt-5.6-luna")
        ttk.Combobox(row,textvariable=self.ai_model,values=("gpt-5.6-luna","gpt-5.6-terra","gpt-5.6-sol"),width=18,state="readonly").pack(side="left")
        ttk.Button(row,text="SAVE KEY LOCALLY",command=self.save_ai_key).pack(side="left",padx=(8,0))
        self.ai_monthly_limit=tk.DoubleVar(value=5.0);self.ai_spend=0.0;self.ai_call_count=0;self.ai_usage_month=""
        self.load_ai_usage()
        budget=ttk.Frame(setup);budget.pack(fill="x",pady=(8,0))
        ttk.Label(budget,text="Monthly AI limit $").pack(side="left")
        ttk.Entry(budget,textvariable=self.ai_monthly_limit,width=8).pack(side="left",padx=(5,7))
        ttk.Button(budget,text="SAVE LIMIT",command=self.save_ai_usage).pack(side="left")
        self.ai_usage_label=tk.StringVar();ttk.Label(budget,textvariable=self.ai_usage_label).pack(side="left",padx=(14,0))
        self.refresh_ai_usage_label()

        quick=ttk.Frame(outer);quick.pack(fill="x",pady=(10,6))
        ttk.Button(quick,text="BEST CRAFTS",command=lambda:self.ai_quick("Find the best crafts in my currently loaded results. Prioritize realistic profit, ROI, sales volume, data freshness, and Focus efficiency. Tell me what to craft, where to craft it, where to sell it, and why.")).pack(side="left")
        ttk.Button(quick,text="BEST FLIPS",command=lambda:self.ai_quick("Find the best market flips in my currently loaded results. Prioritize realistic profit, ROI, volume, live depth, confidence, and fresh prices. Tell me what to buy, where to buy it, where to sell it, and why.")).pack(side="left",padx=(6,0))
        ttk.Button(quick,text="WHAT TO REFRESH",command=lambda:self.ai_quick("Which prices should I refresh in game first to improve the reliability of my best opportunities? Give me a short prioritized list and explain why each matters.")).pack(side="left",padx=(6,0))
        ttk.Button(quick,text="FOCUS ANALYSIS",command=lambda:self.ai_quick("Analyze my loaded crafting results specifically for Focus use. Rank the strongest opportunities by extra profit created by Focus and profit per 10k Focus while accounting for sales volume and stale data.")).pack(side="left",padx=(6,0))

        ttk.Label(outer,text="Ask about your loaded Albion market data:").pack(anchor="w",pady=(4,2))
        self.ai_question=tk.Text(outer,height=4,wrap="word")
        self.ai_question.pack(fill="x")
        self.apply_text_theme(self.ai_question)
        buttons=ttk.Frame(outer);buttons.pack(fill="x",pady=(6,6))
        self.ai_ask_btn=ttk.Button(buttons,text="ASK AI",command=self.ask_ai);self.ai_ask_btn.pack(side="left")
        ttk.Button(buttons,text="CLEAR",command=lambda:self.ai_answer.config(state="normal") or self.ai_answer.delete("1.0","end")).pack(side="left",padx=(6,0))
        self.ai_status=tk.StringVar(value="Ready. Scan Market Flips or Crafting first.")
        ttk.Label(buttons,textvariable=self.ai_status).pack(side="left",padx=(12,0))

        self.ai_answer=tk.Text(outer,wrap="word",height=24)
        self.ai_answer.pack(fill="both",expand=True)
        self.apply_text_theme(self.ai_answer)

    def ai_usage_path(self):
        return os.path.join(APP_DIR,"ai_usage.json")

    def ai_month(self):
        return datetime.now().strftime("%Y-%m")

    def load_ai_usage(self):
        month=self.ai_month()
        try:
            with open(self.ai_usage_path(),"r",encoding="utf-8") as f:d=json.load(f)
            self.ai_monthly_limit.set(float(d.get("monthly_limit",5.0)))
            if d.get("month")==month:
                self.ai_spend=float(d.get("estimated_spend",0.0));self.ai_call_count=int(d.get("calls",0))
            self.ai_usage_month=month
        except Exception:self.ai_usage_month=month

    def save_ai_usage(self):
        try:
            limit=max(0.0,float(self.ai_monthly_limit.get()));self.ai_monthly_limit.set(limit)
            with open(self.ai_usage_path(),"w",encoding="utf-8") as f:
                json.dump({"month":self.ai_month(),"monthly_limit":limit,"estimated_spend":self.ai_spend,"calls":self.ai_call_count},f,indent=2)
            if hasattr(self,"ai_usage_label"):self.refresh_ai_usage_label()
        except Exception as e:messagebox.showerror("AI budget",str(e))

    def refresh_ai_usage_label(self):
        if self.ai_usage_month!=self.ai_month():
            self.ai_spend=0.0;self.ai_call_count=0;self.ai_usage_month=self.ai_month();self.save_ai_usage();return
        limit=max(0.0,float(self.ai_monthly_limit.get()));remaining=max(0.0,limit-self.ai_spend)
        state="STOPPED" if limit<=0 or self.ai_spend>=limit else "ACTIVE"
        self.ai_usage_label.set(f"Estimated spend: ${self.ai_spend:.4f}  |  Calls: {self.ai_call_count}  |  Remaining: ${remaining:.4f}  |  AI {state}")

    def ai_price_rates(self,model):
        return {"gpt-5.6-luna":(0.20,1.20),"gpt-5.6-terra":(2.0,12.0),"gpt-5.6-sol":(4.0,20.0)}.get(model,(0.20,1.20))

    def ai_usage_cost(self,model,usage):
        inp=float((usage or {}).get("input_tokens",0) or 0);out=float((usage or {}).get("output_tokens",0) or 0)
        ip,op=self.ai_price_rates(model)
        return (inp*ip+out*op)/1000000.0

    def ai_key_path(self):
        return os.path.join(APP_DIR,"openai_api_key.txt")

    def save_ai_key(self):
        key=self.ai_api_key.get().strip()
        if not key:
            messagebox.showwarning("API key","Paste an OpenAI API key first.");return
        try:
            with open(self.ai_key_path(),"w",encoding="utf-8") as f:f.write(key)
            messagebox.showinfo("Saved","API key saved locally on this PC. It is not uploaded to GitHub.")
        except Exception as e:messagebox.showerror("Save key",str(e))

    def load_ai_key(self):
        key=self.ai_api_key.get().strip()
        if key:return key
        try:
            with open(self.ai_key_path(),"r",encoding="utf-8") as f:key=f.read().strip()
            if key:self.ai_api_key.set(key)
            return key
        except:return ""

    def ai_quick(self,prompt):
        self.ai_question.delete("1.0","end");self.ai_question.insert("1.0",prompt);self.ask_ai()

    def ai_context(self):
        crafts=sorted(getattr(self,"last_craft_records",[]) or [],key=lambda d:(d.get("profit",0),d.get("roi",0)),reverse=True)[:80]
        flips=sorted(getattr(self,"last_flip_records",[]) or [],key=lambda d:(d.get("profit",0),d.get("roi",0)),reverse=True)[:80]
        def craft_row(d):
            return {"item":d.get("name"),"item_id":d.get("uid"),"tier":f"{tier(d.get('uid',''))}.{enchant(d.get('uid',''))}",
                "craft_city":d.get("craftcity"),"sell_city":d.get("sellcity"),"runs":d.get("runs"),"rrr_pct":round(100*d.get("rrr",0),1),
                "raw_mats":round(d.get("raw",0)),"returned_mats":round(d.get("returned",0)),"station_fee":round(d.get("station",0)),
                "sale_value":round(d.get("sell",0)),"fees":round(d.get("fees",0)),"profit":round(d.get("profit",0)),"roi_pct":round(d.get("roi",0),1),
                "profit_per_10k_focus":round(d["p10k"]) if d.get("p10k") is not None else None,
                "extra_profit_per_10k_focus":round(d["extra10k"]) if d.get("extra10k") is not None else None,
                "sales_per_day":round(d.get("volume",0),2),"days_to_sell":round(d["days"],2) if d.get("days") is not None else None,
                "mat_age":agetxt(d.get("mat_age",10**9)),"output_age":agetxt(d.get("out_age",10**9)),
                "confidence":d.get("confidence"),"needs_refresh":d.get("refresh")}
        def flip_row(d):
            keep=("name","uid","buycity","sellcity","buy","sell","fees","profit","roi","volume","depth","confidence","refresh","buy_age","sell_age")
            return {k:d.get(k) for k in keep if k in d}
        return {"app_version":APP_VERSION,"premium":bool(self.premium.get()),
            "focus_enabled":bool(self.use_focus.get()) if hasattr(self,"use_focus") else None,
            "craft_runs":self.craft_runs.get() if hasattr(self,"craft_runs") else None,
            "station_fee_per_run":self.craft_station_fee.get() if hasattr(self,"craft_station_fee") else None,
            "crafts":[craft_row(d) for d in crafts],"flips":[flip_row(d) for d in flips]}

    def ask_ai(self):
        question=self.ai_question.get("1.0","end").strip()
        if not question:return
        self.refresh_ai_usage_label()
        limit=max(0.0,float(self.ai_monthly_limit.get()))
        if limit<=0 or self.ai_spend>=limit:
            messagebox.showwarning("AI monthly limit",f"AI calls are stopped. Your local monthly limit is ${limit:.2f} and estimated tracked spend is ${self.ai_spend:.4f}. Raise the limit and click SAVE LIMIT to continue.");return
        key=self.load_ai_key()
        if not key:
            messagebox.showwarning("OpenAI API key","Paste your OpenAI API key at the top of the AI Assistant tab, then click SAVE KEY LOCALLY.");return
        ctx=self.ai_context()
        if not ctx["crafts"] and not ctx["flips"]:
            messagebox.showwarning("No market data","Run a Crafting or Market Flips scan first so the AI has current app data to analyze.");return
        self.ai_ask_btn.config(state="disabled");self.ai_status.set("Analyzing loaded market data...")
        threading.Thread(target=self._ask_ai_worker,args=(question,key,ctx),daemon=True).start()

    def _ask_ai_worker(self,question,key,ctx):
        try:
            instructions=("You are the AI analyst inside Albion Market Assistant. Analyze ONLY the supplied app data for numerical market claims. "
                "Never invent a price, recipe, city, profit, volume, or freshness value. Treat stale/low-confidence data cautiously. "
                "The player wants actionable Albion Online crafting and market-flip recommendations. Explain important assumptions briefly. "
                "When recommending crafts, consider profit, ROI, liquidity/sales per day, days to sell, price age, confidence, premium, Focus, RRR, station fee, "
                "and profit or extra profit per 10k Focus when available. If data is stale, explicitly say which prices should be refreshed in game.")
            payload={"model":self.ai_model.get(),"instructions":instructions,
                "input":"PLAYER QUESTION:\\n"+question+"\\n\\nCURRENT APP DATA (JSON):\\n"+json.dumps(ctx,separators=(",",":")),
                "max_output_tokens":1800}
            req=urllib.request.Request("https://api.openai.com/v1/responses",data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization":"Bearer "+key,"Content-Type":"application/json","User-Agent":f"AlbionMarketAssistant/{APP_VERSION}"},method="POST")
            with urllib.request.urlopen(req,timeout=120) as r:data=json.loads(r.read().decode("utf-8"))
            answer=data.get("output_text")
            if not answer:
                parts=[]
                for out in data.get("output",[]):
                    for c in out.get("content",[]):
                        if c.get("type")=="output_text":parts.append(c.get("text",""))
                answer="\\n".join(parts).strip()
            if not answer:answer="The API returned no text response."
            usage=data.get("usage") or {};cost=self.ai_usage_cost(self.ai_model.get(),usage)
            self.root.after(0,lambda a=answer,c=cost:self._show_ai_answer(a,c))
        except Exception as e:
            msg=str(e)
            if hasattr(e,"read"):
                try:msg=e.read().decode("utf-8")[:1000]
                except:pass
            self.root.after(0,lambda m=msg:self._show_ai_error(m))

    def _show_ai_answer(self,answer,cost=0.0):
        self.ai_spend+=float(cost);self.ai_call_count+=1;self.save_ai_usage()
        self.ai_answer.config(state="normal");self.ai_answer.delete("1.0","end");self.ai_answer.insert("1.0",answer)
        self.ai_answer.see("1.0");self.ai_ask_btn.config(state="normal");self.ai_status.set(f"Done — this call ~${cost:.5f}. Answer based on currently loaded app data.")

    def _show_ai_error(self,msg):
        self.ai_ask_btn.config(state="normal");self.ai_status.set("AI request failed.")
        messagebox.showerror("AI Assistant",msg)

    def export_flips_csv(self):
        rows=getattr(self,"last_flip_rows",[])
        if not rows:return
        path=filedialog.asksaveasfilename(defaultextension=".csv",filetypes=[("CSV files","*.csv")],initialfile="albion_market_flips.csv")
        if not path:return
        headers=["Item","Tier","Buy city","Sell city","Buy","Sell","Sell fees","Profit","ROI %","14d/day","Live depth","Buy age","Sell age","Refresh?","Confidence"]
        with open(path,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f);w.writerow(headers);w.writerows(rows)
        messagebox.showinfo("Exported",f"Saved {len(rows)} rows.")

    def export_crafts_csv(self):
        rows=getattr(self,"last_craft_rows",[])
        if not rows:return
        path=filedialog.asksaveasfilename(defaultextension=".csv",filetypes=[("CSV files","*.csv")],initialfile="albion_profitable_crafts.csv")
        if not path:return
        headers=["Item","Tier","Route","Runs","Craft city","Bonus?","RRR %","Sell city","Raw mats","Returned mats value","Net mats","Station","Sell","Sell fees","Profit","ROI %","Profit/10k Focus","Extra/10k Focus","14d/day","Days to sell","Live depth","Mats age","Output age","Refresh?","Confidence"]
        with open(path,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f);w.writerow(headers);w.writerows(rows)
        messagebox.showinfo("Exported",f"Saved {len(rows)} rows.")

root=tk.Tk()
App(root)
root.mainloop()
