"""StormBox storm-risk runtime v2: plain Python, no scikit-learn needed.

    model = load("ai/ml/storm_risk_wind.json")          # or _pressure / _both
    hist = {"p": [...7 hourly sea-level hPa...],          # only what the model needs
            "w": [...7 hourly mean wind m/s...],
            "g": [...7 hourly max gust m/s...]}          # g optional (falls back to w)
    feats = features_from_history(model, hist)          # oldest first: [t-6h, ..., now]
    result = predict(model, feats)                      # {"risk": 0.78, "alert": True, "path": [...]}
    explain(model, result)                              # "Storm risk 78% because ..."
"""
import json, math

P_FEATURES = ["p_now", "d1", "d3", "d6", "min6", "d3_rate"]
W_FEATURES = ["w_now", "w_mean3", "g_max6", "dw3", "dw6"]

def load(path="ai/ml/storm_risk_wind.json"):
    with open(path) as f:
        return json.load(f)

def needs(model):
    """Which inputs the model needs: a set like {"p"}, {"w"} or {"p", "w"}."""
    f = set(model["features"])
    return ({"p"} if f & set(P_FEATURES) else set()) | ({"w"} if f & set(W_FEATURES) else set())

def to_sea_level(p_hpa, altitude_m, temp_c=25.0):
    """Barometer reading at the box -> sea-level pressure (what the model was trained on)."""
    if not altitude_m:
        return p_hpa
    return p_hpa * (1 - 0.0065 * altitude_m / (temp_c + 0.0065 * altitude_m + 273.15)) ** -5.257

def _last7(values, name):
    v = list(values)[-7:]
    if len(v) < 7 or any(x is None for x in v):
        raise ValueError(f"need 7 hourly {name} values (t-6h ... now)")
    return [float(x) for x in v]

def features_from_hourly(p):
    """Pressure features from 7 hourly sea-level pressures, oldest first (kept for older code)."""
    p = _last7(p, "pressure")
    now = p[-1]
    d3 = now - p[-4]
    return {"p_now": now, "d1": now - p[-2], "d3": d3, "d6": now - p[0],
            "min6": min(p), "d3_rate": d3 - (p[-2] - p[-5])}

def wind_features(w, g=None):
    """Wind features from 7 hourly mean winds and 7 hourly max gusts (m/s), oldest first."""
    w = _last7(w, "wind")
    g = _last7(g, "gust") if g is not None else w
    return {"w_now": w[-1], "w_mean3": sum(w[-3:]) / 3, "g_max6": max(g),
            "dw3": w[-1] - w[-4], "dw6": w[-1] - w[0]}

def features_from_history(model, hist):
    feats = {}
    if "p" in needs(model):
        feats.update(features_from_hourly(hist["p"]))
    if "w" in needs(model):
        feats.update(wind_features(hist["w"], hist.get("g")))
    return feats

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
    return {"model": "tree", "set": model.get("set"), "risk": risk,
            "alert": risk >= model["threshold"], "path": path, "leaf": i}

def _logreg(model, feats):
    m = model["logreg"]
    z, parts = m["intercept"], []
    for f, mu, sd, w in zip(model["features"], m["mean"], m["scale"], m["coef"]):
        c = w * (feats[f] - mu) / sd
        z += c
        parts.append((f, c))
    z = max(-30.0, min(30.0, z))
    risk = 1 / (1 + math.exp(-z))
    return {"model": "logreg", "set": model.get("set"), "risk": risk,
            "alert": risk >= model["threshold"],
            "top_factors": sorted(parts, key=lambda t: -abs(t[1]))[:3]}

def _fmt(model, f, v):
    unit = model.get("feature_units", {}).get(f, "hPa")
    if unit == "m/s":
        return f"{v:.1f} m/s (about {abs(v) * 2.237:.0f} mph)"
    return f"{v:.1f} hPa"

def explain(model, result):
    """One plain-English sentence the agent can say or the dashboard can show."""
    h = model["feature_help"]
    pct = round(100 * result["risk"])
    if "path" in result:
        last = {}
        for s in result["path"]:
            last[s["feature"]] = s
        why = [f"{h[f]} is {_fmt(model, f, s['value'])} "
               f"({'at or below' if s['op'] == '<=' else 'above'} {s['threshold']:.1f})"
               for f, s in last.items()]
    else:
        why = [f"{h[f]} {'raises' if c > 0 else 'lowers'} the risk" for f, c in result["top_factors"]]
    return f"Storm risk score {pct} because " + "; ".join(why) + "."
