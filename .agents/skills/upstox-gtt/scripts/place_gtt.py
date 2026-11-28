"""Preview or explicitly confirm an Upstox GTT order; emit structured JSON."""
from __future__ import annotations
import argparse, hashlib, json, math, os, urllib.error, urllib.request


def emit(value: dict, code: int=0) -> int:
    print(json.dumps(value,indent=2,ensure_ascii=False,default=str)); return code


def main() -> int:
    p=argparse.ArgumentParser(description="Preview or place an Upstox multi-leg GTT.")
    p.add_argument("--instrument",required=True)
    p.add_argument("--quantity",type=int,required=True)
    p.add_argument("--entry-trigger",type=float,required=True)
    p.add_argument("--sl-trigger",type=float,required=True)
    p.add_argument("--target-trigger",type=float,required=True)
    p.add_argument("--product",choices=["I","D","MTF"],default="D")
    p.add_argument("--transaction-type",choices=["BUY","SELL"],default="BUY")
    p.add_argument("--confirm",action="store_true")
    p.add_argument("--expected-preview-hash")
    a=p.parse_args()
    try:
        prices=[a.entry_trigger,a.sl_trigger,a.target_trigger]
        if a.quantity<=0: raise ValueError("--quantity must be greater than zero.")
        if any(not math.isfinite(x) or x<=0 for x in prices):
            raise ValueError("All GTT trigger prices must be finite and greater than zero.")
        if a.confirm and not a.expected_preview_hash:
            raise ValueError("--expected-preview-hash from the reviewed preview is required with --confirm.")
        payload={"type":"MULTIPLE","quantity":a.quantity,"product":a.product,
            "instrument_token":a.instrument,"transaction_type":a.transaction_type,
            "rules":[{"strategy":"ENTRY","trigger_type":"IMMEDIATE","trigger_price":a.entry_trigger},
                     {"strategy":"STOPLOSS","trigger_type":"IMMEDIATE","trigger_price":a.sl_trigger},
                     {"strategy":"TARGET","trigger_type":"IMMEDIATE","trigger_price":a.target_trigger}]}
        fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        if not a.confirm:
            return emit({"status":"PREVIEW","gtt_order":payload,"preview_hash":fingerprint,
                "confirmation_required":True,"message":"No GTT was placed. Review every field and approve this exact preview hash."})
        if fingerprint!=a.expected_preview_hash:
            raise ValueError("GTT inputs differ from the reviewed preview hash. No GTT was placed.")
        token=os.getenv("UPSTOX_ACCESS_TOKEN","").strip()
        if not token: raise RuntimeError("UPSTOX_ACCESS_TOKEN is not set in the process environment.")
        req=urllib.request.Request("https://api.upstox.com/v3/order/gtt/place",
            data=json.dumps(payload).encode(),
            headers={"Content-Type":"application/json","Accept":"application/json",
                     "Authorization":f"Bearer {token}"},method="POST")
        try:
            with urllib.request.urlopen(req,timeout=15) as response:
                body=json.loads(response.read().decode("utf-8"))
            return emit({"status":"SUCCESS","gtt_order":payload,"preview_hash":fingerprint,
                         "api_response":body,"message":"Upstox accepted the GTT request."})
        except urllib.error.HTTPError as exc:
            raw=exc.read().decode("utf-8",errors="replace")
            try: detail=json.loads(raw)
            except json.JSONDecodeError: detail={"body":raw}
            return emit({"status":"API_ERROR","gtt_order":payload,"preview_hash":fingerprint,
                         "http_status":exc.code,"api_response":detail},1)
    except Exception as exc:
        return emit({"status":"ERROR","error":{"type":type(exc).__name__,"message":str(exc)},
                     "gtt_order":None,"preview_hash":None},1)


if __name__=="__main__": raise SystemExit(main())
