"""Pure validation helpers for safe multi-leg Upstox GTT updates."""
from __future__ import annotations
import hashlib, json, math
from typing import Any, Mapping

RULE_FIELDS=("strategy","trigger_type","trigger_price","trailing_gap","market_protection")
SUPPORTED_LEGS={"ENTRY","STOPLOSS","TARGET"}

def to_plain(value: Any) -> Any:
    if value is None or isinstance(value,(str,int,float,bool)): return value
    if isinstance(value,(list,tuple)): return [to_plain(x) for x in value]
    if isinstance(value,dict): return {str(k):to_plain(v) for k,v in value.items()}
    conv=getattr(value,"to_dict",None)
    if callable(conv): return to_plain(conv())
    attrs=getattr(value,"__dict__",None)
    if isinstance(attrs,dict): return {k:to_plain(v) for k,v in attrs.items() if not k.startswith("_")}
    raise TypeError(f"Unsupported Upstox response value: {type(value).__name__}")

def order_fingerprint(order: dict) -> str:
    raw=json.dumps(order,sort_keys=True,separators=(",",":"),default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def unwrap_order(response: Any, requested_id: str) -> dict:
    payload=to_plain(getattr(response,"data",response))
    if isinstance(payload,dict) and "data" in payload and not payload.get("gtt_order_id"): payload=payload["data"]
    if isinstance(payload,list):
        matches=[x for x in payload if isinstance(x,dict) and x.get("gtt_order_id")==requested_id]
        if len(matches)!=1: raise ValueError(f"Expected exactly one GTT order matching {requested_id}; found {len(matches)}.")
        payload=matches[0]
    if not isinstance(payload,dict): raise ValueError("Upstox returned an unexpected GTT order response.")
    if payload.get("gtt_order_id") and payload["gtt_order_id"]!=requested_id: raise ValueError("Upstox returned a different GTT order ID.")
    if not isinstance(payload.get("rules"),list) or not payload["rules"]: raise ValueError("GTT has no readable rules; refusing to modify.")
    if not payload.get("type") or payload.get("quantity") is None: raise ValueError("GTT type or quantity is missing; refusing to modify.")
    return payload

def _normalize_updates(updates: Mapping[str,float]) -> dict[str,float]:
    if not isinstance(updates,Mapping) or not updates: raise ValueError("Provide at least one trigger update.")
    out={}
    for raw_leg,raw_price in updates.items():
        leg=str(raw_leg).upper()
        if leg not in SUPPORTED_LEGS: raise ValueError(f"Unsupported GTT leg {raw_leg!r}.")
        if leg in out: raise ValueError(f"Duplicate update for {leg}.")
        try: price=float(raw_price)
        except (TypeError,ValueError): raise ValueError(f"New {leg} trigger price must be numeric.") from None
        if not math.isfinite(price) or price<=0: raise ValueError(f"New {leg} trigger price must be finite and greater than zero.")
        out[leg]=price
    return out

def build_modified_rules(existing_rules:list, updates:Mapping[str,float])->tuple[list,list]:
    updated=[dict(x) for x in existing_rules]; changes=[]
    for leg,price in _normalize_updates(updates).items():
        indexes=[i for i,r in enumerate(updated) if str(r.get("strategy","")).upper()==leg]
        if len(indexes)!=1: raise ValueError(f"Expected exactly one {leg} rule; found {len(indexes)}. No changes made.")
        i=indexes[0]
        try: old=float(updated[i]["trigger_price"])
        except (KeyError,TypeError,ValueError): raise ValueError(f"Existing {leg} trigger price is missing or invalid.") from None
        updated[i]["trigger_price"]=price
        changes.append({"leg":leg,"old_trigger_price":old,"new_trigger_price":price})
    rules=[]
    for rule in updated:
        missing={"strategy","trigger_type","trigger_price"}-set(rule)
        if missing: raise ValueError(f"Existing GTT rule missing fields: {', '.join(sorted(missing))}.")
        rules.append({k:rule[k] for k in RULE_FIELDS if k in rule})
    return rules,changes

def validate_gtt_modifications(order:dict, updates:Mapping[str,float])->dict:
    rules,changes=build_modified_rules(order["rules"],updates)
    return {"gtt_order_id":order.get("gtt_order_id"),"type":order["type"],"quantity":order["quantity"],"changes":changes,
            "modify_request":{"type":order["type"],"gtt_order_id":order.get("gtt_order_id"),"quantity":order["quantity"],"rules":rules}}

def validate_gtt_modification(order:dict,leg:str,new_price:float)->dict:
    result=validate_gtt_modifications(order,{leg:new_price}); change=result["changes"][0]
    return {**result,"leg":change["leg"],"old_trigger_price":change["old_trigger_price"],"new_trigger_price":change["new_trigger_price"]}
