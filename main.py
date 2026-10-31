import os, time, json, logging, concurrent.futures
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timezone
import requests
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('yalgo')
S = requests.Session()
DATABASE_URL = os.getenv('DATABASE_URL', '')
EXCHANGES = [x.strip().upper() for x in os.getenv('EXCHANGES','BINANCE,OKX').split(',')]
MIN_SCORE = float(os.getenv('MIN_SCORE','90'))
MIN_ADX = float(os.getenv('MIN_ADX','25'))
MIN_VOLUME = float(os.getenv('MIN_VOLUME','1000000'))
MAX_POSITIONS = int(os.getenv('MAX_POSITIONS','5'))
POSITION_USDT = float(os.getenv('POSITION_USDT','100'))
SL_PCT = float(os.getenv('SL_PCT','0.012'))
COOLDOWN = int(os.getenv('SIGNAL_COOLDOWN_MINUTES','15')) * 60
LIMIT = int(os.getenv('SCAN_LIMIT','20'))
WORKERS = int(os.getenv('MAX_WORKERS','6'))
DEADLINE = time.monotonic() + int(os.getenv('MAX_SCAN_SECONDS','600'))
WEBHOOK = os.getenv('DISCORD_WEBHOOK','')
TP_R = [float(x) for x in os.getenv('TP_R','0.8,1.5,2.5').split(',')]
WEIGHTS = [float(x) for x in os.getenv('TP_WEIGHTS','0.4,0.3,0.3').split(',')]
assert len(TP_R)==len(WEIGHTS)==3 and abs(sum(WEIGHTS)-1)<1e-8
assert os.getenv('RUN_MODE','PAPER')=='PAPER', 'Live trading is disabled'


def api(url, params=None):
    try:
        r=S.get(url, params=params, timeout=12, headers={'User-Agent':'YAlgoPaperRadar/2.5'})
        if r.status_code==451:
            LOG.warning('HTTP 451 (restricted): %s; skipping', url.split('/')[2]); return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        LOG.warning('API failed: %s: %s', url.split('/')[2], str(exc)[:180]); return None


def notify(message):
    LOG.info('%s', message)
    if not WEBHOOK: return
    try:
        r=S.post(WEBHOOK, json={'content':message[:1900]}, timeout=12)
        r.raise_for_status()
    except requests.RequestException as exc: LOG.warning('Discord failed: %s', exc)


def connect():
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL secret is missing')
    # Never log the connection string: it contains database credentials.
    c = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=12)
    c.execute("""CREATE TABLE IF NOT EXISTS positions (
        id BIGSERIAL PRIMARY KEY,
        market TEXT NOT NULL,
        side TEXT NOT NULL,
        entry DOUBLE PRECISION NOT NULL,
        qty DOUBLE PRECISION NOT NULL,
        remaining DOUBLE PRECISION NOT NULL,
        stop DOUBLE PRECISION NOT NULL,
        tp1 DOUBLE PRECISION NOT NULL,
        tp2 DOUBLE PRECISION NOT NULL,
        tp3 DOUBLE PRECISION NOT NULL,
        hit1 INTEGER DEFAULT 0,
        hit2 INTEGER DEFAULT 0,
        hit3 INTEGER DEFAULT 0,
        realized DOUBLE PRECISION DEFAULT 0,
        opened BIGINT NOT NULL,
        closed BIGINT,
        status TEXT DEFAULT 'OPEN'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS signals (
        market TEXT NOT NULL,
        side TEXT NOT NULL,
        ts BIGINT NOT NULL,
        PRIMARY KEY (market, side)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_yalgo_positions_status ON positions(status)")
    c.commit()
    return c


def execute(c, sql, args=()):
    # Existing scanner SQL uses SQLite-style positional placeholders.
    # Psycopg uses %s; all values remain parameterized.
    return c.execute(sql.replace('?', '%s'), args)


def symbols():
    out=[]
    if 'BINANCE' in EXCHANGES:
        data=api('https://fapi.binance.com/fapi/v1/exchangeInfo')
        if data:
            out += [('BINANCE', x['symbol'], x['symbol']) for x in data.get('symbols',[]) if x.get('quoteAsset')=='USDT' and x.get('contractType')=='PERPETUAL' and x.get('status')=='TRADING']
    if 'OKX' in EXCHANGES:
        data=api('https://www.okx.com/api/v5/public/instruments',{'instType':'SWAP'})
        if data and data.get('code')=='0':
            out += [('OKX', x['instId'], x['instId']) for x in data.get('data',[]) if x.get('settleCcy')=='USDT' and x.get('state')=='live' and x.get('instId','').endswith('-USDT-SWAP')]
    return out[:LIMIT] if LIMIT>0 else out


def candles(ex, symbol, tf):
    if ex=='BINANCE':
        d=api('https://fapi.binance.com/fapi/v1/klines',{'symbol':symbol,'interval':f'{tf}m','limit':220})
        if not isinstance(d,list): return None
        rows=[(int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5]),float(x[7])) for x in d]
    else:
        d=api('https://www.okx.com/api/v5/market/candles',{'instId':symbol,'bar':f'{tf}m','limit':'220'})
        if not d or d.get('code')!='0': return None
        rows=[(int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5]),float(x[7])) for x in d.get('data',[]) if x[8]=='1']
        rows.sort(key=lambda x:x[0])
    if len(rows)<80:return None
    df=pd.DataFrame(rows,columns=['ts','open','high','low','close','volume','quote_volume'])
    # Discard still-forming Binance candle; OKX data already uses confirmed candles.
    if ex=='BINANCE' and len(df)>1 and df.iloc[-1].ts + tf*60000 > int(time.time()*1000): df=df.iloc[:-1]
    return df if len(df)>=80 else None


def indicators(df):
    close=df.close; high=df.high; low=df.low
    ema9=close.ewm(span=9,adjust=False).mean(); ema21=close.ewm(span=21,adjust=False).mean(); ema50=close.ewm(span=50,adjust=False).mean()
    delta=close.diff(); gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean(); loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    rsi=100-100/(1+gain/loss.replace(0,np.nan))
    tr=pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
    up=high.diff(); down=-low.diff()
    plus=pd.Series(np.where((up>down)&(up>0),up,0),index=df.index).ewm(alpha=1/14,adjust=False).mean()
    minus=pd.Series(np.where((down>up)&(down>0),down,0),index=df.index).ewm(alpha=1/14,adjust=False).mean()
    atr=tr.ewm(alpha=1/14,adjust=False).mean().replace(0,np.nan)
    pdi=100*plus/atr; mdi=100*minus/atr
    adx=(100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)).ewm(alpha=1/14,adjust=False).mean()
    macd=close.ewm(span=12,adjust=False).mean()-close.ewm(span=26,adjust=False).mean()
    hist=macd-macd.ewm(span=9,adjust=False).mean()
    # VWAP anchored to available candle history, not an exchange session.
    vwap=(close*df.volume).cumsum()/df.volume.cumsum().replace(0,np.nan)
    return {'close':float(close.iloc[-1]),'ema9':float(ema9.iloc[-1]),'ema21':float(ema21.iloc[-1]),'ema50':float(ema50.iloc[-1]),'rsi':float(rsi.iloc[-1]),'adx':float(adx.iloc[-1]),'pdi':float(pdi.iloc[-1]),'mdi':float(mdi.iloc[-1]),'hist':float(hist.iloc[-1]),'vwap':float(vwap.iloc[-1]),'volume':float(df.quote_volume.iloc[-1]),'break_high':float(high.iloc[-9:-1].max()),'break_low':float(low.iloc[-9:-1].min())}


def scan(item):
    if time.monotonic()>DEADLINE: return None
    ex,sym,_=item
    frames={}
    for tf in (1,5,15):
        df=candles(ex,sym,tf)
        if df is None:return None
        frames[tf]=indicators(df)
    a,b,h=frames[1],frames[5],frames[15]
    if a['volume']<MIN_VOLUME or a['adx']<=MIN_ADX:return None
    for side in ('LONG','SHORT'):
        up=side=='LONG'; direction=1 if up else -1
        aligned=all(direction*(v['ema9']-v['ema21'])>0 and direction*(v['ema21']-v['ema50'])>0 for v in (a,b,h))
        if not aligned:continue
        directional=(a['pdi']>a['mdi']) if up else (a['mdi']>a['pdi'])
        if not directional:continue
        score=20+15+15  # 15m trend + EMA alignment + ADX direction
        score+=15 if (a['rsi']>55 if up else a['rsi']<45) else 0
        score+=10 if direction*a['hist']>0 else 0
        score+=10 if direction*(a['close']-a['vwap'])>0 else 0
        score+=10 if (a['close']>a['break_high'] if up else a['close']<a['break_low']) else 0
        score+=5 if (b['rsi']>50 if up else b['rsi']<50) else 0
        if score>=MIN_SCORE:
            return {'market':f'{ex}:{sym}','side':side,'price':a['close'],'score':score,'adx':a['adx']}
    return None


def update_positions(c, prices, now):
    for p in execute(c, "SELECT * FROM positions WHERE status='OPEN'").fetchall():
        key=p['market']; tick=prices.get(key)
        if tick is None:continue
        high,low=tick
        direction=1 if p['side']=='LONG' else -1
        # Conservative OHLC ordering: stop is assumed hit first if both touched.
        stop_hit=low<=p['stop'] if direction==1 else high>=p['stop']
        if stop_hit:
            pnl=p['remaining']*direction*(p['stop']-p['entry'])
            execute(c, "UPDATE positions SET remaining=0,realized=realized+?,status='CLOSED',closed=? WHERE id=?",(pnl,now,p['id']))
            notify(f'🛑 {key} {p["side"]} {"BE" if p["hit1"] else "SL"} @ {p["stop"]:.8g} | PnL {pnl:+.4f} USDT')
            continue
        remaining=p['remaining']; realized=0; stop=p['stop']; hits=[]
        for idx in (1,2,3):
            if p[f'hit{idx}']:continue
            target=p[f'tp{idx}']; hit=high>=target if direction==1 else low<=target
            if not hit:break
            qty=min(remaining,p['qty']*WEIGHTS[idx-1]); remaining-=qty
            realized+=qty*direction*(target-p['entry']); hits.append(idx)
            execute(c, f'UPDATE positions SET hit{idx}=1 WHERE id=?',(p['id'],))
            if idx==1:stop=p['entry']
            notify(f'🎯 {key} {p["side"]} TP{idx} @ {target:.8g} | PnL {qty*direction*(target-p["entry"]):+.4f} USDT')
        if hits:
            execute(c, 'UPDATE positions SET remaining=?,stop=?,realized=realized+?,status=?,closed=? WHERE id=?',(max(0,remaining),stop,realized,'CLOSED' if remaining<1e-10 else 'OPEN',now if remaining<1e-10 else None,p['id']))


def main():
    c=connect(); now=int(time.time())
    universe=symbols(); LOG.info('Found %d instruments',len(universe))
    if not universe:
        notify('⚠️ YAlgo: no exchange instruments available; state preserved'); return
    # Manage existing positions even if the scanner runs out of time.
    open_markets={p['market'] for p in execute(c, "SELECT market FROM positions WHERE status='OPEN'")}
    tick={}
    for market in open_markets:
        ex,sym=market.split(':',1)
        df=candles(ex,sym,1)
        if df is not None:tick[market]=(float(df.high.iloc[-1]),float(df.low.iloc[-1]))
    update_positions(c,tick,now); c.commit()
    slots=MAX_POSITIONS-execute(c, "SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
    if slots<=0:LOG.info('Position cap reached');return
    # Thread workers only fetch and calculate; SQLite writes occur in the main thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures=[pool.submit(scan,item) for item in universe]
        for f in concurrent.futures.as_completed(futures):
            if time.monotonic()>DEADLINE:break
            try:signal=f.result()
            except Exception as exc:LOG.warning('Scan failed: %s',exc);continue
            if not signal:continue
            market=signal['market'];side=signal['side'];price=signal['price']
            if execute(c, "SELECT 1 FROM positions WHERE market=? AND status='OPEN'",(market,)).fetchone():continue
            previous=execute(c, 'SELECT ts FROM signals WHERE market=? AND side=?',(market,side)).fetchone()
            if previous and now-previous['ts']<COOLDOWN:continue
            if slots<=0:continue
            direction=1 if side=='LONG' else -1
            qty=POSITION_USDT/price; risk=price*SL_PCT
            stop=price-direction*risk
            tps=[price+direction*risk*r for r in TP_R]
            execute(c, 'INSERT INTO positions(market,side,entry,qty,remaining,stop,tp1,tp2,tp3,opened,closed,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(market,side,price,qty,qty,stop,*tps,now,None,'OPEN'))
            execute(c, 'INSERT INTO signals(market,side,ts) VALUES(?,?,?) ON CONFLICT(market,side) DO UPDATE SET ts=EXCLUDED.ts',(market,side,now));c.commit()
            slots-=1
            notify(f'🐱 PAPER {side} {market}\nScore {signal["score"]}/100 | ADX {signal["adx"]:.1f}\nEntry {price:.8g} | SL {stop:.8g}\nTP1 {tps[0]:.8g} / TP2 {tps[1]:.8g} / TP3 {tps[2]:.8g}')
    stats=execute(c, "SELECT COUNT(*) AS n,COALESCE(SUM(realized),0) AS pnl FROM positions WHERE status='CLOSED'").fetchone()
    wins=execute(c, "SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND realized>0").fetchone()[0]
    LOG.info('Closed trades=%d Wins=%d Win rate=%.1f%% Realized=%.4f USDT',stats['n'],wins,100*wins/stats['n'] if stats['n'] else 0,stats['pnl'])
    c.commit();c.close()

if __name__=='__main__':main()
