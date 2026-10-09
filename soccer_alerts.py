#!/usr/bin/env python3
"""
Women's soccer alerts: your teams and players, pushed to your phone via ntfy.

Data: ESPN's public scoreboard API (free, no key, no daily limit) for live alerts,
      plus API-Football's free plan for post-match player ratings (~2-6 requests
      per match, well under its 100/day limit).

Alerts:
  - Match day: kickoff time (Mountain Time) about 24h before, with a BIG MATCH flag
  - Lineups: whether your players start, about an hour before kickoff
  - Kickoff, every goal, red cards, cards for your players, and final score
  - Player ratings after full time (your players + player of the match)

Usage:  python soccer_alerts.py      (runs one check; GitHub runs it every 5 min)
Requires: pip install requests
"""

import json
import os
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
MY_TEAMS = {
    "London City Lionesses", "Chelsea", "Arsenal", "Barcelona", "England", "Spain",
}

# Last names are enough; accents are ignored when matching
MY_PLAYERS = {"putellas": "Alexia Putellas", "bronze": "Lucy Bronze",
              "batlle": "Ona Batlle"}

# ESPN competition codes to check. Codes ESPN doesn't recognize are skipped,
# and the run log lists which ones worked.
COMPETITIONS = {
    "eng.w.1": "WSL",
    "esp.w.1": "Liga F",
    "uefa.wchampions": "Women's Champions League",
    "eng.w.fa": "Women's FA Cup",
    "eng.w.league_cup": "Women's League Cup",
    "uefa.w.nations": "Women's Nations League",
    "fifa.wworldq.uefa": "Women's World Cup Qualifying",
    "fifa.wwc": "Women's World Cup",
    "uefa.weuro": "Women's Euro",
    "fifa.friendly.w": "Women's Friendly",
}

LOCAL_TZ = ZoneInfo("America/Denver")   # Mountain Time
MATCHDAY_HOURS = 24                     # "match tomorrow" heads-up window
LINEUP_MINUTES = 75                     # start checking lineups this long before kickoff

BASE = Path(__file__).parent
STATE_FILE = BASE / "soccer_sent.json"
API = "https://site.api.espn.com/apis/site/v2/sports/soccer"
HEADERS = {"User-Agent": "personal-soccer-alerts/1.0"}

APIFOOTBALL = "https://v3.football.api-sports.io"
RATING_TRIES_AFTER_MIN = [15, 45, 90]   # minutes after full time to look for ratings


# ----------------------------------------------------------------------------
# Notifications (ntfy)
# ----------------------------------------------------------------------------
def notify(title: str, message: str, url: str = "", tags: str = "soccer") -> None:
    print(f"\n🔔 {title}\n{message}\n")
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC not set; alert only printed to the log.")
        return
    headers = {"Title": title.encode("ascii", "ignore").decode().strip(), "Tags": tags}
    if url:
        headers["Click"] = url
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                      headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"Notification failed: {e}")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def plain(text: str) -> str:
    """Lowercase and strip accents so 'Batllé' matches 'batlle'."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def my_player(name: str):
    p = plain(name)
    for key, full in MY_PLAYERS.items():
        if key in p:
            return full
    return None


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    cutoff = time.time() - 10 * 86400  # forget alerts older than 10 days
    STATE_FILE.write_text(json.dumps({k: v for k, v in state.items() if v > cutoff}))


def get(url: str, params=None):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def team_names(comp: dict):
    return [c["team"].get("displayName", "") for c in comp.get("competitors", [])]


def match_link(event: dict) -> str:
    for link in event.get("links", []):
        if link.get("href"):
            return link["href"]
    return f"https://www.espn.com/soccer/match/_/gameId/{event['id']}"


def score_line(comp: dict) -> str:
    home = next((c for c in comp["competitors"] if c.get("homeAway") == "home"),
                comp["competitors"][0])
    away = next((c for c in comp["competitors"] if c is not home))
    return (f"{home['team']['displayName']} {home.get('score', '0')} - "
            f"{away.get('score', '0')} {away['team']['displayName']}")


# ----------------------------------------------------------------------------
# Alert logic
# ----------------------------------------------------------------------------
def handle_event(event: dict, code: str, comp_name: str, state: dict) -> None:
    comp = event["competitions"][0]
    teams = team_names(comp)
    mine = [t for t in teams if t in MY_TEAMS]
    if not mine:
        return

    eid = event["id"]
    status = event["status"]["type"]
    state_name = status.get("state")              # "pre", "in", "post"
    kickoff = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    link = match_link(event)
    title_teams = " vs ".join(teams)

    def once(key: str) -> bool:
        k = f"{eid}:{key}"
        if k in state:
            return False
        state[k] = time.time()
        return True

    # 1. Match-day heads-up
    if state_name == "pre" and kickoff - now <= timedelta(hours=MATCHDAY_HOURS):
        if once("matchday"):
            big = len(mine) == 2 or set(teams) == {"England", "Spain"}
            local = kickoff.astimezone(LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p MT")
            title = ("BIG MATCH: " if big else "Upcoming: ") + title_teams
            notify(title, f"{comp_name}\nKickoff {local}", link,
                   "fire,soccer" if big else "soccer")

    # 2. Lineups
    if state_name == "pre" and kickoff - now <= timedelta(minutes=LINEUP_MINUTES):
        if f"{eid}:lineup" not in state:
            check_lineups(event, code, comp_name, title_teams, link, state)

    # 3. Kickoff
    if state_name in ("in", "post") and once("kickoff"):
        if state_name == "in":
            notify(f"Kickoff: {title_teams}", comp_name, link, "stopwatch")

    # 4. Goals and cards
    for d in comp.get("details", []):
        kind = (d.get("type") or {}).get("text", "")
        minute = (d.get("clock") or {}).get("displayValue", "")
        names = [a.get("displayName", "") for a in d.get("athletesInvolved", [])]
        who = ", ".join(names) or "Unknown"
        key = f"{kind}:{minute}:{who}"

        if d.get("scoringPlay"):
            if once(f"goal:{key}"):
                star = next((my_player(n) for n in names if my_player(n)), None)
                extra = " (own goal)" if d.get("ownGoal") else (
                    " (pen)" if d.get("penaltyKick") else "")
                title = f"GOAL by {star}!" if star and not d.get("ownGoal") else "Goal"
                notify(title, f"{who}{extra} {minute}\n{score_line(comp)}", link,
                       "star,soccer" if star else "soccer")
        elif d.get("redCard"):
            if once(f"red:{key}"):
                notify("Red card", f"{who} {minute}\n{score_line(comp)}", link,
                       "red_circle")
        elif d.get("yellowCard"):
            star = next((my_player(n) for n in names if my_player(n)), None)
            if star and once(f"yellow:{key}"):
                notify(f"Yellow card: {star}", f"{minute}\n{score_line(comp)}", link,
                       "yellow_circle")

    # 5. Full time
    if state_name == "post" and status.get("completed"):
        if once("final"):
            notify(f"Full time: {comp_name}", score_line(comp), link, "checkered_flag")
        maybe_send_ratings(event, comp, teams, state)


def check_lineups(event, code, comp_name, title_teams, link, state) -> None:
    try:
        summary = get(f"{API}/{code}/summary", {"event": event["id"]})
    except requests.RequestException as e:
        print(f"Lineup check failed: {e}")
        return

    rosters = summary.get("rosters") or []
    if not any(r.get("roster") for r in rosters):
        return  # not announced yet; try again next run

    lines = []
    for r in rosters:
        team = (r.get("team") or {}).get("displayName", "")
        for p in r.get("roster", []):
            star = my_player((p.get("athlete") or {}).get("displayName", ""))
            if star:
                lines.append(f"{star} ({team}): "
                             f"{'STARTING' if p.get('starter') else 'on the bench'}")
    found = {l.split(" (")[0] for l in lines}
    for team_roster in rosters:
        team = (team_roster.get("team") or {}).get("displayName", "")
        if team in MY_TEAMS:
            for key, full in MY_PLAYERS.items():
                if full not in found and plays_for(full, team):
                    lines.append(f"{full} ({team}): not in the squad")

    state[f"{event['id']}:lineup"] = time.time()
    if lines:
        notify(f"Lineups: {title_teams}", "\n".join(lines), link, "clipboard")


# ----------------------------------------------------------------------------
# Player ratings (API-Football)
# ----------------------------------------------------------------------------
def team_key(name: str) -> str:
    """'Chelsea W' / 'Chelsea Women' / 'Chelsea' -> 'chelsea'."""
    n = plain(name).replace(" women", "").strip()
    return n[:-2].strip() if n.endswith(" w") else n


def af_get(path: str, params: dict):
    key = os.environ.get("APIFOOTBALL_KEY")
    resp = requests.get(f"{APIFOOTBALL}/{path}", params=params,
                        headers={"x-apisports-key": key}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise requests.RequestException(str(data["errors"]))
    return data.get("response", [])


def maybe_send_ratings(event: dict, comp: dict, teams: list, state: dict) -> None:
    eid = event["id"]
    if not os.environ.get("APIFOOTBALL_KEY") or f"{eid}:ratings" in state:
        return
    tries = sum(1 for k in state if k.startswith(f"{eid}:rtry:"))
    if tries >= len(RATING_TRIES_AFTER_MIN):
        return
    first_seen = state.get(f"{eid}:final", time.time())
    if time.time() - first_seen < RATING_TRIES_AFTER_MIN[tries] * 60:
        return
    state[f"{eid}:rtry:{tries}"] = time.time()

    try:
        # Find the same match in API-Football by date + team names
        day = event["date"][:10]
        wanted = {team_key(t) for t in teams}
        fixture_id = None
        for f in af_get("fixtures", {"date": day}):
            names = {team_key(f["teams"]["home"]["name"]),
                     team_key(f["teams"]["away"]["name"])}
            if names == wanted:
                fixture_id = f["fixture"]["id"]
                break
        if not fixture_id:
            print(f"Ratings: couldn't find {' vs '.join(teams)} in API-Football")
            return

        rated = []  # (rating, name, team)
        for team in af_get("fixtures/players", {"fixture": fixture_id}):
            tname = team["team"]["name"]
            for p in team.get("players", []):
                stats = (p.get("statistics") or [{}])[0]
                r = (stats.get("games") or {}).get("rating")
                if r:
                    rated.append((float(r), p["player"]["name"], tname))
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"Ratings lookup failed: {e}")
        return

    if not rated:
        print("Ratings not available yet for this match")
        return

    state[f"{eid}:ratings"] = time.time()
    rated.sort(reverse=True)
    lines = []
    for r, name, tname in rated:
        star = my_player(name)
        if star:
            lines.append(f"{star}: {r:.1f}")
    best = rated[0]
    lines.append(f"Top rated: {best[1]} ({team_key(best[2]).title()}) {best[0]:.1f}")
    notify(f"Ratings: {' vs '.join(teams)}", "\n".join(lines),
           match_link(event), "bar_chart")


# Which of your teams each player is expected on (for "not in squad" alerts)
PLAYER_TEAMS = {
    "Alexia Putellas": {"London City Lionesses", "Spain"},
    "Lucy Bronze": {"Chelsea", "England"},
    "Ona Batlle": {"Arsenal", "Spain"},
}


def plays_for(player: str, team: str) -> bool:
    return team in PLAYER_TEAMS.get(player, set())


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def fetch_scoreboard(code: str, today: datetime):
    """Try a date range first, then single days, then the default scoreboard.
    Returns (events, None) on success or (None, error_text) on failure."""
    days = [today + timedelta(days=d) for d in (-1, 0, 1, 2)]
    attempts = [{"dates": f"{days[0]:%Y%m%d}-{days[-1]:%Y%m%d}"}, None]
    last_error = ""
    for params in attempts:
        try:
            return get(f"{API}/{code}/scoreboard", params).get("events", []), None
        except requests.HTTPError as e:
            body = (e.response.text or "")[:120].replace("\n", " ")
            last_error = f"HTTP {e.response.status_code}: {body}"
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"[:160]
    # Last resort: one day at a time
    events, ok = [], False
    for d in days:
        try:
            events += get(f"{API}/{code}/scoreboard",
                          {"dates": f"{d:%Y%m%d}"}).get("events", [])
            ok = True
        except requests.RequestException:
            pass
    return (events, None) if ok else (None, last_error)


def main() -> None:
    state = load_state()
    today = datetime.now(timezone.utc)
    working, skipped = [], []

    for code, name in COMPETITIONS.items():
        events, error = fetch_scoreboard(code, today)
        if events is None:
            skipped.append(f"{code} ({error})")
            continue
        working.append(f"{code} [{len(events)} matches]")
        for event in events:
            try:
                handle_event(event, code, name, state)
            except (KeyError, IndexError, ValueError) as e:
                print(f"Skipped an event in {code}: {e}")

    save_state(state)
    print(f"Checked: {', '.join(working) or 'none'}")
    for item in skipped:
        print(f"Not available from ESPN: {item}")


if __name__ == "__main__":
    main()
