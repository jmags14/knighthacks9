"""StormBox storm-risk runtime: plain Python, no scikit-learn needed.

    model = load("models/storm_risk.json")
    feats = features_from_hourly(last_7_hourly_sea_level_pressures)   # oldest first
    result = predict(model, feats)      # {"risk": 0.78, "alert": True, ...}
    explain(model, result)              # "Storm risk 78% because ..."
"""
import json, math

def load(path="models/storm_risk.json"):
    with open(path) as f:
        return json.load(f)

def to_sea_level(p_hpa, altitude_m, temp_c=25.0):
    """Barometer reading at the box -> sea-level pressure (what the model was trained on)."""
    if not altitude_m:
        return p_hpa
    return p_hpa * (1 - 0.0065 * altitude_m / (temp_c + 0.0065 * altitude_m + 273.15)) ** -5.257

def features_from_hourly(p):
    """p: 7 hourly sea-level pressures, oldest first: [t-6h, t-5h, ..., now]."""
    p = list(p)[-7:]
    if len(p) < 7:
        raise ValueError("need 7 hourly values (t-6h ... now)")
    now = p[-1]
    d3 = now - p[-4]
    return {"p_now": now, "d1": now - p[-2], "d3": d3, "d6": now - p[0],
            "min6": min(p), "d3_rate": d3 - (p[-2] - p[-5])}

def predict(model, feats):
    return _tree(model, feats) if model["use"] == "tree" else _logreg(model, feats)

def _tree(model, feats):
    nodes, i, path = model["tree"]["nodes"], 0, []
    while "feature" in nodes[i]:
        n = nodes[i]
        x = feats[n["feature"]]
        left = x <= n["threshold"]
        path.append({"node": i, "feature": n["feature"], "value": x,
                     "threshold": n["threshold"], "op": "<=" if left else ">"})
        i = n["left"] if left else n["right"]
    risk = nodes[i]["risk"]
    return {"model": "tree", "risk": risk, "alert": risk >= model["threshold"],
            "path": path, "leaf": i}

def _logreg(model, feats):
    m = model["logreg"]
    z, parts = m["intercept"], []
    for f, mu, sd, w in zip(model["features"], m["mean"], m["scale"], m["coef"]):
        c = w * (feats[f] - mu) / sd
        z += c
        parts.append((f, c))
    z = max(-30.0, min(30.0, z))
    risk = 1 / (1 + math.exp(-z))
    return {"model": "logreg", "risk": risk, "alert": risk >= model["threshold"],
            "top_factors": sorted(parts, key=lambda t: -abs(t[1]))[:3]}

def explain(model, result):
    """One plain-English sentence the agent can say or the dashboard can show."""
    h = model["feature_help"]
    pct = round(100 * result["risk"])
    if "path" in result:
        last = {}                       # keep the tightest check per feature
        for s in result["path"]:
            last[s["feature"]] = s
        why = [f"{h[f]} is {s['value']:.1f} hPa "
               f"({'at or below' if s['op'] == '<=' else 'above'} {s['threshold']:.1f})"
               for f, s in last.items()]
    else:
        why = [f"{h[f]} {'raises' if c > 0 else 'lowers'} the risk" for f, c in result["top_factors"]]
    return f"Storm risk {pct}% because " + "; ".join(why) + "."
