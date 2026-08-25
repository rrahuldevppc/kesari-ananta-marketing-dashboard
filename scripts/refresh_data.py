#!/usr/bin/env python3
"""
Pulls fresh Meta Ads data for the Kesari Ananta ad account directly from the
Meta Marketing (Graph) API and rewrites the MODEL data-payload embedded in
index.html, leaving all layout/CSS/JS untouched.

Required environment variables:
  META_ACCESS_TOKEN   - a long-lived / system-user access token with ads_read
                         on the ad account below.
Optional:
  META_AD_ACCOUNT_ID  - defaults to act_1273398820495430 (Kesari Ananta Ads)
  META_API_VERSION     - defaults to v21.0

Real data only. If a pull fails, this script exits non-zero rather than
writing partial/fabricated numbers, so the GitHub Action run shows red and
the site is NOT updated with bad data.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

API_VERSION = os.environ.get("META_API_VERSION", "v21.0")
ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID") or "act_1273398820495430"
GRAPH = f"https://graph.facebook.com/{API_VERSION}"
INDEX_HTML = os.path.join(os.path.dirname(__file__), "..", "index.html")

LIFETIME_SINCE = "2023-08-01"  # safely inside the 37-month lookback window


def _get(url, params):
    qs = urllib.parse.urlencode(params)
    full = f"{url}?{qs}"
    req = urllib.request.Request(full, headers={"User-Agent": "kesari-ananta-dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {url}: {body}") from None


def graph_get(path, **params):
    params["access_token"] = ACCESS_TOKEN
    data = _get(f"{GRAPH}/{path}", params)
    if "error" in data:
        raise RuntimeError(f"Meta API error on {path}: {data['error']}")
    return data


def paginate(path, **params):
    out = []
    data = graph_get(path, **params)
    out.extend(data.get("data", []))
    paging = data.get("paging", {})
    next_url = paging.get("next")
    while next_url:
        try:
            req = urllib.request.Request(next_url, headers={"User-Agent": "kesari-ananta-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} on {next_url}: {body}") from None
        if "error" in data:
            raise RuntimeError(f"Meta API error paginating {path}: {data['error']}")
        out.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
    return out


def extract_actions(row, key):
    total = 0.0
    for a in row.get(key, []) or []:
        if a.get("action_type") == "purchase":
            total += float(a.get("value", 0) or 0)
    return total


def insight_row_to_metrics(row):
    spend = float(row.get("spend", 0) or 0)
    impr = int(float(row.get("impressions", 0) or 0))
    clicks = int(float(row.get("clicks", 0) or 0))
    conv = extract_actions(row, "actions")
    rev = extract_actions(row, "action_values")
    ctr = round(clicks / impr * 100, 2) if impr else 0
    cpc = round(spend / clicks, 2) if clicks else 0
    cr = round(conv / clicks * 100, 2) if clicks else 0
    roas = round(rev / spend, 2) if spend else 0
    return {
        "spend": round(spend, 2), "impr": impr, "clicks": clicks, "ctr": ctr, "cpc": cpc,
        "conv": round(conv, 1), "cr": cr, "rev": round(rev, 2), "roas": roas,
    }


def fetch_lifetime_library():
    campaigns = paginate(
        f"{AD_ACCOUNT_ID}/campaigns",
        fields="id,name,status,created_time",
        limit=200,
    )
    insights = paginate(
        f"{AD_ACCOUNT_ID}/insights",
        level="campaign",
        time_range=json.dumps({"since": LIFETIME_SINCE, "until": date.today().isoformat()}),
        fields="campaign_id,campaign_name,spend,impressions,clicks,actions,action_values",
        limit=200,
    )
    insight_by_id = {r["campaign_id"]: r for r in insights}
    library = []
    for c in campaigns:
        row = insight_by_id.get(c["id"], {})
        m = insight_row_to_metrics(row)
        created = c.get("created_time", "")[:10]
        try:
            age = (date.today() - date.fromisoformat(created)).days if created else None
        except ValueError:
            age = None
        library.append({
            "id": c["id"], "name": c["name"], "status": c.get("status", "UNKNOWN"),
            "created": created, "age": age,
            "lifetime_spend": m["spend"], "lifetime_conv": m["conv"], "lifetime_rev": m["rev"],
            "lifetime_roas": m["roas"], "impressions": m["impr"], "clicks": m["clicks"],
        })
    return library, campaigns


def fetch_active_family(campaigns):
    active_campaigns = [c for c in campaigns if c.get("status") == "ACTIVE"]
    active_ids = [c["id"] for c in active_campaigns]
    if not active_ids:
        return active_campaigns, [], []
    adsets = paginate(
        f"{AD_ACCOUNT_ID}/adsets",
        fields="id,name,status,campaign_id",
        filtering=json.dumps([{"field": "campaign.id", "operator": "IN", "value": active_ids}]),
        limit=200,
    )
    adset_ids = [a["id"] for a in adsets]
    ads = []
    if adset_ids:
        ads = paginate(
            f"{AD_ACCOUNT_ID}/ads",
            fields="id,name,status,campaign_id,adset_id",
            filtering=json.dumps([{"field": "adset.id", "operator": "IN", "value": adset_ids}]),
            limit=200,
        )
    return active_campaigns, adsets, ads


def fetch_daily(level, object_ids, days=10):
    """Returns {object_id: {date: metrics_row}} using native time_increment=1."""
    if not object_ids:
        return {}
    until = date.today()
    since = until - timedelta(days=days - 1)
    id_field = {"campaign": "campaign_id", "adset": "adset_id", "ad": "ad_id"}[level]
    name_field = {"campaign": "campaign_name", "adset": "adset_name", "ad": "ad_name"}[level]
    rows = paginate(
        f"{AD_ACCOUNT_ID}/insights",
        level=level,
        time_range=json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        time_increment=1,
        fields=",".join(dict.fromkeys([id_field, name_field, "campaign_id", "campaign_name", "adset_id", "adset_name", "spend", "impressions", "clicks", "actions", "action_values", "date_start"])),
        filtering=json.dumps([{"field": f"{level}.id", "operator": "IN", "value": object_ids}]),
        limit=500,
    )
    by_id = {}
    for r in rows:
        oid = r[id_field]
        by_id.setdefault(oid, {})[r["date_start"]] = insight_row_to_metrics(r)
    return by_id


def agg_n(daily, n):
    days = sorted(daily.keys())[-n:]
    spend = impr = clicks = conv = rev = 0.0
    for d in days:
        m = daily[d]
        spend += m["spend"]; impr += m["impr"]; clicks += m["clicks"]; conv += m["conv"]; rev += m["rev"]
    ctr = round(clicks / impr * 100, 2) if impr else 0
    cpc = round(spend / clicks, 2) if clicks else 0
    cr = round(conv / clicks * 100, 2) if clicks else 0
    roas = round(rev / spend, 2) if spend else 0
    return {"spend": round(spend, 2), "impr": int(impr), "clicks": int(clicks), "ctr": ctr, "cpc": cpc,
            "conv": round(conv, 1), "cr": cr, "rev": round(rev, 2), "roas": roas, "days": len(days)}


def series_n(daily, n):
    days = sorted(daily.keys())[-n:]
    return [{"d": d, "spend": daily[d]["spend"], "roas": daily[d]["roas"], "conv": daily[d]["conv"]} for d in days]


def build_active_block(active_campaigns, adsets, ads, camp_daily, adset_daily, ad_daily, n):
    adset_by_id = {a["id"]: a for a in adsets}
    camp_by_id = {c["id"]: c for c in active_campaigns}

    def launch_age(status_obj):
        # Meta doesn't expose a distinct "launch date" for campaigns beyond created_time;
        # reuse created_time-derived age if available, else None.
        return None, None

    out_campaigns = []
    for c in active_campaigns:
        daily = camp_daily.get(c["id"], {})
        agg = agg_n(daily, n)
        out_campaigns.append({
            "id": c["id"], "name": c["name"], "status": c.get("status", "ACTIVE"),
            "launch": None, "age": None,
            f"agg{n}": agg, f"series{n}": series_n(daily, n),
        })

    out_adsets = []
    for a in adsets:
        camp = camp_by_id.get(a.get("campaign_id"), {})
        daily = adset_daily.get(a["id"], {})
        agg = agg_n(daily, n)
        out_adsets.append({
            "id": a["id"], "name": a["name"], "status": a.get("status", "UNKNOWN"),
            "campaign": camp.get("name", ""),
            f"agg{n}": agg, f"series{n}": series_n(daily, n),
        })

    out_ads = []
    for ad in ads:
        camp = camp_by_id.get(ad.get("campaign_id"), {})
        adset = adset_by_id.get(ad.get("adset_id"), {})
        daily = ad_daily.get(ad["id"], {})
        agg = agg_n(daily, n)
        out_ads.append({
            "id": ad["id"], "name": ad["name"], "status": ad.get("status", "UNKNOWN"),
            "campaign": camp.get("name", ""), "adset": adset.get("name", ""),
            f"agg{n}": agg, f"series{n}": series_n(daily, n),
        })

    return {"campaigns": out_campaigns, "adsets": out_adsets, "ads": out_ads}


def main():
    library, all_campaigns = fetch_lifetime_library()
    active_campaigns, adsets, ads = fetch_active_family(all_campaigns)

    camp_ids = [c["id"] for c in active_campaigns]
    adset_ids = [a["id"] for a in adsets]
    ad_ids = [a["id"] for a in ads]

    camp_daily = fetch_daily("campaign", camp_ids, days=10)
    adset_daily = fetch_daily("adset", adset_ids, days=10)
    ad_daily = fetch_daily("ad", ad_ids, days=10)

    active10 = build_active_block(active_campaigns, adsets, ads, camp_daily, adset_daily, ad_daily, 10)
    active5 = build_active_block(active_campaigns, adsets, ads, camp_daily, adset_daily, ad_daily, 5)
    active1 = build_active_block(active_campaigns, adsets, ads, camp_daily, adset_daily, ad_daily, 1)

    # carry launch/age from campaign created_time if we have it
    created_by_id = {c["id"]: c.get("created_time", "")[:10] for c in all_campaigns}
    for block in (active10, active5, active1):
        for c in block["campaigns"]:
            created = created_by_id.get(c["id"])
            if created:
                c["launch"] = created
                try:
                    c["age"] = (date.today() - date.fromisoformat(created)).days
                except ValueError:
                    c["age"] = None

    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    m = re.search(r'(<script id="data-payload" type="application/json">)(.*?)(</script>)', html, re.S)
    if not m:
        print("ERROR: could not find data-payload script block in index.html", file=sys.stderr)
        sys.exit(1)

    payload = json.loads(m.group(2))
    payload["account"] = {"name": "Kesari Ananta Ads", "account_id": AD_ACCOUNT_ID, "currency": "INR"}
    payload["refreshed"] = f"{date.today().isoformat()}T00:00:00+05:30"
    payload["library"] = library
    payload["active10"] = active10
    payload["active5"] = active5
    payload["active1"] = active1

    new_json = json.dumps(payload)
    new_html = html[: m.start(2)] + new_json + html[m.end(2):]

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"Refreshed: {len(library)} lifetime campaigns, {len(active_campaigns)} active, "
          f"{len(adsets)} adsets, {len(ads)} ads.")


if __name__ == "__main__":
    main()
