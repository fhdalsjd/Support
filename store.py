import os, sqlite3, threading
from settings import settings
os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
_lock=threading.Lock()
def db():
    c=sqlite3.connect(settings.db_path,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init_db():
    with _lock, db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS positions(mint TEXT PRIMARY KEY,symbol TEXT,qty REAL NOT NULL,entry_sol REAL NOT NULL,last_price_usd REAL DEFAULT 0,tp REAL DEFAULT 0,sl REAL DEFAULT 0,trailing REAL DEFAULT 0)"); c.commit()
def upsert(mint,symbol,qty,entry_sol,tp=0,sl=0):
    with _lock,db() as c:
        c.execute("INSERT INTO positions(mint,symbol,qty,entry_sol,tp,sl) VALUES(?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET qty=qty+excluded.qty,entry_sol=entry_sol+excluded.entry_sol,symbol=excluded.symbol",(mint,symbol,qty,entry_sol,tp,sl)); c.commit()
def positions():
    with db() as c:return [dict(r) for r in c.execute("SELECT * FROM positions WHERE qty>0 ORDER BY rowid DESC")]
def remove(mint,fraction=1.0):
    with _lock,db() as c:c.execute("UPDATE positions SET qty=qty*(1-?) WHERE mint=?",(fraction,mint)); c.commit()
