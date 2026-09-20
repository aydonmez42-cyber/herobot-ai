import argparse,glob,os,numpy as np,pandas as pd
from indicators import add_indicators
from strategy import signal
import config as c
def run(path):
    d=pd.read_csv(path); d.columns=[x.lower() for x in d.columns]
    d.timestamp=pd.to_datetime(d.timestamp,utc=True); d=d.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
    if len(d)<c.MIN_BARS:return [],-0.0
    d=add_indicators(d); pos=None; eq=c.INITIAL_CAPITAL; peak=eq; dd=0; tr=[]
    for i in range(251,len(d)-1):
        r=d.iloc[i]
        if pos:
            side=pos['side']; ep=pos['entry']; a=pos['atr']; xp=None; reason=None
            if side=='LONG':
                if r.low<=pos['sl']: xp=pos['sl']*(1-c.SLIPPAGE_RATE); reason='ATR_SL'
                elif r.high>=pos['tp']: xp=pos['tp']*(1-c.SLIPPAGE_RATE); reason='ATR_TP'
                else:
                    if r.high>=ep+c.ATR_TRAIL_ACTIVATION*a: pos['ta']=True
                    if pos['ta']:
                        pos['trail']=max(pos.get('trail',-np.inf),r.high-c.ATR_TRAIL_DISTANCE*a)
                        if r.low<=pos['trail']: xp=pos['trail']*(1-c.SLIPPAGE_RATE); reason='ATR_TRAILING_SL'
            else:
                if r.high>=pos['sl']: xp=pos['sl']*(1+c.SLIPPAGE_RATE); reason='ATR_SL'
                elif r.low<=pos['tp']: xp=pos['tp']*(1+c.SLIPPAGE_RATE); reason='ATR_TP'
                else:
                    if r.low<=ep-c.ATR_TRAIL_ACTIVATION*a: pos['ta']=True
                    if pos['ta']:
                        pos['trail']=min(pos.get('trail',np.inf),r.low+c.ATR_TRAIL_DISTANCE*a)
                        if r.high>=pos['trail']: xp=pos['trail']*(1+c.SLIPPAGE_RATE); reason='ATR_TRAILING_SL'
            if xp is not None:
                pnl=(xp-ep)*(1 if side=='LONG' else -1)-(abs(ep)+abs(xp))*c.FEE_RATE
                eq+=pnl; tr.append([pos['entry_time'],r.timestamp,side,ep,xp,pnl,reason]); pos=None
        if pos is None:
            s=signal(d,i)
            if s:
                n=d.iloc[i+1]; ep=n.open*(1+c.SLIPPAGE_RATE if s=='LONG' else 1-c.SLIPPAGE_RATE); a=r.atr
                pos={'side':s,'entry':ep,'entry_time':n.timestamp,'atr':a,'ta':False,
                     'sl':ep-c.ATR_LONG_SL*a if s=='LONG' else ep+c.ATR_SHORT_SL*a,
                     'tp':ep+c.ATR_LONG_TP*a if s=='LONG' else ep-c.ATR_SHORT_TP*a}
        peak=max(peak,eq); dd=min(dd,eq/peak-1)
    return tr,dd
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--data-dir',default='data'); ap.add_argument('--output',default='results'); a=ap.parse_args()
    os.makedirs(a.output,exist_ok=True); rows=[]; alltr=[]
    for p in sorted(glob.glob(a.data_dir+'/*_4h.csv')):
        sym=os.path.basename(p).replace('_4h.csv','')
        try:
            tr,dd=run(p); alltr += [[sym]+x for x in tr]
            t=pd.DataFrame(tr,columns=['entry_time','exit_time','side','entry','exit','pnl','reason'])
            gp=t.loc[t.pnl>0,'pnl'].sum(); gl=-t.loc[t.pnl<0,'pnl'].sum()
            rows.append([sym,len(t),float((t.pnl>0).mean()) if len(t) else np.nan,float(gp/gl) if gl else np.nan,float(t.pnl.sum()),float(dd)])
        except Exception as e: print('ERROR',sym,e)
    pd.DataFrame(rows,columns=['symbol','trades','win_rate','profit_factor','net_pnl','max_dd']).sort_values('net_pnl',ascending=False).to_csv(a.output+'/coin_summary.csv',index=False)
    pd.DataFrame(alltr,columns=['symbol','entry_time','exit_time','side','entry','exit','pnl','reason']).to_csv(a.output+'/all_trades.csv',index=False)
    print('DONE')
if __name__=='__main__':main()
