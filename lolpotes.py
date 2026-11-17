#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LoL Potes : classement entre potes + revues d'après-partie
-----------------------------------------------------------
Utilisation :
  python lolpotes.py          -> récupère les parties et crée rapport.html
  python lolpotes.py --demo   -> crée un rapport d'exemple (sans clé API)

Réglages dans config.json (créé au premier lancement).
Aucune installation nécessaire : Python 3.8+ suffit.
"""
import json, os, sys, time, random, math, webbrowser, datetime, collections
import urllib.request, urllib.parse, urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "rapport.html")

DEFAULT_CONFIG = {
    "cle_api": "RGAPI-colle-ta-cle-ici",
    "serveur": "euw1",
    "region": "europe",
    "joueurs": ["TonPseudo#EUW", "Pote1#EUW", "Pote2#1234"],
    "mois": 3,
    "max_parties_par_joueur": 300,
    "revues_max": 150,
    "min_parties_classement": 3,
}
QUEUES = {420: "solo", 440: "flex"}
QUEUE_NAMES = {420: "Ranked solo/duo", 440: "Ranked flex"}


# --------------------------------------------------------------------------- utilitaires
def say(*a):
    print(*a, flush=True)


def load_config():
    if not os.path.exists(CONFIG):
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        say("config.json créé. Ouvre-le, mets ta clé API et les Riot ID de tes potes, puis relance.")
        return None
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


class RiotError(Exception):
    pass


class Riot:
    """Client minimal de l'API Riot avec respect des limites (clé de dev : 20 req/s, 100 req/2 min) et cache disque."""

    def __init__(self, key, platform, region):
        self.key, self.platform, self.region = key.strip(), platform, region
        self.calls = collections.deque()
        os.makedirs(CACHE, exist_ok=True)

    def _wait(self):
        while True:
            now = time.time()
            while self.calls and now - self.calls[0] > 120:
                self.calls.popleft()
            last1 = sum(1 for t in self.calls if now - t < 1)
            if len(self.calls) < 95 and last1 < 18:
                self.calls.append(now)
                return
            wait = 1.05 if last1 >= 18 else 120 - (now - self.calls[0]) + 0.2
            if wait > 3:
                say(f"   … limite de requêtes Riot atteinte, pause de {int(wait)} s")
            time.sleep(max(0.05, wait))

    def get(self, host, path, params=None, cache_name=None):
        if cache_name:
            fp = os.path.join(CACHE, cache_name + ".json")
            if os.path.exists(fp):
                with open(fp, encoding="utf-8") as f:
                    return json.load(f)
        url = f"https://{host}.api.riotgames.com{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(6):
            self._wait()
            req = urllib.request.Request(url, headers={"X-Riot-Token": self.key, "User-Agent": "lolpotes/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", "10"))
                    say(f"   … Riot demande d'attendre {wait} s")
                    time.sleep(wait + 0.5)
                    continue
                if e.code == 404:
                    return None
                if e.code in (401, 403):
                    raise RiotError("Clé API refusée (403). Elle est sûrement expirée : les clés de développement durent 24 h. "
                                    "Génère-en une nouvelle sur developer.riotgames.com et colle-la dans config.json.")
                if e.code >= 500 and attempt < 5:
                    time.sleep(2 + attempt * 2)
                    continue
                raise RiotError(f"Erreur Riot {e.code} sur {path}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 5:
                    time.sleep(2 + attempt * 2)
                    continue
                raise RiotError(f"Connexion impossible à l'API Riot ({e})")
        else:
            raise RiotError("Trop de tentatives, réessaie plus tard.")
        if cache_name:
            with open(os.path.join(CACHE, cache_name + ".json"), "w", encoding="utf-8") as f:
                json.dump(data, f)
        return data

    # endpoints
    def account(self, name, tag):
        return self.get(self.region, f"/riot/account/v1/accounts/by-riot-id/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}")

    def leagues(self, puuid):
        return self.get(self.platform, f"/lol/league/v4/entries/by-puuid/{puuid}") or []

    def match_ids(self, puuid, queue, start_time, count):
        ids, start = [], 0
        while len(ids) < count:
            n = min(100, count - len(ids))
            page = self.get(self.region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                            {"queue": queue, "startTime": start_time, "start": start, "count": n}) or []
            ids += page
            if len(page) < n:
                break
            start += n
        return ids

    def match(self, mid):
        return self.get(self.region, f"/lol/match/v5/matches/{mid}", cache_name="m_" + mid)

    def timeline(self, mid):
        return self.get(self.region, f"/lol/match/v5/matches/{mid}/timeline", cache_name="t_" + mid)


def month_start(nb_months):
    """Timestamp du 1er jour (00:00) du mois situé nb_months-1 mois avant le mois en cours."""
    d = datetime.date.today().replace(day=1)
    for _ in range(max(1, int(nb_months)) - 1):
        d = (d - datetime.timedelta(days=1)).replace(day=1)
    return int(time.mktime(d.timetuple()))


TIER_ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"]
DIV_ORDER = {"IV": 0, "III": 1, "II": 2, "I": 3}


def lp_score(r):
    """Rang converti en nombre : 400 par palier, 100 par division, + LP (Maître et plus : à partir de 2800)."""
    if not r:
        return None
    if r["tier"] in TIER_ORDER:
        return TIER_ORDER.index(r["tier"]) * 400 + DIV_ORDER.get(r.get("div"), 0) * 100 + r.get("lp", 0)
    return 2800 + r.get("lp", 0)


def save_lp_history(players):
    fp = os.path.join(CACHE, "historique_lp.json")
    hist = json.load(open(fp, encoding="utf-8")) if os.path.exists(fp) else {}
    now = int(time.time() * 1000)
    for p in players:
        snap = [now, lp_score(p["ranks"].get("solo")), lp_score(p["ranks"].get("flex"))]
        h = hist.setdefault(p["puuid"], [])
        if not h or h[-1][1:] != snap[1:]:
            h.append(snap)
        p["lp"] = h
    json.dump(hist, open(fp, "w", encoding="utf-8"))


def month_key(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m")


def ddragon_version():
    try:
        with urllib.request.urlopen("https://ddragon.leagueoflegends.com/api/versions.json", timeout=10) as r:
            return json.loads(r.read().decode())[0]
    except Exception:
        return None


# --------------------------------------------------------------------------- note de partie
def game_score(p, team_kills, team_dmg, minutes):
    """Note maison sur 10 : KDA, participation aux kills, part des dégâts, farm ou vision, victoire."""
    k, d, a = p["kills"], p["deaths"], p["assists"]
    kda = (k + a) / max(1, d)
    kp = (k + a) / max(1, team_kills)
    share = p["totalDamageDealtToChampions"] / max(1, team_dmg)
    cs = (p["totalMinionsKilled"] + p["neutralMinionsKilled"]) / max(1, minutes)
    vis = p["visionScore"] / max(1, minutes)
    support = p.get("teamPosition") == "UTILITY"
    s = 2.5 * min(kda, 6) / 6 + 2.5 * min(kp, 0.8) / 0.8 + 2.0 * min(share, 0.32) / 0.32
    s += 1.5 * (min(vis, 2.5) / 2.5 if support else min(cs, 8.5) / 8.5)
    s += 0.5 * min(vis, 1.6) / 1.6 + (1.0 if p["win"] else 0)
    return round(min(10, s), 2)


# --------------------------------------------------------------------------- analyse d'une partie (revue)
MONSTERS = {"DRAGON": "Dragon", "BARON_NASHOR": "Baron", "RIFTHERALD": "Héraut", "HORDE": "Larves du néant",
            "ATAKHAN": "Atakhan", "ELDER_DRAGON": "Dragon ancestral"}
DRAKE_TYPES = {"FIRE_DRAGON": "infernal", "WATER_DRAGON": "océan", "EARTH_DRAGON": "montagne", "AIR_DRAGON": "nuages",
               "HEXTECH_DRAGON": "hextech", "CHEMTECH_DRAGON": "chemtech", "ELDER_DRAGON": "ancestral"}


def review(match, tl, friend_puuids):
    info = match["info"]
    parts = info["participants"]
    by_pid = {p["participantId"]: p for p in parts}
    frames = tl["info"]["frames"] if tl else []
    # or d'équipe minute par minute (bleu - rouge)
    gold = []
    for fr in frames:
        pf = fr.get("participantFrames", {})
        t100 = sum(v.get("totalGold", 0) for k, v in pf.items() if by_pid.get(int(k), {}).get("teamId") == 100)
        t200 = sum(v.get("totalGold", 0) for k, v in pf.items() if by_pid.get(int(k), {}).get("teamId") == 200)
        gold.append(t100 - t200)

    def frame_at(minute, pid):
        if not frames:
            return None
        fr = frames[min(minute, len(frames) - 1)]
        return fr.get("participantFrames", {}).get(str(pid))

    events = []
    for fr in frames:
        for e in fr.get("events", []):
            ty, ts = e.get("type"), e.get("timestamp", 0) / 60000
            if ty == "CHAMPION_KILL":
                pos = e.get("position") or {}
                events.append({"t": ts, "k": "kill", "killer": e.get("killerId", 0), "victim": e.get("victimId", 0),
                               "x": pos.get("x"), "y": pos.get("y"),
                               "assists": e.get("assistingParticipantIds", []),
                               "team": by_pid.get(e.get("killerId"), {}).get("teamId") or (300 - by_pid.get(e.get("victimId"), {}).get("teamId", 100))})
            elif ty == "ELITE_MONSTER_KILL":
                name = MONSTERS.get(e.get("monsterType"), e.get("monsterType", "Objectif"))
                if e.get("monsterType") == "DRAGON" and e.get("monsterSubType"):
                    name += " " + DRAKE_TYPES.get(e["monsterSubType"], "").strip()
                events.append({"t": ts, "k": "obj", "name": name.strip(), "team": e.get("killerTeamId")})
            elif ty == "BUILDING_KILL":
                bt = "Inhibiteur" if e.get("buildingType") == "INHIBITOR_BUILDING" else "Tour"
                lane = {"TOP_LANE": "top", "MID_LANE": "mid", "BOT_LANE": "bot"}.get(e.get("laneType"), "")
                events.append({"t": ts, "k": "bld", "name": f"{bt} {lane}".strip(), "team": 300 - e.get("teamId", 100)})
    # tournant : plus grosse variation d'or sur 3 minutes
    turn = None
    for i in range(3, len(gold)):
        sw = gold[i] - gold[i - 3]
        if turn is None or abs(sw) > abs(turn["swing"]):
            turn = {"from": i - 3, "to": i, "swing": sw}

    friends = []
    for p in parts:
        if p["puuid"] not in friend_puuids:
            continue
        pid, team, pos = p["participantId"], p["teamId"], p.get("teamPosition") or ""
        opp = next((o for o in parts if o["teamId"] != team and pos and o.get("teamPosition") == pos), None)
        f10, f15 = frame_at(10, pid), frame_at(15, pid)
        o10 = frame_at(10, opp["participantId"]) if opp else None
        o15 = frame_at(15, opp["participantId"]) if opp else None
        cs = lambda f: (f.get("minionsKilled", 0) + f.get("jungleMinionsKilled", 0)) if f else None
        deaths = [round(e["t"], 1) for e in events if e["k"] == "kill" and e["victim"] == pid]
        tips = []
        early = [d for d in deaths if d < 14]
        if len(early) >= 3:
            tips.append(f"{len(early)} morts avant 14 min : la lane / le début de partie a coûté cher.")
        if f10 and pos not in ("UTILITY", "JUNGLE") and cs(f10) is not None and cs(f10) < 60:
            tips.append(f"{cs(f10)} CS à 10 min (objectif : 70+). Il y a de l'or à récupérer au farm.")
        if pos == "JUNGLE" and f10 and cs(f10) < 50:
            tips.append(f"{cs(f10)} camps/CS à 10 min en jungle : farm un peu lent.")
        if f15 and o15 and opp:
            gd = f15.get("totalGold", 0) - o15.get("totalGold", 0)
            if gd <= -1000:
                tips.append(f"{gd} d'or contre {opp['championName']} à 15 min : lane perdue.")
            elif gd >= 1000:
                tips.append(f"+{gd} d'or contre {opp['championName']} à 15 min : lane gagnée, bien joué.")
        minutes = info["gameDuration"] / 60
        vpm = p["visionScore"] / max(1, minutes)
        if pos != "UTILITY" and vpm < 0.6:
            tips.append(f"Score de vision faible ({vpm:.1f}/min) : pense aux balises et à la lentille.")
        if pos == "UTILITY" and vpm < 1.6:
            tips.append(f"Vision {vpm:.1f}/min pour un support : on peut viser 2+.")
        if not deaths:
            tips.append("Aucune mort : propre.")
        friends.append({
            "puuid": p["puuid"], "pid": pid, "team": team, "pos": pos,
            "cs10": cs(f10), "csOpp10": cs(o10), "gd10": (f10.get("totalGold", 0) - o10.get("totalGold", 0)) if f10 and o10 else None,
            "gd15": (f15.get("totalGold", 0) - o15.get("totalGold", 0)) if f15 and o15 else None,
            "opp": opp["championName"] if opp else None, "deaths": deaths, "tips": tips,
        })
    return {"gold": gold, "events": events, "turn": turn, "friends": friends}


# --------------------------------------------------------------------------- statistiques
def is_remake(info):
    return info.get("gameDuration", 0) < 300 or any(p.get("gameEndedInEarlySurrender") for p in info["participants"])


def build_report(players, matches, timelines, ddv, months, min_games, demo=False):
    """Prépare les données brutes par partie ; les classements sont calculés dans la page (filtre mois / file)."""
    puuids = {p["puuid"]: p for p in players}
    games = []
    for mid, m in matches.items():
        info = m["info"]
        q = QUEUES.get(info.get("queueId"))
        if not q or is_remake(info):
            continue
        minutes = info["gameDuration"] / 60
        team_k = collections.Counter(); team_d = collections.Counter()
        for p in info["participants"]:
            team_k[p["teamId"]] += p["kills"]; team_d[p["teamId"]] += p["totalDamageDealtToChampions"]
        tl = timelines.get(mid)
        rv = review(m, tl, puuids) if tl else None
        parts = []
        for p in info["participants"]:
            fr = p["puuid"] in puuids
            ch = p.get("challenges") or {}
            row = {"puuid": p["puuid"] if fr else None, "pid": p["participantId"],
                   "name": (p.get("riotIdGameName") or p.get("summonerName") or "?"), "champ": p["championName"],
                   "team": p["teamId"], "win": p["win"], "pos": p.get("teamPosition") or "",
                   "k": p["kills"], "d": p["deaths"], "a": p["assists"], "cs": p["totalMinionsKilled"] + p["neutralMinionsKilled"],
                   "gold": p["goldEarned"], "dmg": p["totalDamageDealtToChampions"], "vis": p["visionScore"],
                   "items": [p.get(f"item{i}", 0) for i in range(7)], "lvl": p.get("champLevel", 0),
                   "score": game_score(p, team_k[p["teamId"]], team_d[p["teamId"]], minutes), "friend": fr}
            if fr:
                row.update({"kp": (p["kills"] + p["assists"]) / max(1, team_k[p["teamId"]]),
                            "fb": 1 if p.get("firstBloodKill") else 0, "penta": p.get("pentaKills", 0), "quadra": p.get("quadraKills", 0),
                            "triple": p.get("tripleKills", 0), "dragons": p.get("dragonKills", 0), "barons": p.get("baronKills", 0),
                            "towers": p.get("turretTakedowns", p.get("turretKills", 0)), "solo": ch.get("soloKills", 0),
                            "dodged": ch.get("skillshotsDodged", 0), "dead": p.get("totalTimeSpentDead", 0),
                            "steals": p.get("objectivesStolen", 0), "ally": p.get("totalHealsOnTeammates", 0) + p.get("totalDamageShieldedOnTeammates", 0),
                            "taken": p.get("totalDamageTaken", 0), "wards": p.get("wardsPlaced", 0)})
            parts.append(row)
        ext = None
        if rv and rv["gold"]:
            ext = {"max": max(rv["gold"]), "min": min(rv["gold"])}
        games.append({
            "id": mid, "q": q, "date": info["gameCreation"], "month": month_key(info["gameCreation"]), "dur": info["gameDuration"],
            "version": ".".join(info.get("gameVersion", "").split(".")[:2]), "parts": parts,
            "teams": [{"id": t["teamId"], "win": t["win"], "obj": {k: v.get("kills", 0) for k, v in t.get("objectives", {}).items()}}
                      for t in info.get("teams", [])],
            "review": rv, "ext": ext,
        })
    games.sort(key=lambda g: -g["date"])
    return {
        "generated": int(time.time() * 1000), "months": months, "minGames": min_games, "ddv": ddv, "demo": demo,
        "players": [{"puuid": p["puuid"], "name": p["name"], "tag": p["tag"], "ranks": p.get("ranks", {}), "icon": p.get("icon"),
                     "lp": p.get("lp", [])} for p in players],
        "games": games,
    }


# --------------------------------------------------------------------------- récupération réelle
def fetch_all(cfg):
    key = cfg["cle_api"]
    if not key or "colle" in key:
        raise RiotError("Mets ta clé API dans config.json (champ \"cle_api\").")
    api = Riot(key, cfg["serveur"], cfg["region"])
    start = month_start(cfg["mois"])
    players = []
    acc_cache = os.path.join(CACHE, "comptes.json")
    accs = json.load(open(acc_cache, encoding="utf-8")) if os.path.exists(acc_cache) else {}
    for rid in cfg["joueurs"]:
        if "#" not in rid:
            say(f"⚠ « {rid} » ignoré : il faut le Riot ID complet, ex. Pseudo#EUW")
            continue
        name, tag = rid.rsplit("#", 1)
        if rid.lower() not in accs:
            a = api.account(name, tag)
            if not a:
                say(f"⚠ Compte introuvable : {rid}")
                continue
            accs[rid.lower()] = a["puuid"]
        players.append({"name": name, "tag": tag, "puuid": accs[rid.lower()]})
    json.dump(accs, open(acc_cache, "w", encoding="utf-8"))
    if not players:
        raise RiotError("Aucun joueur valide dans config.json.")
    for p in players:
        say(f"• {p['name']}#{p['tag']}")
        ranks = {}
        for e in api.leagues(p["puuid"]):
            qn = {"RANKED_SOLO_5x5": "solo", "RANKED_FLEX_SR": "flex"}.get(e.get("queueType"))
            if qn:
                ranks[qn] = {"tier": e["tier"], "div": e.get("rank", ""), "lp": e.get("leaguePoints", 0),
                             "w": e.get("wins", 0), "l": e.get("losses", 0)}
        p["ranks"] = ranks
        ids = set()
        for qid in QUEUES:
            ids.update(api.match_ids(p["puuid"], qid, start, cfg["max_parties_par_joueur"]))
        p["ids"] = ids
    all_ids = sorted(set().union(*[p["ids"] for p in players]), reverse=True)
    say(f"{len(all_ids)} parties à lire (déjà en cache : {sum(os.path.exists(os.path.join(CACHE, 'm_' + i + '.json')) for i in all_ids)})")
    matches, timelines = {}, {}
    for n, mid in enumerate(all_ids, 1):
        m = api.match(mid)
        if m:
            matches[mid] = m
        if n % 10 == 0:
            say(f"   parties : {n}/{len(all_ids)}")
    for n, mid in enumerate(all_ids[: cfg["revues_max"]], 1):
        t = api.timeline(mid)
        if t:
            timelines[mid] = t
        if n % 10 == 0:
            say(f"   revues : {n}/{min(len(all_ids), cfg['revues_max'])}")
    save_lp_history(players)
    for p in players:  # icône d'invocateur depuis la dernière partie
        for m in matches.values():
            me = next((x for x in m["info"]["participants"] if x["puuid"] == p["puuid"]), None)
            if me:
                p["icon"] = me.get("profileIcon"); break
    return players, matches, timelines


# --------------------------------------------------------------------------- mode démo (données inventées au format Riot)
DEMO_CHAMPS = ["Darius", "Garen", "Ahri", "LeeSin", "Jinx", "Thresh", "Yasuo", "Lux", "Vi", "Caitlyn", "Nautilus", "Sett",
               "Viego", "Orianna", "Ezreal", "Leona", "Fiora", "KaiSa", "Sylas", "Nasus"]
POS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]


def demo_data(cfg):
    rnd = random.Random(7)
    names = ["Toi", "Kevin", "Sarah", "Mehdi", "Lucas"]
    players = [{"name": n, "tag": "EUW", "puuid": "demo-" + n, "icon": 29 + i,
                "ranks": {"solo": {"tier": t, "div": d, "lp": rnd.randint(0, 99), "w": 40, "l": 38},
                          "flex": {"tier": t2, "div": "II", "lp": rnd.randint(0, 99), "w": 12, "l": 10}}}
               for i, (n, t, d, t2) in enumerate(zip(names, ["GOLD", "PLATINUM", "SILVER", "GOLD", "EMERALD"],
                                                     ["II", "IV", "I", "III", "IV"], ["SILVER", "GOLD", "GOLD", "PLATINUM", "GOLD"]))]
    skill = {p["puuid"]: 0.5 + i * 0.12 for i, p in enumerate(players)}
    matches, timelines = {}, {}
    now = int(time.time() * 1000)
    for g in range(84):
        qid = 420 if g % 3 else 440
        mid = f"EUW1_{7000000000 + g}"
        group = rnd.sample(players, 1 if qid == 420 and rnd.random() < .6 else (2 if qid == 420 else rnd.randint(3, 5)))
        dur = rnd.randint(1250, 2250)
        win100 = rnd.random() < 0.5 + 0.08 * (sum(skill[p["puuid"]] for p in group) / len(group) - 0.7)
        parts = []
        for i in range(10):
            team = 100 if i < 5 else 200
            pos = POS[i % 5]
            f = group[i] if i < len(group) else None
            sk = skill[f["puuid"]] if f else rnd.uniform(.4, .9)
            win = (team == 100) == win100
            k = max(0, int(rnd.gauss(5 + 4 * sk + (2 if win else -1), 3))) if pos != "UTILITY" else rnd.randint(0, 3)
            d = max(0, int(rnd.gauss(6 - 3 * sk + (-1 if win else 1.5), 2)))
            a = max(0, int(rnd.gauss(8 + (4 if pos == "UTILITY" else 0) + (2 if win else 0), 3)))
            cs = int(dur / 60 * (rnd.uniform(5.5, 8.5) if pos not in ("UTILITY",) else rnd.uniform(.8, 1.6)) * (0.8 + sk * .3))
            parts.append({"participantId": i + 1, "puuid": f["puuid"] if f else f"x{g}-{i}", "riotIdGameName": f["name"] if f else f"Joueur{rnd.randint(100, 999)}",
                          "championName": rnd.choice(DEMO_CHAMPS), "teamId": team, "win": win, "teamPosition": pos,
                          "kills": k, "deaths": d, "assists": a, "totalMinionsKilled": int(cs * .85), "neutralMinionsKilled": int(cs * .15),
                          "goldEarned": int(dur / 60 * rnd.uniform(330, 470)), "totalDamageDealtToChampions": int(dur / 60 * rnd.uniform(500, 1100) * (0.6 if pos == "UTILITY" else 1)),
                          "visionScore": int(dur / 60 * (rnd.uniform(1.6, 2.8) if pos == "UTILITY" else rnd.uniform(.4, 1.2))),
                          "champLevel": rnd.randint(13, 18), "pentaKills": 1 if rnd.random() < .02 else 0, "quadraKills": 1 if rnd.random() < .05 else 0,
                          "firstBloodKill": False, "profileIcon": f["icon"] if f else 1,
                          **{f"item{j}": rnd.choice([3078, 3053, 3742, 3075, 6692, 3071, 3031, 3089, 3157, 3020, 3047, 3111, 6672, 3036]) for j in range(6)}, "item6": 3340})
        parts[rnd.randrange(10)]["firstBloodKill"] = True
        teams = [{"teamId": t, "win": (t == 100) == win100, "objectives": {"dragon": {"kills": rnd.randint(0, 4)}, "baron": {"kills": rnd.randint(0, 1)},
                                                                            "tower": {"kills": rnd.randint(2, 10)}}} for t in (100, 200)]
        for pp in parts:
            pp.update({"tripleKills": 1 if rnd.random() < .12 else 0, "dragonKills": rnd.randint(0, 2) if pp["teamPosition"] == "JUNGLE" else (1 if rnd.random() < .1 else 0),
                       "baronKills": 1 if rnd.random() < .08 else 0, "turretTakedowns": rnd.randint(0, 5), "totalTimeSpentDead": pp["deaths"] * rnd.randint(18, 45),
                       "objectivesStolen": 1 if rnd.random() < .03 else 0, "totalHealsOnTeammates": rnd.randint(0, 6000) if pp["teamPosition"] == "UTILITY" else rnd.randint(0, 600),
                       "totalDamageShieldedOnTeammates": rnd.randint(0, 5000) if pp["teamPosition"] == "UTILITY" else 0,
                       "totalDamageTaken": int(dur / 60 * rnd.uniform(700, 1500)), "wardsPlaced": rnd.randint(5, 40),
                       "challenges": {"soloKills": rnd.randint(0, 4), "skillshotsDodged": rnd.randint(5, 90)}})
        matches[mid] = {"metadata": {"matchId": mid}, "info": {"gameCreation": now - g * 26 * 3600 * 1000, "gameDuration": dur, "queueId": qid,
                                                               "gameVersion": "25.22.1", "participants": parts, "teams": teams}}
        # timeline
        frames, lead = [], 0
        tot = {i + 1: 500 for i in range(10)}
        csf = {i + 1: 0 for i in range(10)}
        for minute in range(dur // 60 + 1):
            ev = []
            lead += rnd.gauss(80 if win100 else -80, 450)
            for pid in tot:
                tot[pid] += rnd.randint(280, 420) + (lead / 50 if pid <= 5 else -lead / 50)
                csf[pid] += rnd.randint(4, 9) if parts[pid - 1]["teamPosition"] != "UTILITY" else rnd.randint(0, 2)
            for _ in range(rnd.randint(0, 2)):
                killer = rnd.randint(1, 10); victim = rnd.randint(1, 5) + (5 if killer <= 5 else 0)
                lane = rnd.choice([(2500, 12000), (7400, 7400), (12000, 2500), (5000, 9000), (10000, 5000), (4200, 4200), (10600, 10600)])
                ev.append({"type": "CHAMPION_KILL", "timestamp": minute * 60000 + rnd.randint(0, 59000), "killerId": killer, "victimId": victim,
                           "position": {"x": int(rnd.gauss(lane[0], 1300)), "y": int(rnd.gauss(lane[1], 1300))},
                           "assistingParticipantIds": [rnd.randint(1, 5) + (0 if killer <= 5 else 5)]})
            if minute in (6, 12, 18, 24):
                ev.append({"type": "ELITE_MONSTER_KILL", "timestamp": minute * 60000, "monsterType": "DRAGON", "monsterSubType": rnd.choice(list(DRAKE_TYPES)[:6]), "killerTeamId": rnd.choice([100, 200])})
            if minute == 21:
                ev.append({"type": "ELITE_MONSTER_KILL", "timestamp": minute * 60000, "monsterType": "BARON_NASHOR", "killerTeamId": 100 if win100 else 200})
            if minute in (14, 17, 23, 27):
                ev.append({"type": "BUILDING_KILL", "timestamp": minute * 60000, "buildingType": "TOWER_BUILDING", "laneType": rnd.choice(["TOP_LANE", "MID_LANE", "BOT_LANE"]), "teamId": 200 if win100 else 100})
            frames.append({"timestamp": minute * 60000, "events": ev,
                           "participantFrames": {str(pid): {"totalGold": int(tot[pid]), "minionsKilled": csf[pid], "jungleMinionsKilled": 0} for pid in tot}})
        timelines[mid] = {"info": {"frames": frames}}
    # historique de LP inventé (un point par jour)
    day = 86400000
    for i, p in enumerate(players):
        s, f = lp_score(p["ranks"]["solo"]) - 150, lp_score(p["ranks"]["flex"]) - 100
        p["lp"] = []
        for dd in range(95, -1, -1):
            s += rnd.randint(-25, 30); f += rnd.randint(-20, 24) if dd % 3 == 0 else 0
            p["lp"].append([now - dd * day, max(0, s), max(0, f)])
    return players, matches, timelines


TEMPLATE = r'''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>La ligue des potes</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700;800&family=Barlow:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --night:#0c1820; --ink:#14232d; --ink2:#1b2e3a; --line:#2a3f4d; --ivory:#ece3cf; --mut:#93a3aa;
  --teal:#3cc7bf; --gold:#d4ad62; --red:#e25a50;
  --cond:"Barlow Condensed","Arial Narrow",system-ui,sans-serif; --body:"Barlow",system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
html{background:var(--night)}
body{margin:0;color:var(--ivory);font:15px/1.5 var(--body);background:var(--night);-webkit-font-smoothing:antialiased}
button,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--teal);outline-offset:2px}
img{display:block}
.num{font-family:var(--cond);font-variant-numeric:tabular-nums}
.mut{color:var(--mut)} .win{color:var(--teal)} .loss{color:var(--red)} .gold{color:var(--gold)}
.wrap{max-width:1080px;margin:0 auto;padding:0 18px}

/* ---------- héros : le joueur du mois ---------- */
.hero{position:relative;overflow:hidden;min-height:420px;display:flex;align-items:flex-end;border-bottom:1px solid var(--line)}
.hero .bg{position:absolute;inset:0;background-size:cover;background-position:center 20%;filter:saturate(1.05)}
.hero .bg::after{content:"";position:absolute;inset:0;background:
  linear-gradient(180deg,rgba(12,24,32,.35) 0%,rgba(12,24,32,.55) 45%,var(--night) 100%),
  linear-gradient(90deg,rgba(12,24,32,.92) 0%,rgba(12,24,32,.35) 65%,rgba(12,24,32,.15) 100%)}
.hero .wrap{position:relative;width:100%;padding-top:28px;padding-bottom:26px}
.league{font-family:var(--cond);font-weight:600;font-size:17px;color:var(--gold);letter-spacing:.02em}
.month{display:inline-flex;align-items:center;gap:10px;margin:2px 0 18px;position:relative}
.month select{appearance:none;-webkit-appearance:none;background:transparent;border:0;cursor:pointer;
  font-family:var(--cond);font-weight:800;font-size:clamp(38px,10.5vw,92px);line-height:.95;letter-spacing:-.01em;color:var(--ivory);padding:0 34px 0 0}
.month::after{content:"";position:absolute;right:4px;top:50%;width:14px;height:14px;border-right:3px solid var(--gold);border-bottom:3px solid var(--gold);transform:translateY(-70%) rotate(45deg);pointer-events:none}
.month select option{font-size:16px;background:var(--ink);color:var(--ivory)}
.mvp{display:flex;gap:16px;align-items:center;max-width:620px}
.mvp .av{width:76px;height:76px;border-radius:50%;border:3px solid var(--gold);background:#000;flex:none;box-shadow:0 0 0 6px rgba(212,173,98,.15)}
.mvp .who{font-family:var(--cond);font-weight:700;font-size:34px;line-height:1}
.mvp .what{font-size:14px;color:var(--mut);margin-top:2px}
.mvp .line{margin-top:6px;display:flex;gap:18px;flex-wrap:wrap}
.mvp .line span{font-size:13px;color:var(--mut)} .mvp .line b{font-family:var(--cond);font-size:22px;color:var(--ivory);font-weight:700;margin-right:4px}
.hero .empty{font-size:16px;color:var(--mut);max-width:520px}
.seg{display:inline-flex;margin-top:22px;border:1px solid var(--line);border-radius:999px;background:rgba(12,24,32,.6);padding:3px}
.seg button{border:0;background:transparent;padding:7px 18px;border-radius:999px;cursor:pointer;font-weight:600;color:var(--mut)}
.seg button[aria-pressed=true]{background:var(--ivory);color:var(--night)}
@media (prefers-reduced-motion:no-preference){
  .hero .month,.hero .mvp{animation:rise .7s cubic-bezier(.2,.7,.2,1) both}
  .hero .mvp{animation-delay:.12s}
  @keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
}

/* ---------- onglets ---------- */
nav.tabs{position:sticky;top:0;z-index:5;background:rgba(12,24,32,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
nav.tabs .wrap{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}
nav.tabs button{flex:none;border:0;background:none;cursor:pointer;padding:14px 12px 12px;font-family:var(--cond);font-size:19px;font-weight:600;color:var(--mut);border-bottom:3px solid transparent}
nav.tabs button[aria-current=page]{color:var(--ivory);border-bottom-color:var(--gold)}
main{padding:26px 0 60px}
h2{font-family:var(--cond);font-weight:700;font-size:30px;line-height:1.1;margin:0 0 4px}
.lead{color:var(--mut);margin:0 0 18px;max-width:68ch;font-size:14px}
.demo{border:1px solid var(--gold);color:var(--gold);border-radius:10px;padding:10px 14px;margin-bottom:22px;font-size:14px}

/* ---------- classement ---------- */
.sortbar{display:flex;gap:8px;align-items:center;margin:0 0 12px;flex-wrap:wrap;font-size:13px;color:var(--mut)}
.sortbar select{background:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
.table{border-top:1px solid var(--line)}
.row{display:grid;grid-template-columns:56px 1fr auto;gap:14px;align-items:center;padding:14px 4px;border-bottom:1px solid var(--line);cursor:pointer;width:100%;background:none;border-left:0;border-right:0;border-top:0;text-align:left}
.row:hover{background:linear-gradient(90deg,rgba(60,199,191,.06),transparent 70%)}
.row .rk{font-family:var(--cond);font-weight:800;font-size:44px;line-height:.8;color:var(--line);text-align:center}
.row.r1 .rk{color:var(--gold)} .row.r2 .rk{color:#b9c4c8} .row.r3 .rk{color:#c08a5a}
.row.out{opacity:.5}
.pl{display:flex;gap:12px;align-items:center;min-width:0}
.pl img{width:44px;height:44px;border-radius:50%;background:#000;flex:none}
.pl .n{font-weight:700;font-size:17px;line-height:1.1}
.tier{font-size:12px;font-weight:600}
.strip{display:flex;gap:16px;flex-wrap:wrap;margin-top:6px;font-size:12px;color:var(--mut)}
.strip b{font-family:var(--cond);font-size:17px;font-weight:600;color:var(--ivory);margin-right:3px}
.score{text-align:right}
.score b{display:block;font-family:var(--cond);font-weight:800;font-size:34px;line-height:.9}
.score small{font-size:12px;color:var(--mut)}
.form{display:flex;gap:3px;margin-top:7px;justify-content:flex-end}
.form i{width:7px;height:16px;border-radius:2px;background:var(--red);opacity:.85}
.form i.w{background:var(--teal)}
.champs{display:flex;gap:4px;margin-top:6px}
.champs img{width:26px;height:26px;border-radius:6px;background:#000}
.foot{font-size:13px;color:var(--mut);margin-top:14px;max-width:72ch}

/* ---------- trophées ---------- */
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--mut);margin-bottom:16px}
.legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px;border:2px solid var(--c)}
.tro-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:22px 20px}
.tro{display:grid;grid-template-columns:64px 1fr;gap:12px;align-items:center}
.medal{width:64px;height:64px;border-radius:50%;display:grid;place-items:center;font-size:28px;background:radial-gradient(circle at 35% 30%,var(--ink2),var(--ink));border:2px solid var(--c);box-shadow:inset 0 0 0 4px var(--night),0 0 18px -6px var(--c)}
.tro h3{margin:0;font-family:var(--cond);font-weight:700;font-size:20px;line-height:1.1}
.tro .wn{font-weight:700;margin-top:2px;display:flex;align-items:center;gap:6px}
.tro .wn img{width:20px;height:20px;border-radius:50%}
.tro .v{font-size:13px;color:var(--mut)}
.tro .ru{font-size:12px;color:var(--mut);opacity:.8}
.c-glory{--c:var(--gold)} .c-skill{--c:var(--teal)} .c-shame{--c:var(--red)}

/* ---------- records ---------- */
.rec{display:grid;grid-template-columns:1fr auto;gap:4px 14px;padding:14px 4px;border-bottom:1px solid var(--line);cursor:pointer;width:100%;background:none;border-left:0;border-right:0;border-top:0;text-align:left;align-items:center}
.rec:hover{background:linear-gradient(90deg,rgba(212,173,98,.07),transparent 70%)}
.rec .t{font-family:var(--cond);font-size:20px;font-weight:600}
.rec .big{font-family:var(--cond);font-weight:800;font-size:32px;line-height:1;text-align:right;grid-row:span 2}
.rec .by{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--mut)}
.rec .by img{width:24px;height:24px;border-radius:5px}

/* ---------- liste des parties ---------- */
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.chips button{border:1px solid var(--line);background:var(--ink);border-radius:999px;padding:5px 13px;cursor:pointer;font-size:13px;color:var(--mut)}
.chips button[aria-pressed=true]{border-color:var(--ivory);color:var(--ivory)}
.game{display:grid;grid-template-columns:4px 1fr auto;gap:14px;align-items:center;padding:12px 4px 12px 0;border-bottom:1px solid var(--line);cursor:pointer;width:100%;background:none;border-left:0;border-right:0;border-top:0;text-align:left}
.game:hover{background:rgba(255,255,255,.025)}
.game .bar{align-self:stretch;border-radius:2px}
.game .meta{font-size:12px;color:var(--mut);display:flex;gap:10px;flex-wrap:wrap}
.game .ppl{display:flex;gap:14px;flex-wrap:wrap;margin-top:5px}
.game .p{display:flex;gap:6px;align-items:center;font-size:14px}
.game .p img{width:28px;height:28px;border-radius:6px;background:#000}
.game .res{font-family:var(--cond);font-weight:700;font-size:20px}

/* ---------- duos / palmarès ---------- */
.duo{display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:center;padding:12px 4px;border-bottom:1px solid var(--line)}
.pair{display:flex}.pair img{width:38px;height:38px;border-radius:50%;border:2px solid var(--night);background:#000}.pair img+img{margin-left:-12px}
.duo .wr{font-family:var(--cond);font-weight:800;font-size:28px;text-align:right;line-height:1}
.duo small{display:block;font-size:12px;color:var(--mut);text-align:right}
.hall{display:grid;grid-template-columns:150px 1fr 1fr;gap:16px;align-items:center;padding:14px 4px;border-bottom:1px solid var(--line)}
.hall .m{font-family:var(--cond);font-weight:700;font-size:22px}
.hall .k{font-size:12px;color:var(--mut)}
.hall .w{display:flex;gap:10px;align-items:center}
.hall .w img{width:40px;height:40px;border-radius:50%;border:2px solid var(--gold);background:#000}
.hall .w b{display:block}
.qtitle{font-family:var(--cond);font-size:22px;font-weight:700;margin:28px 0 6px}

/* ---------- fenêtres (profil, revue) ---------- */
.ov{position:fixed;inset:0;background:rgba(5,10,14,.78);z-index:20;overflow:auto;padding:18px 12px}
.sheet{max-width:880px;margin:0 auto;background:var(--ink);border:1px solid var(--line);border-radius:16px;overflow:hidden}
.sh-head{position:relative;padding:22px 20px 18px;background-size:cover;background-position:center 25%}
.sh-head::before{content:"";position:absolute;inset:0;background:linear-gradient(90deg,rgba(20,35,45,.97) 30%,rgba(20,35,45,.6))}
.sh-head>*{position:relative}
.close{position:absolute;top:14px;right:14px;z-index:2;border:1px solid var(--line);background:rgba(12,24,32,.8);border-radius:999px;padding:6px 14px;cursor:pointer}
.sh-title{font-family:var(--cond);font-weight:800;font-size:38px;line-height:1}
.sh-sub{color:var(--mut);font-size:14px;margin-top:4px}
.sh-body{padding:6px 20px 22px}
.sh-body h4{font-family:var(--cond);font-size:21px;font-weight:700;margin:22px 0 8px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:10px}
.tile{background:var(--ink2);border-radius:10px;padding:10px 12px}
.tile b{display:block;font-family:var(--cond);font-weight:700;font-size:26px;line-height:1.05}
.tile span{font-size:12px;color:var(--mut)}
table{border-collapse:collapse;width:100%}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-size:14px}
th{font-weight:600;color:var(--mut);font-size:12px}
th:first-child,td:first-child{text-align:left}
td .ci{display:inline-flex;gap:8px;align-items:center}
td .ci img{width:26px;height:26px;border-radius:6px;background:#000}
.tw{overflow-x:auto}
.bars{display:flex;gap:8px;align-items:flex-end;height:110px;padding-top:10px}
.bars div{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;font-size:11px;color:var(--mut);min-width:0}
.bars i{display:block;width:100%;max-width:46px;background:linear-gradient(180deg,var(--teal),rgba(60,199,191,.35));border-radius:4px 4px 0 0}
.bars b{font-family:var(--cond);font-size:16px;color:var(--ivory)}
.chart{background:var(--night);border-radius:10px;padding:10px}
.callout{border-left:3px solid var(--gold);padding:6px 12px;margin:12px 0;font-size:15px}
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.fcard{background:var(--ink2);border-radius:12px;padding:12px}
.fcard .h{display:flex;gap:10px;align-items:center}
.fcard .h img{width:38px;height:38px;border-radius:8px;background:#000}
.fcard .h b{font-size:16px}
.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:10px 0 8px}
.mini div{background:var(--night);border-radius:8px;padding:7px;text-align:center;font-size:11px;color:var(--mut)}
.mini b{display:block;font-family:var(--cond);font-size:20px;color:var(--ivory)}
.tips{margin:6px 0 0;padding-left:18px;font-size:14px}
.tips li{margin:3px 0}
.ev{display:grid;grid-template-columns:48px 1fr;gap:8px;font-size:14px;padding:5px 0;border-bottom:1px solid rgba(42,63,77,.6)}
.ev .t{font-family:var(--cond);color:var(--mut);font-size:15px}
.evs{max-height:360px;overflow:auto}
.items{display:flex;gap:2px}.items img,.items span{width:22px;height:22px;border-radius:4px;background:#0a141a}
tr.me td{background:rgba(212,173,98,.07)}
.empty-state{padding:30px 0;color:var(--mut);font-size:15px}
.block{margin:0 0 40px}
.block h3{font-family:var(--cond);font-weight:700;font-size:24px;margin:0 0 2px}
.block .lead{margin-bottom:12px}
.svgbox{background:var(--ink);border:1px solid var(--line);border-radius:12px;padding:12px}
.svgbox svg{width:100%;height:auto;display:block}
.keyrow{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;margin-top:8px}
.keyrow span{display:inline-flex;align-items:center;gap:6px}
.keyrow i{width:14px;height:4px;border-radius:2px;display:inline-block}
.heat{display:grid;grid-template-columns:44px repeat(8,1fr);gap:4px;font-size:12px}
.heat .h{color:var(--mut);text-align:center;font-family:var(--cond);font-size:13px}
.heat .d{color:var(--mut);font-family:var(--cond);font-size:14px;align-self:center}
.heat .c{aspect-ratio:1.25;border-radius:6px;display:grid;place-items:center;font-family:var(--cond);font-size:15px;font-weight:600;background:var(--ink2)}
.mapbox{position:relative;max-width:520px;border-radius:12px;overflow:hidden;border:1px solid var(--line);background:#0a141a}
.mapbox img{width:100%;filter:saturate(.75) brightness(.8)}
.mapbox svg{position:absolute;inset:0;width:100%;height:100%}
.duelhead{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center;margin-bottom:18px}
.duelhead .side{display:flex;flex-direction:column;align-items:center;gap:6px}
.duelhead select{background:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px 10px;max-width:100%}
.duelhead img{width:64px;height:64px;border-radius:50%;background:#000}
.duelhead .vs{font-family:var(--cond);font-weight:800;font-size:34px;color:var(--gold)}
.cmp{display:grid;grid-template-columns:1fr 2fr 1fr;gap:10px;align-items:center;padding:9px 0;border-bottom:1px solid var(--line)}
.cmp .a{text-align:right;font-family:var(--cond);font-size:22px;font-weight:600}
.cmp .b{font-family:var(--cond);font-size:22px;font-weight:600}
.cmp .w{color:var(--gold)}
.cmp .mid{text-align:center;font-size:13px;color:var(--mut)}
.cmp .bar{display:flex;height:6px;margin-top:4px;border-radius:3px;overflow:hidden;background:var(--ink2)}
.cmp .bar i:first-child{background:var(--teal)} .cmp .bar i:last-child{background:#a98bff;margin-left:auto}
.verdict{font-family:var(--cond);font-size:26px;font-weight:700;margin:18px 0 6px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px;align-items:start}
@media (max-width:640px){
  .wrap{padding:0 14px}
  .hero{min-height:380px}
  .mvp .av{width:62px;height:62px} .mvp .who{font-size:28px}
  .row{grid-template-columns:36px 1fr auto;gap:10px}
  .row .rk{font-size:32px}
  .pl img{width:38px;height:38px}
  .strip{gap:10px}
  .hall{grid-template-columns:1fr 1fr;}
  .hall .m{grid-column:1/-1}
  .sh-title{font-size:30px}
  nav.tabs button{font-size:17px;padding:12px 9px 10px}
  .tro-grid{grid-template-columns:1fr 1fr;gap:18px 12px}
  .tro{grid-template-columns:1fr;gap:6px;align-content:start}
  .medal{width:52px;height:52px;font-size:23px}
  .tro h3{font-size:18px}
}
</style>
</head>
<body>
<header class="hero" id="hero"></header>
<nav class="tabs" aria-label="Sections"><div class="wrap" id="tabs"></div></nav>
<main><div class="wrap" id="main"></div></main>
<script>
"use strict";
const D=__DATA__;
/* ---------- outils ---------- */
const $=id=>document.getElementById(id);
const el=(t,a={},...k)=>{const e=document.createElement(t);for(const[x,v]of Object.entries(a||{})){if(v==null||v===false)continue;
  if(x==="class")e.className=v;else if(x==="style")e.style.cssText=v;else if(x.startsWith("on"))e.addEventListener(x.slice(2),v);else e.setAttribute(x,v===true?"":v);}
  for(const c of k.flat(9))if(c!=null&&c!==false)e.append(c.nodeType?c:document.createTextNode(String(c)));return e;};
const fmt=(n,d=0)=>(n==null||!isFinite(n))?"–":Number(n).toLocaleString("fr-FR",{maximumFractionDigits:d,minimumFractionDigits:d});
const cap=s=>s.charAt(0).toUpperCase()+s.slice(1);
const pl=(n,one,many,d=0)=>`${fmt(n,d)} ${Math.abs(n)>=2?many:one}`;
const DDV="https://ddragon.leagueoflegends.com/cdn/"+(D.ddv||"15.1.1");
const FIX={FiddleSticks:"Fiddlesticks"};
const cid=c=>FIX[c]||c;
const champImg=(c,cls)=>el("img",{src:`${DDV}/img/champion/${cid(c)}.png`,alt:c,title:c,loading:"lazy",class:cls});
const splash=c=>`https://ddragon.leagueoflegends.com/cdn/img/champion/splash/${cid(c)}_0.jpg`;
const iconSrc=id=>`${DDV}/img/profileicon/${id||29}.png`;
const avatar=(pu,cls)=>el("img",{src:iconSrc(P[pu]?.icon),alt:"",class:cls});
const itemImg=i=>i?el("img",{src:`${DDV}/img/item/${i}.png`,alt:"",loading:"lazy"}):el("span");
const P=Object.fromEntries(D.players.map(p=>[p.puuid,p]));
const TIERS={IRON:["Fer","#8a8a8a"],BRONZE:["Bronze","#b07d58"],SILVER:["Argent","#aab7bf"],GOLD:["Or","#d4ad62"],PLATINUM:["Platine","#45bfae"],
  EMERALD:["Émeraude","#3fcf7f"],DIAMOND:["Diamant","#7fb2ff"],MASTER:["Maître","#c07cff"],GRANDMASTER:["Grand Maître","#ff6b6b"],CHALLENGER:["Challenger","#f4d35e"]};
const tierTxt=r=>{ if(!r) return el("span",{class:"tier mut"},"Non classé"); const [n,c]=TIERS[r.tier]||[r.tier,"#aaa"];
  return el("span",{class:"tier",style:`color:${c}`},`${n}${["MASTER","GRANDMASTER","CHALLENGER"].includes(r.tier)?"":" "+r.div}, ${r.lp} LP`); };
const POS={TOP:"Top",JUNGLE:"Jungle",MIDDLE:"Mid",BOTTOM:"ADC",UTILITY:"Support","":"–"};
const QN={solo:"Solo/duo",flex:"Flex"};
const monthName=k=>{ if(k==="all") return "Toute la période"; const [y,m]=k.split("-").map(Number); return cap(new Date(y,m-1,1).toLocaleString("fr-FR",{month:"long",year:"numeric"})); };
const months=[...new Set(D.games.map(g=>g.month))].sort().reverse();
const nowKey=(()=>{const d=new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}`;})();
if(!months.includes(nowKey)&&!D.demo) months.unshift(nowKey);

let S={month:months[0]||"all",q:"solo",tab:"board",sort:"note",gp:"all",cp:"all",hm:"n",dm:"d",da:D.players[0]?.puuid,db:D.players[1]?.puuid,rc:""};
const PCOL=["#3cc7bf","#d4ad62","#e2786f","#a98bff","#6fb3ff","#9bd46a","#ff8fc7","#ff9f5a","#c9d3d6","#58e0a0"];
const colorOf=pu=>PCOL[D.players.findIndex(p=>p.puuid===pu)%PCOL.length];

/* ---------- agrégation ---------- */
function gamesFor(month,q){ return D.games.filter(g=>(month==="all"||g.month===month)&&(!q||g.q===q)); }
function aggregate(month,q){
  const A={}; for(const p of D.players) A[p.puuid]={puuid:p.puuid,games:0,wins:0,k:0,d:0,a:0,cs:0,min:0,dmg:0,vis:0,gold:0,kp:0,score:0,fb:0,penta:0,quadra:0,triple:0,
    dragons:0,barons:0,towers:0,solo:0,dodged:0,dead:0,steals:0,ally:0,taken:0,remontada:0,throws:0,champs:{},pos:{},res:[],list:[]};
  const gs=gamesFor(month,q).slice().sort((a,b)=>a.date-b.date);
  for(const g of gs){ const minutes=g.dur/60;
    for(const p of g.parts){ if(!p.friend||!A[p.puuid]) continue; const a=A[p.puuid];
      a.games++; a.wins+=p.win?1:0; a.k+=p.k; a.d+=p.d; a.a+=p.a; a.cs+=p.cs; a.min+=minutes; a.dmg+=p.dmg; a.vis+=p.vis; a.gold+=p.gold;
      a.kp+=p.kp||0; a.score+=p.score; for(const f of ["fb","penta","quadra","triple","dragons","barons","towers","solo","dodged","dead","steals","ally","taken"]) a[f]+=p[f]||0;
      a.champs[p.champ]=(a.champs[p.champ]||0)+1; a.pos[p.pos]=(a.pos[p.pos]||0)+1; a.res.push(p.win?1:0); a.list.push({g,p});
      if(g.ext){ const lead=p.team===100?g.ext.min:-g.ext.max, peak=p.team===100?g.ext.max:-g.ext.min;
        if(p.win&&lead<=-3000) a.remontada++; if(!p.win&&peak>=3000) a.throws++; } } }
  for(const a of Object.values(A)){ const n=a.games||1;
    Object.assign(a,{wr:a.wins/n*100,kda:(a.k+a.a)/Math.max(1,a.d),kg:a.k/n,dg:a.d/n,ag:a.a/n,csm:a.cs/(a.min||1),dmgm:a.dmg/(a.min||1),vism:a.vis/(a.min||1),
      goldm:a.gold/(a.min||1),kpp:a.kp/n*100,note:a.score/n,deadg:a.dead/n,takenm:a.taken/(a.min||1),allyg:a.ally/n,towersg:a.towers/n,dodgedg:a.dodged/n,solog:a.solo/n,
      streak:streak(a.res,1),lstreak:streak(a.res,0),nChamps:Object.keys(a.champs).length,topChamp:Object.entries(a.champs).sort((x,y)=>y[1]-x[1])[0]||null}); }
  return A;
}
function streak(r,v){ let b=0,c=0; for(const x of r){ c=x===v?c+1:0; b=Math.max(b,c); } return b; }
const ranked=A=>Object.values(A).filter(a=>a.games>=D.minGames);

/* ---------- héros ---------- */
function renderHero(){
  const A=aggregate(S.month,S.q), R=ranked(A).sort((a,b)=>b.note-a.note), mvp=R[0];
  const h=$("hero"); h.replaceChildren();
  const sel=el("select",{"aria-label":"Mois",onchange:e=>{S.month=e.target.value; render();}},[...months,"all"].map(m=>el("option",{value:m},monthName(m))));
  sel.value=S.month;
  if(mvp?.topChamp) h.append(el("div",{class:"bg",style:`background-image:url('${splash(mvp.topChamp[0])}')`}));
  const seg=el("div",{class:"seg",role:"group","aria-label":"File"},["solo","flex"].map(q=>el("button",{"aria-pressed":S.q===q?"true":"false",onclick:()=>{S.q=q; render();}},QN[q])));
  h.append(el("div",{class:"wrap"},
    el("div",{class:"league"},`La ligue des potes, ${QN[S.q].toLowerCase()}`),
    el("div",{class:"month"},sel),
    mvp?el("div",{class:"mvp"},el("img",{class:"av",src:iconSrc(P[mvp.puuid]?.icon),alt:""}),
      el("div",{},el("div",{class:"what"},S.month==="all"?"Meilleur joueur de la période":"Joueur du mois"),el("div",{class:"who"},P[mvp.puuid].name),
        el("div",{class:"line"},el("span",{},el("b",{},fmt(mvp.note,1)),"de note moyenne"),el("span",{},el("b",{},fmt(mvp.wr)+" %"),"de victoires"),
          el("span",{},el("b",{},mvp.games),"parties"),mvp.topChamp?el("span",{},el("b",{},mvp.topChamp[0]),S.month==="all"?"son champion favori":"son champion du mois"):null)))
      :el("p",{class:"empty"},S.month===nowKey?`Pas encore de vainqueur : il faut ${D.minGames} parties ${QN[S.q].toLowerCase()} dans le mois pour être classé.`:`Personne n'a joué ${D.minGames} parties ${QN[S.q].toLowerCase()} ce mois-là.`),
    seg));
}

/* ---------- navigation ---------- */
const TABS=[["board","Classement"],["trophies","Trophées"],["records","Records"],["charts","Graphiques"],["duel","Duel"],["games","Parties"],["duos","Duos"],["hall","Palmarès"]];
function render(){
  const y=scrollY;
  renderHero();
  $("tabs").replaceChildren(...TABS.map(([k,n])=>el("button",{"aria-current":S.tab===k?"page":null,onclick:()=>{S.tab=k; render();}},n)));
  const m=$("main"); m.replaceChildren();
  if(D.demo) m.append(el("div",{class:"demo"},"Exemple avec des joueurs et des parties inventés. Mets ta clé API dans config.json et relance le programme sans --demo pour voir vos vraies parties."));
  ({board,trophies,records,charts,duel,games,duos,hall})[S.tab](m);
  scrollTo(0,y); // garde la position quand on touche un filtre
}

/* ---------- classement ---------- */
const SORTS=[["note","Note"],["wr","Victoires %"],["kda","KDA"],["csm","CS par minute"],["dmgm","Dégâts par minute"],["vism","Vision par minute"],["games","Parties jouées"]];
const SORT_DEC={note:1,kda:2,csm:1,vism:2,wr:0,dmgm:0,games:0};
function sortVal(a){ return fmt(a[S.sort],SORT_DEC[S.sort]??1)+(S.sort==="wr"?" %":""); }
function board(m){
  const A=aggregate(S.month,S.q); const all=Object.values(A);
  const ok=a=>a.games>=D.minGames;
  all.sort((a,b)=>(ok(b)-ok(a))||(b[S.sort]-a[S.sort])||(b.games-a.games));
  const sort=el("select",{onchange:e=>{S.sort=e.target.value; render();}},SORTS.map(([k,n])=>el("option",{value:k},n))); sort.value=S.sort;
  m.append(el("h2",{},`Classement ${QN[S.q].toLowerCase()}`),
    el("p",{class:"lead"},`${monthName(S.month)}. Touche un joueur pour voir son profil.`),
    el("div",{class:"sortbar"},"Trier par",sort),
    el("div",{class:"table"},all.map((a,i)=>{ const p=P[a.puuid]; const rk=ok(a)?i+1:null;
      return el("button",{class:`row${rk&&rk<=3?" r"+rk:""}${ok(a)?"":" out"}`,onclick:()=>openProfile(a.puuid)},
        el("div",{class:"rk"},rk??"–"),
        el("div",{},el("div",{class:"pl"},avatar(a.puuid),el("div",{},el("div",{class:"n"},p.name),tierTxt(p.ranks?.[S.q]))),
          a.games?el("div",{class:"strip"},el("span",{},el("b",{},`${a.wins}-${a.games-a.wins}`),"V-D"),el("span",{},el("b",{},fmt(a.kda,2)),"KDA"),
            el("span",{},el("b",{},fmt(a.csm,1)),"CS/min"),el("span",{},el("b",{},fmt(a.dmgm)),"dégâts/min"),el("span",{},el("b",{},fmt(a.vism,1)),"vision/min"))
            :el("div",{class:"strip"},"Aucune partie"),
          a.games?el("div",{class:"champs"},Object.entries(a.champs).sort((x,y)=>y[1]-x[1]).slice(0,4).map(([c])=>champImg(c))):null),
        el("div",{class:"score"},el("b",{},a.games?sortVal(a):"–"),
          el("small",{},(SORTS.find(s=>s[0]===S.sort)||[])[1]),
          a.games?el("div",{class:"form",title:"10 dernières parties"},a.res.slice(-10).map(x=>el("i",{class:x?"w":""}))):null)); })),
    el("p",{class:"foot"},`Il faut ${D.minGames} parties pour être classé. La note sur 10 de chaque partie combine le KDA, la participation aux kills, la part des dégâts de l'équipe, le farm (ou la vision pour un support) et la victoire. Les barres à droite montrent les 10 dernières parties : turquoise pour une victoire, rouge pour une défaite.`));
}

/* ---------- trophées ---------- */
const TROPHIES=[
 // [emoji, nom, type (glory/skill/shame), clé, sens, format, description]
 ["👑","Joueur du mois","glory","note",1,a=>`${fmt(a.note,1)} de note moyenne`],
 ["🔥","En feu","glory","streak",1,a=>`${pl(a.streak,"victoire","victoires")} d'affilée`],
 ["🎢","Remontada","glory","remontada",1,a=>`${a.remontada} victoire${a.remontada>1?"s":""} après 3 000 d'or de retard`],
 ["⚡","Multi-kills","glory","mk",1,a=>[a.penta&&`${a.penta} penta`,a.quadra&&`${a.quadra} quadra`,a.triple&&`${a.triple} triple`].filter(Boolean).join(", ")],
 ["💥","Carry","skill","dmgm",1,a=>`${fmt(a.dmgm)} dégâts par minute`],
 ["⚔️","Duelliste","skill","solog",1,a=>`${pl(a.solog,"kill","kills",1)} en solo par partie`],
 ["🩸","Premier sang","skill","fb",1,a=>`${pl(a.fb,"first blood","first bloods")}`],
 ["🌾","Fermier","skill","csm",1,a=>`${fmt(a.csm,1)} CS par minute`],
 ["💰","Banquier","skill","goldm",1,a=>`${fmt(a.goldm)} or par minute`],
 ["👁️","Œil de lynx","skill","vism",1,a=>`${fmt(a.vism,2)} de vision par minute`],
 ["🤝","Altruiste","skill","ag",1,a=>`${pl(a.ag,"assistance","assistances",1)} par partie`],
 ["🚑","Ange gardien","skill","allyg",1,a=>`${fmt(a.allyg)} soins et boucliers sur les alliés par partie`],
 ["🐉","Chasseur de dragons","skill","dragons",1,a=>`${a.dragons} dragon${a.dragons>1?"s":""} achevé${a.dragons>1?"s":""}`],
 ["🏰","Démolisseur","skill","towersg",1,a=>`${pl(a.towersg,"tour détruite","tours détruites",1)} par partie`],
 ["🦝","Voleur","skill","steals",1,a=>`${a.steals} objectif${a.steals>1?"s":""} volé${a.steals>1?"s":""}`],
 ["🕺","Danseur","skill","dodgedg",1,a=>`${fmt(a.dodgedg)} sorts esquivés par partie`],
 ["🧱","L'éponge","skill","takenm",1,a=>`${fmt(a.takenm)} dégâts encaissés par minute`],
 ["🛡️","Le mur","skill","dg",-1,a=>`seulement ${pl(a.dg,"mort","morts",1)} par partie`],
 ["🎯","One-trick","skill","otp",1,a=>`${a.topChamp?.[1]} parties sur ${a.topChamp?.[0]}`],
 ["🌈","Caméléon","skill","nChamps",1,a=>`${a.nChamps} champions différents`],
 ["🎮","No-life","skill","games",1,a=>`${a.games} parties`],
 ["💀","Roi des morts","shame","dg",1,a=>`${pl(a.dg,"mort","morts",1)} par partie`],
 ["⚰️","Touriste du cimetière","shame","deadg",1,a=>`${fmt(a.deadg/60,1)} min passées mort par partie`],
 ["🤡","Le throw","shame","throws",1,a=>`${a.throws} défaite${a.throws>1?"s":""} avec 3 000 d'or d'avance`],
 ["🧊","Maudit","shame","lstreak",1,a=>`${pl(a.lstreak,"défaite","défaites")} d'affilée`],
];
function trophies(m){
  const A=aggregate(S.month,S.q); const R=ranked(A);
  for(const a of R){ a.mk=a.penta*100+a.quadra*10+a.triple; a.otp=a.topChamp?.[1]||0; }
  m.append(el("h2",{},`Trophées ${QN[S.q].toLowerCase()}`),el("p",{class:"lead"},`${monthName(S.month)}, entre les joueurs classés (${D.minGames} parties minimum).`),
    el("div",{class:"legend"},el("span",{class:"c-glory"},"Gloire"),el("span",{class:"c-skill"},"Talent"),el("span",{class:"c-shame"},"Honte")));
  if(!R.length){ m.append(el("p",{class:"empty-state"},"Aucun joueur classé sur cette période pour l'instant.")); return; }
  const grid=el("div",{class:"tro-grid"});
  for(const [e,n,type,key,dir,f] of TROPHIES){
    const list=R.filter(a=>(a[key]||0)!==0||dir<0).sort((a,b)=>(b[key]-a[key])*dir);
    if(!list.length) continue;
    const w=list[0], ru=list[1];
    grid.append(el("div",{class:`tro c-${type}`},el("div",{class:"medal","aria-hidden":"true"},e),
      el("div",{},el("h3",{},n),el("div",{class:"wn"},avatar(w.puuid),P[w.puuid].name),el("div",{class:"v"},f(w)),
        ru?el("div",{class:"ru"},`2e : ${P[ru.puuid].name}`):null)));
  }
  m.append(grid);
}

/* ---------- records (sur une partie) ---------- */
function records(m){
  const rows=[]; for(const g of gamesFor(S.month,S.q)) for(const p of g.parts) if(p.friend) rows.push({g,p,kda:(p.k+p.a)/Math.max(1,p.d),csm:p.cs/(g.dur/60)});
  m.append(el("h2",{},`Records ${QN[S.q].toLowerCase()}`),el("p",{class:"lead"},`${monthName(S.month)}. Les meilleures (et pires) performances sur une seule partie. Touche un record pour revoir la partie.`));
  if(!rows.length){ m.append(el("p",{class:"empty-state"},"Aucune partie sur cette période.")); return; }
  const best=(f,dir=1,filter=()=>true)=>rows.filter(filter).sort((a,b)=>(f(b)-f(a))*dir)[0];
  const R=[
    ["Plus de kills",best(r=>r.p.k),r=>r.p.k,r=>`${r.p.k}/${r.p.d}/${r.p.a}`],
    ["Plus de dégâts",best(r=>r.p.dmg),r=>fmt(r.p.dmg),r=>"dégâts aux champions"],
    ["Meilleure note",best(r=>r.p.score),r=>fmt(r.p.score,1),r=>`${r.p.k}/${r.p.d}/${r.p.a}`],
    ["Meilleur KDA",best(r=>r.kda),r=>fmt(r.kda,1),r=>`${r.p.k}/${r.p.d}/${r.p.a}`],
    ["Meilleur farm",best(r=>r.csm,1,r=>r.p.pos!=="UTILITY"),r=>fmt(r.csm,1),r=>"CS par minute"],
    ["Meilleure vision",best(r=>r.p.vis/(r.g.dur/60)),r=>fmt(r.p.vis),r=>"score de vision"],
    ["Victoire la plus rapide",best(r=>r.g.dur,-1,r=>r.p.win),r=>`${Math.floor(r.g.dur/60)} min`,r=>"victoire"],
    ["Partie la plus longue",best(r=>r.g.dur),r=>`${Math.floor(r.g.dur/60)} min`,r=>r.p.win?"victoire":"défaite"],
    ["Plus de morts",best(r=>r.p.d),r=>r.p.d,r=>`${r.p.k}/${r.p.d}/${r.p.a}`],
    ["Partie à oublier",best(r=>r.p.score,-1),r=>fmt(r.p.score,1),r=>`${r.p.k}/${r.p.d}/${r.p.a}`],
  ];
  m.append(el("div",{class:"table"},R.filter(r=>r[1]).map(([t,r,v,s])=>el("button",{class:"rec",onclick:()=>openReview(r.g)},
    el("div",{class:"t"},t),el("div",{class:"big"},v(r)),
    el("div",{class:"by"},champImg(r.p.champ),el("b",{},r.p.name),el("span",{},s(r)),el("span",{},new Date(r.g.date).toLocaleDateString("fr-FR",{day:"numeric",month:"short"})))))));
}

/* ---------- graphiques : outils SVG ---------- */
const NS="http://www.w3.org/2000/svg";
function svgEl(w,h,label){ const s=document.createElementNS(NS,"svg"); s.setAttribute("viewBox",`0 0 ${w} ${h}`); s.setAttribute("role","img"); s.setAttribute("aria-label",label); return s; }
const esc=t=>String(t).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"})[c]);
function lineChart(series,o){
  const W=Math.round(Math.min(680,Math.max(330,innerWidth-60))),H=o.h||(W<500?240:260),L=o.left||46,R=12,T=12,B=26;
  const xs=series.flatMap(s=>s.pts.map(p=>p[0])), ys=series.flatMap(s=>s.pts.map(p=>p[1]));
  const x0=o.xMin??Math.min(...xs), x1=o.xMax??Math.max(...xs), y0=o.yMin??Math.min(...ys), y1=o.yMax??Math.max(...ys);
  const X=v=>L+(v-x0)/Math.max(1e-9,x1-x0)*(W-L-R), Y=v=>T+(1-(v-y0)/Math.max(1e-9,y1-y0))*(H-T-B);
  let g="";
  for(const [v,lab,strong] of (o.yTicks||[])) if(v>=y0&&v<=y1) g+=`<line x1="${L}" x2="${W-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="${strong?"#3a5263":"#1f3240"}" ${strong?'stroke-dasharray="4 4"':""}/><text x="${L-6}" y="${Y(v)+4}" text-anchor="end" fill="#93a3aa" font-size="12" font-family="Barlow Condensed">${esc(lab)}</text>`;
  for(const [v,lab] of (o.xTicks||[])) g+=`<text x="${X(v)}" y="${H-6}" text-anchor="middle" fill="#93a3aa" font-size="12" font-family="Barlow Condensed">${esc(lab)}</text>`;
  for(const s of series){ if(!s.pts.length) continue; const pts=s.pts.map(p=>`${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
    g+=`<polyline fill="none" stroke="${s.color}" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round" points="${pts}"/>`;
    const lp=s.pts[s.pts.length-1]; g+=`<circle cx="${X(lp[0])}" cy="${Y(lp[1])}" r="4" fill="${s.color}"/>`; }
  const svg=svgEl(W,H,o.label||"graphique"); svg.innerHTML=g;
  return el("div",{class:"svgbox"},svg,el("div",{class:"keyrow"},series.filter(s=>s.pts.length).map(s=>el("span",{},el("i",{style:`background:${s.color}`}),s.name))));
}
function periodBounds(){
  if(S.month==="all"){ const ds=D.games.map(g=>g.date); return [Math.min(...ds,Date.now()-30*864e5),Date.now()]; }
  const [y,m]=S.month.split("-").map(Number); return [new Date(y,m-1,1).getTime(),Math.min(Date.now(),new Date(y,m,1).getTime())];
}
function dayTicks(a,b){ const t=[]; const span=b-a, narrow=innerWidth<640, step=span>40*864e5?(narrow?21:14):span>15*864e5?(narrow?10:7):(narrow?4:2); const d=new Date(a); d.setHours(0,0,0,0);
  for(let x=d.getTime();x<=b;x+=step*864e5) if(x>=a) t.push([x,new Date(x).toLocaleDateString("fr-FR",{day:"numeric",month:"short"})]); return t; }
function playerChips(key,withAll=true){
  return el("div",{class:"chips"},[...(withAll?[["all","Tout le monde"]]:[]),...D.players.map(p=>[p.puuid,p.name])].map(([v,n])=>
    el("button",{"aria-pressed":S[key]===v?"true":"false",onclick:()=>{S[key]=v; render();},style:v!=="all"?`border-left:4px solid ${colorOf(v)}`:""},n)));
}

/* ---------- onglet Graphiques ---------- */
function charts(m){
  m.append(el("h2",{},`Graphiques ${QN[S.q].toLowerCase()}`),el("p",{class:"lead"},`${monthName(S.month)}. Choisis un pote pour filtrer tous les graphiques.`),playerChips("cp"));
  m.append(lpBlock(),formBlock(),heatBlock(),deathBlock());
}
const TNAME=["Fer","Bronze","Argent","Or","Platine","Émeraude","Diamant"], DNAME=["IV","III","II","I"];
const lpLabel=v=>v>=2800?`Maître ${Math.round(v-2800)} LP`:`${TNAME[Math.floor(v/400)]||"?"} ${DNAME[Math.floor(v%400/100)]}`;
function lpBlock(){
  const [a,b]=periodBounds(), qi=S.q==="solo"?1:2;
  const series=D.players.filter(p=>S.cp==="all"||p.puuid===S.cp).map(p=>{ const h=(p.lp||[]).filter(x=>x[qi]!=null);
    const before=h.filter(x=>x[0]<a).pop(); const pts=h.filter(x=>x[0]>=a&&x[0]<=b).map(x=>[x[0],x[qi]]);
    if(before) pts.unshift([a,before[qi]]); return {name:p.name,color:colorOf(p.puuid),pts}; }).filter(s=>s.pts.length);
  const box=el("section",{class:"block"},el("h3",{},"Évolution du rang"),el("p",{class:"lead"},"Le rang de chacun, relevé à chaque lancement du programme. Les pointillés marquent les changements de palier."));
  if(!series.length||series.every(s=>s.pts.length<2)){ box.append(el("p",{class:"empty-state"},"La courbe se remplit au fil des lancements : relance le programme régulièrement (idéalement chaque jour) pour suivre les LP de chacun.")); return box; }
  const ys=series.flatMap(s=>s.pts.map(p=>p[1])); const y0=Math.max(0,Math.floor((Math.min(...ys)-30)/100)*100), y1=Math.ceil((Math.max(...ys)+30)/100)*100;
  const ticks=[]; for(let v=y0;v<=y1;v+=100){ const big=v%400===0; if(y1-y0>500&&!big) continue; ticks.push([v,big&&y1-y0>500?(v>=2800?"Maître":TNAME[v/400]):lpLabel(v),big]); }
  box.append(lineChart(series,{xMin:a,xMax:b,yMin:y0,yMax:y1,yTicks:ticks,xTicks:dayTicks(a,b),left:72,label:"Évolution du rang"}));
  return box;
}
function formBlock(){
  const box=el("section",{class:"block"},el("h3",{},"Forme du moment"),el("p",{class:"lead"},"Pourcentage de victoires sur les 10 dernières parties, partie après partie. Au-dessus de 50 %, c'est la bonne période ; en dessous, gare au tilt."));
  const [a,b]=periodBounds();
  const series=D.players.filter(p=>S.cp==="all"||p.puuid===S.cp).map(p=>{ const l=D.games.filter(g=>g.q===S.q&&g.date<=b).map(g=>({g,me:g.parts.find(x=>x.puuid===p.puuid)})).filter(x=>x.me).sort((x,y)=>x.g.date-y.g.date);
    const pts=[]; l.forEach((x,i)=>{ if(x.g.date<a) return; const w=l.slice(Math.max(0,i-9),i+1); if(w.length<5) return; pts.push([x.g.date,w.filter(y=>y.me.win).length/w.length*100]); });
    return {name:p.name,color:colorOf(p.puuid),pts}; }).filter(s=>s.pts.length>1);
  if(!series.length){ box.append(el("p",{class:"empty-state"},"Pas assez de parties sur cette période (il en faut au moins 5 par joueur).")); return box; }
  box.append(lineChart(series,{xMin:a,xMax:b,yMin:0,yMax:100,yTicks:[[0,"0 %"],[25,"25 %"],[50,"50 %",true],[75,"75 %"],[100,"100 %"]],xTicks:dayTicks(a,b),label:"Forme"}));
  return box;
}
function heatBlock(){
  const days=["Lun","Mar","Mer","Jeu","Ven","Sam","Dim"], slots=["0h","3h","6h","9h","12h","15h","18h","21h"];
  const C=[...Array(7)].map(()=>[...Array(8)].map(()=>({n:0,w:0})));
  for(const g of gamesFor(S.month,S.q)){ const fs=g.parts.filter(p=>p.friend&&(S.cp==="all"||p.puuid===S.cp)); if(!fs.length) continue;
    const d=new Date(g.date), c=C[(d.getDay()+6)%7][Math.floor(d.getHours()/3)]; for(const p of fs){ c.n++; c.w+=p.win?1:0; } }
  const max=Math.max(1,...C.flat().map(c=>c.n));
  const cells=C.flat().map((c,i)=>({c,d:Math.floor(i/8),s:i%8})).filter(x=>x.c.n>=3);
  const best=cells.slice().sort((a,b)=>b.c.w/b.c.n-a.c.w/a.c.n)[0], worst=cells.slice().sort((a,b)=>a.c.w/a.c.n-b.c.w/b.c.n)[0];
  const mix=(a,b,t)=>a.map((v,i)=>Math.round(v+(b[i]-v)*t));
  const col=c=>{ if(!c.n) return "var(--ink2)"; if(S.hm==="n") return `rgba(60,199,191,${(.15+.85*c.n/max).toFixed(2)})`;
    const t=c.w/c.n; const rgb=t<.5?mix([226,90,80],[60,70,78],t*2):mix([60,70,78],[60,199,191],(t-.5)*2); return `rgb(${rgb})`; };
  return el("section",{class:"block"},el("h3",{},"Quand vous jouez (et gagnez)"),
    el("p",{class:"lead"},"Chaque case est un créneau de 3 heures (la colonne « 18h » va de 18h à 21h). "+(S.hm==="n"?"Plus c'est clair, plus il y a de parties.":"Turquoise : on gagne, rouge : on perd (case vide : pas de partie).")),
    el("div",{class:"chips"},[["n","Nombre de parties"],["wr","Victoires %"]].map(([v,n])=>el("button",{"aria-pressed":S.hm===v?"true":"false",onclick:()=>{S.hm=v; render();}},n))),
    el("div",{class:"svgbox"},el("div",{class:"heat"},el("span"),slots.map(x=>el("span",{class:"h"},x)),
      days.map((d,di)=>[el("span",{class:"d"},d),C[di].map((c,si)=>el("div",{class:"c",style:`background:${col(c)};color:${c.n&&S.hm==="n"&&c.n/max>.55?"var(--night)":"var(--ivory)"}`,
        title:c.n?`${d} ${slots[si]}-${si*3+3}h : ${c.n} partie${c.n>1?"s":""}, ${fmt(c.w/c.n*100)} % de victoires`:`${d} ${slots[si]} : aucune partie`},c.n?(S.hm==="n"?c.n:fmt(c.w/c.n*100)):""))]))),
    best&&worst&&best!==worst?el("p",{class:"foot"},`Meilleur créneau : ${days[best.d]} ${slots[best.s]} (${fmt(best.c.w/best.c.n*100)} % de victoires sur ${best.c.n} parties). Pire créneau : ${days[worst.d]} ${slots[worst.s]} (${fmt(worst.c.w/worst.c.n*100)} % sur ${worst.c.n}).`):null);
}
function deathBlock(){
  // joueurs affichés sur la carte (plusieurs possibles) et moment de la partie
  if(!Array.isArray(S.mp)) S.mp=D.players.map(p=>p.puuid);
  const PH=[["all","Toute la partie",0,999],["early","Début (0-14 min)",0,14],["mid","Milieu (14-25 min)",14,25],["late","Fin (25 min et +)",25,999]];
  const ph=PH.find(x=>x[0]===(S.mph||"all"));
  let n=0; const pts=[]; const count={};
  for(const g of gamesFor(S.month,S.q)){ if(!g.review) continue;
    const fr=g.parts.filter(p=>p.friend&&S.mp.includes(p.puuid)); if(!fr.length) continue; n++;
    const who=Object.fromEntries(fr.map(p=>[p.pid,p.puuid]));
    for(const e of g.review.events){ if(e.k!=="kill"||e.x==null||e.t<ph[2]||e.t>=ph[3]) continue;
      const pu=S.dm==="d"?who[e.victim]:who[e.killer]; if(!pu) continue; pts.push([e.x,e.y,pu]); count[pu]=(count[pu]||0)+1; } }
  const toggle=pu=>{ S.mp=S.mp.includes(pu)?S.mp.filter(x=>x!==pu):[...S.mp,pu]; render(); };
  const box=el("section",{class:"block",id:"carte"},el("h3",{},S.dm==="d"?"Où vous mourez":"Où vous tuez"),
    el("p",{class:"lead"},`${pts.length} ${S.dm==="d"?"morts":"kills"} sur ${n} partie${n>1?"s":""} analysée${n>1?"s":""}. Touche les noms pour afficher ou cacher un joueur : chacun a sa couleur.`),
    el("div",{class:"chips"},[["d","Morts"],["k","Kills"]].map(([v,t])=>el("button",{"aria-pressed":S.dm===v?"true":"false",onclick:()=>{S.dm=v; render();}},t)),
      el("span",{style:"width:10px"}),PH.map(([v,t])=>el("button",{"aria-pressed":(S.mph||"all")===v?"true":"false",onclick:()=>{S.mph=v; render();}},t))),
    el("div",{class:"chips"},
      el("button",{"aria-pressed":S.mp.length===D.players.length?"true":"false",onclick:()=>{S.mp=S.mp.length===D.players.length?[]:D.players.map(p=>p.puuid); render();}},S.mp.length===D.players.length?"Tout décocher":"Tout le monde"),
      D.players.map(p=>{ const on=S.mp.includes(p.puuid), c=colorOf(p.puuid);
        return el("button",{"aria-pressed":on?"true":"false",onclick:()=>toggle(p.puuid),style:on?`border-color:${c};color:var(--ivory);background:${c}26`:""},
          el("span",{style:`display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;background:${on?c:"transparent"};border:2px solid ${c}`}),
          p.name,on&&count[p.puuid]?el("span",{class:"mut"},` ${count[p.puuid]}`):null); })));
  if(!pts.length){ box.append(el("p",{class:"empty-state"},S.mp.length?"Aucune position pour cette sélection : la carte utilise les parties avec revue détaillée.":"Choisis au moins un joueur.")); return box; }
  const svg=svgEl(15000,15000,"carte"); svg.setAttribute("preserveAspectRatio","none");
  svg.innerHTML=pts.map(([x,y,pu])=>{ const c=colorOf(pu); return `<circle cx="${x}" cy="${15000-y}" r="250" fill="${c}" fill-opacity=".5" stroke="${c}" stroke-opacity=".95" stroke-width="45"/>`; }).join("");
  box.append(el("div",{class:"mapbox"},el("img",{src:`${DDV}/img/map/map11.png`,alt:"Carte de la Faille de l'invocateur"}),svg));
  return box;
}

/* ---------- radar ---------- */
const AXES=[["Combat",a=>a.kda*.5+a.kpp/100*3],["Farm",a=>a.csm],["Vision",a=>a.vism],["Objectifs",a=>a.towersg+a.dragons/Math.max(1,a.games)*1.5+a.barons/Math.max(1,a.games)*2],
  ["Survie",a=>1/Math.max(.8,a.dg)],["Aide",a=>a.ag+a.allyg/1500]];
function radarValues(A){ const act=Object.values(A).filter(a=>a.games);
  const mx=AXES.map(([,f])=>Math.max(1e-9,...act.map(f)));
  return pu=>A[pu]?.games?AXES.map(([,f],i)=>Math.max(.04,f(A[pu])/mx[i])):null; }
function radar(list){
  const W=340,H=320,cx=170,cy=165,R=112, n=AXES.length, ang=i=>-Math.PI/2+i*2*Math.PI/n, Pt=(i,r)=>[cx+Math.cos(ang(i))*R*r,cy+Math.sin(ang(i))*R*r];
  let g=""; for(const r of [.25,.5,.75,1]) g+=`<polygon points="${AXES.map((_,i)=>Pt(i,r).join(",")).join(" ")}" fill="none" stroke="#24394a"/>`;
  AXES.forEach(([nm],i)=>{ const [x,y]=Pt(i,1); const [lx,ly]=Pt(i,1.2); g+=`<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}" stroke="#24394a"/><text x="${lx}" y="${ly+5}" text-anchor="middle" fill="#ece3cf" font-size="15" font-family="Barlow Condensed" font-weight="600">${nm}</text>`; });
  for(const s of list){ if(!s.v) continue; g+=`<polygon points="${s.v.map((v,i)=>Pt(i,v).join(",")).join(" ")}" fill="${s.color}" fill-opacity=".22" stroke="${s.color}" stroke-width="2.5" stroke-linejoin="round"/>`; }
  const svg=svgEl(W,H,"radar"); svg.innerHTML=g; svg.style.maxWidth="380px"; svg.style.width="100%";
  return el("div",{},svg,el("div",{class:"keyrow"},list.filter(s=>s.v).map(s=>el("span",{},el("i",{style:`background:${s.color}`}),s.name))));
}
function radarBlock(pu){
  const A=aggregate(S.month,S.q), rv=radarValues(A);
  const box=el("div");
  const draw=()=>{ const list=[{name:P[pu].name,color:colorOf(pu),v:rv(pu)}]; if(S.rc&&S.rc!==pu) list.push({name:P[S.rc].name,color:colorOf(S.rc),v:rv(S.rc)});
    const sel=el("select",{style:"background:var(--night);border:1px solid var(--line);border-radius:8px;padding:5px 8px",onchange:e=>{S.rc=e.target.value; draw();}},
      el("option",{value:""},"Personne"),D.players.filter(p=>p.puuid!==pu).map(p=>el("option",{value:p.puuid},p.name))); sel.value=S.rc&&S.rc!==pu?S.rc:"";
    box.replaceChildren(el("div",{class:"sortbar"},"Comparer avec",sel),radar(list),
      el("p",{class:"foot"},"Chaque branche compare le joueur au meilleur du groupe sur la période : le bord extérieur, c'est le meilleur.")); };
  draw(); return box;
}

/* ---------- onglet Duel ---------- */
function duel(m){
  const A=aggregate(S.month,S.q); if(S.da===S.db) S.db=D.players.find(p=>p.puuid!==S.da)?.puuid;
  const a=A[S.da], b=A[S.db];
  const pick=key=>{ const s=el("select",{"aria-label":"Joueur",onchange:e=>{S[key]=e.target.value; render();}},D.players.map(p=>el("option",{value:p.puuid},p.name))); s.value=S[key]; return s; };
  m.append(el("h2",{},`Duel de potes, ${QN[S.q].toLowerCase()}`),el("p",{class:"lead"},`${monthName(S.month)}. Choisis deux potes : celui qui gagne la catégorie est en doré.`),
    el("div",{class:"duelhead"},el("div",{class:"side"},avatar(S.da),pick("da")),el("div",{class:"vs"},"VS"),el("div",{class:"side"},avatar(S.db),pick("db"))));
  if(!a?.games||!b?.games){ m.append(el("p",{class:"empty-state"},`${!a?.games?P[S.da].name:P[S.db].name} n'a pas de partie ${QN[S.q].toLowerCase()} sur cette période.`)); return; }
  const rows=[["Note moyenne","note",1,1],["Victoires %","wr",0,1],["KDA","kda",2,1],["Kills par partie","kg",1,1],["Morts par partie","dg",1,-1],["Assistances par partie","ag",1,1],
    ["CS par minute","csm",1,1],["Dégâts par minute","dmgm",0,1],["Or par minute","goldm",0,1],["Vision par minute","vism",2,1],["Participation aux kills %","kpp",0,1],["Tours par partie","towersg",1,1],["Parties jouées","games",0,1]];
  let sa=0,sb=0;
  const cmp=rows.map(([n,k,d,dir])=>{ const va=a[k],vb=b[k]; const wa=(va-vb)*dir>1e-9, wb=(vb-va)*dir>1e-9; sa+=wa?1:0; sb+=wb?1:0;
    const tot=Math.abs(va)+Math.abs(vb)||1;
    return el("div",{class:"cmp"},el("div",{class:"a"+(wa?" w":"")},fmt(va,d)),
      el("div",{class:"mid"},n,el("div",{class:"bar"},el("i",{style:`width:${Math.abs(va)/tot*100}%`}),el("i",{style:`width:${Math.abs(vb)/tot*100}%`}))),
      el("div",{class:"b"+(wb?" w":"")},fmt(vb,d))); });
  const win=sa===sb?null:sa>sb?S.da:S.db;
  let tog=0,togW=0,vs=0,vsA=0;
  for(const g of gamesFor(S.month,S.q)){ const pa=g.parts.find(p=>p.puuid===S.da), pb=g.parts.find(p=>p.puuid===S.db); if(!pa||!pb) continue;
    if(pa.team===pb.team){ tog++; togW+=pa.win?1:0; } else { vs++; vsA+=pa.win?1:0; } }
  const rv=radarValues(A);
  m.append(el("div",{class:"two"},el("div",{},cmp,
      el("div",{class:"verdict"},win?`${P[win].name} gagne le duel, ${Math.max(sa,sb)} catégories à ${Math.min(sa,sb)}`:`Égalité parfaite, ${sa} partout`),
      el("p",{class:"foot"},tog?`Ensemble : ${tog} partie${tog>1?"s":""}, ${fmt(togW/tog*100)} % de victoires.`:"Ils n'ont pas joué ensemble sur cette période.",
        vs?` Face à face : ${P[S.da].name} ${vsA}, ${P[S.db].name} ${vs-vsA}.`:"")),
    el("div",{},radar([{name:P[S.da].name,color:"#3cc7bf",v:rv(S.da)},{name:P[S.db].name,color:"#a98bff",v:rv(S.db)}]))));
}

/* ---------- parties ---------- */
function games(m){
  const list=gamesFor(S.month,S.q).filter(g=>S.gp==="all"||g.parts.some(p=>p.puuid===S.gp));
  m.append(el("h2",{},`Parties ${QN[S.q].toLowerCase()}`),el("p",{class:"lead"},`${monthName(S.month)}, ${list.length} partie${list.length>1?"s":""}. Touche une partie pour la revue détaillée.`),
    el("div",{class:"chips"},[["all","Tout le monde"],...D.players.map(p=>[p.puuid,p.name])].map(([v,n])=>el("button",{"aria-pressed":S.gp===v?"true":"false",onclick:()=>{S.gp=v; render();}},n))));
  if(!list.length){ m.append(el("p",{class:"empty-state"},"Aucune partie sur cette période.")); return; }
  m.append(el("div",{class:"table"},list.map(g=>{ const fs=g.parts.filter(p=>p.friend); const win=fs[0]?.win;
    return el("button",{class:"game",onclick:()=>openReview(g)},el("div",{class:"bar",style:`background:${win?"var(--teal)":"var(--red)"}`}),
      el("div",{},el("div",{class:"meta"},el("span",{},new Date(g.date).toLocaleString("fr-FR",{weekday:"short",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})),
          el("span",{},`${Math.floor(g.dur/60)} min`),g.review?el("span",{class:"gold"},"revue disponible"):null),
        el("div",{class:"ppl"},fs.map(p=>el("span",{class:"p"},champImg(p.champ),el("b",{},p.name),el("span",{class:"num"},`${p.k}/${p.d}/${p.a}`))))),
      el("div",{class:`res ${win?"win":"loss"}`},win?"Victoire":"Défaite")); })));
}

/* ---------- duos ---------- */
function duos(m){
  const D2={};
  for(const g of gamesFor(S.month,S.q)){ const fs=g.parts.filter(p=>p.friend);
    for(let i=0;i<fs.length;i++) for(let j=i+1;j<fs.length;j++){ if(fs[i].team!==fs[j].team) continue;
      const k=[fs[i].puuid,fs[j].puuid].sort().join("|"); (D2[k]??={a:k.split("|")[0],b:k.split("|")[1],n:0,w:0}); D2[k].n++; D2[k].w+=fs[i].win?1:0; } }
  const rows=Object.values(D2).sort((a,b)=>b.w/b.n-a.w/a.n||b.n-a.n);
  m.append(el("h2",{},`Duos ${QN[S.q].toLowerCase()}`),el("p",{class:"lead"},`${monthName(S.month)}. Qui gagne le plus quand ils jouent ensemble, du meilleur au pire.`));
  if(!rows.length){ m.append(el("p",{class:"empty-state"},"Personne n'a joué ensemble sur cette période.")); return; }
  m.append(el("div",{class:"table"},rows.map(r=>el("div",{class:"duo"},el("div",{class:"pair"},avatar(r.a),avatar(r.b)),
    el("div",{},el("b",{},`${P[r.a].name} et ${P[r.b].name}`),el("div",{class:"mut",style:"font-size:13px"},`${r.n} partie${r.n>1?"s":""} ensemble, ${r.w} victoire${r.w>1?"s":""}`)),
    el("div",{},el("div",{class:`wr ${r.w/r.n>=.5?"win":"loss"}`},fmt(r.w/r.n*100)+" %"),el("small",{},"de victoires"))))));
}

/* ---------- palmarès ---------- */
function hall(m){
  m.append(el("h2",{},"Palmarès"),el("p",{class:"lead"},"Le joueur du mois de chaque file, mois après mois."));
  const cell=(mo,q)=>{ const R=ranked(aggregate(mo,q)).sort((a,b)=>b.note-a.note); const w=R[0];
    return el("div",{},el("div",{class:"k"},QN[q]),w?el("div",{class:"w"},avatar(w.puuid),el("div",{},el("b",{},P[w.puuid].name),el("span",{class:"mut",style:"font-size:13px"},`${fmt(w.note,1)} de note, ${w.games} parties`)))
      :el("div",{class:"mut",style:"font-size:13px"},"Pas de vainqueur")); };
  m.append(el("div",{class:"table"},months.map(mo=>el("div",{class:"hall"},el("div",{class:"m"},monthName(mo)),cell(mo,"solo"),cell(mo,"flex")))));
  // nombre de titres
  const titles={}; for(const mo of months) for(const q of ["solo","flex"]){ const w=ranked(aggregate(mo,q)).sort((a,b)=>b.note-a.note)[0]; if(w&&mo!==nowKey) titles[w.puuid]=(titles[w.puuid]||0)+1; }
  const t=Object.entries(titles).sort((a,b)=>b[1]-a[1]);
  if(t.length) m.append(el("div",{class:"qtitle"},"Titres de joueur du mois (mois terminés)"),el("div",{class:"table"},t.map(([pu,n])=>el("div",{class:"duo"},el("div",{class:"pair"},avatar(pu)),el("b",{},P[pu].name),el("div",{class:"wr gold"},n)))));
}

/* ---------- fenêtre générique ---------- */
function sheet(head,body,bg){
  const ov=el("div",{class:"ov",onclick:e=>{ if(e.target===ov) close(); }});
  const close=()=>{ ov.remove(); document.body.style.overflow=""; document.removeEventListener("keydown",esc); };
  const esc=e=>{ if(e.key==="Escape") close(); }; document.addEventListener("keydown",esc);
  ov.append(el("div",{class:"sheet",role:"dialog","aria-modal":"true"},el("div",{class:"sh-head",style:bg?`background-image:url('${bg}')`:""},el("button",{class:"close",onclick:close},"Fermer"),head),el("div",{class:"sh-body"},body)));
  document.body.append(ov); document.body.style.overflow="hidden"; ov.querySelector(".close").focus();
}

/* ---------- profil joueur ---------- */
function openProfile(pu){
  const p=P[pu], A=aggregate(S.month,S.q)[pu];
  const tiles=A.games?[[`${A.wins}-${A.games-A.wins}`,"victoires-défaites"],[fmt(A.wr)+" %","de victoires"],[fmt(A.note,1),"note moyenne"],[fmt(A.kda,2),"KDA"],
    [`${fmt(A.kg,1)}/${fmt(A.dg,1)}/${fmt(A.ag,1)}`,"K/D/A par partie"],[fmt(A.csm,1),"CS par minute"],[fmt(A.dmgm),"dégâts par minute"],[fmt(A.vism,2),"vision par minute"],[fmt(A.kpp)+" %","participation aux kills"]]:[];
  const trend=months.slice().reverse().map(mo=>({mo,a:aggregate(mo,S.q)[pu]})).filter(x=>x.a.games);
  const maxN=10;
  const champs=Object.entries(A.champs).sort((a,b)=>b[1]-a[1]).map(([c,n])=>{ const l=A.list.filter(x=>x.p.champ===c); const w=l.filter(x=>x.p.win).length;
    const k=l.reduce((s,x)=>s+x.p.k,0),d=l.reduce((s,x)=>s+x.p.d,0),a=l.reduce((s,x)=>s+x.p.a,0); return {c,n,w,kda:(k+a)/Math.max(1,d)}; });
  const roles=Object.entries(A.pos).sort((a,b)=>b[1]-a[1]);
  sheet(el("div",{style:"display:flex;gap:14px;align-items:center"},el("img",{src:iconSrc(p.icon),alt:"",style:"width:72px;height:72px;border-radius:50%;border:3px solid var(--gold)"}),
      el("div",{},el("div",{class:"sh-title"},p.name),el("div",{class:"sh-sub"},`${p.name}#${p.tag}`),
        el("div",{style:"display:flex;gap:14px;flex-wrap:wrap;margin-top:4px"},el("span",{},el("span",{class:"mut"},"Solo/duo "),tierTxt(p.ranks?.solo)),el("span",{},el("span",{class:"mut"},"Flex "),tierTxt(p.ranks?.flex))))),
    [el("h4",{},`${QN[S.q]}, ${monthName(S.month).toLowerCase()}`),
     A.games?el("div",{class:"tiles"},tiles.map(([v,l])=>el("div",{class:"tile"},el("b",{},v),el("span",{},l)))):el("p",{class:"mut"},"Aucune partie sur cette période."),
     A.games?[el("h4",{},"Style de jeu"),radarBlock(pu)]:null,
     trend.length>1?[el("h4",{},"Note moyenne par mois"),el("div",{class:"bars"},trend.map(({mo,a})=>el("div",{},el("b",{},fmt(a.note,1)),el("i",{style:`height:${Math.max(4,a.note/maxN*70)}px`}),el("span",{},(()=>{const [y,mm]=mo.split("-").map(Number); return new Date(y,mm-1,1).toLocaleString("fr-FR",{month:"short"});})())) ))]:null,
     champs.length?[el("h4",{},"Champions"),el("div",{class:"tw"},el("table",{},el("tr",{},el("th",{},"Champion"),el("th",{},"Parties"),el("th",{},"Victoires %"),el("th",{},"KDA")),
       champs.map(c=>el("tr",{},el("td",{},el("span",{class:"ci"},champImg(c.c),c.c)),el("td",{},c.n),el("td",{class:c.w/c.n>=.5?"win":"loss"},fmt(c.w/c.n*100)),el("td",{},fmt(c.kda,2))))))]:null,
     roles.length?[el("h4",{},"Postes"),el("p",{},roles.map(([r,n],i)=>[i?", ":"",el("b",{},POS[r]||r)," ",`${n} partie${n>1?"s":""}`]))]:null,
     A.list.length?[el("h4",{},"Dernières parties"),el("div",{class:"table"},A.list.slice(-8).reverse().map(({g,p:x})=>el("button",{class:"game",onclick:()=>openReview(g)},
       el("div",{class:"bar",style:`background:${x.win?"var(--teal)":"var(--red)"}`}),el("div",{},el("div",{class:"meta"},new Date(g.date).toLocaleDateString("fr-FR",{weekday:"short",day:"numeric",month:"short"})),
         el("div",{class:"ppl"},el("span",{class:"p"},champImg(x.champ),el("span",{class:"num"},`${x.k}/${x.d}/${x.a}`),el("span",{class:"mut"},`note ${fmt(x.score,1)}`)))),
       el("div",{class:`res ${x.win?"win":"loss"}`},x.win?"Victoire":"Défaite"))))]:null],
    A.topChamp?splash(A.topChamp[0]):null);
}

/* ---------- revue d'une partie ---------- */
function openReview(g){
  const fs=g.parts.filter(p=>p.friend), my=fs[0]?.team||100, win=fs[0]?.win, R=g.review;
  const name=pid=>{ const p=g.parts.find(x=>x.pid===pid); return p?(p.friend?p.name:p.champ):"?"; };
  const body=[];
  if(R){
    const sign=my===100?1:-1, gold=R.gold.map(v=>v*sign);
    body.push(el("h4",{},"Écart d'or"),goldChart(gold,R),
      R.turn?el("p",{class:"callout"},`Le tournant : entre ${R.turn.from} et ${R.turn.to} min, l'écart a bougé de `,el("b",{class:R.turn.swing*sign>0?"win":"loss"},`${R.turn.swing*sign>0?"+":""}${fmt(R.turn.swing*sign)} d'or`),`. ${turnEvents(R,my)}`):null);
    body.push(el("h4",{},"Les potes dans cette partie"),el("div",{class:"fgrid"},R.friends.map(f=>{ const p=g.parts.find(x=>x.pid===f.pid);
      return el("div",{class:"fcard"},el("div",{class:"h"},champImg(p.champ),el("div",{},el("b",{},p.name),el("div",{class:"mut",style:"font-size:13px"},`${POS[f.pos]||""}, ${p.k}/${p.d}/${p.a}, note ${fmt(p.score,1)}`))),
        el("div",{class:"mini"},el("div",{},el("b",{},f.cs10??"–"),"CS à 10 min",f.csOpp10!=null?el("div",{},`adversaire ${f.csOpp10}`):null),
          el("div",{},el("b",{class:f.gd10>=0?"win":"loss"},f.gd10!=null?(f.gd10>0?"+":"")+fmt(f.gd10):"–"),"or à 10 min",f.opp?el("div",{},"contre "+f.opp):null),
          el("div",{},el("b",{class:f.gd15>=0?"win":"loss"},f.gd15!=null?(f.gd15>0?"+":"")+fmt(f.gd15):"–"),"or à 15 min")),
        el("div",{class:"mut",style:"font-size:13px"},f.deaths.length?`Morts à ${f.deaths.map(d=>fmt(d,0)+" min").join(", ")}`:"Aucune mort"),
        f.tips.length?el("ul",{class:"tips"},f.tips.map(t=>el("li",{},t))):null); })));
    const evs=R.events.filter(e=>e.k!=="kill"||[e.killer,e.victim,...(e.assists||[])].some(pid=>g.parts.find(p=>p.pid===pid)?.friend));
    body.push(el("h4",{},"Moments clés"),el("div",{class:"evs"},evs.map(e=>{ const ours=e.team===my;
      const txt=e.k==="kill"?`${name(e.killer)} tue ${name(e.victim)}${e.assists?.length?` avec ${e.assists.map(name).join(", ")}`:""}`:`${e.name}, ${ours?"pour vous":"pour eux"}`;
      return el("div",{class:"ev"},el("span",{class:"t"},`${Math.floor(e.t)}:${String(Math.round((e.t%1)*60)).padStart(2,"0")}`),el("span",{style:`color:${ours?"var(--teal)":"var(--red)"}`},(e.k==="kill"?"⚔️ ":e.k==="obj"?"🐉 ":"🏰 ")+txt)); })));
  } else body.push(el("p",{class:"mut"},"Pas de revue détaillée pour cette partie. Augmente « revues_max » dans config.json pour en avoir plus."));
  body.push(el("h4",{},"Tableau des scores"),scoreboard(g,my));
  const top=fs.slice().sort((a,b)=>b.score-a.score)[0];
  sheet(el("div",{},el("div",{class:`sh-title ${win?"win":"loss"}`},win?"Victoire":"Défaite"),
      el("div",{class:"sh-sub"},`${QN[g.q]}, ${new Date(g.date).toLocaleString("fr-FR",{dateStyle:"full",timeStyle:"short"})}, ${Math.floor(g.dur/60)} min ${g.dur%60} s, patch ${g.version}`)),
    body,top?splash(top.champ):null);
}
function turnEvents(R,my){
  const inT=R.events.filter(e=>e.t>=R.turn.from&&e.t<=R.turn.to);
  const k=inT.filter(e=>e.k==="kill"), ours=k.filter(e=>e.team===my).length, objs=inT.filter(e=>e.k!=="kill");
  return `Pendant ce moment : ${ours} kill${ours>1?"s":""} pour vous, ${k.length-ours} pour eux`+(objs.length?`, et ${objs.map(e=>e.name+(e.team===my?" pour vous":" pour eux")).join(", ")}.`:".");
}
function goldChart(gold,R){
  const W=640,H=180,n=Math.max(gold.length-1,1),max=Math.max(1500,...gold.map(Math.abs));
  const x=i=>i/n*W, y=v=>H/2-v/max*(H/2-10);
  const pts=gold.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const ns="http://www.w3.org/2000/svg", svg=document.createElementNS(ns,"svg"); svg.setAttribute("viewBox",`0 0 ${W} ${H+20}`); svg.setAttribute("role","img");
  svg.setAttribute("aria-label","Écart d'or de ton équipe minute par minute"); svg.style.width="100%";
  let ticks=""; for(let mm=5;mm<=n;mm+=5) ticks+=`<line x1="${x(mm)}" y1="0" x2="${x(mm)}" y2="${H}" stroke="#1b2e3a"/><text x="${x(mm)}" y="${H+15}" fill="#93a3aa" font-size="12" text-anchor="middle" font-family="Barlow Condensed">${mm} min</text>`;
  const turn=R.turn?`<rect x="${x(R.turn.from)}" y="0" width="${x(R.turn.to)-x(R.turn.from)}" height="${H}" fill="#d4ad62" opacity=".13"/>`:"";
  const uid="c"+Math.random().toString(36).slice(2,7);
  svg.innerHTML=`${ticks}${turn}<line x1="0" y1="${H/2}" x2="${W}" y2="${H/2}" stroke="#2a3f4d"/>
   <clipPath id="${uid}u"><rect x="0" y="0" width="${W}" height="${H/2}"/></clipPath><clipPath id="${uid}d"><rect x="0" y="${H/2}" width="${W}" height="${H/2}"/></clipPath>
   <polygon clip-path="url(#${uid}u)" fill="rgba(60,199,191,.28)" points="0,${H/2} ${pts} ${W},${H/2}"/><polygon clip-path="url(#${uid}d)" fill="rgba(226,90,80,.28)" points="0,${H/2} ${pts} ${W},${H/2}"/>
   <polyline fill="none" stroke="#ece3cf" stroke-width="2" points="${pts}"/>
   <text x="6" y="14" fill="#3cc7bf" font-size="12" font-family="Barlow Condensed">+${fmt(max)} pour vous</text><text x="6" y="${H-6}" fill="#e25a50" font-size="12" font-family="Barlow Condensed">+${fmt(max)} pour eux</text>`;
  return el("div",{class:"chart"},svg);
}
function scoreboard(g,my){
  return el("div",{class:"tw"},[my,300-my].map(t=>{ const ps=g.parts.filter(p=>p.team===t), w=ps[0]?.win;
    return el("table",{style:"margin-bottom:14px"},el("tr",{},el("th",{},el("span",{class:w?"win":"loss"},`${t===my?"Ton équipe":"Adversaires"}, ${w?"victoire":"défaite"}`)),el("th",{},"KDA"),el("th",{},"CS"),el("th",{},"Dégâts"),el("th",{},"Vision"),el("th",{},"Note"),el("th",{style:"text-align:left"},"Objets")),
      ps.map(p=>el("tr",{class:p.friend?"me":""},el("td",{},el("span",{class:"ci"},champImg(p.champ),el("span",{},el("b",{},p.name),el("span",{class:"mut"}," "+(POS[p.pos]||""))))),
        el("td",{},`${p.k}/${p.d}/${p.a}`),el("td",{},p.cs),el("td",{},fmt(p.dmg)),el("td",{},p.vis),el("td",{},fmt(p.score,1)),el("td",{style:"text-align:left"},el("div",{class:"items"},p.items.map(itemImg)))))); }));
}
render();
</script>
</body>
</html>
'''


# --------------------------------------------------------------------------- programme principal
def main():
    demo = "--demo" in sys.argv
    try:
        if demo:
            say("Mode démo : données inventées.")
            cfg = dict(DEFAULT_CONFIG)
            players, matches, timelines = demo_data(cfg)
        else:
            cfg = load_config()
            if not cfg:
                return 1
            say("Récupération des parties sur l'API Riot…")
            players, matches, timelines = fetch_all(cfg)
        ddv = ddragon_version()
        data = build_report(players, matches, timelines, ddv, cfg["mois"], cfg["min_parties_classement"], demo)
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(TEMPLATE.replace("__DATA__", payload))
        n = len(data["games"])
        say(f"✓ rapport.html créé ({n} parties, {sum(1 for g in data['games'] if g['review'])} revues).")
        if "--no-open" not in sys.argv:
            webbrowser.open("file://" + OUT)
        return 0
    except RiotError as e:
        say("✗ " + str(e))
        return 2


if __name__ == "__main__":
    code = main()
    if os.name == "nt" and "--no-open" not in sys.argv and not sys.stdin.closed:
        try:
            input("\nAppuie sur Entrée pour fermer…")
        except Exception:
            pass
    sys.exit(code)
