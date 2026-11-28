"""Preview or explicitly confirm a normal Upstox order; emit structured JSON."""
from __future__ import annotations
import argparse, hashlib, json, math, os, sys, urllib.error, urllib.request


def round_to_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def emit(value: dict, code: int = 0) -> int:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    return code


def main() -> int:
    p=argparse.ArgumentParser(description="Preview or place a normal Upstox order.")
    p.add_argument("--instrument",required=True)
    p.add_argument("--quantity",type=int,required=True)
    p.add_argument("--order-type",choices=["MARKET","LIMIT"],required=True)
    p.add_argument("--price",type=float,default=0.0)
    p.add_argument("--transaction-type",choices=["BUY","SELL"],default="BUY")
    p.add_argument("--product",choices=["I","D"],default="D")
    p.add_argument("--validity",choices=["DAY","IOC"],default="DAY")
    p.add_argument("--confirm",action="store_true")
    p.add_argument("--expected-preview-hash")
    a=p.parse_args()
    try:
        if a.quantity<=0: raise ValueError("--quantity must be greater than zero.")
        if not math.isfinite(a.price) or a.price<0: raise ValueError("--price must be finite and non-negative.")
        if a.order_type=="LIMIT" and a.price<=0: raise ValueError("--price must be greater than zero for LIMIT.")
        if a.confirm and not a.expected_preview_hash:
            raise ValueError("--expected-preview-hash from the reviewed preview is required with --confirm.")
        price=round_to_tick(a.price) if a.order_type=="LIMIT" else 0.0
        payload={"quantity":a.quantity,"product":a.product,"validity":a.validity,
            "price":price,"tag":"algoapp_order","instrument_token":a.instrument,
            "order_type":a.order_type,"transaction_type":a.transaction_type,
            "disclosed_quantity":0,"trigger_price":0,"is_amo":False}
        fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        if not a.confirm:
            return emit({"status":"PREVIEW","order":payload,"preview_hash":fingerprint,
                "confirmation_required":True,"message":"No order was placed. Review the complete order and approve this exact preview hash."})
        if fingerprint!=a.expected_preview_hash:
            raise ValueError("Order inputs differ from the reviewed preview hash. No order was placed.")
        token=os.getenv("UPSTOX_ACCESS_TOKEN","").strip()
        if not token: raise RuntimeError("UPSTOX_ACCESS_TOKEN is not set in the process environment.")
        req=urllib.request.Request("https://api.upstox.com/v2/order/place",
            data=json.dumps(payload).encode(),
            headers={"Content-Type":"application/json","Accept":"application/json",
                     "Authorization":f"Bearer {token}"},method="POST")
        try:
            with urllib.request.urlopen(req,timeout=15) as response:
                body=json.loads(response.read().decode("utf-8"))
            return emit({"status":"SUCCESS","order":payload,"preview_hash":fingerprint,
                         "api_response":body,"message":"Upstox accepted the order request."})
        except urllib.error.HTTPError as exc:
            raw=exc.read().decode("utf-8",errors="replace")
            try: detail=json.loads(raw)
            except json.JSONDecodeError: detail={"body":raw}
            return emit({"status":"API_ERROR","order":payload,"preview_hash":fingerprint,
                         "http_status":exc.code,"api_response":detail},1)
    except Exception as exc:
        return emit({"status":"ERROR","error":{"type":type(exc).__name__,"message":str(exc)},
                     "order":None,"preview_hash":None},1)


if __name__=="__main__": raise SystemExit(main())
