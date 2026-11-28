"""Fetch Upstox margin requirement or brokerage estimate as JSON."""
from __future__ import annotations
import argparse, json, math
from pathlib import Path
import sys
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
    p.add_argument("--kind",choices=["required-margin","brokerage"],required=True)
    p.add_argument("--instrument",required=True)
    p.add_argument("--quantity",type=int,required=True)
    p.add_argument("--transaction-type",choices=["BUY","SELL"],required=True)
    p.add_argument("--product",choices=["I","D","CO","OCO","MTF"],required=True)
    p.add_argument("--price",type=float,required=True)
    a=p.parse_args()
    try:
        if a.quantity<=0 or not math.isfinite(a.price) or a.price<0:
            raise ValueError("Quantity must be positive and price must be finite and non-negative.")
        import upstox_client
        api=upstox_client.ChargeApi(get_client())
        if a.kind=="required-margin":
            instrument=upstox_client.Instrument(instrument_key=a.instrument,
                quantity=a.quantity,transaction_type=a.transaction_type,
                product=a.product,price=a.price)
            response=api.post_margin(upstox_client.MarginRequest(instruments=[instrument]))
        else:
            response=api.get_brokerage(instrument_token=a.instrument,quantity=a.quantity,
                product=a.product,transaction_type=a.transaction_type,price=a.price,
                api_version="2.0")
        raw=to_plain(response)
        data=raw.get("data",raw) if isinstance(raw,dict) else raw
        result={"status":"success","kind":a.kind,"request":{
            "instrument":a.instrument,"quantity":a.quantity,
            "transaction_type":a.transaction_type,"product":a.product,"price":a.price},
            "data":data,"raw_response":raw,"error":None}
        print(json.dumps(result,indent=2,ensure_ascii=False,default=str)); return 0
    except Exception as exc:
        print(json.dumps({"status":"error","kind":a.kind,"request":{
            "instrument":a.instrument,"quantity":a.quantity,
            "transaction_type":a.transaction_type,"product":a.product,"price":a.price},
            "data":None,"raw_response":None,
            "error":{"type":type(exc).__name__,"message":str(exc)}},
            indent=2,ensure_ascii=False)); return 0

if __name__=="__main__": raise SystemExit(main())
