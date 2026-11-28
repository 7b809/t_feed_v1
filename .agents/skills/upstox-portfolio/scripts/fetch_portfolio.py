"""Fetch complete Upstox holdings or positions as validated JSON."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"upstox"/"scripts"))
from upstox_helpers import get_client

def to_plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        return to_plain(converter())
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {key: to_plain(item) for key, item in attrs.items() if not key.startswith("_")}
    return str(value)


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--kind",choices=["holdings","positions"],required=True)
    a=p.parse_args()
    try:
        import upstox_client
        api=upstox_client.PortfolioApi(get_client())
        response=(api.get_holdings(api_version="2.0") if a.kind=="holdings"
                  else api.get_positions(api_version="2.0"))
        raw=to_plain(response)
        data=raw.get("data",[]) if isinstance(raw,dict) else raw
        if data is None: data=[]
        if isinstance(data,dict): records=[data]
        elif isinstance(data,list): records=data
        else: raise ValueError("Unexpected portfolio response structure.")
        result={"status":"success","kind":a.kind,"count":len(records),
                "records":records,"raw_response":raw,"error":None}
        print(json.dumps(result,indent=2,ensure_ascii=False,default=str)); return 0
    except Exception as exc:
        print(json.dumps({"status":"error","kind":a.kind,"count":0,"records":[],
            "raw_response":None,"error":{"type":type(exc).__name__,"message":str(exc)}},
            indent=2,ensure_ascii=False)); return 0

if __name__=="__main__": raise SystemExit(main())
