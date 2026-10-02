#!/usr/bin/env python3
"""Build Causeway Weekly.

Pulls live data (checkpoint cameras, public holidays on both sides, the ringgit
rate), combines it with the dated facts in data/facts.json, and writes the whole
site into public/: the landing page, one page per issue, an RSS feed, and the
markdown body that the weekly GitHub issue (and so the email) carries.
"""
import datetime as dt
import html
import json
import re
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).parent
PUBLIC = ROOT / "public"
ISSUES = PUBLIC / "issues"
FACTS = json.loads((ROOT / "data" / "facts.json").read_text())

SGT = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime.now(SGT)
TODAY = NOW.date()
WINDOW_DAYS = 14
LOOKAHEAD_DAYS = 120
FX_RANGE_DAYS = 30
FLAT_THRESHOLD = 0.05

SITE_NAME = "Causeway Weekly"
SITE_URL = "https://causeway-weekly.vercel.app"
REPO_URL = "https://github.com/kohjunhao/causeway-weekly"
SPONSOR_PRICE_SGD = 200
SPONSOR_URL = REPO_URL + "/issues/new?title=Sponsor%20an%20issue"
LIVE_CAMERAS_URL = "https://onemotoring.lta.gov.sg/content/onemotoring/home/driving/traffic_information/traffic-cameras.html"

TRAFFIC_API = "https://api.data.gov.sg/v1/transport/traffic-images"
FX_API = "https://api.frankfurter.dev/v1"
SG_ICS = "https://calendar.google.com/calendar/ical/en.singapore%23holiday%40group.v.calendar.google.com/public/basic.ics"
MY_ICS = "https://calendar.google.com/calendar/ical/en.malaysia%23holiday%40group.v.calendar.google.com/public/basic.ics"

# Camera ids from LTA DataMall, names as OneMotoring labels them. Since 30 June
# 2026 only the checkpoint cameras (plus Sentosa) stay switched on.
CAMERAS = {
    "2701": "Woodlands Causeway, towards Johor",
    "2702": "Woodlands Checkpoint, towards BKE",
    "2704": "Woodlands Flyover, towards the checkpoint",
    "4703": "Second Link at Tuas",
    "4713": "Tuas Checkpoint",
    "4712": "AYE after Tuas West Road, towards the checkpoint",
}
CAMERA_WIDTH = 960

HIGH, MEDIUM, LOW = "High", "Medium", "Low"
HIGH_SCORE = 3
MEDIUM_SCORE = 2
WEEKEND_SCORE = 2
TIMEOUT = 30


def get(url, **kwargs):
    r = requests.get(url, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r


# ---------- live data ----------

def fetch_cameras():
    data = get(TRAFFIC_API).json()["items"][0]
    stamp = data["timestamp"]
    out = []
    for cam in data["cameras"]:
        cid = cam["camera_id"]
        if cid not in CAMERAS:
            continue
        out.append({"id": cid, "name": CAMERAS[cid], "image": cam["image"]})
    out.sort(key=lambda c: list(CAMERAS).index(c["id"]))
    return stamp, out


def save_camera_images(cams, folder):
    folder.mkdir(parents=True, exist_ok=True)
    for cam in cams:
        raw = get(cam["image"]).content
        path = folder / f"cam-{cam['id']}.jpg"
        tmp = folder / f"cam-{cam['id']}.src"
        tmp.write_bytes(raw)
        img = Image.open(tmp).convert("RGB")
        ratio = CAMERA_WIDTH / img.width
        img = img.resize((CAMERA_WIDTH, round(img.height * ratio)))
        img.save(path, "JPEG", quality=72, optimize=True)
        tmp.unlink()
        cam["file"] = path.name


def parse_ics(text):
    """Return a list of (date, summary, description) for all-day events."""
    lines = []
    for line in text.splitlines():
        if line.startswith(" ") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    events, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if "date" in cur:
                events.append((cur["date"], cur.get("summary", ""), cur.get("desc", "")))
            cur = None
        elif cur is not None:
            if line.startswith("DTSTART;VALUE=DATE:"):
                cur["date"] = dt.datetime.strptime(line.split(":", 1)[1][:8], "%Y%m%d").date()
            elif line.startswith("SUMMARY:"):
                cur["summary"] = line[8:].replace("\\,", ",")
            elif line.startswith("DESCRIPTION:"):
                cur["desc"] = line[12:].replace("\\,", ",")
    return events


def fetch_holidays():
    sg = {}
    for date, summary, desc in parse_ics(get(SG_ICS).text):
        if desc.startswith("Public holiday"):
            sg[date] = clean_name(summary)
    my = {}
    for date, summary, desc in parse_ics(get(MY_ICS).text):
        first = desc.split("\n")[0]
        national = first.startswith("Public holiday") and " in " not in first
        johor = first.startswith("Public holiday in") and "Johor" in first
        if national or johor:
            my[date] = clean_name(summary) + ("" if national else ", Johor only")
    return sg, my


def clean_name(summary):
    return re.sub(r"\s*\((regional|state) holiday\)", "", summary).strip()


def fetch_fx():
    latest = get(f"{FX_API}/latest", params={"base": "SGD", "symbols": "MYR"}).json()
    week_ago = get(f"{FX_API}/{TODAY - dt.timedelta(days=7)}", params={"base": "SGD", "symbols": "MYR"}).json()
    start = TODAY - dt.timedelta(days=FX_RANGE_DAYS)
    series = get(f"{FX_API}/{start}..{TODAY}", params={"base": "SGD", "symbols": "MYR"}).json()["rates"]
    values = [v["MYR"] for v in series.values()]
    return {
        "rate": latest["rates"]["MYR"],
        "date": latest["date"],
        "week_ago": week_ago["rates"]["MYR"],
        "low": min(values),
        "high": max(values),
    }


# ---------- forecast ----------

def school_ranges():
    out = []
    for r in FACTS["school_holidays"]:
        out.append({
            "side": r["side"],
            "label": r["label"],
            "start": dt.date.fromisoformat(r["start"]),
            "end": dt.date.fromisoformat(r["end"]),
        })
    return out


def level(score):
    if score >= HIGH_SCORE:
        return HIGH
    if score >= MEDIUM_SCORE:
        return MEDIUM
    return LOW


def forecast_day(d, sg_ph, my_ph, ranges):
    """Rule-based jam risk for one day, for each direction.

    The rules encode the commute pattern ICA describes every long weekend:
    traffic to Johor builds on the eve of a day off, traffic back to Singapore
    builds on the last day off, and school holidays on either side lift the
    whole fortnight.
    """
    to_jb, to_sg, why = 0, 0, []
    prev, nxt = d - dt.timedelta(days=1), d + dt.timedelta(days=1)

    def is_off(x):
        return x.weekday() >= 5 or x in sg_ph or x in my_ph

    wd = d.weekday()
    if wd == 4:
        to_jb += WEEKEND_SCORE
        why.append("Friday evening exodus")
    if wd == 5:
        to_jb += WEEKEND_SCORE
        why.append("Saturday morning crowd")
    if wd == 6:
        to_sg += WEEKEND_SCORE
        why.append("Sunday evening return")
    for cal, name in ((sg_ph, "Singapore"), (my_ph, "Malaysia")):
        if nxt in cal:
            to_jb += 2
            why.append(f"eve of {cal[nxt]} in {name}")
        if d in cal:
            to_jb += 1
            to_sg += 1
            why.append(f"{cal[d]} in {name}")
    if is_off(d) and not is_off(nxt) and (d in sg_ph or d in my_ph or prev in sg_ph or prev in my_ph):
        to_sg += 2
        why.append("last day of the break, return jam")
    for r in ranges:
        side = "Singapore" if r["side"] == "sg" else "Johor"
        if d == r["start"] - dt.timedelta(days=1) or d == r["start"]:
            to_jb += 2
            why.append(f"{side} school holidays begin")
        elif d == r["end"]:
            to_sg += 2
            why.append(f"{side} school holidays end")
        elif r["start"] < d < r["end"]:
            to_jb += 1
            to_sg += 1
            if f"{side} school holidays" not in why:
                why.append(f"{side} school holidays")
    return {
        "date": d,
        "to_jb": level(to_jb),
        "to_sg": level(to_sg),
        "why": why,
    }


def forecast(sg_ph, my_ph):
    ranges = school_ranges()
    days = [forecast_day(TODAY + dt.timedelta(days=i), sg_ph, my_ph, ranges) for i in range(1, WINDOW_DAYS + 1)]
    beyond = []
    for i in range(WINDOW_DAYS + 1, LOOKAHEAD_DAYS + 1):
        f = forecast_day(TODAY + dt.timedelta(days=i), sg_ph, my_ph, ranges)
        if HIGH in (f["to_jb"], f["to_sg"]):
            beyond.append(f)
    return days, beyond


def rule_changes_in_play():
    soon, later = [], []
    for r in FACTS["rule_changes"]:
        date = dt.date.fromisoformat(r["date"])
        item = dict(r, when=date)
        if date <= TODAY + dt.timedelta(days=LOOKAHEAD_DAYS):
            soon.append(item)
        else:
            later.append(item)
    soon.sort(key=lambda x: x["when"])
    later.sort(key=lambda x: x["when"])
    return soon, later


# ---------- rendering helpers ----------

def fmt(d):
    return d.strftime("%a %-d %b")


def fmt_long(d):
    return d.strftime("%A %-d %B %Y")


def esc(s):
    return html.escape(str(s))


def badge(lvl):
    return f'<span class="risk risk-{lvl.lower()}">{lvl}</span>'


def headline(days, soon):
    worst = max(days, key=lambda f: (f["to_jb"] == HIGH) + (f["to_sg"] == HIGH) + 0.5 * ((f["to_jb"] == MEDIUM) + (f["to_sg"] == MEDIUM)))
    worst_txt = f"Worst day in the next fortnight: {fmt(worst['date'])}" if HIGH in (worst["to_jb"], worst["to_sg"]) else "No long-weekend jam in the next fortnight"
    change = next((r for r in soon if r["when"] >= TODAY), None)
    change_txt = f"{r_date(change)}: {change['title']}" if change else "No rule changes due"
    return worst_txt, change_txt


def r_date(r):
    return fmt(r["when"]) if r["when"].year == TODAY.year else r["when"].strftime("%-d %b %Y")


STYLE = """
:root{--bg:#ffffff;--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--soft:#f6f7f9;--accent:#0f5c9c;--accent-ink:#ffffff;
--high-bg:#fee4e2;--high-ink:#912018;--med-bg:#fef0c7;--med-ink:#93370d;--low-bg:#d1fadf;--low-ink:#05603a}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent)}img{max-width:100%;height:auto;display:block}
.wrap{max-width:720px;margin:0 auto;padding:0 16px}
header.top{border-bottom:1px solid var(--line)}header.top .wrap{display:flex;align-items:center;justify-content:space-between;gap:10px 16px;padding:14px 16px;flex-wrap:wrap}
.brand{font-weight:700;text-decoration:none;color:var(--ink);font-size:18px;letter-spacing:-.01em}
nav{display:flex;flex-wrap:wrap;gap:4px 14px}nav a{font-size:14px;text-decoration:none;color:var(--muted)}nav a:hover{color:var(--ink)}
h1{font-size:34px;line-height:1.15;letter-spacing:-.02em;margin:40px 0 12px}h2{font-size:22px;letter-spacing:-.01em;margin:40px 0 10px}h3{font-size:17px;margin:22px 0 6px}
p.lead{font-size:18px;color:#374151;margin:0 0 24px}.muted{color:var(--muted)}small,.small{font-size:13.5px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:24px 0}.stat{border:1px solid var(--line);border-radius:10px;padding:14px}
.stat b{display:block;font-size:22px;letter-spacing:-.01em}.stat span{font-size:13px;color:var(--muted)}
@media(max-width:560px){.stats{grid-template-columns:1fr}h1{font-size:28px}}
.box{border:1px solid var(--line);border-radius:12px;padding:18px;background:var(--soft)}
form.sub{display:flex;gap:8px;flex-wrap:wrap}form.sub input{flex:1 1 220px;padding:10px 12px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}
button,.btn{background:var(--accent);color:var(--accent-ink);border:0;border-radius:8px;padding:10px 16px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
.btn.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:14.5px}th,td{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
tr.weekend td{background:var(--soft)}
.risk{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12.5px;font-weight:600;white-space:nowrap}
.risk-high{background:var(--high-bg);color:var(--high-ink)}.risk-medium{background:var(--med-bg);color:var(--med-ink)}.risk-low{background:var(--low-bg);color:var(--low-ink)}
.cams{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:560px){.cams{grid-template-columns:1fr}}
.cam img{border-radius:8px;border:1px solid var(--line)}.cam figcaption{font-size:13px;color:var(--muted);margin-top:4px}
.rule{border-left:3px solid var(--accent);padding:2px 0 2px 14px;margin:14px 0}.rule b{display:block}
.oneline{font-size:18px;font-weight:600;margin:6px 0}
ul.archive{list-style:none;padding:0}ul.archive li{padding:8px 0;border-bottom:1px solid var(--line)}
footer{border-top:1px solid var(--line);margin-top:48px;padding:24px 0 40px;color:var(--muted);font-size:13.5px}
.sponsor{border:1px dashed #cbd5e1;border-radius:12px;padding:16px;margin:12px 0}
.kicker{font-size:12.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
"""


def page(title, body, description):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><meta name="description" content="{esc(description)}">
<link rel="alternate" type="application/rss+xml" title="{SITE_NAME}" href="{SITE_URL}/feed.xml">
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(description)}">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%230f5c9c'/%3E%3Cpath d='M4 20h24M4 14h24' stroke='%23fff' stroke-width='3'/%3E%3C/svg%3E">
<style>{STYLE}</style></head>
<body>
<header class="top"><div class="wrap"><a class="brand" href="/">{SITE_NAME}</a>
<nav><a href="/#latest">Latest</a><a href="/#archive">Archive</a><a href="/#method">Method</a><a href="/#sponsor">Sponsor</a><a href="/feed.xml">RSS</a></nav></div></header>
<main class="wrap">
{body}
</main>
<footer><div class="wrap">
<p>{SITE_NAME} is a public good. A script builds it every Sunday at 6 pm Singapore time from data.gov.sg traffic cameras, the public holiday calendars of both countries, MOE and KPM school calendars, ICA media releases and ECB exchange rates. No human edits an issue before it goes out, so check anything you plan a trip around against the linked source.</p>
<p>The forecast is a set of rules, it is not live traffic. For the live picture use the camera images above or call ICA's CHECK-TIPS line on 6863 0117. Source code: <a href="{REPO_URL}">GitHub</a>.</p>
</div></footer>
</body></html>"""


# ---------- issue rendering ----------

def render_forecast_table(days):
    rows = []
    for f in days:
        cls = ' class="weekend"' if f["date"].weekday() >= 5 else ""
        why = "; ".join(f["why"]) if f["why"] else "Normal working day"
        rows.append(f"<tr{cls}><td><b>{fmt(f['date'])}</b></td><td>{esc(why)}</td><td>{badge(f['to_jb'])}</td><td>{badge(f['to_sg'])}</td></tr>")
    return f"""<table><thead><tr><th>Date</th><th>What is on</th><th>To JB</th><th>To SG</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""


def render_beyond(beyond):
    if not beyond:
        return "<p>No long-weekend jams in the next four months. Enjoy it.</p>"
    items = []
    for f in beyond[:8]:
        d = "both directions" if f["to_jb"] == HIGH and f["to_sg"] == HIGH else ("to JB" if f["to_jb"] == HIGH else "to SG")
        items.append(f"<li><b>{fmt(f['date'])}</b>, {d}: {esc('; '.join(f['why']))}</li>")
    return "<ul>" + "".join(items) + "</ul>"


def render_rules(soon, later):
    out = []
    for r in soon:
        tag = "Already in force" if r["when"] <= TODAY else f"From {fmt_long(r['when'])}"
        out.append(f'<div class="rule"><span class="kicker">{esc(tag)}</span><b>{esc(r["title"])}</b>{esc(r["body"])} <a href="{r["source"]}">{esc(r["source_name"])}</a></div>')
    if later:
        out.append("<h3>Further out</h3>")
        for r in later:
            out.append(f'<div class="rule"><span class="kicker">{esc(r["when"].strftime("%B %Y"))}</span><b>{esc(r["title"])}</b>{esc(r["body"])} <a href="{r["source"]}">{esc(r["source_name"])}</a></div>')
    out.append("<h3>Standing rules</h3><ul>")
    for r in FACTS["standing_rules"]:
        out.append(f'<li><b>{esc(r["title"])}.</b> {esc(r["body"])} <a href="{r["source"]}">{esc(r["source_name"])}</a></li>')
    out.append("</ul>")
    return "".join(out)


def render_fx(fx):
    hundred = fx["rate"] * 100
    return f"""<p class="oneline">S$1 = RM{fx['rate']:.4f}</p>
<p>The Singapore dollar is {week_change(fx)} (RM{fx['week_ago']:.4f} a week ago). Over the last {FX_RANGE_DAYS} days it ranged from RM{fx['low']:.4f} to RM{fx['high']:.4f}. S$100 changes to about RM{hundred:,.0f} at the mid-market rate on {fx['date']}; a money changer takes a cut on top. Rate from the European Central Bank reference series via frankfurter.dev.</p>"""


def week_change(fx):
    change = (fx["rate"] - fx["week_ago"]) / fx["week_ago"] * 100
    if abs(change) < FLAT_THRESHOLD:
        return "flat against the ringgit this week"
    return f"{abs(change):.1f}% {'stronger' if change > 0 else 'weaker'} against the ringgit than a week ago"


def render_cams(cams, stamp, folder_rel):
    figs = []
    for c in cams:
        figs.append(f'<figure class="cam"><img src="/{folder_rel}/{c["file"]}" alt="{esc(c["name"])}" loading="lazy" width="{CAMERA_WIDTH}"><figcaption>{esc(c["name"])}</figcaption></figure>')
    when = dt.datetime.fromisoformat(stamp).strftime("%A %-d %B %Y, %H:%M")
    return f'<p class="small muted">Captured {when} Singapore time from LTA cameras via data.gov.sg. <a href="{LIVE_CAMERAS_URL}">Live view on OneMotoring</a>.</p><div class="cams">{"".join(figs)}</div>'


def render_sponsor():
    return f"""<div class="sponsor"><span class="kicker">Sponsor slot</span>
<p>This slot is empty this week. One sponsor per issue, S${SPONSOR_PRICE_SGD}, plain text, two lines, no tracking. The reader is someone who crosses the Causeway and plans around it: a money changer, a JB clinic, a car insurer, an e-hailing operator or a mall gets the right eyes. <a href="{SPONSOR_URL}">Book the next issue</a>.</p></div>"""


def issue_sections(ctx):
    days, beyond, soon, later, fx, cams, stamp, folder_rel = (ctx[k] for k in ("days", "beyond", "soon", "later", "fx", "cams", "stamp", "folder_rel"))
    worst_txt, change_txt = headline(days, soon)
    first, last = days[0]["date"], days[-1]["date"]
    return f"""
<p class="kicker">Issue {ctx['number']} · {fmt_long(TODAY)}</p>
<h2 style="margin-top:6px">The fortnight ahead on the Causeway</h2>
<p class="oneline">{esc(worst_txt)}.</p>
<p class="oneline">{esc(change_txt)}.</p>

<h2>Jam forecast, {fmt(first)} to {fmt(last)}</h2>
<p class="small muted">Rule-based risk from the holiday calendars of both countries and both school calendars. High means plan around it or take the train. Medium means leave early. Low means a normal day.</p>
{render_forecast_table(days)}

<h2>The next big jams after that</h2>
{render_beyond(beyond)}

<h2>Rules that bite</h2>
{render_rules(soon, later)}

<h2>The ringgit this week</h2>
{render_fx(fx)}

<h2>The checkpoints at build time</h2>
{render_cams(cams, stamp, folder_rel)}

<h2 id="sponsor-slot">Sponsor</h2>
{render_sponsor()}
"""


def issue_markdown(ctx):
    days, beyond, soon, later, fx = (ctx[k] for k in ("days", "beyond", "soon", "later", "fx"))
    worst_txt, change_txt = headline(days, soon)
    lines = [f"# {SITE_NAME}, issue {ctx['number']}, {fmt_long(TODAY)}", "",
             f"**{worst_txt}.** **{change_txt}.**", "",
             f"Read online: {SITE_URL}/issues/{TODAY.isoformat()}", "",
             f"## Jam forecast, {fmt(days[0]['date'])} to {fmt(days[-1]['date'])}", "",
             "| Date | What is on | To JB | To SG |", "|---|---|---|---|"]
    for f in days:
        why = "; ".join(f["why"]) if f["why"] else "Normal working day"
        lines.append(f"| {fmt(f['date'])} | {why} | {f['to_jb']} | {f['to_sg']} |")
    lines += ["", "## The next big jams after that", ""]
    if beyond:
        for f in beyond[:8]:
            d = "both directions" if f["to_jb"] == HIGH and f["to_sg"] == HIGH else ("to JB" if f["to_jb"] == HIGH else "to SG")
            lines.append(f"- **{fmt(f['date'])}**, {d}: {'; '.join(f['why'])}")
    else:
        lines.append("No long-weekend jams in the next four months.")
    lines += ["", "## Rules that bite", ""]
    for r in soon + later:
        tag = "Already in force" if r["when"] <= TODAY else f"From {fmt_long(r['when'])}"
        lines.append(f"- **{tag}: {r['title']}.** {r['body']} [{r['source_name']}]({r['source']})")
    lines += ["", "Standing rules:", ""]
    for r in FACTS["standing_rules"]:
        lines.append(f"- **{r['title']}.** {r['body']} [{r['source_name']}]({r['source']})")
    lines += ["", "## The ringgit this week", "",
              f"S$1 = RM{fx['rate']:.4f} on {fx['date']}, {week_change(fx)}. {FX_RANGE_DAYS}-day range RM{fx['low']:.4f} to RM{fx['high']:.4f}.",
              "", "## The checkpoints at build time", ""]
    for c in ctx["cams"]:
        lines.append(f"![{c['name']}]({SITE_URL}/{ctx['folder_rel']}/{c['file']})")
        lines.append(f"*{c['name']}*")
        lines.append("")
    lines += ["## Sponsor", "", f"This slot is empty this week. One sponsor per issue, S${SPONSOR_PRICE_SGD}, two plain lines. {SPONSOR_URL}", "",
              "---", f"Built by a script, every Sunday 6 pm SGT. Sources: data.gov.sg, ICA, MOE, KPM, frankfurter.dev. Code: {REPO_URL}"]
    return "\n".join(lines)


# ---------- site assembly ----------

def load_manifest():
    path = ISSUES / "issues.json"
    return json.loads(path.read_text()) if path.exists() else []


def write_feed(manifest):
    items = []
    for m in manifest[:20]:
        items.append(f"""<item><title>{esc(m['title'])}</title><link>{SITE_URL}/issues/{m['date']}</link><guid>{SITE_URL}/issues/{m['date']}</guid>
<pubDate>{dt.datetime.fromisoformat(m['date']).strftime('%a, %d %b %Y 18:00:00 +0800')}</pubDate><description>{esc(m['summary'])}</description></item>""")
    (PUBLIC / "feed.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>{SITE_NAME}</title><link>{SITE_URL}</link><description>The fortnight ahead on the Johor to Singapore Causeway: jam forecast, rule changes, ringgit rate. Every Sunday.</description>
{''.join(items)}</channel></rss>""")


def write_index(manifest, latest_html, cam_count):
    rec = FACTS["records"]["single_day"]
    archive = "".join(f'<li><a href="/issues/{m["date"]}">{esc(m["title"])}</a><br><span class="small muted">{esc(m["summary"])}</span></li>' for m in manifest)
    body = f"""
<h1>The fortnight ahead on the Causeway, every Sunday evening.</h1>
<p class="lead">A 14-day jam forecast for Woodlands and Tuas, every rule change with the date it bites, and what the ringgit is doing. Built from government data by a script that runs on Sunday at 6 pm, so it never forgets a long weekend.</p>
<div class="stats">
<div class="stat"><b>{rec['count']:,}</b><span>crossings in one day, {dt.date.fromisoformat(rec['date']).strftime('%-d %b %Y')}, the ICA record</span></div>
<div class="stat"><b>2 countries</b><span>public holiday and school calendars, both sides, merged</span></div>
<div class="stat"><b>{cam_count} cameras</b><span>Woodlands and Tuas approaches, captured each issue</span></div>
</div>
<div class="box" id="subscribe">
<b>Get it by email every Sunday</b>
<p class="small muted" style="margin:4px 0 10px">One email a week, no tracking, unsubscribe by replying "stop".</p>
<form class="sub" id="subform"><input type="email" name="email" placeholder="you@example.com" required autocomplete="email"><button type="submit">Subscribe</button></form>
<p class="small" id="submsg" style="margin:8px 0 0">Or follow the <a href="/feed.xml">RSS feed</a> or <a href="{REPO_URL}">watch the GitHub repo</a> to get each issue as it lands.</p>
</div>

<section id="latest">
{latest_html}
</section>

<h2 id="archive">Archive</h2>
<ul class="archive">{archive}</ul>

<h2 id="method">How the forecast works</h2>
<p>Each day in the window gets a score for each direction. Friday evening and Saturday morning add one point towards Johor, Sunday evening adds one point back to Singapore. The eve of a public holiday on either side adds two points towards Johor, and the last day of a break adds two points back. The day before a school holiday starts, and the day it ends, add two points in the matching direction, and every day inside a school holiday adds one point both ways. Three points is High, two is Medium.</p>
<p>The calendars behind it: Singapore public holidays and Malaysian national plus Johor holidays from the public Google holiday calendars, MOE school terms for 2026 and 2027, and the KPM Group B calendar that Johor follows from 2026. The ICA record figures come from its media releases. All of it is in <a href="{REPO_URL}/blob/main/data/facts.json">one dated file</a>; if a date is wrong, fix it there and the next issue is right.</p>

<h2 id="sponsor">Sponsor an issue</h2>
{render_sponsor()}
<script>
(function(){{
  var f=document.getElementById('subform'),m=document.getElementById('submsg');
  f.addEventListener('submit',function(e){{
    e.preventDefault();
    var email=f.email.value.trim();
    m.textContent='Saving...';
    fetch('/api/subscribe',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{email:email}})}})
      .then(function(r){{return r.json().then(function(j){{return [r.status,j]}})}})
      .then(function(x){{
        var s=x[0],j=x[1];
        if(s===200){{m.textContent='Done. The next issue lands on Sunday evening.';f.reset();return}}
        if(s===503){{m.innerHTML='Email signup is not switched on yet. Use the <a href="/feed.xml">RSS feed</a> for now.';return}}
        m.textContent='That email did not go through. Check it and try again.';
      }}).catch(function(){{m.textContent='Could not reach the server. Try again in a minute.'}});
  }});
}})();
</script>
"""
    (PUBLIC / "index.html").write_text(page(f"{SITE_NAME}: the fortnight ahead on the JB to Singapore Causeway", body,
                                            "A 14-day jam forecast for Woodlands and Tuas, rule changes with dates, and the ringgit rate. Every Sunday, built from government data."))


def main():
    PUBLIC.mkdir(exist_ok=True)
    ISSUES.mkdir(exist_ok=True)
    manifest = load_manifest()
    manifest = [m for m in manifest if m["date"] != TODAY.isoformat()]
    number = len(manifest) + 1

    stamp, cams = fetch_cameras()
    folder_rel = f"issues/{TODAY.isoformat()}"
    save_camera_images(cams, PUBLIC / folder_rel)
    sg_ph, my_ph = fetch_holidays()
    fx = fetch_fx()
    days, beyond = forecast(sg_ph, my_ph)
    soon, later = rule_changes_in_play()

    ctx = dict(number=number, days=days, beyond=beyond, soon=soon, later=later, fx=fx, cams=cams, stamp=stamp, folder_rel=folder_rel)
    sections = issue_sections(ctx)
    worst_txt, change_txt = headline(days, soon)
    title = f"{SITE_NAME} {TODAY.isoformat()}: {worst_txt}"
    summary = f"{worst_txt}. {change_txt}."

    (ISSUES / f"{TODAY.isoformat()}.html").write_text(page(title, sections + f'<p><a href="/">All issues</a></p>', summary))
    manifest.insert(0, {"date": TODAY.isoformat(), "title": f"Issue {number}, {fmt_long(TODAY)}", "summary": summary})
    (ISSUES / "issues.json").write_text(json.dumps(manifest, indent=1))
    (PUBLIC / "latest.md").write_text(issue_markdown(ctx))
    (PUBLIC / "latest-title.txt").write_text(title + "\n")
    write_feed(manifest)
    write_index(manifest, sections, len(cams))
    print(f"built issue {number} for {TODAY}: {summary}")


if __name__ == "__main__":
    main()
