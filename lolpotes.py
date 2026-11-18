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
    "site_url": "",
    "alertes": {"parties": True, "exploits": True, "series": True, "rangs": True, "joueur_du_mois": True, "en_game": False},
}
DEFAULT_POINTS = {"victoire": 3, "defaite": 1, "bonne_partie": 1, "partie_parfaite": 2, "quadra": 2, "penta": 5,
                  "max_parties_par_jour": 8, "membres_comptes": 10}
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
        vf = os.path.join(CACHE, "version.txt")
        if not os.path.exists(vf) or open(vf).read().strip() != CACHE_VERSION:
            n = 0
            for f in os.listdir(CACHE):
                if f.startswith(("m_", "t_")) and f.endswith(".json"):
                    os.remove(os.path.join(CACHE, f)); n += 1
            if n:
                say(f"Nouvelle version des revues : {n} fichiers de cache seront relus (une seule fois, ça peut prendre un moment).")
            open(vf, "w").write(CACHE_VERSION)

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
            if cache_name.startswith("m_"):
                data = compact_match(data)
            elif cache_name.startswith("t_"):
                data = compact_timeline(data)
            with open(os.path.join(CACHE, cache_name + ".json"), "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
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

    def live(self, puuid):
        """Partie en cours (None si le joueur n'est pas en game)."""
        try:
            return self.get(self.platform, f"/lol/spectator/v5/active-games/by-summoner/{puuid}")
        except RiotError:
            return None

    def mastery(self, puuid, champ):
        try:
            return self.get(self.platform, f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/by-champion/{champ}")
        except RiotError:
            return None

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


def roster_ids(cfg):
    """Tous les Riot ID suivis : liste « joueurs » + membres de chaque structure (sans doublon)."""
    out, seen = [], set()
    def add(rid):
        if rid and rid.lower() not in seen:
            seen.add(rid.lower()); out.append(rid)
    for rid in cfg.get("joueurs", []):
        add(rid)
    for st in cfg.get("structures", []):
        for eq in st.get("equipes", []):
            for j in eq.get("joueurs", []):
                add(j["riot"] if isinstance(j, dict) else j)
        for rid in st.get("membres", []):
            add(rid)
    return out


def structures_payload(cfg, players):
    """Structures, équipes, matchs et règles de points pour la page (les Riot ID deviennent des identifiants de joueur)."""
    if not cfg.get("structures"):
        return {"mode": "potes"}
    by = {p["rid"].lower(): p["puuid"] for p in players if p.get("rid")}
    sts = []
    for st in cfg["structures"]:
        sid = st.get("id") or st["nom"].lower().replace(" ", "-")
        members, teams = [], []
        for eq in st.get("equipes", []):
            roster = []
            for j in eq.get("joueurs", []):
                j = j if isinstance(j, dict) else {"riot": j}
                pu = by.get(j["riot"].lower())
                roster.append({"riot": j["riot"], "puuid": pu, "poste": j.get("poste", ""), "role": j.get("role", "titulaire")})
                if pu and pu not in members:
                    members.append(pu)
            teams.append({"nom": eq.get("nom", "Équipe"), "coach": eq.get("coach", ""), "joueurs": roster})
        for rid in st.get("membres", []):
            pu = by.get(rid.lower())
            if pu and pu not in members:
                members.append(pu)
        sts.append({"id": sid, "nom": st.get("nom", sid), "tag": st.get("tag", sid[:3].upper()), "logo": st.get("logo", ""),
                    "couleur": st.get("couleur", "#d4ad62"), "description": st.get("description", ""), "discord": st.get("discord", ""),
                    "recrutement": st.get("recrutement", False), "reseaux": st.get("reseaux", {}), "equipes": teams, "membres": members,
                    "postes_recherches": st.get("postes_recherches", []), "niveau": st.get("niveau", ""), "contact": st.get("contact", "")})
    for p in players:
        p["structs"] = [s["id"] for s in sts if p["puuid"] in s["membres"]]
    pts = dict(DEFAULT_POINTS); pts.update(cfg.get("points") or {})
    allm = cfg.get("matchs", [])
    return {"mode": "structures", "structures": sts, "points": pts, "ligue": cfg.get("nom_ligue", "La ligue des structures"),
            "matchs": [m for m in allm if str(m.get("score", "")).strip()],              # résultats
            "agenda": [m for m in allm if not str(m.get("score", "")).strip()],          # à venir (sans score)
            "duels": cfg.get("duels", [])}


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


P_KEYS = ["participantId", "puuid", "riotIdGameName", "summonerName", "championName", "teamId", "win", "teamPosition", "kills", "deaths",
          "assists", "totalMinionsKilled", "neutralMinionsKilled", "goldEarned", "totalDamageDealtToChampions", "visionScore", "champLevel",
          "pentaKills", "quadraKills", "tripleKills", "firstBloodKill", "dragonKills", "baronKills", "turretTakedowns", "turretKills",
          "totalTimeSpentDead", "objectivesStolen", "totalHealsOnTeammates", "totalDamageShieldedOnTeammates", "totalDamageTaken",
          "wardsPlaced", "profileIcon", "gameEndedInEarlySurrender", "visionWardsBoughtInGame", "wardsKilled", "detectorWardsPlaced",
          "damageDealtToObjectives", "damageDealtToBuildings", "timeCCingOthers", "goldSpent"] + [f"item{i}" for i in range(7)]
CH_KEYS = ["soloKills", "skillshotsDodged", "skillshotsHit", "controlWardsPlaced", "killParticipation", "teamDamagePercentage",
           "damageTakenOnTeamPercentage", "turretPlatesTaken", "laneMinionsFirst10Minutes", "jungleCsBefore10Minutes",
           "maxCsAdvantageOnLaneOpponent", "visionScoreAdvantageLaneOpponent", "earlyLaningPhaseGoldExpAdvantage",
           "laningPhaseGoldExpAdvantage", "enemyJungleMonsterKills", "killsNearEnemyTurret", "survivedSingleDigitHpCount",
           "outnumberedKills", "effectiveHealAndShielding", "wardTakedowns", "stealthWardsPlaced", "visionScorePerMinute"]
CACHE_VERSION = "3"


def compact_match(m):
    """Garde seulement ce que le rapport utilise (une partie passe d'environ 60 Ko à 6 Ko)."""
    if not m or "info" not in m:
        return m
    i = m["info"]
    parts = []
    for p in i.get("participants", []):
        q = {k: p[k] for k in P_KEYS if k in p}
        ch = p.get("challenges") or {}
        q["challenges"] = {k: ch[k] for k in CH_KEYS if k in ch}
        parts.append(q)
    teams = [{"teamId": t.get("teamId"), "win": t.get("win"), "objectives": t.get("objectives", {})} for t in i.get("teams", [])]
    return {"metadata": {"matchId": m.get("metadata", {}).get("matchId")},
            "info": dict({k: i.get(k) for k in ("gameCreation", "gameDuration", "queueId", "gameVersion")}, participants=parts, teams=teams)}


def compact_timeline(t):
    """Garde l'or et le farm minute par minute + les kills, objectifs et bâtiments (environ 1 Mo -> 30 Ko)."""
    if not t or "info" not in t:
        return t
    keep = {"CHAMPION_KILL", "ELITE_MONSTER_KILL", "BUILDING_KILL", "ITEM_PURCHASED", "WARD_PLACED", "TURRET_PLATE_DESTROYED"}
    ev_keys = ["type", "timestamp", "killerId", "victimId", "assistingParticipantIds", "position", "monsterType", "monsterSubType",
               "killerTeamId", "buildingType", "laneType", "teamId", "participantId", "itemId", "creatorId", "wardType"]
    frames = []
    for fr in t["info"].get("frames", []):
        frames.append({"timestamp": fr.get("timestamp"),
                       "participantFrames": {k: dict({x: v.get(x, 0) for x in ("totalGold", "minionsKilled", "jungleMinionsKilled", "level", "xp")},
                                                     position=v.get("position"))
                                             for k, v in fr.get("participantFrames", {}).items()},
                       "events": [{k: e[k] for k in ev_keys if k in e} for e in fr.get("events", []) if e.get("type") in keep]})
    return {"info": {"frames": frames}}


def champion_names(ddv):
    """Numéro de champion -> nom (Data Dragon), pour le bandeau « En game »."""
    try:
        with urllib.request.urlopen(f"https://ddragon.leagueoflegends.com/cdn/{ddv}/data/en_US/champion.json", timeout=10) as r:
            return {int(c["key"]): c["id"] for c in json.loads(r.read().decode())["data"].values()}
    except Exception:
        return {}


QUEUE_LABELS = {420: "Ranked solo/duo", 440: "Ranked flex", 400: "Normale draft", 430: "Normale", 450: "ARAM", 480: "Swiftplay",
                490: "Partie rapide", 700: "Clash", 1700: "Arena", 1900: "URF"}


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
            "coach": coach_game(match, tl, pid) if tl else None,
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
                     "lp": p.get("lp", []), "live": p.get("live"), "structs": p.get("structs", [])} for p in players],
        "games": games,
    }


# --------------------------------------------------------------------------- récupération réelle
LIVE_GAMES = {}


def live_game(api, g, known):
    """Partie en cours : la draft complète, avec le rang et la maîtrise de chaque joueur."""
    say("   en game : lecture de la draft")
    out = []
    for x in g.get("participants", []):
        pu = x.get("puuid")
        rank = None
        if pu and pu not in known:
            try:
                for e in api.leagues(pu):
                    if e.get("queueType") == ("RANKED_FLEX_SR" if g.get("gameQueueConfigId") == 440 else "RANKED_SOLO_5x5"):
                        rank = {"tier": e["tier"], "div": e.get("rank", ""), "lp": e.get("leaguePoints", 0), "w": e.get("wins", 0), "l": e.get("losses", 0)}
            except RiotError:
                pass
        m = api.mastery(pu, x.get("championId")) if pu else None
        perks = x.get("perks") or {}
        out.append({"team": x.get("teamId"), "champId": x.get("championId"), "riot": x.get("riotId") or x.get("summonerName") or "Joueur",
                    "puuid": pu, "friend": pu in known, "spells": [x.get("spell1Id"), x.get("spell2Id")],
                    "key": (perks.get("perkIds") or [None])[0], "sub": perks.get("perkSubStyle"), "rank": rank,
                    "mast": {"lvl": m.get("championLevel"), "pts": m.get("championPoints"), "last": m.get("lastPlayTime")} if m else None})
    return {"id": g.get("gameId"), "queue": g.get("gameQueueConfigId"), "mode": QUEUE_LABELS.get(g.get("gameQueueConfigId"), g.get("gameMode", "Partie")),
            "start": g.get("gameStartTime"), "parts": out,
            "bans": [{"team": b.get("teamId"), "champId": b.get("championId"), "turn": b.get("pickTurn")} for b in g.get("bannedChampions", [])]}


def fetch_all(cfg):
    key = os.environ.get("RIOT_API_KEY") or cfg.get("cle_api", "")
    if not key or "colle" in key:
        raise RiotError("Mets ta clé API dans config.json (champ \"cle_api\") ou dans le secret RIOT_API_KEY sur GitHub.")
    api = Riot(key, cfg["serveur"], cfg["region"])
    start = month_start(cfg["mois"])
    players = []
    acc_cache = os.path.join(CACHE, "comptes.json")
    accs = json.load(open(acc_cache, encoding="utf-8")) if os.path.exists(acc_cache) else {}
    for rid in roster_ids(cfg):
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
        players.append({"name": name, "tag": tag, "puuid": accs[rid.lower()], "rid": rid})
    json.dump(accs, open(acc_cache, "w", encoding="utf-8"))
    if not players:
        raise RiotError("Aucun joueur valide dans config.json.")
    LIVE_GAMES.clear()
    known = {p["puuid"] for p in players}
    for p in players:
        say(f"• {p['name']}#{p['tag']}")
        ranks = {}
        for e in api.leagues(p["puuid"]):
            qn = {"RANKED_SOLO_5x5": "solo", "RANKED_FLEX_SR": "flex"}.get(e.get("queueType"))
            if qn:
                ranks[qn] = {"tier": e["tier"], "div": e.get("rank", ""), "lp": e.get("leaguePoints", 0),
                             "w": e.get("wins", 0), "l": e.get("losses", 0)}
        p["ranks"] = ranks
        g = api.live(p["puuid"])
        if g:
            me = next((x for x in g.get("participants", []) if x.get("puuid") == p["puuid"]), {})
            p["live"] = {"champId": me.get("championId"), "queue": g.get("gameQueueConfigId"), "start": g.get("gameStartTime"),
                         "mode": QUEUE_LABELS.get(g.get("gameQueueConfigId"), g.get("gameMode", "Partie")), "game": g.get("gameId")}
            if g.get("gameId") not in LIVE_GAMES:
                LIVE_GAMES[g.get("gameId")] = live_game(api, g, known)
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


def demo_data(cfg, names=None):
    rnd = random.Random(7)
    names = names or ["Toi", "Kevin", "Sarah", "Mehdi", "Lucas"]
    players = [{"name": n, "tag": "EUW", "puuid": "demo-" + n, "icon": 29 + i,
                "ranks": {"solo": {"tier": t, "div": d, "lp": rnd.randint(0, 99), "w": 40, "l": 38},
                          "flex": {"tier": t2, "div": "II", "lp": rnd.randint(0, 99), "w": 12, "l": 10}}}
               for i, (n, t, d, t2) in enumerate(zip(names, (["GOLD", "PLATINUM", "SILVER", "GOLD", "EMERALD", "DIAMOND", "SILVER"] * 4)[:len(names)],
                                                     (["II", "IV", "I", "III", "IV", "IV", "II"] * 4)[:len(names)], (["SILVER", "GOLD", "GOLD", "PLATINUM", "GOLD"] * 5)[:len(names)]))]
    for p in players:
        p["rid"] = p["name"] + "#EUW"
    skill = {p["puuid"]: 0.5 + (i % 6) * 0.1 for i, p in enumerate(players)}
    matches, timelines = {}, {}
    now = int(time.time() * 1000)
    for g in range(84 if len(names) <= 5 else 160):
        qid = 420 if g % 3 else 440
        mid = f"EUW1_{7000000000 + g}"
        group = rnd.sample(players, 1 if qid == 420 and rnd.random() < .6 else (2 if qid == 420 else rnd.randint(3, min(5, len(players)))))
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
                       "challenges": {"soloKills": rnd.randint(0, 4), "skillshotsDodged": rnd.randint(5, 90), "controlWardsPlaced": rnd.randint(0, 6),
                                      "killParticipation": rnd.uniform(.3, .8), "teamDamagePercentage": rnd.uniform(.12, .34), "turretPlatesTaken": rnd.randint(0, 3)}})
        matches[mid] = {"metadata": {"matchId": mid}, "info": {"gameCreation": now - g * (26 if len(names) <= 5 else 14) * 3600 * 1000, "gameDuration": dur, "queueId": qid,
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
            if minute in (11, 13, 15, 17, 19):
                for pid in tot:
                    if rnd.random() < .35:
                        ev.append({"type": "ITEM_PURCHASED", "timestamp": minute * 60000 + 5000, "participantId": pid, "itemId": rnd.choice([3078, 6692, 3071, 3031])})
            LANES = {"TOP": (1800, 12500), "JUNGLE": (5000, 9000), "MIDDLE": (7400, 7400), "BOTTOM": (12500, 1800), "UTILITY": (12000, 2400)}
            frames.append({"timestamp": minute * 60000, "events": ev,
                           "participantFrames": {str(pid): {"totalGold": int(tot[pid]), "minionsKilled": csf[pid], "jungleMinionsKilled": 0,
                                                            "level": min(18, 1 + minute // 2), "position": {"x": int(rnd.gauss(LANES[parts[pid - 1]["teamPosition"]][0], 2200)),
                                                                                                         "y": int(rnd.gauss(LANES[parts[pid - 1]["teamPosition"]][1], 2200))}} for pid in tot}})
        timelines[mid] = {"info": {"frames": frames}}
    st = int(time.time() * 1000) - 9 * 60000
    players[1]["live"] = {"champ": "Darius", "queue": 420, "start": st, "mode": "Ranked solo/duo", "game": 1}
    players[3]["live"] = {"champ": "Thresh", "queue": 420, "start": st, "mode": "Ranked solo/duo", "game": 1}
    TIERS = ["SILVER", "GOLD", "GOLD", "PLATINUM", "EMERALD"]
    ally = [("Darius", players[1]), ("Viego", None), ("Ahri", None), ("Jinx", None), ("Thresh", players[3])]
    foe = ["Garen", "Lee Sin", "Syndra", "Kai'Sa", "Nautilus"]
    sp = {"Darius": [4, 14], "Viego": [4, 11], "Ahri": [4, 14], "Jinx": [4, 7], "Thresh": [4, 14], "Garen": [4, 12], "Lee Sin": [11, 4],
          "Syndra": [4, 12], "Kai'Sa": [4, 7], "Nautilus": [4, 14]}
    ks = {"Darius": 8010, "Viego": 8010, "Ahri": 8112, "Jinx": 8008, "Thresh": 8439, "Garen": 8010, "Lee Sin": 8010, "Syndra": 8229, "Kai'Sa": 9923, "Nautilus": 8439}
    pk, RN = [], rnd.sample(["Zeyrox", "Nami Lover", "KCorp fan", "Wolfy", "Tenebris", "Mister V", "Lynx", "Orka", "Baguette", "Sakura"], 10)
    for i, (c, f) in enumerate(ally + [(c, None) for c in foe]):
        t = TIERS[rnd.randrange(5)]
        pk.append({"team": 100 if i < 5 else 200, "champ": c, "riot": f"{f['name']}#{f['tag']}" if f else RN[i] + "#EUW",
                   "puuid": f["puuid"] if f else None, "friend": bool(f), "spells": sp[c], "key": ks[c], "sub": rnd.choice([8100, 8200, 8300, 8400]),
                   "rank": None if f else {"tier": t, "div": rnd.choice(["I", "II", "III", "IV"]), "lp": rnd.randint(0, 99), "w": rnd.randint(20, 120), "l": rnd.randint(20, 120)},
                   "mast": {"lvl": rnd.randint(3, 40), "pts": rnd.choice([4000, 25000, 80000, 240000, 610000]), "last": now - rnd.randint(1, 60) * 86400000}})
    LIVE_GAMES.clear()
    LIVE_GAMES[1] = {"id": 1, "queue": 420, "mode": "Ranked solo/duo", "start": st, "parts": pk,
                     "bans": [{"team": 100 if i < 5 else 200, "champ": c, "turn": i + 1} for i, c in enumerate(["Yasuo", "Zed", "Master Yi", "Teemo", "Shaco", "Yone", "Blitzcrank", "Morgana", "Vayne", "Katarina"])]}
    # historique de LP inventé (un point par jour)
    day = 86400000
    for i, p in enumerate(players):
        s, f = lp_score(p["ranks"]["solo"]) - 150, lp_score(p["ranks"]["flex"]) - 100
        p["lp"] = []
        for dd in range(95, -1, -1):
            s += rnd.randint(-25, 30); f += rnd.randint(-20, 24) if dd % 3 == 0 else 0
            p["lp"].append([now - dd * day, max(0, s), max(0, f)])
    return players, matches, timelines


def demo_structures_config():
    """Ligue d'exemple : 3 structures, équipes, matchs (noms inventés)."""
    S = lambda *n: [f"{x}#EUW" for x in n]
    return dict(DEFAULT_CONFIG, joueurs=[], nom_ligue="La ligue des structures", min_parties_classement=3, structures=[
        {"id": "nexus", "nom": "Nexus Gaming", "tag": "NXS", "couleur": "#3cc7bf", "recrutement": True,
         "postes_recherches": ["JUNGLE", "UTILITY"], "niveau": "Platine et plus", "contact": "MP Kairo sur Discord",
         "description": "Structure compétitive et chill, entraînements le mardi et le jeudi.", "discord": "https://discord.gg/exemple",
         "reseaux": {"Twitter": "https://twitter.com/exemple"},
         "equipes": [{"nom": "Nexus Prime", "coach": "Coach Max", "joueurs": [
             {"riot": "Kairo#EUW", "poste": "TOP"}, {"riot": "Vexa#EUW", "poste": "JUNGLE"}, {"riot": "Lumen#EUW", "poste": "MIDDLE"},
             {"riot": "Tyr#EUW", "poste": "BOTTOM"}, {"riot": "Sola#EUW", "poste": "UTILITY"}, {"riot": "Brix#EUW", "poste": "TOP", "role": "remplaçant"}]}],
         "membres": S("Nox", "Rivo")},
        {"id": "phoenix", "nom": "Phoenix Rising", "tag": "PHX", "couleur": "#e2786f", "recrutement": False,
         "description": "On renaît de nos cendres à chaque saison.", "discord": "https://discord.gg/exemple2",
         "equipes": [{"nom": "Phoenix Rouge", "coach": "", "joueurs": [
             {"riot": "Ember#EUW", "poste": "TOP"}, {"riot": "Ash#EUW", "poste": "JUNGLE"}, {"riot": "Pyra#EUW", "poste": "MIDDLE"},
             {"riot": "Cendre#EUW", "poste": "BOTTOM"}, {"riot": "Flamme#EUW", "poste": "UTILITY"}]}], "membres": S("Brasier")},
        {"id": "atlas", "nom": "Atlas Esport", "tag": "ATL", "couleur": "#a98bff", "recrutement": True,
         "postes_recherches": ["TOP", "MIDDLE", "BOTTOM"], "niveau": "Tous niveaux", "contact": "Salon #recrutement",
         "description": "Structure d'entraide pour progresser ensemble, tous niveaux.", "discord": "https://discord.gg/exemple3",
         "equipes": [{"nom": "Atlas", "coach": "Mira", "joueurs": [
             {"riot": "Orion#EUW", "poste": "TOP"}, {"riot": "Vega#EUW", "poste": "JUNGLE"}, {"riot": "Rigel#EUW", "poste": "MIDDLE"},
             {"riot": "Deneb#EUW", "poste": "BOTTOM"}, {"riot": "Altair#EUW", "poste": "UTILITY"}]}], "membres": []},
    ], matchs=[
        {"date": "2026-11-12", "a": "nexus", "b": "phoenix", "score": "2-1", "type": "Scrim BO3"},
        {"date": "2026-11-08", "a": "atlas", "b": "nexus", "score": "1-0", "type": "Officiel"},
        {"date": "2026-10-30", "a": "phoenix", "b": "atlas", "score": "2-0", "type": "Scrim BO3"},
        {"date": (datetime.date.today() + datetime.timedelta(days=1)).isoformat(), "heure": "21:00", "a": "nexus", "b": "atlas", "type": "Scrim BO3"},
        {"date": (datetime.date.today() + datetime.timedelta(days=6)).isoformat(), "heure": "20:30", "a": "phoenix", "b": "nexus", "type": "Officiel BO5", "note": "Finale de la coupe d'automne"},
    ], duels=[
        {"a": "nexus", "b": "phoenix", "debut": (datetime.date.today() - datetime.timedelta(days=4)).isoformat(),
         "fin": (datetime.date.today() + datetime.timedelta(days=3)).isoformat(), "titre": "Le choc de la semaine"},
        {"a": "atlas", "b": "phoenix", "debut": (datetime.date.today() - datetime.timedelta(days=20)).isoformat(),
         "fin": (datetime.date.today() - datetime.timedelta(days=13)).isoformat(), "titre": "Duel d'octobre"},
    ])


# --------------------------------------------------------------------------- le coach : analyse concrète d'une partie
# Objectifs par poste (niveau Or-Platine environ). Ils servent de repères, pas de vérité absolue.
TARGETS = {
    "TOP":     {"cs10": 70, "vpm": 0.7, "cw": 2, "kp": 45, "d14": 2, "share": 22},
    "JUNGLE":  {"cs10": 60, "vpm": 1.0, "cw": 3, "kp": 60, "d14": 2, "share": 18},
    "MIDDLE":  {"cs10": 72, "vpm": 0.7, "cw": 2, "kp": 55, "d14": 2, "share": 25},
    "BOTTOM":  {"cs10": 75, "vpm": 0.6, "cw": 1, "kp": 55, "d14": 2, "share": 25},
    "UTILITY": {"cs10": None, "vpm": 2.0, "cw": 4, "kp": 60, "d14": 2, "share": 10},
}
ROLE_FR = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "ADC", "UTILITY": "support"}
LEGENDARY = set()      # objets « finis » (rempli au lancement depuis Data Dragon)
MAP = 14800


def legendary_items(ddv):
    try:
        with urllib.request.urlopen(f"https://ddragon.leagueoflegends.com/cdn/{ddv}/data/en_US/item.json", timeout=10) as r:
            data = json.loads(r.read().decode())["data"]
        return {int(i) for i, it in data.items() if it.get("gold", {}).get("total", 0) >= 2200 and not it.get("into")
                and "Boots" not in it.get("tags", []) and it.get("maps", {}).get("11")}
    except Exception:
        return set()


def zone_of(x, y, team):
    """Zone de la carte (approximative) depuis les coordonnées d'un événement."""
    if x is None or y is None:
        return None
    blue = x + y < MAP
    mine = blue if team == 100 else not blue
    if x + y < 5200:
        return "ta base" if team == 100 else "base adverse"
    if x + y > 2 * MAP - 5200:
        return "base adverse" if team == 100 else "ta base"
    if x < 2600 or y > 12200:
        return "lane top"
    if y < 2600 or x > 12200:
        return "lane bot"
    if abs(x - y) < 1700:
        return "lane mid"
    if abs(x + y - MAP) < 1700:
        return "rivière"
    return "ta jungle" if mine else "jungle adverse"


def mmss(t):
    return f"{int(t)}:{int(round((t % 1) * 60)):02d}"


def fr_n(x, d=0):
    s = f"{x:,.{d}f}".replace(",", " ").replace(".", ",")
    return s


def coach_game(match, tl, pid):
    """Analyse d'une partie pour un joueur : points forts, axes de travail chiffrés, moments à revoir, exercice."""
    info = match["info"]
    parts = info["participants"]
    by = {p["participantId"]: p for p in parts}
    me = by[pid]
    team, pos = me["teamId"], me.get("teamPosition") or ""
    T = TARGETS.get(pos, TARGETS["MIDDLE"])
    minutes = info["gameDuration"] / 60
    ch = me.get("challenges") or {}
    frames = tl["info"]["frames"] if tl else []
    opp = next((o for o in parts if o["teamId"] != team and pos and o.get("teamPosition") == pos), None)

    def fr(minute, who):
        if not frames or minute >= len(frames):
            return None
        return frames[minute].get("participantFrames", {}).get(str(who))

    def cs(f):
        return (f.get("minionsKilled", 0) + f.get("jungleMinionsKilled", 0)) if f else None

    # ---------- mesures
    cs10 = cs(fr(10, pid))
    gd = {m: (fr(m, pid)["totalGold"] - fr(m, opp["participantId"])["totalGold"]) if opp and fr(m, pid) and fr(m, opp["participantId"]) else None
          for m in (10, 15, 20)}
    lvd15 = (fr(15, pid).get("level", 0) - fr(15, opp["participantId"]).get("level", 0)) if opp and fr(15, pid) and fr(15, opp["participantId"]) else None
    lane_curve = []
    if opp:
        for m in range(1, min(len(frames), 26)):
            a, b = fr(m, pid), fr(m, opp["participantId"])
            if a and b:
                lane_curve.append((m, a["totalGold"] - b["totalGold"]))
    vpm = me["visionScore"] / max(1, minutes)
    cw = ch.get("controlWardsPlaced")
    if cw is None:
        cw = me.get("visionWardsBoughtInGame")
    kp = (ch.get("killParticipation") or 0) * 100 if "killParticipation" in ch else None
    share = (ch.get("teamDamagePercentage") or 0) * 100 if "teamDamagePercentage" in ch else None
    plates = ch.get("turretPlatesTaken")
    first_item = None
    events = [e for f in frames for e in f.get("events", [])]
    if LEGENDARY:
        buys = [e["timestamp"] / 60000 for e in events if e.get("type") == "ITEM_PURCHASED" and e.get("participantId") == pid and e.get("itemId") in LEGENDARY]
        first_item = min(buys) if buys else None

    # morts : quand, où, seul ou pas, et ce que l'adversaire a pris juste après
    deaths = []
    for e in events:
        if e.get("type") != "CHAMPION_KILL" or e.get("victimId") != pid:
            continue
        t = e["timestamp"] / 60000
        p = e.get("position") or {}
        z = zone_of(p.get("x"), p.get("y"), team)
        alone = None
        if p.get("x") is not None and frames:
            f = frames[min(len(frames) - 1, int(round(t)))].get("participantFrames", {})
            dist = []
            for k, v in f.items():
                k = int(k)
                if k != pid and by.get(k, {}).get("teamId") == team and v.get("position"):
                    dist.append(math.hypot(v["position"]["x"] - p["x"], v["position"]["y"] - p["y"]))
            alone = bool(dist) and min(dist) > 2800
        lost = None
        for o in events:
            ot = o.get("timestamp", 0) / 60000
            if 0 <= ot - t <= 1.5 and o.get("type") == "ELITE_MONSTER_KILL" and o.get("killerTeamId") not in (None, team):
                lost = MONSTERS.get(o.get("monsterType"), "un objectif"); break
            if 0 <= ot - t <= 1.0 and o.get("type") == "BUILDING_KILL" and o.get("teamId") == team:
                lost = "un inhibiteur" if o.get("buildingType") == "INHIBITOR_BUILDING" else "une tour"
        deaths.append({"t": t, "zone": z, "alone": alone, "lost": lost, "killers": 1 + len(e.get("assistingParticipantIds", []))})
    d14 = sum(1 for d in deaths if d["t"] < 14)
    alone_n = sum(1 for d in deaths if d["alone"])
    enemy_jg = sum(1 for d in deaths if d["zone"] == "jungle adverse")
    cost = [d for d in deaths if d["lost"]]

    # ---------- axes de travail (classés par importance)
    work, good, moments = [], [], []

    def add(key, sev, title, detail, val=None, target=None, unit=""):
        work.append({"key": key, "sev": sev, "title": title, "detail": detail, "val": val, "target": target, "unit": unit})

    if d14 > T["d14"]:
        add("early_deaths", 3 + d14 - T["d14"], f"{d14} morts avant 14 min",
            f"Objectif : {T['d14']} au maximum. Chaque mort en début de partie donne environ 300 or et de l'XP à l'adversaire, et te fait rater une ou deux vagues.",
            d14, T["d14"])
    if alone_n >= 2:
        add("isolated", 2 + alone_n, f"{alone_n} morts loin de ton équipe",
            "Aucun allié à proximité au moment de ces morts. Avant d'avancer seul (side lane, jungle adverse), regarde où sont les ennemis sur la minimap : s'il en manque 2 ou plus, recule.",
            alone_n, 1)
    if enemy_jg >= 2:
        add("enemy_jungle", 2 + enemy_jg, f"{enemy_jg} morts dans la jungle adverse",
            "Entre dans la jungle adverse seulement avec ton jungler ou ton mid, ou après y avoir posé une balise.", enemy_jg, 0)
    if cost:
        add("costly_deaths", 2 + 2 * len(cost), f"{len(cost)} mort{'s' if len(cost) > 1 else ''} qui {'ont' if len(cost) > 1 else 'a'} coûté un objectif",
            " ".join(f"À {mmss(d['t'])}, ta mort a laissé {d['lost']} à l'adversaire." for d in cost[:3]) +
            " Quand un objectif apparaît dans moins d'une minute, joue en sécurité et place-toi avec ton équipe.", len(cost), 0)
    if T["cs10"] and cs10 is not None and cs10 < T["cs10"] - 8:
        miss = T["cs10"] - cs10
        add("cs", 2 + miss / 10, f"{cs10} CS à 10 min",
            f"Objectif : {T['cs10']}. Il te manque {miss} sbires, soit environ {fr_n(miss * 21)} or. En entraînement, joue une partie sans chercher les échanges, juste pour viser {T['cs10'] + 10} CS à 10 min.",
            cs10, T["cs10"])
    if gd[15] is not None and gd[15] <= -800 and pos != "UTILITY":
        lv = f" et {abs(lvd15)} niveau{'x' if abs(lvd15) > 1 else ''} de retard" if lvd15 and lvd15 < 0 else ""
        add("lane_lost", 2 + abs(gd[15]) / 800, f"Lane perdue : {fr_n(gd[15])} or à 15 min",
            f"Face à {opp['championName']}{lv}. Si la lane est dure, joue près de ta tour, demande de l'aide à ton jungler, et prends l'or que tu peux sans te mettre en danger.",
            gd[15], 0, " or")
    if lane_curve:
        peak_m, peak = max(lane_curve, key=lambda x: x[1])
        last_m, last = lane_curve[-1]
        if peak >= 1000 and last <= peak - 1500 and last_m > peak_m:
            add("lead_thrown", 3 + (peak - last) / 1000, f"Avance gâchée : +{fr_n(peak)} or à {peak_m} min, {fr_n(last)} à {last_m} min",
                "Après avoir gagné ta lane, transforme ton avance : prends les plaques de tour, aide pour les dragons, et évite les combats en infériorité numérique.",
                last, peak, " or")
            moments.append({"t": peak_m, "txt": f"Pic d'avance sur {opp['championName']} : +{fr_n(peak)} or. À partir d'ici, l'avance fond."})
    if vpm < T["vpm"] - 0.25:
        add("vision", 1 + (T["vpm"] - vpm) * 3, f"Vision : {fr_n(vpm, 1)} par minute",
            f"Objectif pour un {ROLE_FR.get(pos, 'joueur')} : {fr_n(T['vpm'], 1)}. Pose ta balise jaune dès qu'elle est prête, et pense à la lentille de balayage en milieu de partie.",
            round(vpm, 2), T["vpm"])
    if cw is not None and minutes > 18 and cw < T["cw"]:
        add("control_wards", 1 + T["cw"] - cw, f"{cw} balise{'s' if cw > 1 else ''} de contrôle posée{'s' if cw > 1 else ''}",
            f"Objectif : {T['cw']} par partie au moins. Achète-en une à chaque retour en base (75 or) et pose-la près du prochain objectif.",
            cw, T["cw"])
    if kp is not None and minutes > 15 and kp < T["kp"] - 15:
        add("kp", 1 + (T["kp"] - kp) / 10, f"Participation aux kills : {fr_n(kp)} %",
            f"Objectif : {T['kp']} %. Quand ton équipe se regroupe pour un objectif, rejoins-la au lieu de rester seul à farmer.",
            round(kp), T["kp"], " %")
    if share is not None and pos in ("MIDDLE", "BOTTOM") and minutes > 20 and share < T["share"] - 8:
        add("damage", 1 + (T["share"] - share) / 5, f"{fr_n(share)} % des dégâts de l'équipe",
            f"Objectif pour un {ROLE_FR[pos]} : {T['share']} % ou plus. En combat, place-toi pour taper la cible la plus proche sans risquer ta vie.",
            round(share), T["share"], " %")
    if first_item and pos != "UTILITY" and first_item > 15.5:
        add("first_item", 1 + (first_item - 14) / 2, f"Premier objet fini à {mmss(first_item)}",
            "Repère : vers 12-14 min. Récupère les vagues qui arrivent sur ta lane et rentre en base avec assez d'or pour un gros composant.",
            round(first_item, 1), 14, " min")
    if plates is not None and pos in ("TOP", "MIDDLE", "BOTTOM") and gd[15] is not None and gd[15] >= 500 and plates == 0:
        add("plates", 1.5, "Lane gagnée mais aucune plaque de tour",
            "Une plaque vaut 125 or (jusqu'à 14 min). Quand ton adversaire rentre en base ou meurt, frappe sa tour.", 0, 2)

    # ---------- points forts
    if T["cs10"] and cs10 is not None and cs10 >= T["cs10"] + 5:
        good.append(f"Très bon farm : {cs10} CS à 10 min (objectif {T['cs10']}).")
    if deaths and d14 == 0 and minutes > 16 or not deaths:
        good.append("Aucune mort en début de partie : début de partie propre." if deaths else "Aucune mort de la partie.")
    if gd[15] is not None and gd[15] >= 800 and opp:
        good.append(f"Lane gagnée : +{fr_n(gd[15])} or face à {opp['championName']} à 15 min.")
    if vpm >= T["vpm"] + 0.3:
        good.append(f"Bonne vision : {fr_n(vpm, 1)} par minute (objectif {fr_n(T['vpm'], 1)}).")
    if kp is not None and kp >= T["kp"] + 10:
        good.append(f"Toujours présent dans les combats : {fr_n(kp)} % de participation aux kills.")
    if ch.get("soloKills", 0) >= 2:
        good.append(f"{ch['soloKills']} kills en solo.")
    if share is not None and share >= 30 and pos != "UTILITY":
        good.append(f"Gros carry : {fr_n(share)} % des dégâts de l'équipe.")
    if first_item and first_item <= 12.5 and pos != "UTILITY":
        good.append(f"Premier objet rapide ({mmss(first_item)}).")
    if ch.get("skillshotsDodged", 0) >= 40:
        good.append(f"{ch['skillshotsDodged']} sorts esquivés.")

    # ---------- moments à revoir (minutage pour retrouver l'action dans le replay)
    for d in deaths:
        why = []
        if d["zone"]:
            why.append(d["zone"])
        if d["alone"]:
            why.append("sans allié proche")
        if d["killers"] >= 3:
            why.append(f"contre {d['killers']} ennemis")
        if d["lost"]:
            why.append(f"l'adversaire prend {d['lost']} juste après")
        if d["t"] < 14 or d["alone"] or d["lost"]:
            moments.append({"t": round(d["t"], 2), "txt": "Mort" + (f" ({', '.join(why)})" if why else "")})
    moments.sort(key=lambda m: m["t"])

    work.sort(key=lambda w: -w["sev"])
    DRILLS = {
        "early_deaths": "Prochaine partie : objectif 0 mort avant 10 min. Si tu ne vois pas le jungler adverse sur la carte, joue comme s'il était dans le buisson à côté de toi.",
        "isolated": "Prochaine partie : avant chaque action seul, compte les ennemis visibles sur la minimap. S'il en manque deux, recule d'abord.",
        "enemy_jungle": "Prochaine partie : n'entre jamais dans la jungle adverse sans balise ou sans un allié à côté.",
        "costly_deaths": "Prochaine partie : 60 secondes avant chaque dragon ou Baron, arrête de prendre des risques et va te placer avec ton équipe.",
        "cs": "En normale ou en entraînement : une partie entière concentrée sur le farm. Vise 80 CS à 10 min, même sans tuer personne.",
        "lane_lost": "Prochaine partie : regarde le matchup avant le lancement (pic de puissance de l'adversaire) et joue autour de ta tour pendant ses moments forts.",
        "lead_thrown": "Prochaine partie : quand tu as de l'avance, chaque retour en base doit être suivi d'un objectif (plaques, dragon, Héraut), pas d'un combat au hasard.",
        "vision": "Prochaine partie : pose ta balise jaune à chaque fois qu'elle est prête, et achète une balise de contrôle à chaque retour.",
        "control_wards": "Prochaine partie : une balise de contrôle dans ton inventaire en permanence.",
        "kp": "Prochaine partie : à chaque dragon ou Baron, sois sur place 30 secondes avant l'apparition.",
        "damage": "Prochaine partie : en combat, tape la cible la plus proche plutôt que de chercher le carry adverse.",
        "first_item": "Prochaine partie : note ton premier objet fini, et vise 13 min.",
        "plates": "Prochaine partie : à chaque mort ou retour en base de ton adversaire, va frapper sa tour.",
    }
    score = game_score(me, sum(p["kills"] for p in parts if p["teamId"] == team),
                       sum(p["totalDamageDealtToChampions"] for p in parts if p["teamId"] == team), minutes)
    grade = "S" if score >= 8.5 else "A" if score >= 7.5 else "B" if score >= 6.5 else "C" if score >= 5.5 else "D"
    zones = collections.Counter(d["zone"] for d in deaths if d["zone"])
    return {
        "grade": grade, "good": good[:4], "work": work[:4], "moments": moments[:8],
        "drill": DRILLS.get(work[0]["key"]) if work else "Belle partie : garde les mêmes habitudes et vise une note encore plus haute.",
        "m": {"cs10": cs10, "cs10t": T["cs10"], "vpm": round(vpm, 2), "vpmt": T["vpm"], "cw": cw, "cwt": T["cw"], "d14": d14,
              "alone": alone_n, "kp": round(kp) if kp is not None else None, "kpt": T["kp"], "gd15": gd[15], "first": round(first_item, 1) if first_item else None,
              "zones": dict(zones), "deaths": len(deaths), "costly": len(cost), "keys": [w["key"] for w in work]},
    }


# --------------------------------------------------------------------------- alertes Discord
TIER_FR = {"IRON": "Fer", "BRONZE": "Bronze", "SILVER": "Argent", "GOLD": "Or", "PLATINUM": "Platine", "EMERALD": "Émeraude",
           "DIAMOND": "Diamant", "MASTER": "Maître", "GRANDMASTER": "Grand Maître", "CHALLENGER": "Challenger"}
QFR = {"solo": "solo/duo", "flex": "flex"}
POS_FR = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "ADC", "UTILITY": "support"}
GREEN, RED, GOLD, TEAL, PURPLE = 0x3CC7BF, 0xE25A50, 0xD4AD62, 0x3CC7BF, 0xA98BFF


def fr1(x):
    return f"{x:.1f}".replace(".", ",")


def rank_txt(r):
    if not r:
        return "non classé"
    t = TIER_FR.get(r["tier"], r["tier"])
    return t if r["tier"] in ("MASTER", "GRANDMASTER", "CHALLENGER") else f"{t} {r.get('div', '')}"


BOT_NAME = ["LoL Potes"]


def post_discord(url, embeds, site_url=""):
    """Envoie les messages par paquets de 10 encarts (limite Discord)."""
    for i in range(0, len(embeds), 10):
        body = json.dumps({"username": BOT_NAME[0], "embeds": embeds[i:i + 10]}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "DiscordBot (lolpotes, 1.0)"})
        for attempt in range(3):
            try:
                urllib.request.urlopen(req, timeout=15).read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(float(e.headers.get("Retry-After", "2")) + 0.5)
                    continue
                say(f"⚠ Discord a refusé le message ({e.code}). Vérifie le lien du webhook.")
                return False
            except Exception as e:
                say(f"⚠ Impossible de joindre Discord ({e}).")
                return False
        time.sleep(0.6)
    return True


def month_mvp(games, month, q, min_games, names, isf=lambda p: p["friend"]):
    agg = {}
    for g in games:
        if g["month"] != month or g["q"] != q:
            continue
        for p in g["parts"]:
            if isf(p):
                a = agg.setdefault(p["puuid"], [0, 0.0, 0])
                a[0] += 1; a[1] += p["score"]; a[2] += 1 if p["win"] else 0
    ranked = [(pu, a[1] / a[0], a[0], a[2]) for pu, a in agg.items() if a[0] >= min_games]
    ranked.sort(key=lambda x: -x[1])
    return ranked[:3]


def struct_points(data, month):
    """Points du mois par structure (même calcul que la page) : victoire/défaite + bonus, plafond par jour, meilleurs membres."""
    R = data.get("points") or DEFAULT_POINTS
    out = []
    for st in data.get("structures", []):
        mem = set(st["membres"]); per = {}
        for g in sorted(data["games"], key=lambda g: g["date"]):
            if g["month"] != month:
                continue
            day = datetime.datetime.fromtimestamp(g["date"] / 1000).strftime("%Y-%m-%d")
            for p in g["parts"]:
                if not p["friend"] or p["puuid"] not in mem:
                    continue
                a = per.setdefault(p["puuid"], {"pts": 0, "n": 0, "w": 0, "days": {}})
                if a["days"].get(day, 0) >= R["max_parties_par_jour"]:
                    continue
                a["days"][day] = a["days"].get(day, 0) + 1
                v = R["victoire"] if p["win"] else R["defaite"]
                if p["score"] >= 8: v += R["bonne_partie"]
                if p["d"] == 0 and p["k"] + p["a"] >= 10: v += R["partie_parfaite"]
                v += p.get("penta", 0) * R["penta"] + p.get("quadra", 0) * R["quadra"]
                a["pts"] += v; a["n"] += 1; a["w"] += 1 if p["win"] else 0
        top = sorted(per.values(), key=lambda a: -a["pts"])[:R["membres_comptes"]]
        out.append({"st": st, "pts": sum(a["pts"] for a in top), "n": sum(a["n"] for a in per.values()), "w": sum(a["w"] for a in per.values())})
    return sorted(out, key=lambda x: -x["pts"])


def discord_alerts(cfg, players, data, webhook, scope=None, state_name="etat_alertes.json", only=None, league=False, title=""):
    """Compare avec l'état mémorisé au passage précédent et poste les nouveautés sur Discord.
    scope : joueurs concernés (une structure) ; only : types d'alertes autorisés ; league : annonce de la structure du mois."""
    opts = dict(DEFAULT_CONFIG["alertes"]); opts.update(cfg.get("alertes") or {})
    if only is not None:
        opts = {k: (v and k in only) for k, v in opts.items()}
    isf = lambda p: p["friend"] and (scope is None or p["puuid"] in scope)
    players = [p for p in players if scope is None or p["puuid"] in scope]
    fp = os.path.join(CACHE, state_name)
    os.makedirs(CACHE, exist_ok=True)
    first = not os.path.exists(fp)
    st = json.load(open(fp, encoding="utf-8")) if not first else {}
    seen = set(st.get("seen", []))
    names = {p["puuid"]: p["name"] for p in players}
    site = cfg.get("site_url") or ""
    now_month = datetime.date.today().strftime("%Y-%m")
    embeds = []

    games = sorted([g for g in data["games"] if any(isf(p) for p in g["parts"])], key=lambda g: g["date"])
    new = [g for g in games if g["id"] not in seen]
    # séries : position de chaque partie dans la série en cours de chaque joueur
    streak_at = {}
    run = {}
    for g in games:
        for p in g["parts"]:
            if not isf(p):
                continue
            last = run.get(p["puuid"], (None, 0))
            n = last[1] + 1 if last[0] == p["win"] else 1
            run[p["puuid"]] = (p["win"], n)
            streak_at[(g["id"], p["puuid"])] = (p["win"], n)

    if not first:
        # parties terminées
        if opts.get("parties"):
            for g in new[-12:]:
                fs = [p for p in g["parts"] if isf(p)]
                win = fs[0]["win"]
                lines = [f"**{names.get(p['puuid'], p['name'])}** · {p['champ']} · {p['k']}/{p['d']}/{p['a']} · note {fr1(p['score'])}" for p in fs]
                embeds.append({"title": f"{'✅ Victoire' if win else '❌ Défaite'} en {QFR[g['q']]} · {g['dur'] // 60} min",
                               "description": "\n".join(lines), "color": GREEN if win else RED, **({"url": site} if site else {})})
            if len(new) > 12:
                embeds.append({"description": f"… et {len(new) - 12} autres parties depuis la dernière mise à jour.", "color": 0x2A3F4D})
        # exploits
        if opts.get("exploits"):
            for g in new:
                for p in g["parts"]:
                    if not isf(p):
                        continue
                    nm = names.get(p["puuid"], p["name"])
                    if p.get("penta"):
                        embeds.append({"title": f"⚡ PENTAKILL de {nm} !", "description": f"Sur {p['champ']}, en {QFR[g['q']]}.", "color": GOLD})
                    elif p.get("quadra"):
                        embeds.append({"title": f"⚡ Quadrakill de {nm}", "description": f"Sur {p['champ']}, en {QFR[g['q']]}.", "color": GOLD})
                    if p["d"] == 0 and p["k"] + p["a"] >= 10:
                        embeds.append({"title": f"🛡️ Partie parfaite de {nm}", "description": f"{p['k']}/0/{p['a']} sur {p['champ']}, sans mourir une seule fois.", "color": TEAL})
                    if p["k"] >= 15:
                        embeds.append({"title": f"💥 {nm} a tout cassé", "description": f"{p['k']} kills sur {p['champ']}.", "color": GOLD})
        # séries de victoires / défaites
        if opts.get("series"):
            for g in new:
                for p in g["parts"]:
                    if not isf(p):
                        continue
                    w, n = streak_at[(g["id"], p["puuid"])]
                    if n >= 5 and n % 5 == 0 or (n == 3 and w):
                        nm = names.get(p["puuid"], p["name"])
                        embeds.append({"title": f"🔥 {nm} est en feu : {n} victoires d'affilée" if w else f"🧊 {nm} enchaîne {n} défaites d'affilée",
                                       "description": "Qui l'arrête ?" if w else "Une pause s'impose peut-être…", "color": GREEN if w else RED})
        # changements de rang
        if opts.get("rangs"):
            old = st.get("ranks", {})
            for p in players:
                for q in ("solo", "flex"):
                    r = p.get("ranks", {}).get(q)
                    before = old.get(p["puuid"], {}).get(q)
                    if not r or not before:
                        continue
                    a, b = lp_score(before), lp_score(r)
                    if rank_txt(before) != rank_txt(r):
                        up = b > a
                        embeds.append({"title": f"{'📈' if up else '📉'} {p['name']} {'monte' if up else 'descend'} {rank_txt(r)} en {QFR[q]}",
                                       "description": f"Avant : {rank_txt(before)} ({before.get('lp', 0)} LP). Maintenant : {r.get('lp', 0)} LP.",
                                       "color": GOLD if up else RED})
        # joueur du mois (au premier passage d'un nouveau mois)
        if opts.get("joueur_du_mois") and st.get("month") and st["month"] != now_month:
            for q in ("solo", "flex"):
                top = month_mvp(data["games"], st["month"], q, cfg.get("min_parties_classement", 3), names, isf)
                if top:
                    medals = ["🥇", "🥈", "🥉"]
                    y, m = map(int, st["month"].split("-"))
                    mname = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"][m - 1]
                    de = "d'" if mname[0] in "aeiou" else "de "
                    embeds.append({"title": f"👑 Joueur du mois {de}{mname} en {QFR[q]} : {names[top[0][0]]}",
                                   "description": "\n".join(f"{medals[i]} **{names[pu]}** · note {fr1(sc)} · {n} parties · {round(w / n * 100)} % de victoires"
                                                            for i, (pu, sc, n, w) in enumerate(top)),
                                   "color": GOLD, **({"url": site} if site else {})})
        # structure du mois (ligue)
        if league and data.get("structures") and st.get("month") and st["month"] != now_month:
            ranking = [x for x in struct_points(data, st["month"]) if x["pts"]]
            if ranking:
                medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 20
                embeds.append({"title": f"🏆 Structure du mois : {ranking[0]['st']['nom']}",
                               "description": "\n".join(f"{medals[i]} **{x['st']['nom']}** · {x['pts']} points · {x['w']} victoires sur {x['n']} parties"
                                                        for i, x in enumerate(ranking[:10])),
                               "color": GOLD, **({"url": site} if site else {})})
        # en game (désactivé par défaut)
        if opts.get("en_game"):
            was = set(st.get("live", []))
            for p in players:
                if p.get("live") and p["puuid"] not in was:
                    embeds.append({"title": f"🎮 {p['name']} lance une partie", "description": f"{p['live'].get('champ') or ''} en {p['live'].get('mode', 'partie')}".strip(), "color": PURPLE})

    ok = True
    if embeds and webhook:
        say(f"Discord{(' ' + title) if title else ''} : {len(embeds)} message(s) envoyé(s).")
        ok = post_discord(webhook, embeds, site)
    elif first:
        say("Discord : premier passage, état mémorisé (aucun message pour ne pas inonder le salon).")
    if ok:
        json.dump({"seen": sorted(seen | {g["id"] for g in games}), "month": now_month,
                   "ranks": {p["puuid"]: p.get("ranks", {}) for p in players},
                   "live": [p["puuid"] for p in players if p.get("live")]}, open(fp, "w", encoding="utf-8"))
    return embeds


def points_window(data, st, t0, t1):
    """Points d'une structure entre deux dates (même barème que le classement du mois)."""
    R = data.get("points") or DEFAULT_POINTS
    mem = set(st["membres"]); per = {}
    for g in sorted(data["games"], key=lambda g: g["date"]):
        if not (t0 <= g["date"] < t1):
            continue
        day = datetime.datetime.fromtimestamp(g["date"] / 1000).strftime("%Y-%m-%d")
        for p in g["parts"]:
            if not p["friend"] or p["puuid"] not in mem:
                continue
            a = per.setdefault(p["puuid"], {"pts": 0, "days": {}})
            if a["days"].get(day, 0) >= R["max_parties_par_jour"]:
                continue
            a["days"][day] = a["days"].get(day, 0) + 1
            v = R["victoire"] if p["win"] else R["defaite"]
            if p["score"] >= 8: v += R["bonne_partie"]
            if p["d"] == 0 and p["k"] + p["a"] >= 10: v += R["partie_parfaite"]
            a["pts"] += v + p.get("penta", 0) * R["penta"] + p.get("quadra", 0) * R["quadra"]
    return sum(sorted((a["pts"] for a in per.values()), reverse=True)[:R["membres_comptes"]])


def day_ms(d, end=False):
    dt = datetime.datetime.strptime(d, "%Y-%m-%d") + (datetime.timedelta(days=1) if end else datetime.timedelta())
    return int(dt.timestamp() * 1000)


def league_event_alerts(cfg, data, league_hook, hooks):
    """Rappels d'agenda la veille + annonces de début et de résultat des duels (salon ligue + salons des structures concernées)."""
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, "etat_ligue.json")
    first = not os.path.exists(fp)
    st = json.load(open(fp, encoding="utf-8")) if not first else {}
    done = set(st.get("done", []))
    S = {s["id"]: s for s in data.get("structures", [])}
    nm = lambda i: S.get(i, {}).get("nom", i)
    today = datetime.date.today()
    out = []  # (cle, embed, [ids des structures concernées])
    for ev in data.get("agenda", []):
        try:
            d = datetime.date.fromisoformat(ev["date"])
        except Exception:
            continue
        key = f"rappel-{ev['date']}-{ev.get('heure', '')}-{ev.get('a')}-{ev.get('b')}"
        if d == today + datetime.timedelta(days=1) and key not in done:
            out.append((key, {"title": f"📅 Demain {('à ' + ev['heure']) if ev.get('heure') else ''} : {nm(ev.get('a'))} contre {nm(ev.get('b'))}".replace("  ", " "),
                              "description": " · ".join(x for x in [ev.get("type", ""), ev.get("note", "")] if x) or "Match entre structures",
                              "color": PURPLE}, [ev.get("a"), ev.get("b")]))
    for du in data.get("duels", []):
        try:
            d0, d1 = datetime.date.fromisoformat(du["debut"]), datetime.date.fromisoformat(du["fin"])
        except Exception:
            continue
        base = f"duel-{du['a']}-{du['b']}-{du['debut']}"
        if d0 <= today <= d1 and base + "-debut" not in done:
            out.append((base + "-debut", {"title": f"⚔️ Duel lancé : {nm(du['a'])} contre {nm(du['b'])}",
                                          "description": f"{du.get('titre', '')}\nDu {d0.strftime('%d/%m')} au {d1.strftime('%d/%m')} : la structure qui marque le plus de points gagne.".strip(),
                                          "color": GOLD}, [du["a"], du["b"]]))
        if today > d1 and base + "-fin" not in done and du["a"] in S and du["b"] in S:
            t0, t1 = day_ms(du["debut"]), day_ms(du["fin"], end=True)
            pa, pb = points_window(data, S[du["a"]], t0, t1), points_window(data, S[du["b"]], t0, t1)
            win = nm(du["a"]) if pa > pb else nm(du["b"]) if pb > pa else None
            out.append((base + "-fin", {"title": f"🏁 {'Victoire de ' + win if win else 'Égalité'} dans le duel {nm(du['a'])} contre {nm(du['b'])}",
                                        "description": f"{nm(du['a'])} : {pa} points\n{nm(du['b'])} : {pb} points", "color": GOLD}, [du["a"], du["b"]]))
    if first:  # premier passage : on mémorise sans poster (sauf ce qui arrive demain)
        out = [o for o in out if o[0].startswith("rappel-")]
    sent = set()
    for key, emb, who in out:
        if league_hook:
            BOT_NAME[0] = data.get("ligue", "La ligue"); post_discord(league_hook, [emb])
        for sid in who:
            if hooks.get(sid):
                BOT_NAME[0] = nm(sid); post_discord(hooks[sid], [emb])
        sent.add(key)
    if out:
        say(f"Discord ligue : {len(out)} annonce(s) (agenda / duels).")
    # au premier passage, les duels passés sont marqués comme déjà annoncés
    if first:
        for du in data.get("duels", []):
            try:
                d0, d1 = datetime.date.fromisoformat(du["debut"]), datetime.date.fromisoformat(du["fin"])
            except Exception:
                continue
            base = f"duel-{du['a']}-{du['b']}-{du['debut']}"
            if d0 <= today: sent.add(base + "-debut")
            if today > d1: sent.add(base + "-fin")
    json.dump({"done": sorted(done | sent)}, open(fp, "w", encoding="utf-8"))


# --------------------------------------------------------------------------- appli téléphone (PWA)
ICONS = {'icon-192.png': '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', 'icon-512.png': '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', 'apple-touch-icon.png': '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'}

MANIFEST = {
    "name": "La ligue des potes", "short_name": "Ligue potes", "lang": "fr", "start_url": "./", "scope": "./",
    "display": "standalone", "background_color": "#0c1820", "theme_color": "#0c1820",
    "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
              {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
}

SERVICE_WORKER = """// Réseau d'abord (toujours la dernière version), copie locale si pas de connexion
const C = "ligue-potes";
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET" || !e.request.url.startsWith(self.location.origin)) return;
  e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(C).then(x => x.put(e.request, c)); return r; })
    .catch(() => caches.match(e.request)));
});
"""


def write_pwa(cfg):
    import base64
    d = os.path.join(HERE, "docs")
    os.makedirs(d, exist_ok=True)
    for name, b64 in ICONS.items():
        fp = os.path.join(d, name)
        if not os.path.exists(fp):
            with open(fp, "wb") as f:
                f.write(base64.b64decode(b64))
    with open(os.path.join(d, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f, ensure_ascii=False, indent=1)
    with open(os.path.join(d, "sw.js"), "w", encoding="utf-8") as f:
        f.write(SERVICE_WORKER)


TEMPLATE = r'''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>La ligue des potes</title>
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0c1820">
<link rel="icon" href="icon-192.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Ligue potes">
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
.grade{display:inline-grid;place-items:center;min-width:44px;height:44px;border-radius:12px;border:2px solid var(--g);color:var(--g);font-family:var(--cond);font-weight:800;font-size:26px;padding:0 6px}
.grade.sm{min-width:0;height:auto;font-size:15px;padding:2px 9px;border-radius:999px;border-width:1px}
.grades{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.fcard.coach{display:grid;gap:4px}
.cblock{margin-top:8px}
.ct{font-family:var(--cond);font-weight:700;font-size:17px;margin-bottom:4px}
.ct.good{color:var(--teal)} .ct.work{color:var(--gold)}
.witem{border-left:3px solid var(--gold);padding:4px 0 4px 10px;margin:6px 0}
.witem b{display:block}.witem p{margin:3px 0 0;font-size:13.5px;color:#cfd6d9}
.cbar{position:relative;height:6px;border-radius:3px;background:var(--night);margin:6px 0 2px;max-width:280px}
.cbar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px}
.cbar em{position:absolute;top:-4px;width:2px;height:14px;background:var(--teal)}
.moms div{display:flex;gap:10px;font-size:13.5px;padding:3px 0;border-bottom:1px dashed var(--line)}
.moms .num{color:var(--gold);min-width:42px;font-size:15px}
.drill{margin-top:12px;background:rgba(60,199,191,.08);border:1px solid rgba(60,199,191,.4);border-radius:10px;padding:9px 12px;font-size:14px}
.drill span{display:block;font-family:var(--cond);font-weight:700;color:var(--teal);font-size:16px}
.prio{display:grid;grid-template-columns:36px 1fr;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.pn{width:32px;height:32px;border-radius:50%;background:var(--gold);color:var(--night);display:grid;place-items:center;font-family:var(--cond);font-weight:800;font-size:18px}
.pv{font-size:14px;margin-top:4px}
.zbars div{display:grid;grid-template-columns:120px 1fr 48px;gap:8px;align-items:center;font-size:13.5px;padding:3px 0}
.zbars i{height:8px;border-radius:4px;background:var(--red);opacity:.8;display:block}
.zbars b{text-align:right;font-family:var(--cond)}
.ldot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--red);margin-left:6px;vertical-align:middle}
.lcard{background:var(--ink);border:1px solid var(--line);border-radius:14px;padding:14px;margin:14px 0}
.lhead{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-end}
.lhead h3{margin:0;font-family:var(--cond);font-size:26px}
.lavg b{font-family:var(--cond);font-size:20px;margin-left:6px}.lavg .teal{color:var(--teal)}.lavg .red{color:var(--red)}
.lduo{background:rgba(212,173,98,.1);border:1px solid rgba(212,173,98,.35);border-radius:10px;padding:8px 12px;font-size:14px;margin:12px 0 0}
.lgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:12px}
@media (max-width:760px){.lgrid{grid-template-columns:1fr}}
.lteam{border-top:3px solid var(--teal);padding-top:8px}.lteam.foe{border-color:var(--red)}
.lth{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;font-family:var(--cond);font-size:18px}
.lbans{display:flex;gap:3px}.ban img,.ban.none{width:24px;height:24px;border-radius:5px;filter:grayscale(1);opacity:.65;display:block;background:var(--night)}
.lrow{display:grid;grid-template-columns:48px 26px 1fr;gap:8px;align-items:center;padding:7px 4px;border-bottom:1px solid var(--line);width:100%;text-align:left;background:none;border-left:0;border-right:0;border-top:0;color:inherit;font:inherit}
.lrow.me{background:rgba(60,199,191,.08);border-radius:8px;cursor:pointer}
.lch img,.noimg{width:48px;height:48px;border-radius:10px;display:block;background:var(--night)}
.lsp{display:grid;gap:2px}.lsp img{width:24px;height:24px;border-radius:5px;display:block}
.lru{position:relative;display:block;width:24px;height:24px}.lru img{width:24px;height:24px}.lru .sub{position:absolute;width:12px;height:12px;right:-4px;bottom:-2px;background:var(--night);border-radius:50%}
.lwho{min-width:0;font-size:13.5px}.lwho b{font-size:15px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lrk{font-size:13px}.lm{font-size:12.5px}
.ltags{display:flex;gap:4px;flex-wrap:wrap;margin-top:3px}
.ltag{font-size:11.5px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.ltag.gold{color:var(--gold);border-color:rgba(212,173,98,.5)}.ltag.red{color:var(--red);border-color:rgba(226,90,80,.5)}
.lnotes{margin:4px 0 8px 4px;font-size:13px}

.acard{background:var(--ink);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:14px}
.acard>summary{font-family:var(--cond);font-size:22px;font-weight:700;color:var(--ivory);cursor:pointer}
.acard input,.acard select,.acard textarea{background:var(--night);border:1px solid var(--line);border-radius:8px;padding:8px 10px;color:var(--ivory);font:inherit;min-width:0}
.acard input[type=color]{padding:2px;height:38px;width:70px}
.acard input[type=checkbox]{width:20px;height:20px;accent-color:var(--gold)}
.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin:12px 0}
.af{display:grid;gap:4px;font-size:13px}.af>span{color:var(--mut)}.af small{color:var(--mut);font-size:11px}.af textarea{width:100%}
.alist{display:grid;margin-top:12px}
.arow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 0;border-bottom:1px solid var(--line);font-size:14px}
.arow b{flex:1;min-width:160px}
.inl{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:6px 0}
.btn.sm{padding:5px 11px;font-size:13px}.btn.ghost{background:transparent}.btn:disabled{opacity:.45;cursor:default}
.steps{font-size:14px;color:var(--mut);padding-left:20px}.steps li{margin:4px 0}
.tedit{border-left:3px solid var(--line);padding-left:12px;margin:12px 0}
.chk{display:inline-flex;gap:6px;align-items:center;font-size:14px}
.savebar{position:sticky;bottom:0;z-index:4;background:rgba(12,24,32,.96);border-top:1px solid var(--line);margin:20px -18px 0;padding:12px 0}
.savebar .wrap{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;font-size:14px}
.duels{display:grid;gap:12px}
.duelc{background:var(--ink);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.duelc.live{border-color:var(--gold)}
.dh{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:13px}
.dh span[class^=tag-]{font-weight:700;border-radius:999px;padding:1px 9px;font-size:12px}
.tag-live{background:var(--gold);color:var(--night)} .tag-soon{border:1px solid var(--teal);color:var(--teal)} .tag-done{border:1px solid var(--line);color:var(--mut)}
.dv{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:center;margin-top:10px}
.ds{display:flex;gap:10px;align-items:center;background:none;border:0;cursor:pointer;padding:0;text-align:left;color:var(--mut);min-width:0}
.ds.r{justify-content:flex-end;text-align:right}
.ds b{font-size:15px;min-width:0;overflow:hidden;text-overflow:ellipsis}
.ds .num{font-family:var(--cond);font-weight:800;font-size:30px;color:var(--ivory)}
.ds.w b{color:var(--gold2)}
.vs2{font-family:var(--cond);font-weight:800;color:var(--gold)}
.dbar{display:flex;height:6px;border-radius:3px;overflow:hidden;margin-top:10px;background:var(--ink2)}
.dream{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.dcard{min-height:190px;border-radius:14px;border:1px solid var(--line);background:var(--ink) center 20%/cover;display:flex;flex-direction:column;justify-content:flex-end;align-items:flex-start;gap:3px;padding:10px;text-align:left;cursor:pointer;color:var(--ivory)}
div.dcard{cursor:default}
.dcard img{width:40px;height:40px;border-radius:50%;border:2px solid var(--gold)}
.dpos{font-family:var(--cond);font-weight:700;color:var(--gold);font-size:18px;margin-bottom:auto}
.dcard b{font-size:16px}.dcard small{font-size:12px;color:var(--mut)}
.recs{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.recc{background:var(--ink);border:1px solid var(--line);border-top:4px solid var(--c);border-radius:14px;padding:14px;display:grid;gap:10px}
.rh{display:flex;gap:12px;align-items:center}.rh b{font-size:18px}
.rl{display:grid;gap:3px;font-size:14px}.rl .mut{font-size:12px}
.chipsr{display:flex;gap:6px;flex-wrap:wrap}.chipsr span{border:1px solid var(--teal);color:var(--teal);border-radius:999px;padding:2px 10px;font-size:13px;font-weight:600}
.tstats{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-top:12px}
.tstats .wide{grid-column:1/-1}.tstats .tile span{display:block}
.palm{display:flex;gap:6px;flex-wrap:wrap}
.palm span{border:1px solid var(--c);border-radius:999px;padding:3px 10px;font-size:13px}
@media (max-width:640px){.dream{grid-template-columns:1fr 1fr}.dream>:last-child{grid-column:1/-1}.ds .num{font-size:24px}.ds b{font-size:13px}}
.scopes{border-bottom:1px solid var(--line);background:var(--night)}
.scopes .wrap{display:flex;gap:8px;overflow-x:auto;padding-top:10px;padding-bottom:10px;scrollbar-width:none}
.sc{flex:none;display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);background:var(--ink);border-radius:999px;padding:4px 14px 4px 4px;cursor:pointer;font-weight:600;font-size:14px;color:var(--mut)}
.sc[aria-pressed=true]{border-color:var(--gold);color:var(--ivory);background:var(--ink2)}
.slogo{border-radius:28%;object-fit:cover;flex:none;background:#000}
.slogo.txt{display:inline-grid;place-items:center;font-family:var(--cond);font-weight:800;color:var(--night)}
.mvp .slogo{box-shadow:0 0 0 6px rgba(212,173,98,.15)}
.sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.scard{display:flex;gap:12px;align-items:center;text-align:left;background:var(--ink);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:12px;padding:12px;cursor:pointer}
.scard:hover{background:var(--ink2)}
.badge{display:inline-block;margin-top:4px;font-size:12px;border:1px solid var(--teal);color:var(--teal);border-radius:999px;padding:1px 9px}
.badge.off{border-color:var(--line);color:var(--mut)}
.fiche{display:flex;gap:18px;align-items:center;flex-wrap:wrap;padding:18px;border-radius:14px;background:linear-gradient(120deg,color-mix(in srgb,var(--c) 22%,transparent),transparent 70%);border:1px solid var(--line)}
.links{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
a.btn{text-decoration:none;color:inherit;display:inline-block}
.champrow{display:flex;gap:14px;flex-wrap:wrap}
.champrow span{display:inline-flex;align-items:center;gap:6px}
.champrow img{width:30px;height:30px;border-radius:6px}
.team{margin-bottom:26px}
.team .th{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;border-bottom:2px solid var(--line);padding-bottom:6px}
.team h3{font-family:var(--cond);font-size:24px;margin:0}
.rrow{display:grid;grid-template-columns:62px 40px 1fr auto;gap:12px;align-items:center;padding:10px 4px;border-bottom:1px solid var(--line);width:100%;background:none;border-left:0;border-right:0;border-top:0;text-align:left;cursor:pointer}
div.rrow{cursor:default}
.rrow img{width:38px;height:38px;border-radius:50%}
.post{font-family:var(--cond);font-weight:700;font-size:16px;color:var(--gold)}
.rs{text-align:right}.rs b{display:block;font-family:var(--cond);font-size:24px;line-height:1}.rs small{font-size:12px;color:var(--mut)}
.match{display:grid;grid-template-columns:110px 1fr 70px 1fr;gap:10px;align-items:center;padding:12px 4px;border-bottom:1px solid var(--line)}
.match .md{font-size:13px}
.match .side{display:flex;gap:8px;align-items:center;font-weight:600;color:var(--mut)}
.match .side.a{justify-content:flex-end;text-align:right}
.match .side.w{color:var(--ivory)}
.match .sc2{font-family:var(--cond);font-weight:800;font-size:26px;text-align:center}
@media (max-width:640px){.match{grid-template-columns:1fr 54px 1fr}.match .md{grid-column:1/-1}.rrow{grid-template-columns:44px 36px 1fr auto;gap:8px}}
.live{background:linear-gradient(90deg,rgba(226,90,80,.18),rgba(226,90,80,.04));border-bottom:1px solid rgba(226,90,80,.35)}
.live .wrap{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding-top:9px;padding-bottom:9px;font-size:14px}
.live .dot{width:10px;height:10px;border-radius:50%;background:var(--red);box-shadow:0 0 0 0 rgba(226,90,80,.6)}
@media (prefers-reduced-motion:no-preference){.live .dot{animation:pulse 1.6s infinite}@keyframes pulse{70%{box-shadow:0 0 0 9px rgba(226,90,80,0)}100%{box-shadow:0 0 0 0 rgba(226,90,80,0)}}}
.live .lp{display:inline-flex;gap:6px;align-items:center}
.live .lp img{width:24px;height:24px;border-radius:50%;background:#000}
.live .lp img+img{border-radius:5px}
.wrapped{position:relative;max-width:440px;border-radius:22px;overflow:hidden;border:1px solid var(--line);background:var(--ink)}
.wrapped .wbg{position:absolute;inset:0;background-size:cover;background-position:center 15%}
.wrapped .wbg::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(12,24,32,.35) 0%,rgba(12,24,32,.92) 32%,var(--night) 70%)}
.wrapped .winner{position:relative;padding:22px 20px 18px}
.wtop{display:flex;gap:12px;align-items:center;margin-bottom:18px}
.wtop img{width:60px;height:60px;border-radius:50%;border:3px solid var(--gold);background:#000}
.wm{font-family:var(--cond);color:var(--gold);font-size:17px;font-weight:600}
.wn{font-family:var(--cond);font-weight:800;font-size:40px;line-height:.95}
.wgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px 16px}
.wi span{display:block;font-size:12px;color:var(--mut)}
.wi b{display:block;font-family:var(--cond);font-weight:700;font-size:26px;line-height:1.05}
.wi small{display:block;font-size:12px;color:var(--mut);line-height:1.3}
.wtro{margin-top:18px;padding-top:12px;border-top:1px solid var(--line)}
.wtro>span{font-size:12px;color:var(--mut)}
.wtro div{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.wt{border:1px solid var(--gold);color:var(--gold2);border-radius:999px;padding:3px 10px;font-size:13px}
.wfoot{margin-top:16px;font-family:var(--cond);font-size:14px;color:var(--mut)}
.btn{border:1px solid var(--line);background:var(--ink2);border-radius:999px;padding:8px 16px;cursor:pointer;font-weight:600}
.btn.gold{background:var(--gold);border-color:var(--gold);color:var(--night)}
footer{border-top:1px solid var(--line);padding:22px 0 40px;font-size:12px;color:var(--mut)}
footer .wrap{display:flex;gap:16px;flex-wrap:wrap;justify-content:space-between;align-items:center}
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
<div id="live"></div>
<header class="hero" id="hero"></header>
<div id="scopes"></div>
<nav class="tabs" aria-label="Sections"><div class="wrap" id="tabs"></div></nav>
<main><div class="wrap" id="main"></div></main>
<footer><div class="wrap"><span><span id="pname">La ligue des potes</span> isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.</span><span id="install"></span><a href="#admin" class="mut" style="font-size:12px">Admin</a></div></footer>
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
const ord=n=>n===1?"1er":`${n}e`;
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
const ALL=D.players;
let PL=ALL, SC=new Set(ALL.map(p=>p.puuid));      // joueurs du périmètre affiché (toute la ligue ou une structure)
const isF=p=>!!p&&p.friend&&SC.has(p.puuid);
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

let S={month:months[0]||"all",q:"solo",tab:"board",sort:"note",gp:"all",cp:"all",hm:"n",dm:"d",da:PL[0]?.puuid,db:PL[1]?.puuid,rc:""};
const PCOL=["#3cc7bf","#d4ad62","#e2786f","#a98bff","#6fb3ff","#9bd46a","#ff8fc7","#ff9f5a","#c9d3d6","#58e0a0"];
const colorOf=pu=>PCOL[D.players.findIndex(p=>p.puuid===pu)%PCOL.length];

/* ---------- agrégation ---------- */
function gamesFor(month,q){ return D.games.filter(g=>(month==="all"||g.month===month)&&(!q||g.q===q)&&g.parts.some(isF)); }
function aggregate(month,q){
  const A={}; for(const p of PL) A[p.puuid]={puuid:p.puuid,games:0,wins:0,k:0,d:0,a:0,cs:0,min:0,dmg:0,vis:0,gold:0,kp:0,score:0,fb:0,penta:0,quadra:0,triple:0,
    dragons:0,barons:0,towers:0,solo:0,dodged:0,dead:0,steals:0,ally:0,taken:0,remontada:0,throws:0,champs:{},pos:{},res:[],list:[]};
  const gs=gamesFor(month,q).slice().sort((a,b)=>a.date-b.date);
  for(const g of gs){ const minutes=g.dur/60;
    for(const p of g.parts){ if(!isF(p)||!A[p.puuid]) continue; const a=A[p.puuid];
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
  if(MULTI&&S.scope==="league"){ const h=$("hero"); h.replaceChildren(); leagueHero(h); return; }
  const A=aggregate(S.month,S.q), R=ranked(A).sort((a,b)=>b.note-a.note), mvp=R[0];
  const h=$("hero"); h.replaceChildren();
  const sel=el("select",{"aria-label":"Mois",onchange:e=>{S.month=e.target.value; render();}},[...months,"all"].map(m=>el("option",{value:m},monthName(m))));
  sel.value=S.month;
  if(mvp?.topChamp) h.append(el("div",{class:"bg",style:`background-image:url('${splash(mvp.topChamp[0])}')`}));
  const seg=el("div",{class:"seg",role:"group","aria-label":"File"},["solo","flex"].map(q=>el("button",{"aria-pressed":S.q===q?"true":"false",onclick:()=>{S.q=q; render();}},QN[q])));
  h.append(el("div",{class:"wrap"},
    el("div",{class:"league"},MULTI?`${SBY[S.scope].nom}, ${QN[S.q].toLowerCase()}`:`La ligue des potes, ${QN[S.q].toLowerCase()}`),
    el("div",{class:"month"},sel),
    mvp?el("div",{class:"mvp"},el("img",{class:"av",src:iconSrc(P[mvp.puuid]?.icon),alt:""}),
      el("div",{},el("div",{class:"what"},S.month==="all"?"Meilleur joueur de la période":"Joueur du mois"),el("div",{class:"who"},P[mvp.puuid].name),
        el("div",{class:"line"},el("span",{},el("b",{},fmt(mvp.note,1)),"de note moyenne"),el("span",{},el("b",{},fmt(mvp.wr)+" %"),"de victoires"),
          el("span",{},el("b",{},mvp.games),"parties"),mvp.topChamp?el("span",{},el("b",{},mvp.topChamp[0]),S.month==="all"?"son champion favori":"son champion du mois"):null)))
      :el("p",{class:"empty"},S.month===nowKey?`Pas encore de vainqueur : il faut ${D.minGames} parties ${QN[S.q].toLowerCase()} dans le mois pour être classé.`:`Personne n'a joué ${D.minGames} parties ${QN[S.q].toLowerCase()} ce mois-là.`),
    seg));
}

/* ---------- navigation ---------- */
const TABS=[["board","Classement"],["live","En game"],["coach","Coach"],["postes","Postes"],["trophies","Trophées"],["wrapped","Wrapped"],["records","Records"],["charts","Graphiques"],["duel","Duel"],["games","Parties"],["duos","Duos"],["hall","Palmarès"]];
const TABS_L=[["league","Structures"],["board","Joueurs"],["live","En game"],["coach","Coach"],["postes","Postes"],["recrut","Recrutement"],["agenda","Agenda"],["matches","Matchs"],["trophies","Trophées"],["records","Records"],["charts","Graphiques"],["lhall","Palmarès"]];
const TABS_S=[["sfiche","Fiche"],["teams","Équipes"],["board","Classement"],["live","En game"],["coach","Coach"],["postes","Postes"],["agenda","Agenda"],["matches","Matchs"],["trophies","Trophées"],["wrapped","Wrapped"],["records","Records"],["charts","Graphiques"],["duel","Duel"],["games","Parties"],["duos","Duos"],["hall","Palmarès"]];
function render(){
  const y=scrollY;
  $("hero").style.display=location.hash==="#admin"?"none":"";
  if(location.hash==="#admin"){ $("live").replaceChildren(); $("hero").replaceChildren(); $("scopes").replaceChildren();
    $("tabs").replaceChildren(el("button",{"aria-current":"page"},"Admin"),el("button",{onclick:()=>{ if(AD.dirty&&!confirm("Quitter sans enregistrer ?")) return; location.hash=""; }},"Retour au site"));
    const m=$("main"); m.replaceChildren(); admin(m); scrollTo(0,y); return; }
  const tabs=MULTI?(S.scope==="league"?TABS_L:TABS_S):TABS; if(!tabs.some(t=>t[0]===S.tab)) S.tab=tabs[0][0];
  $("scopes").replaceChildren(...[scopeBar()].filter(Boolean));
  $("live").replaceChildren(...[liveBanner()].filter(Boolean));
  renderHero();
  $("tabs").replaceChildren(...tabs.map(([k,n])=>el("button",{"aria-current":S.tab===k?"page":null,onclick:()=>{S.tab=k; render();}},n,k==="live"&&liveGames().length?el("span",{class:"ldot","aria-label":"partie en cours"}):null)));
  const m=$("main"); m.replaceChildren();
  if(D.demo) m.append(el("div",{class:"demo"},MULTI?"Exemple avec des structures, des joueurs et des parties inventés.":"Exemple avec des joueurs et des parties inventés. Mets ta clé API dans config.json et relance le programme sans --demo pour voir vos vraies parties."));
  ({board,trophies,wrapped,records,charts,duel,games,duos,hall,league,sfiche,teams,matches,lhall:leagueHall,postes,recrut,agenda,coach:coachTab,live:liveTab})[S.tab](m);
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
  const rows=[]; for(const g of gamesFor(S.month,S.q)) for(const p of g.parts) if(isF(p)) rows.push({g,p,kda:(p.k+p.a)/Math.max(1,p.d),csm:p.cs/(g.dur/60)});
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

/* =====================================================================
   LIGUE DES STRUCTURES (mode « structures » de config.json)
   ===================================================================== */
const MULTI=D.mode==="structures";
const STS=D.structures||[];
const SBY=Object.fromEntries(STS.map(s=>[s.id,s]));
const PTS=D.points||{victoire:3,defaite:1,bonne_partie:1,partie_parfaite:2,quadra:2,penta:5,max_parties_par_jour:8,membres_comptes:10};
function setScope(id,keepTab){
  S.scope=MULTI&&SBY[id]?id:"league";
  PL=S.scope==="league"?ALL:ALL.filter(p=>(p.structs||[]).includes(S.scope));
  SC=new Set(PL.map(p=>p.puuid));
  const ok=v=>v&&SC.has(v);
  if(!ok(S.cp)) S.cp="all"; if(!ok(S.gp)) S.gp="all"; if(!ok(S.wp)) S.wp=PL[0]?.puuid;
  if(!ok(S.da)) S.da=PL[0]?.puuid; if(!ok(S.db)||S.db===S.da) S.db=PL.find(p=>p.puuid!==S.da)?.puuid;
  S.mp=PL.map(p=>p.puuid); S.rc="";
  if(!keepTab) S.tab=MULTI?(S.scope==="league"?"league":"sfiche"):"board";
  if(MULTI&&location.hash!=="#admin"&&!location.hash.startsWith("#j-")){ const h=S.scope==="league"?"":"#s-"+S.scope; if(location.hash!==h) history.replaceState(null,"",h||location.pathname); }
}
const logo=(st,size=40)=>st?.logo?el("img",{src:st.logo,alt:"",class:"slogo",style:`width:${size}px;height:${size}px`})
  :el("span",{class:"slogo txt",style:`width:${size}px;height:${size}px;background:${st?.couleur||"#2a3f4d"};font-size:${Math.round(size*.38)}px`},(st?.tag||"?").slice(0,3));
function structPoints(month){
  return STS.map(st=>{ const mem=new Set(st.membres), per={};
    for(const g of D.games.filter(g=>month==="all"||g.month===month).sort((a,b)=>a.date-b.date)){
      const day=new Date(g.date).toDateString();
      for(const p of g.parts){ if(!p.friend||!mem.has(p.puuid)) continue;
        const a=(per[p.puuid]??={pts:0,n:0,w:0,days:{}}); if((a.days[day]||0)>=PTS.max_parties_par_jour) continue; a.days[day]=(a.days[day]||0)+1;
        let v=p.win?PTS.victoire:PTS.defaite; if(p.score>=8) v+=PTS.bonne_partie; if(p.d===0&&p.k+p.a>=10) v+=PTS.partie_parfaite;
        v+=(p.penta||0)*PTS.penta+(p.quadra||0)*PTS.quadra; a.pts+=v; a.n++; a.w+=p.win?1:0; } }
    const list=Object.entries(per).map(([pu,a])=>({pu,...a})).sort((a,b)=>b.pts-a.pts);
    const top=list.slice(0,PTS.membres_comptes);
    return {st,pts:top.reduce((s,a)=>s+a.pts,0),n:list.reduce((s,a)=>s+a.n,0),w:list.reduce((s,a)=>s+a.w,0),active:list.length,best:list[0]||null,list};
  }).sort((a,b)=>b.pts-a.pts||b.w-a.w);
}
function scopeBar(){
  if(!MULTI) return null;
  return el("div",{class:"scopes"},el("div",{class:"wrap"},
    el("button",{class:"sc","aria-pressed":S.scope==="league"?"true":"false",onclick:()=>{setScope("league"); render(); scrollTo(0,0);}},el("span",{class:"slogo txt",style:"width:26px;height:26px;background:var(--gold);color:var(--night);font-size:13px"},"★"),"La ligue"),
    STS.map(st=>el("button",{class:"sc","aria-pressed":S.scope===st.id?"true":"false",onclick:()=>{setScope(st.id); render(); scrollTo(0,0);}},logo(st,26),st.nom))));
}
function leagueHero(h){
  const R=structPoints(S.month), lead=R[0]&&R[0].pts?R[0]:null;
  const sel=el("select",{"aria-label":"Mois",onchange:e=>{S.month=e.target.value; render();}},[...months,"all"].map(m=>el("option",{value:m},monthName(m)))); sel.value=S.month;
  const bestP=lead?.best;
  if(bestP&&P[bestP.pu]){ const top=aggregate(S.month,null)[bestP.pu]?.topChamp; if(top) h.append(el("div",{class:"bg",style:`background-image:url('${splash(top[0])}')`})); }
  h.append(el("div",{class:"wrap"},el("div",{class:"league"},D.ligue||"La ligue des structures"),el("div",{class:"month"},sel),
    lead?el("div",{class:"mvp"},logo(lead.st,76),el("div",{},el("div",{class:"what"},S.month==="all"?"En tête sur la période":"Structure du mois"),el("div",{class:"who"},lead.st.nom),
      el("div",{class:"line"},el("span",{},el("b",{},fmt(lead.pts)),"points"),el("span",{},el("b",{},lead.w),"victoires"),el("span",{},el("b",{},lead.active),"joueurs actifs"))))
    :el("p",{class:"empty"},"Pas encore de partie classée ce mois-ci : le premier qui gagne met sa structure en tête.")));
}
/* ---------- onglet Structures (ligue) ---------- */
function league(m){
  const R=structPoints(S.month);
  const db=duelsBlock(); if(db) m.append(db);
  m.append(el("h2",{},"Classement des structures"),el("p",{class:"lead"},`${monthName(S.month)}. Touche une structure pour ouvrir sa page.`),
    el("div",{class:"table"},R.map((x,i)=>el("button",{class:`row${x.pts&&i<3?" r"+(i+1):""}`,onclick:()=>{setScope(x.st.id); render(); scrollTo(0,0);}},
      el("div",{class:"rk"},x.pts?i+1:"–"),
      el("div",{},el("div",{class:"pl"},logo(x.st,44),el("div",{},el("div",{class:"n"},x.st.nom),el("span",{class:"tier",style:`color:${x.st.couleur}`},x.st.tag+(x.st.recrutement?", recrute":"")))),
        el("div",{class:"strip"},el("span",{},el("b",{},`${x.w}-${x.n-x.w}`),"V-D"),el("span",{},el("b",{},x.active),"joueurs actifs"),
          x.best?el("span",{},el("b",{},P[x.best.pu]?.name),`meilleur contributeur, ${x.best.pts} pts`):null)),
      el("div",{class:"score"},el("b",{},fmt(x.pts)),el("small",{},"points"))))),
    el("p",{class:"foot"},`Calcul des points : chaque partie classée (solo/duo ou flex) d'un membre rapporte ${PTS.victoire} points pour une victoire et ${PTS.defaite} pour une défaite, plus ${PTS.bonne_partie} si sa note est de 8 ou plus, ${PTS.partie_parfaite} pour une partie sans mourir (10 kills + assistances au moins), ${PTS.quadra} par quadrakill et ${PTS.penta} par pentakill. Au plus ${PTS.max_parties_par_jour} parties comptées par joueur et par jour, et seuls les ${PTS.membres_comptes} meilleurs membres de chaque structure comptent : une petite structure active peut battre une grosse. Remise à zéro chaque mois.`));
  m.append(el("div",{class:"qtitle"},"Les structures"),el("div",{class:"sgrid"},STS.map(st=>el("button",{class:"scard",style:`--c:${st.couleur}`,onclick:()=>{setScope(st.id); render(); scrollTo(0,0);}},
    logo(st,52),el("div",{},el("b",{},st.nom),el("div",{class:"mut",style:"font-size:13px"},`${st.membres.length} joueurs, ${st.equipes.length} équipe${st.equipes.length>1?"s":""}`),
      st.recrutement?el("span",{class:"badge"},"Recrute"):null)))));
}
/* ---------- fiche d'une structure ---------- */
function sfiche(m){
  const st=SBY[S.scope]; const R=structPoints(S.month), me=R.find(x=>x.st.id===st.id), place=R.indexOf(me)+1;
  const A=aggregate(S.month,null), act=ranked(A).sort((a,b)=>b.note-a.note);
  const soloAvg=PL.map(p=>p.ranks?.solo).filter(Boolean).map(r=>["IRON","BRONZE","SILVER","GOLD","PLATINUM","EMERALD","DIAMOND"].includes(r.tier)?["IRON","BRONZE","SILVER","GOLD","PLATINUM","EMERALD","DIAMOND"].indexOf(r.tier)*400+({IV:0,III:1,II:2,I:3}[r.div]||0)*100+r.lp:2800+r.lp);
  const avg=soloAvg.length?soloAvg.reduce((a,b)=>a+b,0)/soloAvg.length:null;
  const champs={}; for(const g of gamesFor(S.month,null)) for(const p of g.parts) if(isF(p)) champs[p.champ]=(champs[p.champ]||0)+1;
  const topC=Object.entries(champs).sort((a,b)=>b[1]-a[1]).slice(0,6);
  m.append(...[el("div",{class:"fiche",style:`--c:${st.couleur}`},logo(st,96),el("div",{},el("h2",{style:"margin:0"},st.nom),el("div",{class:"mut"},st.tag),
      st.description?el("p",{style:"margin:8px 0 0;max-width:62ch"},st.description):null,
      el("div",{class:"links"},st.discord?el("a",{class:"btn gold",href:st.discord,target:"_blank",rel:"noopener"},"Rejoindre le Discord"):null,
        Object.entries(st.reseaux||{}).map(([n,u])=>el("a",{class:"btn",href:u,target:"_blank",rel:"noopener"},n)),
        el("span",{class:st.recrutement?"badge":"badge off"},st.recrutement?"Recrutement ouvert":"Recrutement fermé")))),
    el("div",{class:"tiles",style:"margin-top:18px"},
      [[place&&me.pts?ord(place):"–",`au classement des structures (${monthName(S.month).toLowerCase()})`],[fmt(me?.pts||0),"points ce mois-ci"],
       [`${me?.w||0}-${(me?.n||0)-(me?.w||0)}`,"victoires-défaites"],[String(st.membres.length),"joueurs suivis"],
       [avg!=null?lpLabel(avg):"–","rang solo moyen"],[act[0]?P[act[0].puuid].name:"–","joueur du mois"]].map(([v,l])=>el("div",{class:"tile"},el("b",{},v),el("span",{},l)))),
    topC.length?[el("div",{class:"qtitle"},"Champions les plus joués"),el("div",{class:"champrow"},topC.map(([c,n])=>el("span",{},champImg(c),`${c} `,el("span",{class:"mut"},n))))]:null,
    duelsBlock(st.id),
    el("div",{class:"qtitle"},"Lien de la page"),el("p",{class:"mut",style:"font-size:13px;word-break:break-all"},location.href.split("#")[0]+"#s-"+st.id)].flat(9).filter(Boolean));
}
/* ---------- équipes ---------- */
function teams(m){
  const st=SBY[S.scope], A=aggregate(S.month,null);
  m.append(el("h2",{},"Équipes"),el("p",{class:"lead"},`Rosters de ${st.nom}. Stats ranked de ${monthName(S.month).toLowerCase()}. Touche un joueur pour son profil.`));
  const inTeam=new Set();
  const row=(pu,riot,poste,role)=>{ const p=P[pu], a=pu?A[pu]:null; if(pu) inTeam.add(pu);
    return el(pu?"button":"div",{class:"rrow",onclick:pu?()=>openProfile(pu):null},
      el("span",{class:"post"},({TOP:"Top",JUNGLE:"Jgl",MIDDLE:"Mid",BOTTOM:"ADC",UTILITY:"Sup"})[poste]||poste||"–"),
      pu?avatar(pu):el("span",{class:"slogo txt",style:"width:38px;height:38px;background:var(--ink2)"},"?"),
      el("div",{},el("b",{},p?p.name:riot),role&&role!=="titulaire"?el("span",{class:"mut"},` ${role}`):null,el("div",{},p?tierTxt(p.ranks?.solo):el("span",{class:"tier loss"},"compte introuvable"))),
      el("div",{class:"rs"},a?.games?[el("b",{},fmt(a.note,1)),el("small",{},`${pl(a.games,"partie","parties")}, ${fmt(a.wr)} %`)]:el("small",{class:"mut"},"pas de partie"))); };
  for(const t of st.equipes) m.append(el("div",{class:"team"},el("div",{class:"th"},el("h3",{},t.nom),t.coach?el("span",{class:"mut"},`Coach : ${t.coach}`):null),
    t.joueurs.map(j=>row(j.puuid,j.riot,j.poste,j.role)),teamStats(t)));
  const others=st.membres.filter(pu=>!inTeam.has(pu));
  if(others.length) m.append(el("div",{class:"team"},el("div",{class:"th"},el("h3",{},"Autres membres")),others.map(pu=>row(pu,"",P[pu]?.pos||"",""))));
}
/* ---------- matchs entre structures ---------- */
function matches(m){
  const all=(D.matchs||[]).filter(x=>S.scope==="league"||x.a===S.scope||x.b===S.scope).slice().sort((a,b)=>String(b.date).localeCompare(String(a.date)));
  m.append(el("h2",{},"Matchs entre structures"),el("p",{class:"lead"},"Scrims et matchs officiels, saisis dans la liste « matchs » de config.json."));
  if(!all.length){ m.append(el("p",{class:"empty-state"},"Aucun match enregistré pour l'instant.")); return; }
  const win=x=>{ const [a,b]=String(x.score||"").split(/[-:]/).map(Number); return a>b?x.a:b>a?x.b:null; };
  // bilan
  const rec={}; for(const x of all){ const w=win(x); for(const s of [x.a,x.b]){ const r=(rec[s]??={w:0,l:0}); if(w===s) r.w++; else if(w) r.l++; } }
  const order=Object.entries(rec).filter(([id])=>S.scope==="league"||id!==S.scope).sort((a,b)=>b[1].w-a[1].w||a[1].l-b[1].l);
  if(S.scope==="league") m.append(el("div",{class:"qtitle",style:"margin-top:0"},"Bilan"),el("div",{class:"table"},order.map(([id,r])=>el("div",{class:"duo"},logo(SBY[id],38),el("b",{},SBY[id]?.nom||id),el("div",{class:"wr"},`${r.w}-${r.l}`)))));
  else { const vs={}; for(const x of all){ const o=x.a===S.scope?x.b:x.a; const w=win(x); const r=(vs[o]??={w:0,l:0}); if(w===S.scope) r.w++; else if(w) r.l++; }
    m.append(el("div",{class:"qtitle",style:"margin-top:0"},"Confrontations"),el("div",{class:"table"},Object.entries(vs).map(([o,r])=>el("div",{class:"duo"},logo(SBY[o],38),el("b",{},`contre ${SBY[o]?.nom||o}`),el("div",{class:`wr ${r.w>=r.l?"win":"loss"}`},`${r.w}-${r.l}`))))); }
  m.append(el("div",{class:"qtitle"},"Résultats"),el("div",{class:"table"},all.map(x=>{ const w=win(x); const [sa,sb]=String(x.score||"").split(/[-:]/);
    return el("div",{class:"match"},el("div",{class:"md"},new Date(x.date).toLocaleDateString("fr-FR",{day:"numeric",month:"short",year:"numeric"}),el("br"),el("span",{class:"mut"},x.type||"")),
      el("div",{class:"side a"+(w===x.a?" w":"")},el("span",{},SBY[x.a]?.nom||x.a),logo(SBY[x.a],34)),
      el("div",{class:"sc2"},`${sa??"?"} - ${sb??"?"}`),
      el("div",{class:"side b"+(w===x.b?" w":"")},logo(SBY[x.b],34),el("span",{},SBY[x.b]?.nom||x.b))); })));
}
/* ---------- palmarès de la ligue ---------- */
function leagueHall(m){
  m.append(el("h2",{},"Palmarès de la ligue"),el("p",{class:"lead"},"La structure du mois, mois après mois."));
  const titles={};
  m.append(el("div",{class:"table"},months.map(mo=>{ const R=structPoints(mo), w=R[0]?.pts?R[0]:null; if(w&&mo!==nowKey) titles[w.st.id]=(titles[w.st.id]||0)+1;
    return el("div",{class:"hall"},el("div",{class:"m"},monthName(mo)),w?el("div",{class:"w"},logo(w.st,40),el("div",{},el("b",{},w.st.nom),el("span",{class:"mut",style:"font-size:13px"},`${w.pts} points`))):el("div",{class:"mut"},"Pas de vainqueur"),
      R[1]?.pts?el("div",{class:"mut",style:"font-size:13px"},`2e : ${R[1].st.nom} (${R[1].pts})`):el("div")); })));
  const t=Object.entries(titles).sort((a,b)=>b[1]-a[1]);
  if(t.length) m.append(el("div",{class:"qtitle"},"Titres de structure du mois (mois terminés)"),el("div",{class:"table"},t.map(([id,n])=>el("div",{class:"duo"},logo(SBY[id],38),el("b",{},SBY[id].nom),el("div",{class:"wr gold"},n)))));
}

/* ---------- duels de structures ---------- */
function pointsWindow(st,t0,t1){
  const mem=new Set(st.membres), per={};
  for(const g of D.games.filter(g=>g.date>=t0&&g.date<t1).sort((a,b)=>a.date-b.date)){ const day=new Date(g.date).toDateString();
    for(const p of g.parts){ if(!p.friend||!mem.has(p.puuid)) continue; const a=(per[p.puuid]??={pts:0,days:{}});
      if((a.days[day]||0)>=PTS.max_parties_par_jour) continue; a.days[day]=(a.days[day]||0)+1;
      let v=p.win?PTS.victoire:PTS.defaite; if(p.score>=8) v+=PTS.bonne_partie; if(p.d===0&&p.k+p.a>=10) v+=PTS.partie_parfaite; a.pts+=v+(p.penta||0)*PTS.penta+(p.quadra||0)*PTS.quadra; } }
  return Object.values(per).map(a=>a.pts).sort((a,b)=>b-a).slice(0,PTS.membres_comptes).reduce((s,v)=>s+v,0);
}
const dayStart=d=>{ const [y,m,dd]=d.split("-").map(Number); return new Date(y,m-1,dd).getTime(); };
const dfmt=(d,o={day:"numeric",month:"short"})=>new Date(dayStart(d)).toLocaleDateString("fr-FR",o);
function duelsBlock(onlyId){
  const list=(D.duels||[]).filter(d=>SBY[d.a]&&SBY[d.b]&&(!onlyId||d.a===onlyId||d.b===onlyId));
  if(!list.length) return null;
  const now=Date.now();
  const rows=list.map(d=>{ const t0=dayStart(d.debut), t1=dayStart(d.fin)+864e5; const st=now<t0?"soon":now>=t1?"done":"live";
    const pa=st==="soon"?0:pointsWindow(SBY[d.a],t0,t1), pb=st==="soon"?0:pointsWindow(SBY[d.b],t0,t1); return {d,st,pa,pb,t0}; })
    .sort((x,y)=>({live:0,soon:1,done:2})[x.st]-({live:0,soon:1,done:2})[y.st]||y.t0-x.t0);
  return el("section",{class:"block"},el("h3",{},"Duels de structures"),el("p",{class:"lead"},"Deux structures s'affrontent sur une période : celle qui marque le plus de points (même barème que le classement) gagne."),
    el("div",{class:"duels"},rows.map(({d,st,pa,pb})=>{ const tot=pa+pb||1, wa=st==="done"&&pa>pb, wb=st==="done"&&pb>pa;
      return el("div",{class:"duelc "+st},el("div",{class:"dh"},el("span",{class:"tag-"+st},st==="live"?"En cours":st==="soon"?"À venir":"Terminé"),
          el("span",{class:"mut"},`${d.titre?d.titre+", ":""}du ${dfmt(d.debut)} au ${dfmt(d.fin)}`)),
        el("div",{class:"dv"},el("button",{class:"ds"+(wa?" w":""),onclick:()=>{setScope(d.a); render(); scrollTo(0,0);}},logo(SBY[d.a],40),el("b",{},SBY[d.a].nom),el("span",{class:"num"},fmt(pa))),
          el("span",{class:"vs2"},"VS"),
          el("button",{class:"ds r"+(wb?" w":""),onclick:()=>{setScope(d.b); render(); scrollTo(0,0);}},el("span",{class:"num"},fmt(pb)),el("b",{},SBY[d.b].nom),logo(SBY[d.b],40))),
        st!=="soon"?el("div",{class:"dbar"},el("i",{style:`width:${pa/tot*100}%;background:${SBY[d.a].couleur}`}),el("i",{style:`width:${pb/tot*100}%;background:${SBY[d.b].couleur}`})):null); })));
}

/* ---------- classement par poste + équipe type ---------- */
const POSTES=[["TOP","Top"],["JUNGLE","Jungle"],["MIDDLE","Mid"],["BOTTOM","ADC"],["UTILITY","Support"]];
function byPost(month,q){
  const R={}; for(const [k] of POSTES) R[k]={};
  for(const g of gamesFor(month,q)) for(const p of g.parts){ if(!isF(p)||!R[p.pos]) continue;
    const a=(R[p.pos][p.puuid]??={pu:p.puuid,n:0,w:0,s:0,k:0,d:0,a:0,ch:{}}); a.n++; a.w+=p.win?1:0; a.s+=p.score; a.k+=p.k; a.d+=p.d; a.a+=p.a; a.ch[p.champ]=(a.ch[p.champ]||0)+1; }
  const out={}; for(const [k] of POSTES) out[k]=Object.values(R[k]).map(a=>({...a,note:a.s/a.n,wr:a.w/a.n*100,kda:(a.k+a.a)/Math.max(1,a.d),top:Object.entries(a.ch).sort((x,y)=>y[1]-x[1])[0][0]}))
    .sort((x,y)=>(y.n>=D.minGames)-(x.n>=D.minGames)||y.note-x.note);
  return out;
}
function postes(m){
  const B=byPost(S.month,S.q);
  m.append(el("h2",{},`Postes, ${QN[S.q].toLowerCase()}`),el("p",{class:"lead"},`${monthName(S.month)}. Classés par note moyenne sur les parties jouées à ce poste (${D.minGames} parties minimum pour être classé).`));
  const team=POSTES.map(([k,n])=>{ let p=B[k].find(a=>a.n>=D.minGames), few=false; if(!p&&B[k].length){ p=B[k].slice().sort((a,b)=>b.note-a.note)[0]; few=true; } return {k,n,p,few}; });
  m.append(el("div",{class:"qtitle",style:"margin-top:0"},"Équipe type du mois"),el("div",{class:"dream"},team.map(({n,p,few})=>el(p?"button":"div",{class:"dcard",onclick:p?()=>openProfile(p.pu):null,style:p?`background-image:linear-gradient(180deg,rgba(12,24,32,.15),rgba(12,24,32,.95) 75%),url('${splash(p.top)}')`:""},
    el("span",{class:"dpos"},n),p?[avatar(p.pu),el("b",{},P[p.pu].name),el("small",{},`${fmt(p.note,1)} de note, ${pl(p.n,"partie","parties")}${few?" (provisoire)":""}`),
      MULTI&&P[p.pu].structs?.[0]?el("small",{class:"gold"},SBY[P[p.pu].structs[0]]?.tag||""):null]:el("small",{class:"mut"},"Personne")))));
  for(const [k,n] of POSTES){ const L=B[k]; if(!L.length) continue;
    m.append(el("div",{class:"qtitle"},n),el("div",{class:"table"},L.slice(0,10).map((a,i)=>{ const ok=a.n>=D.minGames;
      return el("button",{class:`row${ok&&i<3?" r"+(i+1):""}${ok?"":" out"}`,onclick:()=>openProfile(a.pu)},el("div",{class:"rk"},ok?i+1:"–"),
        el("div",{},el("div",{class:"pl"},avatar(a.pu),el("div",{},el("div",{class:"n"},P[a.pu].name),
            MULTI?el("span",{class:"tier mut"},(P[a.pu].structs||[]).map(s=>SBY[s]?.nom).join(", ")):tierTxt(P[a.pu].ranks?.[S.q]))),
          el("div",{class:"strip"},el("span",{},el("b",{},`${a.w}-${a.n-a.w}`),"V-D"),el("span",{},el("b",{},fmt(a.kda,2)),"KDA"),el("span",{},el("b",{},a.top),"le plus joué"))),
        el("div",{class:"score"},el("b",{},fmt(a.note,1)),el("small",{},"note"))); }))); }
}

/* ---------- recrutement ---------- */
function recrut(m){
  const R=structPoints(S.month); const L=STS.filter(s=>s.recrutement);
  m.append(el("h2",{},"Recrutement"),el("p",{class:"lead"},"Les structures qui cherchent des joueurs en ce moment. Contacte-les sur leur Discord."));
  if(!L.length){ m.append(el("p",{class:"empty-state"},"Aucune structure ne recrute pour l'instant.")); return; }
  m.append(el("div",{class:"recs"},L.map(st=>{ const r=R.findIndex(x=>x.st.id===st.id)+1;
    return el("div",{class:"recc",style:`--c:${st.couleur}`},el("div",{class:"rh"},logo(st,56),el("div",{},el("b",{},st.nom),el("div",{class:"mut",style:"font-size:13px"},`${st.membres.length} joueurs, ${ord(r)} au classement du mois`))),
      el("div",{class:"rl"},el("span",{class:"mut"},"Postes recherchés"),el("div",{class:"chipsr"},(st.postes_recherches?.length?st.postes_recherches:["Tous"]).map(x=>el("span",{},POS[x]||x)))),
      st.niveau?el("div",{class:"rl"},el("span",{class:"mut"},"Niveau"),el("b",{},st.niveau)):null,
      st.contact?el("div",{class:"rl"},el("span",{class:"mut"},"Contact"),el("span",{},st.contact)):null,
      el("div",{class:"links"},st.discord?el("a",{class:"btn gold",href:st.discord,target:"_blank",rel:"noopener"},"Rejoindre le Discord"):null,
        el("button",{class:"btn",onclick:()=>{setScope(st.id); render(); scrollTo(0,0);}},"Voir la structure"))); })));
}

/* ---------- agenda ---------- */
function agenda(m){
  const today=new Date(); today.setHours(0,0,0,0);
  const L=(D.agenda||[]).filter(x=>S.scope==="league"||x.a===S.scope||x.b===S.scope).filter(x=>dayStart(x.date)>=today.getTime())
    .sort((a,b)=>(a.date+(a.heure||"")).localeCompare(b.date+(b.heure||"")));
  m.append(el("h2",{},"Agenda"),el("p",{class:"lead"},"Les prochains scrims et matchs entre structures. Un rappel part sur Discord la veille."));
  if(!L.length){ m.append(el("p",{class:"empty-state"},"Rien de prévu pour l'instant.")); return; }
  let last="";
  for(const x of L){ if(x.date!==last){ last=x.date; m.append(el("div",{class:"qtitle"},cap(dfmt(x.date,{weekday:"long",day:"numeric",month:"long"})))); }
    m.append(el("div",{class:"match"},el("div",{class:"md"},el("b",{class:"num",style:"font-size:20px"},x.heure||"–"),el("br"),el("span",{class:"mut"},x.type||"")),
      el("div",{class:"side a w"},el("span",{},SBY[x.a]?.nom||x.a),logo(SBY[x.a],34)),el("div",{class:"sc2",style:"font-size:18px;color:var(--gold)"},"VS"),
      el("div",{class:"side b w"},logo(SBY[x.b],34),el("span",{},SBY[x.b]?.nom||x.b)),
      x.note?el("div",{class:"mut",style:"grid-column:1/-1;font-size:13px"},x.note):null)); }
}

/* ---------- stats d'équipe ---------- */
function teamStats(t){
  const ids=t.joueurs.map(j=>j.puuid).filter(Boolean); if(ids.length<2) return null;
  const starters=t.joueurs.filter(j=>j.puuid&&(!j.role||j.role==="titulaire")).map(j=>j.puuid);
  const pos=Object.fromEntries(t.joueurs.filter(j=>j.puuid).map(j=>[j.puuid,j.poste]));
  const pairs={}; let full=0,fullW=0;
  for(const g of gamesFor(S.month,null)){ const here=g.parts.filter(p=>p.friend&&ids.includes(p.puuid));
    for(let i=0;i<here.length;i++) for(let j=i+1;j<here.length;j++){ if(here[i].team!==here[j].team) continue;
      const k=[here[i].puuid,here[j].puuid].sort().join("|"); const r=(pairs[k]??={n:0,w:0}); r.n++; r.w+=here[i].win?1:0; }
    if(g.q==="flex"&&starters.length>=5){ const st=here.filter(p=>starters.includes(p.puuid)); if(st.length>=5&&st.every(p=>p.team===st[0].team)){ full++; fullW+=st[0].win?1:0; } } }
  const LANES=[["TOP","JUNGLE","Top + jungle"],["MIDDLE","JUNGLE","Mid + jungle"],["BOTTOM","UTILITY","Duo bot"]];
  const lane=LANES.map(([a,b,n])=>{ const pa=ids.find(i=>pos[i]===a&&starters.includes(i)), pb=ids.find(i=>pos[i]===b&&starters.includes(i)); if(!pa||!pb) return null;
    const r=pairs[[pa,pb].sort().join("|")]||{n:0,w:0}; return {lab:n,pa,pb,...r}; }).filter(Boolean);
  const best=Object.entries(pairs).filter(([,r])=>r.n>=2).sort((a,b)=>b[1].w/b[1].n-a[1].w/a[1].n||b[1].n-a[1].n).slice(0,3);
  return el("div",{class:"tstats"},
    el("div",{class:"tile"},el("b",{},full?`${fmt(fullW/full*100)} %`:"–"),el("span",{},full?`de victoires quand les 5 titulaires jouent ensemble en flex (${full} parties)`:"les 5 titulaires n'ont pas encore joué ensemble en flex")),
    lane.map(l=>el("div",{class:"tile"},el("b",{},l.n?`${fmt(l.w/l.n*100)} %`:"–"),el("span",{},`${l.lab} : ${P[l.pa].name} et ${P[l.pb].name}, ${l.n?`${l.n} partie${l.n>1?"s":""} ensemble`:"jamais ensemble"}`))),
    best.length?el("div",{class:"tile wide"},el("span",{},"Meilleurs duos de l'équipe"),best.map(([k,r])=>{ const [a,b]=k.split("|"); return el("div",{},el("b",{style:"font-size:17px"},`${P[a].name} + ${P[b].name}`),` ${fmt(r.w/r.n*100)} % sur ${r.n} parties`); })):null);
}

/* ---------- fiche joueur publique ---------- */
const slug=p=>(p.name+"-"+p.tag).toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g,"").replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"");
function withAll(fn){ const a=PL,b=SC; PL=ALL; SC=new Set(ALL.map(p=>p.puuid)); try{ return fn(); } finally{ PL=a; SC=b; } }
function palmares(pu){
  return withAll(()=>{ const out=[];
    for(const mo of months) for(const q of ["solo","flex"]){ const R=ranked(aggregate(mo,q)); if(!R.length) continue;
      for(const x of R){ x.mk=x.penta*100+x.quadra*10+x.triple; x.otp=x.topChamp?.[1]||0; }
      for(const [e,n,type,key,dir] of TROPHIES){ const l=R.filter(z=>(z[key]||0)!==0||dir<0).sort((u,v)=>(v[key]-u[key])*dir); if(l[0]?.puuid===pu) out.push({e,n,type,mo,q}); } }
    return out; });
}
function openFromHash(){ const m=location.hash.match(/^#j-(.+)$/); if(!m) return false; const p=ALL.find(x=>slug(x)===m[1]); if(p) openProfile(p.puuid); return !!p; }

/* =====================================================================
   PANNEAU D'ADMIN (#admin) : modifie config.json directement sur GitHub
   ===================================================================== */
const LS={get:k=>{try{return localStorage.getItem(k)||"";}catch(e){return "";}},set:(k,v)=>{try{localStorage.setItem(k,v);}catch(e){}}};
const AD={owner:LS.get("lp_owner")||(location.hostname.endsWith(".github.io")?location.hostname.split(".")[0]:""),
  repo:LS.get("lp_repo")||(location.hostname.endsWith(".github.io")?(location.pathname.split("/")[1]||""):""),
  token:LS.get("lp_token"),cfg:null,sha:null,dirty:false,msg:"",err:false,edit:"",busy:false};
const cfgText=()=>JSON.stringify(AD.cfg,(k,v)=>k.startsWith("_")?undefined:v,2);
const AOPEN={};
const acard=(key,title,def,...kids)=>el("details",{class:"acard",open:(AOPEN[key]??def)||null,ontoggle:e=>AOPEN[key]=e.target.open},el("summary",{},title),...kids);
const b64e=s=>{ const b=new TextEncoder().encode(s); let out=""; for(let i=0;i<b.length;i+=0x8000) out+=String.fromCharCode(...b.subarray(i,i+0x8000)); return btoa(out); };
const b64d=b=>new TextDecoder().decode(Uint8Array.from(atob(b.replace(/\s/g,"")),c=>c.charCodeAt(0)));
const API=p=>`https://api.github.com/repos/${AD.owner}/${AD.repo}${p}`;
const hdr=()=>({"Accept":"application/vnd.github+json",...(AD.token?{"Authorization":"Bearer "+AD.token}:{}),"X-GitHub-Api-Version":"2022-11-28"});
function adMsg(t,err){ AD.msg=t; AD.err=!!err; render(); }
async function adLoad(){
  if(!AD.owner||!AD.repo) return adMsg("Indique ton pseudo GitHub et le nom du référentiel.",true);
  LS.set("lp_owner",AD.owner); LS.set("lp_repo",AD.repo); LS.set("lp_token",AD.token);
  AD.busy=true; adMsg("Chargement de config.json…");
  try{
    if(AD.token){ const r=await fetch(API("/contents/config.json"),{headers:hdr(),cache:"no-store"});
      if(r.status===401) throw new Error("jeton refusé (vérifie qu'il est copié en entier et pas expiré)");
      if(r.status===404) throw new Error("config.json introuvable (vérifie le pseudo, le référentiel et que le jeton a accès à ce référentiel)");
      if(!r.ok) throw new Error("GitHub a répondu "+r.status);
      const j=await r.json(); AD.sha=j.sha; AD.cfg=JSON.parse(b64d(j.content)); }
    else { const r=await fetch(`https://raw.githubusercontent.com/${AD.owner}/${AD.repo}/main/config.json?t=${Date.now()}`);
      if(!r.ok) throw new Error("config.json introuvable ("+r.status+")"); AD.cfg=JSON.parse(await r.text()); AD.sha=null; }
    AD.cfg.structures??=[]; AD.cfg.matchs??=[]; AD.cfg.duels??=[]; AD.cfg.joueurs??=[];
    AD.dirty=false; AD.busy=false; adMsg(AD.token?"config.json chargé. Tu peux modifier puis enregistrer.":"config.json chargé en lecture seule : ajoute ton jeton GitHub pour pouvoir enregistrer.");
  }catch(e){ AD.busy=false; adMsg("Impossible de charger : "+e.message,true); }
}
function adCheck(){
  const c=AD.cfg, errs=[], ids=new Set();
  for(const s of c.structures){ if(!/^[a-z0-9-]+$/.test(s.id||"")) errs.push(`Structure « ${s.nom} » : l'identifiant doit être en minuscules, sans espace ni accent.`);
    if(ids.has(s.id)) errs.push(`Identifiant en double : ${s.id}`); ids.add(s.id);
    for(const t of s.equipes||[]) for(const j of t.joueurs||[]) if(!String(j.riot||"").includes("#")) errs.push(`« ${j.riot||"(vide)"} » dans ${s.nom} : il faut le Riot ID complet (Pseudo#TAG).`);
    for(const r of s.membres||[]) if(!String(r).includes("#")) errs.push(`« ${r} » dans ${s.nom} : il faut le Riot ID complet (Pseudo#TAG).`); }
  for(const m of c.matchs) if(!ids.has(m.a)||!ids.has(m.b)) errs.push(`Un match du ${m.date} vise une structure qui n'existe plus.`);
  return errs;
}
async function adSave(){
  const errs=adCheck(); if(errs.length) return adMsg("À corriger avant d'enregistrer : "+errs.join(" "),true);
  if(!AD.token) return adMsg("Il faut ton jeton GitHub pour enregistrer (section Connexion).",true);
  AD.busy=true; adMsg("Enregistrement sur GitHub…");
  try{ const r=await fetch(API("/contents/config.json"),{method:"PUT",headers:{...hdr(),"Content-Type":"application/json"},
      body:JSON.stringify({message:"Admin : mise à jour de la ligue",content:b64e(cfgText()+"\n"),sha:AD.sha})});
    if(r.status===409) throw new Error("le fichier a changé entre-temps : recharge-le puis refais ta modification");
    if(r.status===403||r.status===401) throw new Error("le jeton n'a pas le droit d'écrire (permission « Contents : Read and write »)");
    if(!r.ok) throw new Error("GitHub a répondu "+r.status);
    const j=await r.json(); AD.sha=j.content.sha; AD.dirty=false; AD.busy=false;
    adMsg("Enregistré ✓ Le site sera à jour à la prochaine mise à jour automatique (10 min au plus), ou tout de suite avec « Mettre à jour le site ».");
  }catch(e){ AD.busy=false; adMsg("Échec de l'enregistrement : "+e.message,true); }
}
async function adRun(){
  if(!AD.token) return adMsg("Il faut ton jeton GitHub.",true);
  try{ const r=await fetch(API("/actions/workflows/maj.yml/dispatches"),{method:"POST",headers:{...hdr(),"Content-Type":"application/json"},body:JSON.stringify({ref:"main"})});
    if(r.status===403||r.status===401) throw new Error("le jeton n'a pas le droit de lancer les mises à jour (permission « Actions : Read and write »)");
    if(r.status!==204) throw new Error("GitHub a répondu "+r.status);
    adMsg("Mise à jour lancée ✓ Le site sera à jour dans 2 à 5 minutes (recharge la page ensuite).");
  }catch(e){ adMsg("Impossible de lancer la mise à jour : "+e.message,true); }
}
const chg=f=>e=>{ f(e.target.type==="checkbox"?e.target.checked:e.target.value); AD.dirty=true; render(); };
const fld=(label,input,hint)=>el("label",{class:"af"},el("span",{},label),input,hint?el("small",{},hint):null);
const inp=(val,on,attrs={})=>el("input",{value:val??"",onchange:chg(on),...attrs});
const stSel=(val,on,empty)=>{ const s=el("select",{onchange:chg(on)},empty?el("option",{value:""},empty):null,AD.cfg.structures.map(x=>el("option",{value:x.id},x.nom))); s.value=val||""; return s; };
const postSel=(val,on)=>{ const s=el("select",{onchange:chg(on)},el("option",{value:""},"–"),POSTES.map(([k,n])=>el("option",{value:k},n))); s.value=val||""; return s; };
const stName=id=>AD.cfg.structures.find(s=>s.id===id)?.nom||id;
let NM={date:new Date().toISOString().slice(0,10),heure:"",a:"",b:"",sa:"",sb:"",type:"Scrim BO3",note:""};
let ND={a:"",b:"",debut:new Date().toISOString().slice(0,10),fin:new Date(Date.now()+6*864e5).toISOString().slice(0,10),titre:""};
function admin(m){
  m.append(el("h2",{},"Panneau d'admin"),el("p",{class:"lead"},"Ajoute des matchs, des événements d'agenda, des duels et des structures. « Enregistrer » met à jour config.json sur GitHub : le site suit à la mise à jour suivante."));
  // connexion
  m.append(acard("cx",AD.cfg?"Connexion ✓":"Connexion",!AD.cfg,
    el("div",{class:"agrid"},fld("Pseudo GitHub",inp(AD.owner,v=>AD.owner=v.trim())),fld("Référentiel",inp(AD.repo,v=>AD.repo=v.trim())),
      fld("Jeton GitHub",inp(AD.token,v=>AD.token=v.trim(),{type:"password",autocomplete:"off"}),"Gardé seulement dans ce navigateur, jamais envoyé ailleurs que sur GitHub.")),
    el("div",{class:"links"},el("button",{class:"btn gold",onclick:adLoad},AD.cfg?"Recharger config.json":"Se connecter"),
      AD.token?el("button",{class:"btn",onclick:()=>{AD.token=""; LS.set("lp_token",""); render();}},"Oublier le jeton"):null),
    el("details",{style:"margin-top:10px"},el("summary",{class:"mut"},"Comment créer le jeton (une seule fois)"),
      el("ol",{class:"steps"},el("li",{},"GitHub → ta photo en haut à droite → Settings → Developer settings (tout en bas) → Personal access tokens → Fine-grained tokens → Generate new token."),
        el("li",{},"Nom : « admin ligue ». Expiration : 1 an. Repository access : Only select repositories → lol-potes."),
        el("li",{},"Permissions → Repository permissions : Contents → Read and write, et Actions → Read and write."),
        el("li",{},"Generate token, copie-le et colle-le ici. Ne le partage avec personne.")))));
  if(!AD.cfg){ return; }
  const c=AD.cfg;
  // matchs et agenda
  const mlist=c.matchs.map((x,i)=>({x,i})).sort((a,b)=>String(b.x.date).localeCompare(String(a.x.date)));
  m.append(acard("m",`Matchs et agenda (${c.matchs.length})`,true,
    el("p",{class:"mut"},"Avec un score : c'est un résultat (onglet Matchs). Sans score : c'est un événement à venir (onglet Agenda, rappel Discord la veille). Tu pourras ajouter le score plus tard."),
    el("div",{class:"agrid"},fld("Date",el("input",{type:"date",value:NM.date,onchange:e=>NM.date=e.target.value})),fld("Heure (facultatif)",el("input",{type:"time",value:NM.heure,onchange:e=>NM.heure=e.target.value})),
      fld("Structure A",(()=>{const s=el("select",{onchange:e=>NM.a=e.target.value},el("option",{value:""},"Choisir"),c.structures.map(x=>el("option",{value:x.id},x.nom))); s.value=NM.a; return s;})()),
      fld("Structure B",(()=>{const s=el("select",{onchange:e=>NM.b=e.target.value},el("option",{value:""},"Choisir"),c.structures.map(x=>el("option",{value:x.id},x.nom))); s.value=NM.b; return s;})()),
      fld("Score A",el("input",{type:"number",min:0,value:NM.sa,onchange:e=>NM.sa=e.target.value})),fld("Score B",el("input",{type:"number",min:0,value:NM.sb,onchange:e=>NM.sb=e.target.value})),
      fld("Type",el("input",{list:"mtypes",value:NM.type,onchange:e=>NM.type=e.target.value})),fld("Note (facultatif)",el("input",{value:NM.note,onchange:e=>NM.note=e.target.value}))),
    el("datalist",{id:"mtypes"},["Scrim BO1","Scrim BO3","Scrim BO5","Officiel BO1","Officiel BO3","Officiel BO5","Amical"].map(v=>el("option",{value:v}))),
    el("button",{class:"btn gold",onclick:()=>{ if(!NM.a||!NM.b||NM.a===NM.b) return adMsg("Choisis deux structures différentes.",true); if(!NM.date) return adMsg("Indique la date.",true);
      const x={date:NM.date,a:NM.a,b:NM.b,type:NM.type}; if(NM.heure) x.heure=NM.heure; if(NM.note) x.note=NM.note; if(NM.sa!==""&&NM.sb!=="") x.score=`${NM.sa}-${NM.sb}`;
      c.matchs.push(x); AD.dirty=true; NM={...NM,sa:"",sb:"",note:""}; adMsg(x.score?"Résultat ajouté (pense à enregistrer).":"Événement ajouté à l'agenda (pense à enregistrer)."); }},"Ajouter"),
    el("div",{class:"alist"},mlist.map(({x,i})=>el("div",{class:"arow"},el("span",{class:"mut"},`${x.date}${x.heure?" "+x.heure:""}`),
      el("b",{},`${stName(x.a)} ${x.score?x.score.replace("-"," - "):"vs"} ${stName(x.b)}`),el("span",{class:"mut"},x.type||""),
      !x.score&&!x._edit?el("button",{class:"btn sm",onclick:()=>{ x._edit=true; render(); }},"Saisir le score"):null,
      x._edit?el("span",{class:"inl"},el("input",{type:"number",min:0,placeholder:"A",id:"sa"+i,style:"width:56px"}),el("input",{type:"number",min:0,placeholder:"B",id:"sb"+i,style:"width:56px"}),
        el("button",{class:"btn sm gold",onclick:()=>{ const a=$("sa"+i).value,b=$("sb"+i).value; if(a===""||b==="") return; x.score=`${a}-${b}`; delete x._edit; AD.dirty=true; render(); }},"OK")):null,
      el("button",{class:"btn sm ghost",onclick:()=>{ c.matchs.splice(i,1); AD.dirty=true; render(); }},"Supprimer"))))));
  // duels
  m.append(acard("d",`Duels de structures (${c.duels.length})`,false,
    el("div",{class:"agrid"},fld("Structure A",(()=>{const s=el("select",{onchange:e=>ND.a=e.target.value},el("option",{value:""},"Choisir"),c.structures.map(x=>el("option",{value:x.id},x.nom))); s.value=ND.a; return s;})()),
      fld("Structure B",(()=>{const s=el("select",{onchange:e=>ND.b=e.target.value},el("option",{value:""},"Choisir"),c.structures.map(x=>el("option",{value:x.id},x.nom))); s.value=ND.b; return s;})()),
      fld("Début",el("input",{type:"date",value:ND.debut,onchange:e=>ND.debut=e.target.value})),fld("Fin (incluse)",el("input",{type:"date",value:ND.fin,onchange:e=>ND.fin=e.target.value})),
      fld("Titre (facultatif)",el("input",{value:ND.titre,onchange:e=>ND.titre=e.target.value}))),
    el("button",{class:"btn gold",onclick:()=>{ if(!ND.a||!ND.b||ND.a===ND.b) return adMsg("Choisis deux structures différentes.",true); if(ND.fin<ND.debut) return adMsg("La fin doit être après le début.",true);
      const d={a:ND.a,b:ND.b,debut:ND.debut,fin:ND.fin}; if(ND.titre) d.titre=ND.titre; c.duels.push(d); AD.dirty=true; adMsg("Duel ajouté (pense à enregistrer)."); }},"Ajouter le duel"),
    el("div",{class:"alist"},c.duels.map((d,i)=>el("div",{class:"arow"},el("span",{class:"mut"},`${d.debut} → ${d.fin}`),el("b",{},`${stName(d.a)} vs ${stName(d.b)}`),el("span",{class:"mut"},d.titre||""),
      el("button",{class:"btn sm ghost",onclick:()=>{ c.duels.splice(i,1); AD.dirty=true; render(); }},"Supprimer"))))));
  // structures
  const cur=c.structures.find(s=>s.id===AD.edit);
  m.append(acard("s",`Structures (${c.structures.length})`,!!AD.edit,
    el("div",{class:"links"},c.structures.map(s=>el("button",{class:"btn"+(s.id===AD.edit?" gold":""),onclick:()=>{AD.edit=s.id; AOPEN.s=true; render();}},s.nom)),
      el("button",{class:"btn",onclick:()=>{ let n=1; while(c.structures.some(s=>s.id==="structure-"+n)) n++; c.structures.push({id:"structure-"+n,nom:"Nouvelle structure",tag:"NEW",couleur:"#3cc7bf",logo:"",description:"",discord:"",recrutement:false,reseaux:{},postes_recherches:[],niveau:"",contact:"",equipes:[],membres:[]}); AD.edit="structure-"+n; AD.dirty=true; render(); }},"+ Nouvelle structure")),
    cur?structEditor(cur):el("p",{class:"mut"},"Choisis une structure à modifier.")));
  // joueurs hors structure + réglages + barème
  m.append(acard("j",`Joueurs suivis hors structure (${c.joueurs.length})`,false,
    el("div",{class:"alist"},c.joueurs.map((r,i)=>el("div",{class:"arow"},el("b",{},r),el("button",{class:"btn sm ghost",onclick:()=>{ c.joueurs.splice(i,1); AD.dirty=true; render(); }},"Retirer")))),
    el("div",{class:"inl"},el("input",{id:"newj",placeholder:"Pseudo#TAG"}),el("button",{class:"btn",onclick:()=>{ const v=$("newj").value.trim(); if(!v.includes("#")) return adMsg("Il faut le Riot ID complet : Pseudo#TAG.",true); c.joueurs.push(v); AD.dirty=true; render(); }},"Ajouter"))));
  c.points??={...PTS};
  const PN=[["victoire","Victoire"],["defaite","Défaite"],["bonne_partie","Note de 8 ou plus"],["partie_parfaite","Partie parfaite"],["quadra","Quadrakill"],["penta","Pentakill"],["max_parties_par_jour","Parties max par jour"],["membres_comptes","Membres comptés"]];
  m.append(acard("r","Réglages de la ligue et barème des points",false,
    el("div",{class:"agrid"},fld("Nom de la ligue",inp(c.nom_ligue,v=>c.nom_ligue=v)),fld("Adresse du site",inp(c.site_url,v=>c.site_url=v)),
      fld("Mois récupérés",inp(c.mois,v=>c.mois=+v||1,{type:"number",min:1,max:12})),fld("Parties min. pour être classé",inp(c.min_parties_classement,v=>c.min_parties_classement=+v||1,{type:"number",min:1}))),
    el("div",{class:"agrid"},PN.map(([k,n])=>fld(n,inp(c.points[k]??PTS[k],v=>c.points[k]=+v||0,{type:"number",min:0}))))));
  // barre d'enregistrement
  m.append(el("div",{class:"savebar"},el("div",{class:"wrap"},
    el("span",{class:AD.err?"loss":AD.dirty?"gold":"mut"},AD.msg||(AD.dirty?"Modifications non enregistrées.":"Rien à enregistrer.")),
    el("div",{class:"links",style:"margin:0"},el("button",{class:"btn gold",disabled:!AD.dirty||AD.busy,onclick:adSave},"Enregistrer sur GitHub"),
      el("button",{class:"btn",onclick:adRun,disabled:AD.busy},"Mettre à jour le site"),
      el("button",{class:"btn ghost",onclick:e=>navigator.clipboard?.writeText(cfgText()).then(()=>e.target.textContent="JSON copié ✓")},"Copier le JSON")))));
}
function structEditor(s){
  const c=AD.cfg; s.equipes??=[]; s.membres??=[]; s.postes_recherches??=[]; s.reseaux??={};
  return el("div",{class:"sedit"},
    el("div",{class:"agrid"},fld("Identifiant (lien #s-…)",inp(s.id,v=>{ const nv=v.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g,"").replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"");
        for(const m of c.matchs){ if(m.a===s.id) m.a=nv; if(m.b===s.id) m.b=nv; } for(const d of c.duels){ if(d.a===s.id) d.a=nv; if(d.b===s.id) d.b=nv; } s.id=nv; AD.edit=nv; }),"minuscules, sans espace"),
      fld("Nom",inp(s.nom,v=>s.nom=v)),fld("Tag (3 lettres)",inp(s.tag,v=>s.tag=v.toUpperCase().slice(0,4))),fld("Couleur",inp(s.couleur||"#3cc7bf",v=>s.couleur=v,{type:"color"})),
      fld("Logo (lien d'image)",inp(s.logo,v=>s.logo=v.trim()),"Clic droit sur le logo dans Discord → Copier le lien de l'image"),fld("Lien d'invitation Discord",inp(s.discord,v=>s.discord=v.trim()))),
    fld("Description",el("textarea",{rows:2,onchange:chg(v=>s.description=v)},s.description||"")),
    el("div",{class:"agrid"},fld("Recrute",el("input",{type:"checkbox",checked:!!s.recrutement,onchange:chg(v=>s.recrutement=v)})),
      fld("Niveau recherché",inp(s.niveau,v=>s.niveau=v)),fld("Contact",inp(s.contact,v=>s.contact=v))),
    el("div",{class:"af"},el("span",{},"Postes recherchés"),el("div",{class:"links",style:"margin:0"},POSTES.map(([k,n])=>el("label",{class:"chk"},el("input",{type:"checkbox",checked:s.postes_recherches.includes(k),
      onchange:chg(v=>{ s.postes_recherches=s.postes_recherches.filter(x=>x!==k); if(v) s.postes_recherches.push(k); })}),n)))),
    el("h4",{},"Équipes"),
    s.equipes.map((t,ti)=>el("div",{class:"tedit"},el("div",{class:"agrid"},fld("Nom de l'équipe",inp(t.nom,v=>t.nom=v)),fld("Coach",inp(t.coach,v=>t.coach=v))),
      (t.joueurs||=[]).map((j,ji)=>el("div",{class:"inl"},inp(j.riot,v=>j.riot=v.trim(),{placeholder:"Pseudo#TAG"}),postSel(j.poste,v=>j.poste=v),
        (()=>{ const r=el("select",{onchange:chg(v=>j.role=v)},["titulaire","remplaçant"].map(x=>el("option",{value:x},x))); r.value=j.role||"titulaire"; return r; })(),
        el("button",{class:"btn sm ghost",onclick:()=>{ t.joueurs.splice(ji,1); AD.dirty=true; render(); }},"✕"))),
      el("div",{class:"links"},el("button",{class:"btn sm",onclick:()=>{ t.joueurs.push({riot:"",poste:"",role:"titulaire"}); AD.dirty=true; render(); }},"+ Joueur"),
        el("button",{class:"btn sm ghost",onclick:()=>{ if(confirm?.(`Supprimer l'équipe « ${t.nom} » ?`)??true){ s.equipes.splice(ti,1); AD.dirty=true; render(); } }},"Supprimer l'équipe")))),
    el("button",{class:"btn",onclick:()=>{ s.equipes.push({nom:"Équipe "+(s.equipes.length+1),coach:"",joueurs:[]}); AD.dirty=true; render(); }},"+ Équipe"),
    el("h4",{},"Autres membres (hors équipe)"),
    el("div",{class:"alist"},s.membres.map((r,i)=>el("div",{class:"arow"},el("b",{},r),el("button",{class:"btn sm ghost",onclick:()=>{ s.membres.splice(i,1); AD.dirty=true; render(); }},"Retirer")))),
    el("div",{class:"inl"},el("input",{id:"newm",placeholder:"Pseudo#TAG"}),el("button",{class:"btn",onclick:()=>{ const v=$("newm").value.trim(); if(!v.includes("#")) return adMsg("Il faut le Riot ID complet : Pseudo#TAG.",true); s.membres.push(v); AD.dirty=true; render(); }},"Ajouter")),
    el("div",{class:"links",style:"margin-top:18px"},el("button",{class:"btn ghost loss",onclick:()=>{ const used=c.matchs.some(m=>m.a===s.id||m.b===s.id)||c.duels.some(d=>d.a===s.id||d.b===s.id);
      if(used) return adMsg("Cette structure a des matchs ou des duels : supprime-les d'abord.",true); c.structures.splice(c.structures.indexOf(s),1); AD.edit=""; AD.dirty=true; render(); }},"Supprimer la structure")));
}

/* ---------- le coach ---------- */
const CKEY={early_deaths:["Morts en début de partie","d14",-1,"morts avant 14 min",()=>2],isolated:["Morts loin de l'équipe","alone",-1,"morts sans allié proche",()=>1],
  enemy_jungle:["Morts dans la jungle adverse","ejg",-1,"morts dans la jungle adverse",()=>0],costly_deaths:["Morts qui coûtent des objectifs","costly",-1,"morts suivies d'un objectif perdu",()=>0],
  cs:["Farm à 10 min","cs10",1,"CS à 10 min",m=>m.cs10t],lane_lost:["Lanes perdues","gd15",1,"or à 15 min face à l'adversaire",()=>0],lead_thrown:["Avances gâchées",null],
  vision:["Vision","vpm",1,"de vision par minute",m=>m.vpmt],control_wards:["Balises de contrôle","cw",1,"balises de contrôle par partie",m=>m.cwt],
  kp:["Présence en combat","kp",1,"% de participation aux kills",m=>m.kpt],damage:["Dégâts en combat",null],first_item:["Premier objet","first",-1,"min pour le premier objet fini",()=>14],
  plates:["Plaques de tour",null]};
const GCOL={S:"var(--gold)",A:"var(--teal)",B:"#6fb3ff",C:"var(--mut)",D:"var(--red)"};
function coachCard(g,f){
  const p=g.parts.find(x=>x.pid===f.pid), c=f.coach;
  const head=el("div",{class:"h"},champImg(p.champ),el("div",{style:"flex:1"},el("b",{},p.name),el("div",{class:"mut",style:"font-size:13px"},`${POS[f.pos]||""}, ${p.k}/${p.d}/${p.a}, note ${fmt(p.score,1)}`)),
    c?el("span",{class:"grade",style:`--g:${GCOL[c.grade]}`},c.grade):null);
  const mini=el("div",{class:"mini"},el("div",{},el("b",{},f.cs10??"–"),"CS à 10 min",f.csOpp10!=null?el("div",{},`adversaire ${f.csOpp10}`):null),
    el("div",{},el("b",{class:f.gd10>=0?"win":"loss"},f.gd10!=null?(f.gd10>0?"+":"")+fmt(f.gd10):"–"),"or à 10 min",f.opp?el("div",{},"contre "+f.opp):null),
    el("div",{},el("b",{class:f.gd15>=0?"win":"loss"},f.gd15!=null?(f.gd15>0?"+":"")+fmt(f.gd15):"–"),"or à 15 min"));
  if(!c) return el("div",{class:"fcard"},head,mini,f.tips?.length?el("ul",{class:"tips"},f.tips.map(t=>el("li",{},t))):null);
  const bar=w=>{ if(w.val==null||w.target==null||typeof w.val!=="number") return null; const lower=["early_deaths","isolated","enemy_jungle","costly_deaths","first_item"].includes(w.key);
    const max=Math.max(Math.abs(w.val),Math.abs(w.target))*1.25||1, pv=Math.max(0,Math.min(100,Math.abs(w.val)/max*100)), pt=Math.max(0,Math.min(100,Math.abs(w.target)/max*100));
    return el("div",{class:"cbar",title:`toi : ${w.val}${w.unit||""}, objectif : ${w.target}${w.unit||""}`},el("i",{style:`width:${pv}%;background:${lower?"var(--red)":"var(--gold)"}`}),el("em",{style:`left:${pt}%`})); };
  return el("div",{class:"fcard coach"},head,mini,
    c.good.length?el("div",{class:"cblock"},el("div",{class:"ct good"},"Ce qui a marché"),el("ul",{class:"tips"},c.good.map(t=>el("li",{},t)))):null,
    c.work.length?el("div",{class:"cblock"},el("div",{class:"ct work"},"À travailler"),c.work.map(w=>el("div",{class:"witem"},el("b",{},w.title),bar(w),el("p",{},w.detail)))):null,
    c.moments.length?el("div",{class:"cblock"},el("div",{class:"ct"},"Moments à revoir dans le replay"),el("div",{class:"moms"},c.moments.map(x=>el("div",{},el("span",{class:"num"},mmss(x.t)),x.txt)))):null,
    el("div",{class:"drill"},el("span",{},"Pour la prochaine partie"),c.drill));
}
const mmss=t=>`${Math.floor(t)}:${String(Math.round((t%1)*60)).padStart(2,"0")}`;
function coachData(pu,month,q){
  const L=[]; for(const g of gamesFor(month,q).slice().sort((a,b)=>a.date-b.date)){ const f=g.review?.friends?.find(x=>x.puuid===pu&&x.coach); if(f) L.push({g,f,c:f.coach,m:f.coach.m,p:g.parts.find(x=>x.pid===f.pid)}); }
  return L;
}
function coachSummary(pu,month,q){
  const L=coachData(pu,month,q), box=el("div",{class:"csum"});
  if(L.length<2){ box.append(el("p",{class:"empty-state"},L.length?"Encore une partie analysée et le bilan s'affiche.":"Aucune partie avec revue détaillée sur cette période.")); return box; }
  for(const x of L) x.m.ejg=x.m.zones?.["jungle adverse"]||0;
  const cnt={}; for(const x of L) for(const k of x.m.keys||[]) cnt[k]=(cnt[k]||0)+1;
  const pri=Object.entries(cnt).filter(([k])=>CKEY[k]).sort((a,b)=>b[1]-a[1]).slice(0,3);
  const avg=(arr,k)=>{ const v=arr.map(x=>x.m[k]).filter(v=>v!=null&&isFinite(v)); return v.length?v.reduce((a,b)=>a+b,0)/v.length:null; };
  const recent=L.slice(-5), before=L.slice(0,-5);
  const grades={}; for(const x of L) grades[x.c.grade]=(grades[x.c.grade]||0)+1;
  box.append(el("div",{class:"grades"},el("span",{class:"mut"},`${L.length} parties analysées :`),["S","A","B","C","D"].filter(g=>grades[g]).map(g=>el("span",{class:"grade sm",style:`--g:${GCOL[g]}`},`${g} ×${grades[g]}`))));
  // plan : 3 priorités
  box.append(el("div",{class:"qtitle",style:"margin-top:14px"},"Le plan : 3 priorités"));
  if(!pri.length) box.append(el("p",{},"Aucun défaut récurrent sur cette période : bravo, continue comme ça."));
  pri.forEach(([k,n],i)=>{ const [title,mk,dir,lab,tf]=CKEY[k]; const r=mk?avg(recent,mk):null, b=mk&&before.length?avg(before,mk):null, exg=L.slice().reverse().find(x=>x.c.work.some(w=>w.key===k)), t=mk?tf((exg||L[L.length-1]).m):null;
    const trend=r!=null&&b!=null&&Math.abs(r-b)>0.01?((r-b)*dir>0?["▲ en progrès","win"]:["▼ en recul","loss"]):null;
    const ex=L.slice().reverse().find(x=>x.c.work.some(w=>w.key===k))?.c.work.find(w=>w.key===k);
    box.append(el("div",{class:"prio"},el("span",{class:"pn"},i+1),el("div",{},el("b",{},title),el("div",{class:"mut",style:"font-size:13px"},`Revient dans ${n} partie${n>1?"s":""} sur ${L.length}`),
      mk&&r!=null?el("div",{class:"pv"},`Tes 5 dernières parties : `,el("b",{},`${fmt(r,mk==="vpm"?2:1)} ${lab}`),t!=null?` (objectif : ${fmt(t,mk==="vpm"?1:0)})`:"",trend?el("span",{class:trend[1]},` ${trend[0]}`):null):null,
      ex?el("p",{class:"mut",style:"font-size:13px;margin:4px 0 0"},ex.detail):null))); });
  // zones de mort
  const z={}; let tot=0; for(const x of L) for(const [k,v] of Object.entries(x.m.zones||{})){ z[k]=(z[k]||0)+v; tot+=v; }
  if(tot) box.append(el("div",{class:"qtitle"},"Où tu meurs"),el("div",{class:"zbars"},Object.entries(z).sort((a,b)=>b[1]-a[1]).map(([k,v])=>el("div",{},el("span",{},k),el("i",{style:`width:${v/tot*100}%`}),el("b",{},fmt(v/tot*100)+" %")))));
  // tilt : résultat après 2 défaites d'affilée
  const all=gamesFor(month,null).slice().sort((a,b)=>a.date-b.date).map(g=>({g,p:g.parts.find(x=>x.puuid===pu)})).filter(x=>x.p);
  let after=[],streak=0,lastT=0; for(const x of all){ if(streak>=2&&x.g.date-lastT<4*3600e3) after.push(x.p.win); streak=x.p.win?0:streak+1; lastT=x.g.date+x.g.dur*1000; }
  const wrAll=all.filter(x=>x.p.win).length/Math.max(1,all.length)*100;
  if(after.length>=3){ const w=after.filter(Boolean).length/after.length*100;
    box.append(el("div",{class:"qtitle"},"Gestion du tilt"),el("p",{},`Après 2 défaites d'affilée, tu gagnes `,el("b",{class:w<wrAll-10?"loss":"win"},fmt(w)+" %"),` de tes parties (${after.length} parties), contre ${fmt(wrAll)} % en temps normal. `,
      w<wrAll-10?"Conseil : après deux défaites, fais une pause de 15 minutes ou passe en normale.":"Tu encaisses bien les défaites, rien à signaler.")); }
  // champions
  const ch={}; for(const x of all){ const r=(ch[x.p.champ]??={n:0,w:0}); r.n++; r.w+=x.p.win?1:0; }
  const cl=Object.entries(ch).filter(([,r])=>r.n>=3).map(([c,r])=>({c,...r,wr:r.w/r.n*100})).sort((a,b)=>b.wr-a.wr);
  if(cl.length>=2){ const best=cl[0], worst=cl[cl.length-1];
    box.append(el("div",{class:"qtitle"},"Tes champions"),el("p",{},el("span",{class:"ci"},champImg(best.c)),` À jouer : `,el("b",{},best.c),` (${fmt(best.wr)} % sur ${best.n} parties). `,
      worst.wr<45?[el("span",{class:"ci"},champImg(worst.c))," À mettre de côté : ",el("b",{},worst.c),` (${fmt(worst.wr)} % sur ${worst.n} parties).`]:null)); }
  // dernier exercice
  const last=L[L.length-1]; box.append(el("div",{class:"drill"},el("span",{},"Exercice de la prochaine partie"),last.c.drill));
  return box;
}
function coachTab(m){
  if(!S.cc||!SC.has(S.cc)) S.cc=PL[0]?.puuid;
  m.append(el("h2",{},"Le coach"),el("p",{class:"lead"},`${monthName(S.month)}, toutes ranked confondues. Le coach repère ce qui revient d'une partie à l'autre et propose 3 priorités chiffrées. Les objectifs correspondent environ au niveau Or-Platine : ce sont des repères, pas des règles absolues.`),playerChips("cc",false));
  if(!S.cc){ m.append(el("p",{class:"empty-state"},"Aucun joueur.")); return; }
  m.append(coachSummary(S.cc,S.month,null));
  const L=coachData(S.cc,S.month,null).reverse().slice(0,8);
  if(L.length) m.append(el("div",{class:"qtitle"},"Dernières parties analysées"),el("div",{class:"table"},L.map(({g,c,p})=>el("button",{class:"game",onclick:()=>openReview(g)},
    el("div",{class:"bar",style:`background:${p.win?"var(--teal)":"var(--red)"}`}),
    el("div",{},el("div",{class:"meta"},new Date(g.date).toLocaleDateString("fr-FR",{weekday:"short",day:"numeric",month:"short"})),
      el("div",{class:"ppl"},el("span",{class:"p"},champImg(p.champ),el("span",{class:"num"},`${p.k}/${p.d}/${p.a}`),el("span",{class:"mut"},c.work[0]?.title||"rien à signaler")))),
    el("span",{class:"grade",style:`--g:${GCOL[c.grade]}`},c.grade)))));
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
  return el("div",{class:"chips"},[...(withAll?[["all","Tout le monde"]]:[]),...PL.map(p=>[p.puuid,p.name])].map(([v,n])=>
    el("button",{"aria-pressed":S[key]===v?"true":"false",onclick:()=>{S[key]=v; render();},style:v!=="all"?`border-left:4px solid ${colorOf(v)}`:""},n)));
}

/* ---------- onglet Graphiques ---------- */
function charts(m){
  m.append(el("h2",{},`Graphiques ${QN[S.q].toLowerCase()}`),el("p",{class:"lead"},`${monthName(S.month)}. Choisis un pote pour filtrer tous les graphiques.`),playerChips("cp"));
  m.append(lpBlock(),formBlock(),matchupBlock(),heatBlock(),deathBlock());
}
const TNAME=["Fer","Bronze","Argent","Or","Platine","Émeraude","Diamant"], DNAME=["IV","III","II","I"];
const lpLabel=v=>v>=2800?`Maître ${Math.round(v-2800)} LP`:`${TNAME[Math.floor(v/400)]||"?"} ${DNAME[Math.floor(v%400/100)]}`;
function lpBlock(){
  const [a,b]=periodBounds(), qi=S.q==="solo"?1:2;
  const series=PL.filter(p=>S.cp==="all"||p.puuid===S.cp).map(p=>{ const h=(p.lp||[]).filter(x=>x[qi]!=null);
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
  const series=PL.filter(p=>S.cp==="all"||p.puuid===S.cp).map(p=>{ const l=D.games.filter(g=>g.q===S.q&&g.date<=b).map(g=>({g,me:g.parts.find(x=>x.puuid===p.puuid)})).filter(x=>x.me).sort((x,y)=>x.g.date-y.g.date);
    const pts=[]; l.forEach((x,i)=>{ if(x.g.date<a) return; const w=l.slice(Math.max(0,i-9),i+1); if(w.length<5) return; pts.push([x.g.date,w.filter(y=>y.me.win).length/w.length*100]); });
    return {name:p.name,color:colorOf(p.puuid),pts}; }).filter(s=>s.pts.length>1);
  if(!series.length){ box.append(el("p",{class:"empty-state"},"Pas assez de parties sur cette période (il en faut au moins 5 par joueur).")); return box; }
  box.append(lineChart(series,{xMin:a,xMax:b,yMin:0,yMax:100,yTicks:[[0,"0 %"],[25,"25 %"],[50,"50 %",true],[75,"75 %"],[100,"100 %"]],xTicks:dayTicks(a,b),label:"Forme"}));
  return box;
}
function heatBlock(){
  const days=["Lun","Mar","Mer","Jeu","Ven","Sam","Dim"], slots=["0h","3h","6h","9h","12h","15h","18h","21h"];
  const C=[...Array(7)].map(()=>[...Array(8)].map(()=>({n:0,w:0})));
  for(const g of gamesFor(S.month,S.q)){ const fs=g.parts.filter(p=>isF(p)&&(S.cp==="all"||p.puuid===S.cp)); if(!fs.length) continue;
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
  if(!Array.isArray(S.mp)) S.mp=PL.map(p=>p.puuid);
  const PH=[["all","Toute la partie",0,999],["early","Début (0-14 min)",0,14],["mid","Milieu (14-25 min)",14,25],["late","Fin (25 min et +)",25,999]];
  const ph=PH.find(x=>x[0]===(S.mph||"all"));
  let n=0; const pts=[]; const count={};
  for(const g of gamesFor(S.month,S.q)){ if(!g.review) continue;
    const fr=g.parts.filter(p=>isF(p)&&S.mp.includes(p.puuid)); if(!fr.length) continue; n++;
    const who=Object.fromEntries(fr.map(p=>[p.pid,p.puuid]));
    for(const e of g.review.events){ if(e.k!=="kill"||e.x==null||e.t<ph[2]||e.t>=ph[3]) continue;
      const pu=S.dm==="d"?who[e.victim]:who[e.killer]; if(!pu) continue; pts.push([e.x,e.y,pu]); count[pu]=(count[pu]||0)+1; } }
  const toggle=pu=>{ S.mp=S.mp.includes(pu)?S.mp.filter(x=>x!==pu):[...S.mp,pu]; render(); };
  const box=el("section",{class:"block",id:"carte"},el("h3",{},S.dm==="d"?"Où vous mourez":"Où vous tuez"),
    el("p",{class:"lead"},`${pts.length} ${S.dm==="d"?"morts":"kills"} sur ${n} partie${n>1?"s":""} analysée${n>1?"s":""}. Touche les noms pour afficher ou cacher un joueur : chacun a sa couleur.`),
    el("div",{class:"chips"},[["d","Morts"],["k","Kills"]].map(([v,t])=>el("button",{"aria-pressed":S.dm===v?"true":"false",onclick:()=>{S.dm=v; render();}},t)),
      el("span",{style:"width:10px"}),PH.map(([v,t])=>el("button",{"aria-pressed":(S.mph||"all")===v?"true":"false",onclick:()=>{S.mph=v; render();}},t))),
    el("div",{class:"chips"},
      el("button",{"aria-pressed":S.mp.length===PL.length?"true":"false",onclick:()=>{S.mp=S.mp.length===PL.length?[]:PL.map(p=>p.puuid); render();}},S.mp.length===PL.length?"Tout décocher":"Tout le monde"),
      PL.map(p=>{ const on=S.mp.includes(p.puuid), c=colorOf(p.puuid);
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
      el("option",{value:""},"Personne"),PL.filter(p=>p.puuid!==pu).map(p=>el("option",{value:p.puuid},p.name))); sel.value=S.rc&&S.rc!==pu?S.rc:"";
    box.replaceChildren(el("div",{class:"sortbar"},"Comparer avec",sel),radar(list),
      el("p",{class:"foot"},"Chaque branche compare le joueur au meilleur du groupe sur la période : le bord extérieur, c'est le meilleur.")); };
  draw(); return box;
}

/* ---------- onglet Duel ---------- */
function duel(m){
  const A=aggregate(S.month,S.q); if(S.da===S.db) S.db=PL.find(p=>p.puuid!==S.da)?.puuid;
  const a=A[S.da], b=A[S.db];
  const pick=key=>{ const s=el("select",{"aria-label":"Joueur",onchange:e=>{S[key]=e.target.value; render();}},PL.map(p=>el("option",{value:p.puuid},p.name))); s.value=S[key]; return s; };
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

/* ---------- en game maintenant ---------- */
function liveBanner(){
  const now=Date.now();
  const live=PL.filter(p=>p.live&&p.live.start&&now-p.live.start<75*60000);
  if(!live.length) return null;
  return el("div",{class:"live",role:"button",tabindex:"0",style:"cursor:pointer",title:"Voir la draft",onclick:()=>{S.tab="live"; render(); $("tabs").scrollIntoView({behavior:"smooth"});}},el("div",{class:"wrap"},el("span",{class:"dot","aria-hidden":"true"}),el("b",{},"En game"),
    live.map(p=>el("span",{class:"lp"},avatar(p.puuid),p.live.champ?champImg(p.live.champ):null,
      el("span",{},el("b",{},p.name),` ${p.live.champ||""}, ${p.live.mode||"partie"}, depuis ${Math.max(1,Math.round((now-p.live.start)/60000))} min`)))));
}

/* ---------- onglet En game : la draft de la partie en cours ---------- */
const SPELLS={1:"SummonerBoost",3:"SummonerExhaust",4:"SummonerFlash",6:"SummonerHaste",7:"SummonerHeal",11:"SummonerSmite",12:"SummonerTeleport",13:"SummonerMana",14:"SummonerDot",21:"SummonerBarrier",32:"SummonerSnowball"};
const SPELL_FR={1:"Purge",3:"Fatigue",4:"Flash",6:"Fantôme",7:"Soins",11:"Châtiment",12:"Téléportation",14:"Embrasement",21:"Barrière",32:"Boule de neige"};
const RUNES={8005:["Precision/PressTheAttack/PressTheAttack","Attaque soutenue"],8008:["Precision/LethalTempo/LethalTempoTemp","Tempo mortel"],8021:["Precision/FleetFootwork/FleetFootwork","Jeu de jambes"],
  8010:["Precision/Conqueror/Conqueror","Conquérant"],8112:["Domination/Electrocute/Electrocute","Électrocution"],8128:["Domination/DarkHarvest/DarkHarvest","Moisson noire"],
  9923:["Domination/HailOfBlades/HailOfBlades","Déluge de lames"],8214:["Sorcery/SummonAery/SummonAery","Invocation d'Aery"],8229:["Sorcery/ArcaneComet/ArcaneComet","Comète arcanique"],
  8230:["Sorcery/PhaseRush/PhaseRush","Rush de phase"],8437:["Resolve/GraspOfTheUndying/GraspOfTheUndying","Poigne de l'immortel"],8439:["Resolve/VeteranAftershock/VeteranAftershock","Répercussion"],
  8465:["Resolve/Guardian/Guardian","Gardien"],8351:["Inspiration/GlacialAugment/GlacialAugment","Amélioration glaciale"],8360:["Inspiration/UnsealedSpellbook/UnsealedSpellbook","Grimoire déchaîné"],
  8369:["Inspiration/FirstStrike/FirstStrike","Premier coup"]};
const STYLES={8000:["7201_Precision","Précision"],8100:["7200_Domination","Domination"],8200:["7202_Sorcery","Sorcellerie"],8300:["7203_Whimsy","Inspiration"],8400:["7204_Resolve","Volonté"]};
const RIMG="https://ddragon.leagueoflegends.com/cdn/img/perk-images/Styles/";
const TORD=["IRON","BRONZE","SILVER","GOLD","PLATINUM","EMERALD","DIAMOND","MASTER","GRANDMASTER","CHALLENGER"], DORD={IV:0,III:1,II:2,I:3};
const rkScore=r=>r?TORD.indexOf(r.tier)*400+(TORD.indexOf(r.tier)>=7?r.lp:(DORD[r.div]||0)*100+r.lp):null;
const rkFromScore=s=>{ const t=Math.min(9,Math.floor(s/400)); if(t>=7) return `${TIERS[TORD[t]][0]}`; const d=Math.floor((s%400)/100); return `${TIERS[TORD[t]][0]} ${["IV","III","II","I"][d]}`; };
function liveGames(){ const now=Date.now(); return (D.live||[]).filter(g=>g.parts.some(x=>x.friend&&SC.has(x.puuid))&&(!g.start||now-g.start<75*60000)); }
function liveRank(x){
  if(x.friend){ const p=P[x.puuid]; return p?.ranks?.[D.live.find(g=>g.parts.includes(x))?.queue===440?"flex":"solo"]||null; }
  return x.rank;
}
function liveFriendNotes(x,g){
  const out=[], games=D.games.filter(gm=>gm.parts.some(p=>p.puuid===x.puuid&&p.champ===x.champ)).map(gm=>gm.parts.find(p=>p.puuid===x.puuid));
  if(games.length){ const w=games.filter(p=>p.win).length; out.push(`${pl(games.length,"partie","parties")} sur ${x.champ} : ${fmt(w/games.length*100)} % de victoires, note moyenne ${fmt(games.reduce((a,p)=>a+p.score,0)/games.length,1)}`); }
  else out.push(`Première partie de ${x.champ} sur le site.`);
  const L=coachData(x.puuid,"all",null).slice(-10), cnt={}; for(const c of L) for(const k of c.m.keys||[]) cnt[k]=(cnt[k]||0)+1;
  const top=Object.entries(cnt).sort((a,b)=>b[1]-a[1])[0];
  if(top&&CKEY[top[0]]){ const d=L.slice().reverse().find(c=>c.m.keys.includes(top[0])).c.drill; out.push(["Rappel du coach : ",el("b",{},CKEY[top[0]][0].toLowerCase()),`. ${d.replace(/^Prochaine partie : /,"")}`]); }
  return out;
}
function liveRow(x,g){
  const r=liveRank(x), m=x.mast, name=(x.riot||"").split("#")[0];
  const wr=r&&r.w+r.l>0?Math.round(r.w/(r.w+r.l)*100):null;
  const sp=el("div",{class:"lsp"},x.spells.map(s=>SPELLS[s]?el("img",{src:`${DDV}/img/spell/${SPELLS[s]}.png`,alt:SPELL_FR[s]||"",title:SPELL_FR[s]||""}):el("span",{})),
    el("span",{class:"lru"},RUNES[x.key]?el("img",{src:RIMG+RUNES[x.key][0]+".png",alt:RUNES[x.key][1],title:RUNES[x.key][1]}):null,
      STYLES[x.sub]?el("img",{class:"sub",src:RIMG+STYLES[x.sub][0]+".png",alt:STYLES[x.sub][1],title:STYLES[x.sub][1]}):null));
  const tags=[];
  if(m&&m.pts>=200000) tags.push(el("span",{class:"ltag gold"},"Spécialiste"));
  else if(m&&m.pts<10000) tags.push(el("span",{class:"ltag"},"Peu joué"));
  if(wr!=null&&r.w+r.l>=30&&wr>=58) tags.push(el("span",{class:"ltag red"},`${wr} % de victoires`));
  if(r&&r.w+r.l<15) tags.push(el("span",{class:"ltag"},"Peu de parties"));
  const row=el(x.friend?"button":"div",{class:"lrow"+(x.friend?" me":""),onclick:x.friend?()=>openProfile(x.puuid):null},
    el("div",{class:"lch"},x.champ?champImg(x.champ):el("span",{class:"noimg"})),sp,
    el("div",{class:"lwho"},el("b",{},name),el("div",{},x.champ||"?"),el("div",{class:"lrk"},r?tierTxt(r):el("span",{class:"mut"},"Non classé"),wr!=null?el("span",{class:"mut"},`, ${wr} % sur ${r.w+r.l}`):null),
      m?el("div",{class:"mut lm"},`Maîtrise ${m.lvl}, ${fmt(m.pts)} points`):null, tags.length?el("div",{class:"ltags"},tags):null));
  return x.friend?[row,el("ul",{class:"tips lnotes"},liveFriendNotes(x,g).map(t=>el("li",{},t)))]:row;
}
function liveCard(g){
  const now=Date.now(), mine=g.parts.find(x=>x.friend&&SC.has(x.puuid))?.team||100;
  const sides=[mine,mine===100?200:100];
  const avg=t=>{ const v=g.parts.filter(x=>x.team===t).map(x=>rkScore(liveRank(x))).filter(v=>v!=null); return v.length?v.reduce((a,b)=>a+b,0)/v.length:null; };
  const a0=avg(sides[0]), a1=avg(sides[1]);
  const fr=g.parts.filter(x=>x.friend), dur=g.start?Math.max(1,Math.round((now-g.start)/60000)):null;
  const head=el("div",{class:"lhead"},el("div",{},el("h3",{},g.mode),el("div",{class:"mut"},dur?`Commencée il y a ${pl(dur,"minute","minutes")}`:"Écran de chargement",
    (()=>{ const n=Math.round((now-D.generated)/60000); return n<1?". Infos à jour.":`. Infos de la mise à jour d'il y a ${pl(n,"minute","minutes")}.`; })())),
    a0!=null&&a1!=null?el("div",{class:"lavg"},el("div",{},el("span",{class:"mut"},"Niveau moyen"),el("b",{class:"teal"},rkFromScore(a0)),el("span",{class:"mut"}," contre "),el("b",{class:"red"},rkFromScore(a1)))):null);
  const duo=fr.length>=2?(()=>{ const ids=fr.map(x=>x.puuid); const tg=D.games.filter(gm=>ids.every(id=>gm.parts.some(p=>p.puuid===id&&p.team===gm.parts.find(q=>q.puuid===ids[0]).team)));
    const w=tg.filter(gm=>gm.parts.find(p=>p.puuid===ids[0]).win).length;
    return el("p",{class:"lduo"},`${fr.map(x=>P[x.puuid]?.name||x.riot).join(" et ")} jouent ensemble. `,tg.length?`Sur le site : ${pl(tg.length,"partie","parties")} ensemble, ${fmt(w/tg.length*100)} % de victoires.`:"Première partie ensemble sur le site."); })():null;
  const bans=t=>el("div",{class:"lbans"},g.bans.filter(b=>b.team===t).map(b=>b.champ?el("span",{class:"ban"},champImg(b.champ)):el("span",{class:"ban none",title:"Pas de ban"})));
  const team=(t,lab,cls)=>el("div",{class:"lteam "+cls},el("div",{class:"lth"},el("b",{},lab),bans(t)),g.parts.filter(x=>x.team===t).map(x=>liveRow(x,g)));
  return el("div",{class:"lcard"},head,duo,el("div",{class:"lgrid"},team(sides[0],"Votre équipe","ally"),team(sides[1],"Adversaires","foe")));
}
function liveTab(m){
  const L=liveGames();
  m.append(el("h2",{},"En game"),el("p",{class:"lead"},"La partie en cours de tes potes : la draft, les bans, les sorts, les runes, le rang et la maîtrise de chaque joueur. Le site se met à jour toutes les 10 minutes environ, la partie peut donc avoir commencé un peu avant d'apparaître."));
  if(!L.length){ m.append(el("p",{class:"empty-state"},"Personne n'est en game en ce moment.")); return; }
  m.append(...L.map(liveCard));
}

/* ---------- matchups ---------- */
function matchups(pu,month,q){
  const M={};
  for(const g of gamesFor(month,q)){ const me=g.parts.find(p=>p.puuid===pu); if(!me||!me.pos) continue;
    const opp=g.parts.find(o=>o.team!==me.team&&o.pos===me.pos); if(!opp) continue;
    const f=g.review?.friends?.find(x=>x.pid===me.pid);
    const m=(M[opp.champ]??={c:opp.champ,n:0,w:0,gd:0,gn:0}); m.n++; m.w+=me.win?1:0; if(f&&f.gd15!=null){ m.gd+=f.gd15; m.gn++; } }
  return Object.values(M).map(m=>({...m,wr:m.w/m.n,gda:m.gn?m.gd/m.gn:null}));
}
function matchupLists(pu,month,q){
  const all=matchups(pu,month,q); let L=all.filter(m=>m.n>=2); if(L.length<3) L=all;
  if(!L.length) return el("p",{class:"mut"},"Pas encore de partie sur cette période.");
  const hard=L.slice().sort((a,b)=>a.wr-b.wr||(a.gda??0)-(b.gda??0)).slice(0,5);
  const easy=L.slice().sort((a,b)=>b.wr-a.wr||(b.gda??0)-(a.gda??0)).filter(m=>!hard.includes(m)||L.length>5).slice(0,5);
  const row=m=>el("tr",{},el("td",{},el("span",{class:"ci"},champImg(m.c),m.c)),el("td",{},`${m.w}V-${m.n-m.w}D`),
    el("td",{class:m.wr>=.5?"win":"loss"},fmt(m.wr*100)+" %"),el("td",{class:m.gda==null?"mut":m.gda>=0?"win":"loss"},m.gda==null?"–":(m.gda>0?"+":"")+fmt(m.gda)));
  const head=el("tr",{},el("th",{},"Adversaire direct"),el("th",{},"Bilan"),el("th",{},"Victoires"),el("th",{},"Or à 15 min"));
  return el("div",{class:"two"},el("div",{},el("div",{class:"qtitle",style:"margin-top:0"},"Les plus durs"),el("div",{class:"tw"},el("table",{},head,hard.map(row)))),
    el("div",{},el("div",{class:"qtitle",style:"margin-top:0"},"Les plus faciles"),el("div",{class:"tw"},el("table",{},head.cloneNode(true),easy.map(row)))));
}
function matchupBlock(){
  const box=el("section",{class:"block"},el("h3",{},"Matchups"),el("p",{class:"lead"},"Contre quels champions on gagne ou on perd sa lane (adversaire au même poste, écart d'or à 15 min quand la revue existe)."));
  if(S.cp==="all"){ box.append(el("p",{class:"empty-state"},"Choisis un pote en haut de la page pour voir ses matchups.")); return box; }
  box.append(matchupLists(S.cp,S.month,S.q)); return box;
}

/* ---------- wrapped du mois ---------- */
function wrapped(m){
  if(!S.wp||!P[S.wp]) S.wp=PL[0]?.puuid;
  const A=aggregate(S.month,null), a=A[S.wp], p=P[S.wp];
  m.append(el("h2",{},`Wrapped, ${monthName(S.month).toLowerCase()}`),
    el("p",{class:"lead"},"Le résumé du mois de chaque pote, toutes ranked confondues. Fais une capture pour la partager, ou copie le texte pour Discord."),playerChips("wp",false));
  if(!a?.games){ m.append(el("p",{class:"empty-state"},`${p.name} n'a pas joué de ranked sur cette période.`)); return; }
  const list=a.list.map(x=>x.p.score!=null?x:null).filter(Boolean);
  const best=list.slice().sort((x,y)=>y.p.score-x.p.score)[0], worst=list.slice().sort((x,y)=>x.p.score-y.p.score)[0];
  const hours=list.reduce((s,x)=>s+x.g.dur,0)/3600;
  const top=a.topChamp, topL=list.filter(x=>x.p.champ===top[0]), topW=topL.filter(x=>x.p.win).length;
  const days=["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]; const slot={};
  for(const x of list){ const d=new Date(x.g.date); const k=`${(d.getDay()+6)%7}-${d.getHours()}`; slot[k]=(slot[k]||0)+1; }
  const fav=Object.entries(slot).sort((x,y)=>y[1]-x[1])[0][0].split("-").map(Number);
  const pos=Object.entries(a.pos).sort((x,y)=>y[1]-x[1])[0];
  // rang : début et fin de période
  const [t0,t1]=periodBounds(), lpH=(p.lp||[]).filter(x=>x[1]!=null);
  const lpStart=lpH.filter(x=>x[0]<=t0).pop()||lpH.find(x=>x[0]>=t0), lpEnd=lpH.filter(x=>x[0]<=t1).pop();
  // place dans la ligue (note, toutes files) et trophées gagnés (solo/duo)
  const R=ranked(A).sort((x,y)=>y.note-x.note), place=R.findIndex(x=>x.puuid===S.wp)+1;
  const AQ=aggregate(S.month,"solo"), RQ=ranked(AQ); for(const x of RQ){ x.mk=x.penta*100+x.quadra*10+x.triple; x.otp=x.topChamp?.[1]||0; }
  const won=RQ.length?TROPHIES.filter(([,,,key,dir])=>{ const l=RQ.filter(z=>(z[key]||0)!==0||dir<0).sort((u,v)=>(v[key]-u[key])*dir); return l[0]?.puuid===S.wp; }):[];
  const items=[
    ["Heures de LoL",fmt(hours,1)+" h",`${a.games} parties classées`],
    ["Victoires",fmt(a.wr)+" %",`${a.wins} gagnées, ${a.games-a.wins} perdues`],
    ["Ton champion",top[0],`${top[1]} parties, ${fmt(topW/top[1]*100)} % de victoires`],
    ["Ton poste",POS[pos[0]]||"–",`${pos[1]} parties`],
    ["Ta meilleure partie",`${best.p.k}/${best.p.d}/${best.p.a}`,`${best.p.champ}, note ${fmt(best.p.score,1)}`],
    ["La partie à oublier",`${worst.p.k}/${worst.p.d}/${worst.p.a}`,`${worst.p.champ}, note ${fmt(worst.p.score,1)}`],
    ["Plus longue série",`${a.streak} victoire${a.streak>1?"s":""}`,a.lstreak>=3?`et ${a.lstreak} défaites d'affilée au pire`:"d'affilée"],
    ["Ton créneau",`${days[fav[0]]} ${fav[1]}h`,"le moment où tu joues le plus"],
    ["Kills",fmt(a.k),`${fmt(a.d)} morts, ${fmt(a.dead/60)} min passées à attendre`],
    lpStart&&lpEnd&&lpStart!==lpEnd?["Ton rang",lpLabel(lpEnd[1]),`${lpEnd[1]-lpStart[1]>=0?"+":""}${lpEnd[1]-lpStart[1]} points depuis ${lpLabel(lpStart[1])}`]:null,
    place?["Dans la ligue",ord(place),`sur ${R.length} joueurs classés`]:null,
  ].filter(Boolean);
  const card=el("article",{class:"wrapped"},el("div",{class:"wbg",style:`background-image:url('${splash(top[0])}')`}),
    el("div",{class:"winner"},el("div",{class:"wtop"},el("img",{src:iconSrc(p.icon),alt:""}),el("div",{},el("div",{class:"wm"},monthName(S.month)),el("div",{class:"wn"},p.name))),
      el("div",{class:"wgrid"},items.map(([k,v,s])=>el("div",{class:"wi"},el("span",{},k),el("b",{},v),el("small",{},s)))),
      won.length?el("div",{class:"wtro"},el("span",{},"Trophées du mois"),el("div",{},won.map(([e,n])=>el("span",{class:"wt"},`${e} ${n}`)))):null,
      el("div",{class:"wfoot"},"La ligue des potes")));
  const txt=[`🎮 Wrapped de ${p.name}, ${monthName(S.month).toLowerCase()}`,...items.map(([k,v,s])=>`• ${k} : ${v} (${s})`),won.length?`🏆 ${won.map(([e,n])=>e+" "+n).join(", ")}`:""].filter(Boolean).join("\n");
  m.append(card,el("div",{class:"row",style:"margin-top:12px;display:flex;gap:10px"},
    el("button",{class:"btn",onclick:e=>{ navigator.clipboard?.writeText(txt).then(()=>e.target.textContent="Copié ✓",()=>e.target.textContent="Copie impossible"); }},"Copier le résumé pour Discord")));
}

/* ---------- parties ---------- */
function games(m){
  const list=gamesFor(S.month,S.q).filter(g=>S.gp==="all"||g.parts.some(p=>p.puuid===S.gp));
  m.append(el("h2",{},`Parties ${QN[S.q].toLowerCase()}`),el("p",{class:"lead"},`${monthName(S.month)}, ${list.length} partie${list.length>1?"s":""}. Touche une partie pour la revue détaillée.`),
    el("div",{class:"chips"},[["all","Tout le monde"],...PL.map(p=>[p.puuid,p.name])].map(([v,n])=>el("button",{"aria-pressed":S.gp===v?"true":"false",onclick:()=>{S.gp=v; render();}},n))));
  if(!list.length){ m.append(el("p",{class:"empty-state"},"Aucune partie sur cette période.")); return; }
  m.append(el("div",{class:"table"},list.map(g=>{ const fs=g.parts.filter(p=>isF(p)); const win=fs[0]?.win;
    return el("button",{class:"game",onclick:()=>openReview(g)},el("div",{class:"bar",style:`background:${win?"var(--teal)":"var(--red)"}`}),
      el("div",{},el("div",{class:"meta"},el("span",{},new Date(g.date).toLocaleString("fr-FR",{weekday:"short",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})),
          el("span",{},`${Math.floor(g.dur/60)} min`),g.review?el("span",{class:"gold"},"revue disponible"):null),
        el("div",{class:"ppl"},fs.map(p=>el("span",{class:"p"},champImg(p.champ),el("b",{},p.name),el("span",{class:"num"},`${p.k}/${p.d}/${p.a}`))))),
      el("div",{class:`res ${win?"win":"loss"}`},win?"Victoire":"Défaite")); })));
}

/* ---------- duos ---------- */
function duos(m){
  const D2={};
  for(const g of gamesFor(S.month,S.q)){ const fs=g.parts.filter(p=>isF(p));
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
    [el("div",{class:"links",style:"margin-top:4px"},(p.structs||[]).map(id=>SBY[id]?el("button",{class:"btn",onclick:()=>{document.querySelector(".ov")?.remove(); document.body.style.overflow=""; setScope(id); render(); scrollTo(0,0);}},logo(SBY[id],20)," ",SBY[id].nom):null),
       el("button",{class:"btn",onclick:e=>{ const u=location.href.split("#")[0]+"#j-"+slug(p); navigator.clipboard?.writeText(u).then(()=>e.target.textContent="Lien copié ✓",()=>e.target.textContent=u); }},"Copier le lien de sa fiche")),
     el("h4",{},`${QN[S.q]}, ${monthName(S.month).toLowerCase()}`),
     A.games?el("div",{class:"tiles"},tiles.map(([v,l])=>el("div",{class:"tile"},el("b",{},v),el("span",{},l)))):el("p",{class:"mut"},"Aucune partie sur cette période."),
     (()=>{ const pal=palmares(pu); if(!pal.length) return null; const cnt={}; for(const t of pal) (cnt[t.n]??={...t,c:0,months:[]}).c++, cnt[t.n].months.push(monthName(t.mo).split(" ")[0]+" ("+QN[t.q].toLowerCase()+")");
       return [el("h4",{},`Palmarès : ${pal.length} trophée${pal.length>1?"s":""}`),el("div",{class:"palm"},Object.values(cnt).sort((a,b)=>b.c-a.c).map(t=>el("span",{class:"c-"+t.type,title:t.months.join(", ")},`${t.e} ${t.n}`,t.c>1?el("b",{},` ×${t.c}`):null)))]; })(),
     A.games?[el("h4",{},"Bilan du coach"),coachSummary(pu,S.month,null)]:null,
     A.games?[el("h4",{},"Style de jeu"),radarBlock(pu)]:null,
     trend.length>1?[el("h4",{},"Note moyenne par mois"),el("div",{class:"bars"},trend.map(({mo,a})=>el("div",{},el("b",{},fmt(a.note,1)),el("i",{style:`height:${Math.max(4,a.note/maxN*70)}px`}),el("span",{},(()=>{const [y,mm]=mo.split("-").map(Number); return new Date(y,mm-1,1).toLocaleString("fr-FR",{month:"short"});})())) ))]:null,
     champs.length?[el("h4",{},"Champions"),el("div",{class:"tw"},el("table",{},el("tr",{},el("th",{},"Champion"),el("th",{},"Parties"),el("th",{},"Victoires %"),el("th",{},"KDA")),
       champs.map(c=>el("tr",{},el("td",{},el("span",{class:"ci"},champImg(c.c),c.c)),el("td",{},c.n),el("td",{class:c.w/c.n>=.5?"win":"loss"},fmt(c.w/c.n*100)),el("td",{},fmt(c.kda,2))))))]:null,
     A.games?[el("h4",{},"Matchups"),matchupLists(pu,S.month,S.q)]:null,
     roles.length?[el("h4",{},"Postes"),el("p",{},roles.map(([r,n],i)=>[i?", ":"",el("b",{},POS[r]||r)," ",`${n} partie${n>1?"s":""}`]))]:null,
     A.list.length?[el("h4",{},"Dernières parties"),el("div",{class:"table"},A.list.slice(-8).reverse().map(({g,p:x})=>el("button",{class:"game",onclick:()=>openReview(g)},
       el("div",{class:"bar",style:`background:${x.win?"var(--teal)":"var(--red)"}`}),el("div",{},el("div",{class:"meta"},new Date(g.date).toLocaleDateString("fr-FR",{weekday:"short",day:"numeric",month:"short"})),
         el("div",{class:"ppl"},el("span",{class:"p"},champImg(x.champ),el("span",{class:"num"},`${x.k}/${x.d}/${x.a}`),el("span",{class:"mut"},`note ${fmt(x.score,1)}`)))),
       el("div",{class:`res ${x.win?"win":"loss"}`},x.win?"Victoire":"Défaite"))))]:null],
    A.topChamp?splash(A.topChamp[0]):null);
}

/* ---------- revue d'une partie ---------- */
function openReview(g){
  const fs=g.parts.filter(p=>isF(p)), my=fs[0]?.team||100, win=fs[0]?.win, R=g.review;
  const name=pid=>{ const p=g.parts.find(x=>x.pid===pid); return p?(isF(p)?p.name:p.champ):"?"; };
  const body=[];
  if(R){
    const sign=my===100?1:-1, gold=R.gold.map(v=>v*sign);
    body.push(el("h4",{},"Écart d'or"),goldChart(gold,R),
      R.turn?el("p",{class:"callout"},`Le tournant : entre ${R.turn.from} et ${R.turn.to} min, l'écart a bougé de `,el("b",{class:R.turn.swing*sign>0?"win":"loss"},`${R.turn.swing*sign>0?"+":""}${fmt(R.turn.swing*sign)} d'or`),`. ${turnEvents(R,my)}`):null);
    body.push(el("h4",{},"Le coach"),el("div",{class:"fgrid"},R.friends.filter(f=>SC.has(f.puuid)).map(f=>coachCard(g,f))));
    const evs=R.events.filter(e=>e.k!=="kill"||[e.killer,e.victim,...(e.assists||[])].some(pid=>isF(g.parts.find(p=>p.pid===pid))));
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
      ps.map(p=>el("tr",{class:isF(p)?"me":""},el("td",{},el("span",{class:"ci"},champImg(p.champ),el("span",{},el("b",{},p.name),el("span",{class:"mut"}," "+(POS[p.pos]||""))))),
        el("td",{},`${p.k}/${p.d}/${p.a}`),el("td",{},p.cs),el("td",{},fmt(p.dmg)),el("td",{},p.vis),el("td",{},fmt(p.score,1)),el("td",{style:"text-align:left"},el("div",{class:"items"},p.items.map(itemImg)))))); }));
}
if(MULTI) $("pname").textContent=D.ligue||"La ligue des structures";
const H0=location.hash;
setScope((H0.match(/^#s-(.+)$/)||[])[1]||"league");
addEventListener("hashchange",()=>{ if(location.hash.startsWith("#j-")){ openFromHash(); return; } if(location.hash==="#admin"){ render(); return; }
  const id=(location.hash.match(/^#s-(.+)$/)||[])[1]||"league"; if(id!==S.scope){ setScope(id); render(); } });
render();
if(H0.startsWith("#j-")){ history.replaceState(null,"",H0); openFromHash(); }
/* ---------- appli téléphone ---------- */
if(location.protocol==="https:"&&"serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(()=>{});
let installEvt=null;
const standalone=matchMedia("(display-mode: standalone)").matches||navigator.standalone;
addEventListener("beforeinstallprompt",e=>{ e.preventDefault(); installEvt=e;
  $("install").replaceChildren(el("button",{class:"btn gold",onclick:async()=>{ installEvt.prompt(); await installEvt.userChoice; $("install").replaceChildren(); }},"Installer l'appli sur ce téléphone")); });
if(!standalone&&/iphone|ipad/i.test(navigator.userAgent)&&location.protocol==="https:")
  $("install").replaceChildren(el("span",{},"Sur iPhone : bouton Partager puis « Sur l'écran d'accueil » pour avoir l'appli."));
</script>
</body>
</html>
'''


# --------------------------------------------------------------------------- programme principal
def main():
    global OUT
    demo = "--demo" in sys.argv
    site = "--site" in sys.argv
    if site:  # mode GitHub : la page va dans docs/index.html (publiée par GitHub Pages)
        os.makedirs(os.path.join(HERE, "docs"), exist_ok=True)
        OUT = os.path.join(HERE, "docs", "index.html")
    try:
        if demo:
            say("Mode démo : données inventées.")
            cfg = dict(DEFAULT_CONFIG)
            if "--structures" in sys.argv:
                cfg = demo_structures_config()
                players, matches, timelines = demo_data(cfg, [r.split("#")[0] for r in roster_ids(cfg)])
            else:
                players, matches, timelines = demo_data(cfg)
        else:
            cfg = load_config()
            if not cfg:
                return 1
            say("Récupération des parties sur l'API Riot…")
            players, matches, timelines = fetch_all(cfg)
        ddv = ddragon_version()
        LEGENDARY.update(legendary_items(ddv) if ddv else set())
        if demo and not LEGENDARY:
            LEGENDARY.update({3078, 6692, 3071, 3031})
        if any(p.get("live") for p in players) and ddv:
            names = champion_names(ddv)
            for p in players:
                if p.get("live") and not p["live"].get("champ"):
                    p["live"]["champ"] = names.get(p["live"].get("champId"))
        data = build_report(players, matches, timelines, ddv, cfg["mois"], cfg["min_parties_classement"], demo)
        if LIVE_GAMES:
            names = champion_names(ddv) if ddv else {}
            for gm in LIVE_GAMES.values():
                for x in gm["parts"] + gm["bans"]:
                    x.setdefault("champ", names.get(x.get("champId")))
        data["live"] = list(LIVE_GAMES.values())
        data.update(structures_payload(cfg, players))
        for p in data["players"]:
            p["structs"] = next((x.get("structs", []) for x in players if x["puuid"] == p["puuid"]), [])
        webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("discord_webhook", "")
        if not demo:
            if data.get("mode") == "structures":
                # salon de la ligue : exploits, joueur du mois, structure du mois
                if webhook:
                    BOT_NAME[0] = data.get("ligue", "La ligue")
                    discord_alerts(cfg, players, data, webhook, only={"exploits", "joueur_du_mois"}, league=True, title="ligue")
                # salon de chaque structure : toutes les alertes de ses membres (secret DISCORD_WEBHOOKS en JSON {"id": "url"})
                try:
                    hooks = json.loads(os.environ.get("DISCORD_WEBHOOKS") or "{}")
                except ValueError:
                    hooks = {}
                    say("⚠ Le secret DISCORD_WEBHOOKS n'est pas un JSON valide.")
                if webhook or hooks:
                    league_event_alerts(cfg, data, webhook, hooks)
                for stc in data["structures"]:
                    if hooks.get(stc["id"]):
                        BOT_NAME[0] = stc["nom"]
                        discord_alerts(cfg, players, data, hooks[stc["id"]], scope=set(stc["membres"]),
                                       state_name=f"etat_alertes_{stc['id']}.json", title=stc["nom"])
            elif webhook:
                discord_alerts(cfg, players, data, webhook)
        if site:
            write_pwa(cfg)
        # mode site : si rien n'a changé (pas de nouvelle partie, pas de LP différents), on ne republie pas la page
        if site and not demo:
            import hashlib
            sig = hashlib.sha256(json.dumps({k: v for k, v in data.items() if k != "generated"}, sort_keys=True).encode()).hexdigest()
            sig_file = os.path.join(HERE, "docs", ".signature")
            if os.path.exists(sig_file) and open(sig_file).read().strip() == sig and os.path.exists(OUT):
                say("Rien de nouveau depuis la dernière mise à jour : la page n'est pas republiée.")
                return 0
            with open(sig_file, "w") as f:
                f.write(sig)
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(TEMPLATE.replace("__DATA__", payload))
        n = len(data["games"])
        say(f"✓ {os.path.relpath(OUT, HERE)} créé ({n} parties, {sum(1 for g in data['games'] if g['review'])} revues).")
        if "--no-open" not in sys.argv and not site:
            webbrowser.open("file://" + OUT)
        return 0
    except RiotError as e:
        say("✗ " + str(e))
        return 2


if __name__ == "__main__":
    code = main()
    if os.name == "nt" and "--no-open" not in sys.argv and "--site" not in sys.argv and not sys.stdin.closed:
        try:
            input("\nAppuie sur Entrée pour fermer…")
        except Exception:
            pass
    sys.exit(code if "--site" in sys.argv else 0 if code == 0 else code)
