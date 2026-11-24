"""Preview and safely submit one or more Upstox GTT trigger updates."""
from __future__ import annotations
import argparse, json, math, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"upstox"/"scripts"))
import upstox_client
from upstox_helpers import order_api_v3
from validate_gtt_modify import order_fingerprint,to_plain,unwrap_order,validate_gtt_modifications

def fetch(api,oid):
    return unwrap_order(api.get_gtt_order_details(gtt_order_id=oid),oid)

def verify(order,changes):
    output=[]; all_ok=True
    for change in changes:
        leg=change["leg"]; expected=float(change["new_trigger_price"])
        matches=[r for r in order.get("rules",[]) if str(r.get("strategy","")).upper()==leg]
        actual=None; ok=False
        if len(matches)==1:
            try:
                actual=float(matches[0]["trigger_price"])
                ok=math.isclose(actual,expected,rel_tol=0.0,abs_tol=0.000001)
            except (KeyError,TypeError,ValueError): pass
        all_ok=all_ok and ok
        output.append({"leg":leg,"expected_trigger_price":expected,"actual_trigger_price":actual,"verified":ok})
    return all_ok,output

def emit(data):
    print(json.dumps(data,indent=2,default=str))

def main():
    p=argparse.ArgumentParser(description="Modify one or more ENTRY, STOPLOSS, or TARGET GTT trigger prices.")
    p.add_argument("--gtt-order-id",required=True)
    p.add_argument("--update",action="append",metavar="LEG=PRICE",help="Repeat for each leg, e.g. TARGET=300 STOPLOSS=230.")
    p.add_argument("--leg",choices=["ENTRY","STOPLOSS","TARGET"])
    p.add_argument("--trigger-price",type=float)
    p.add_argument("--confirm",action="store_true")
    p.add_argument("--expected-state-hash",help="Fingerprint returned by the exact preview; required with --confirm.")
    p.add_argument("--json",action="store_true",help="Emit JSON output.")
    a=p.parse_args(); submitted=False
    try:
        updates={}
        for item in a.update or []:
            if "=" not in item: raise ValueError(f"Invalid --update {item!r}; expected LEG=PRICE.")
            leg,price=item.split("=",1); leg=leg.strip().upper()
            if leg in updates: raise ValueError(f"Duplicate update for {leg}.")
            try: updates[leg]=float(price.strip())
            except ValueError: raise ValueError(f"Invalid price in --update {item!r}.") from None
        if a.leg or a.trigger_price is not None:
            if not a.leg or a.trigger_price is None: raise ValueError("Use --leg and --trigger-price together.")
            if a.update: raise ValueError("Do not mix --update with --leg/--trigger-price.")
            updates[a.leg]=a.trigger_price
        if not updates: raise ValueError("Provide one or more --update LEG=PRICE values.")
        if a.confirm and not a.expected_state_hash: raise ValueError("--expected-state-hash from the reviewed preview is required with --confirm.")
        api=order_api_v3(); current=fetch(api,a.gtt_order_id); fingerprint=order_fingerprint(current)
        preview=validate_gtt_modifications(current,updates)
        if a.confirm and fingerprint!=a.expected_state_hash:
            raise RuntimeError("GTT state differs from the reviewed preview. No modification submitted; generate a fresh preview.")
        if not a.confirm:
            emit({"status":"PREVIEW","gtt_order_id":a.gtt_order_id,"changes":preview["changes"],
                  "quantity_preserved":preview["quantity"],"gtt_type_preserved":preview["type"],
                  "all_other_rules_preserved":True,"state_fingerprint":fingerprint,
                  "confirmation_required":True,"message":"No modification submitted. Approve this exact GTT ID, every change, and fingerprint."})
            return 0
        latest=fetch(api,a.gtt_order_id)
        if order_fingerprint(latest)!=a.expected_state_hash:
            raise RuntimeError("GTT state changed after validation. No modification submitted; generate a fresh preview.")
        final=validate_gtt_modifications(latest,updates); req=final["modify_request"]
        body=upstox_client.GttModifyOrderRequest(type=req["type"],gtt_order_id=req["gtt_order_id"] or a.gtt_order_id,
            quantity=req["quantity"],rules=[upstox_client.GttRule(**r) for r in req["rules"]])
        response=api.modify_gtt_order(body=body)  # one API call for all selected legs
        submitted=True; after=fetch(api,a.gtt_order_id); ok,checks=verify(after,final["changes"])
        emit({"status":"SUCCESS" if ok else "VERIFICATION_FAILED","gtt_order_id":a.gtt_order_id,
              "changes":final["changes"],"quantity_preserved":req["quantity"],"gtt_type_preserved":req["type"],
              "all_other_rules_preserved":True,"verification":checks,"api_response":to_plain(response)})
        return 0 if ok else 2
    except Exception as exc:
        emit({"status":"ERROR","gtt_order_id":a.gtt_order_id,"error":str(exc),"modification_submitted":submitted})
        return 1

if __name__=="__main__":
    raise SystemExit(main())
