"""Read-only API availability and quota check, never prints or persists credentials."""
from pathlib import Path
import argparse,json,concurrent.futures
import apimart_wan as a

def main():
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);x=p.parse_args();key=a.credential()
 def one(path):
  try:
   d=a.call(key,path,timeout=40)
   if path=='/models':
    rows=d.get('data',[])if isinstance(d,dict)else d
    if isinstance(rows,dict):rows=rows.get('models',[])
    return path,{'request_ok':True,'seedance_2_0_listed':any(isinstance(r,dict)and r.get('id')=='seedance-2.0'for r in rows)}
   fields=('success','remain_balance','used_balance','unlimited_quota','total_balance','message')
   return path,{k:d[k]for k in fields if k in d}
  except Exception as e:return path,{'request_ok':False,'error':str(e).replace(key,'***')[:300]}
 with concurrent.futures.ThreadPoolExecutor(3)as ex:d=dict(ex.map(one,['/balance','/user/balance','/models']))
 q=Path(x.out);q.parent.mkdir(parents=True,exist_ok=True);q.write_text(json.dumps(d,ensure_ascii=False,indent=2))
 print(json.dumps({'account_auth_ok':d['/user/balance'].get('success'),'token_auth_ok':d['/balance'].get('success'),'seedance_2_0_listed':d['/models'].get('seedance_2_0_listed'),'receipt':str(q)},ensure_ascii=False))
if __name__=='__main__':main()
