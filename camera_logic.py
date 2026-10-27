"""Camera brain: decides WHERE the camera goes and HOW it moves, from what happens in the shot.

This file is pure Python (no Blender), so it can be tested on its own.

Inputs  : who is in the shot, where they stand, whether they move, what action they play,
          how much dialogue there is, which shot number this is.
Outputs : a Plan = lens + 2..3 camera/target waypoints (start / middle / end of the shot).

Rules of thumb used (standard film grammar):
  * moving character      -> tracking shot from the side, camera travels with them
  * jump / fight / dance  -> wider full-body frame, low angle or slow orbit
  * talking, one person   -> medium close-up with a slow push-in
  * talking, two people   -> establishing two-shot, then over-the-shoulder / reverse shots
  * three or more people  -> group shot, framing is calculated so everybody fits
  * sad / small / sitting -> slightly high angle, angry / strong -> slightly low angle
  * first shot            -> establishing wide, last shot -> slow pull-out
  * same camera is never repeated twice in a row (angle flips, frame gets tighter)
The camera always stays on the audience side (-Y) of the characters (180-degree rule).
"""
import math
import re

SENSOR = 36.0
MOVE_MIN = 1.0            # metres a character must travel before it counts as a "moving" shot

LOCOMOTION = ("walk", "run")
BIG_ACTION = ("jump", "fight", "dance", "action", "fall")

# (category, word stems). Stems shorter than 4 letters must match at the start of a word.
_RULES = (
    ("run", ("run", "sprint", "jog", "chase", "dash")),
    ("walk", ("walk", "stroll", "march", "sneak", "strut", "pace")),
    ("jump", ("jump", "leap", "flip", "vault", "climb")),
    ("fight", ("punch", "kick", "fight", "attack", "slash", "boxing", "combat", "shoot", "throw",
               "sword", "block", "strike")),
    ("dance", ("dance", "danc", "samba", "hiphop", "salsa", "macarena", "twerk", "breakdance",
               "gangnam", "swing")),
    ("fall", ("fall", "death", "dying", "knock", "stumble", "faint", "collapse", "hit reaction")),
    ("sit", ("sit", "seat", "chair", "kneel", "crouch", "sleep", "lying")),
    ("sad", ("sad", "cry", "sob", "depress", "defeat", "sorry", "disappoint", "tired")),
    ("angry", ("angry", "yell", "shout", "argu", "furious", "rage", "scream")),
    ("laugh", ("laugh", "giggle", "happy", "cheer", "celebrat", "victory", "clap", "joy")),
    ("wave", ("wave", "greet", "hello", "bye", "salute", "bow", "point")),
    ("talk", ("talk", "speak", "say", "conversation", "discuss", "explain", "dialog", "chat")),
    ("idle", ("idle", "stand", "breath", "wait", "look", "pose", "neutral", "tpose", "rest")),
)


def classify_name(name):
    """Action name -> category, or None when no keyword matches."""
    text = " ".join(re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", name or "").lower()))
    for cat, stems in _RULES:
        for st in stems:
            if len(st) >= 4:
                if st in text:
                    return cat
            elif re.search(r"\b" + re.escape(st), text):
                return cat
    return None


def classify(name, stats=None, seconds=3.0):
    """Category from the action name; falls back to measured motion when the name says nothing.
    stats = {"travel": heights, "lift": heights, "energy": float} (all optional)."""
    cat = classify_name(name)
    if cat:
        return cat
    stats = stats or {}
    travel = stats.get("travel", 0.0)
    lift = stats.get("lift", 0.0)
    energy = stats.get("energy", 0.0)
    if travel > 0.8:
        return "run" if travel / max(seconds, 0.5) > 1.4 else "walk"
    if lift > 0.3:
        return "jump"
    if energy > 0.35:
        return "action"
    if energy < 0.06:
        return "idle"
    return "gesture"


class Member:
    """One character in a shot. Positions are metres, height is the character height."""

    def __init__(self, name, x0, y0, x1=None, y1=None, height=1.75, category="idle", lift=0.0):
        self.name = name
        self.x0, self.y0 = float(x0), float(y0)
        self.x1 = self.x0 if x1 is None else float(x1)
        self.y1 = self.y0 if y1 is None else float(y1)
        self.height = max(0.3, float(height))
        self.category = category
        self.lift = lift

    def travel(self):
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)

    def at(self, t):
        return (self.x0 + (self.x1 - self.x0) * t, self.y0 + (self.y1 - self.y0) * t)


class Plan:
    def __init__(self, kind, size, lens, waypoints, linear, note):
        self.kind = kind            # short label, used to avoid repeating the same camera
        self.size = size
        self.lens = lens
        self.waypoints = waypoints  # [(t, (camx, camy, camz), (tgtx, tgty, tgtz)), ...]  t in 0..1
        self.linear = linear        # True = constant-speed camera move (tracking)
        self.note = note

    def __repr__(self):
        return f"Plan({self.kind!r}, {self.size!r}, lens={self.lens:.0f}, {len(self.waypoints)} pts)"


# ------------------------------------------------------------------ framing maths
# fh = visible frame height / character height, cz = look-at height / character height,
# wf = extra width needed around one character / character height
SIZES = {
    "wide":   {"fh": 2.60, "cz": 0.85, "wf": 1.30, "lens": 28.0, "label": "wide"},
    "full":   {"fh": 1.50, "cz": 0.52, "wf": 0.75, "lens": 35.0, "label": "full body"},
    "medium": {"fh": 0.85, "cz": 0.72, "wf": 0.55, "lens": 40.0, "label": "medium"},
    "mcu":    {"fh": 0.52, "cz": 0.82, "wf": 0.35, "lens": 50.0, "label": "medium close-up"},
    "close":  {"fh": 0.30, "cz": 0.90, "wf": 0.20, "lens": 65.0, "label": "close-up"},
}
SIZE_ORDER = ("wide", "full", "medium", "mcu", "close")


def tighter(size, steps=1):
    i = SIZE_ORDER.index(size) + steps
    return SIZE_ORDER[max(0, min(len(SIZE_ORDER) - 1, i))]


def half_tans(lens, aspect):
    """tan(half horizontal fov), tan(half vertical fov) for a Blender camera, sensor fit AUTO."""
    s = SENSOR / (2.0 * lens)
    if aspect >= 1.0:
        return s, s / aspect
    return s * aspect, s


def fit_distance(width, height, lens, aspect):
    th, tv = half_tans(lens, aspect)
    return max((width / 2.0) / th, (height / 2.0) / tv)


def _unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 1e-9 else (0.0, 0.0)


def _frame(members, t, size, aspect, lens=None, fh_scale=1.0):
    """Target point, distance and lens that fit every member at time t into the given shot size."""
    spec = SIZES[size]
    pts = [m.at(t) for m in members]
    hmax = max(m.height for m in members)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    spread_x, spread_y = max(xs) - min(xs), max(ys) - min(ys)
    lens = lens or spec["lens"]
    fh = spec["fh"] * hmax * fh_scale
    if len(members) > 1:
        # keep everyone in frame: tall enough for the tallest member, wide enough for the group
        fh = max(fh, 1.15 * hmax)
    fw = spread_x + spec["wf"] * hmax
    if len(members) > 1:
        fw *= 1.10          # slack so a slow push-in never cuts off the outer characters
    d = fit_distance(fw, fh, lens, aspect) + spread_y / 2.0 + 0.12 * hmax
    tz = spec["cz"] * hmax
    if len(members) > 1:
        tz = max(tz, 0.5 * hmax)
    # jumping: follow the highest point a little so the head is not cut off
    lift = max((m.lift for m in members), default=0.0)
    tz += min(lift, 1.0) * 0.35
    return (cx, cy, tz), d, lens


def _place(target, d, az_deg, cam_z=None):
    """Camera position at straight distance d from target, rotated az_deg around it (0 = in front, -Y)."""
    az = math.radians(az_deg)
    cz = target[2] if cam_z is None else cam_z
    cz = max(0.12, cz)
    dz = cz - target[2]
    dh = math.sqrt(max(d * d - dz * dz, (0.35 * d) ** 2))
    return (target[0] + dh * math.sin(az), target[1] - dh * math.cos(az), cz)


def _height_for(mode, target, d, hmax):
    if mode == "low":
        return max(0.2, 0.28 * hmax)
    if mode == "lowmid":
        return max(0.3, 0.55 * hmax)
    if mode == "high":
        return min(target[2] + 0.30 * d, 7.0)
    if mode == "tophigh":
        return min(target[2] + 0.50 * d, 9.0)
    return None  # eye level


def _orbit_push(members, size, aspect, az0, az1=None, push=1.0, mode="eye", lens=None,
                fh_scale=1.0, mid=False, linear=False):
    """Generic camera: frames the group, optionally pushes in/out and orbits while the shot runs."""
    az1 = az0 if az1 is None else az1
    ts = (0.0, 0.5, 1.0) if (mid or az1 != az0) else (0.0, 1.0)
    hmax = max(m.height for m in members)
    wps = []
    used_lens = lens
    for t in ts:
        tgt, d, used_lens = _frame(members, t, size, aspect, lens, fh_scale)
        d_t = d * (1.0 + (push - 1.0) * t)
        az_t = az0 + (az1 - az0) * t
        cam = _place(tgt, d_t, az_t, _height_for(mode, tgt, d_t, hmax))
        wps.append((t, cam, tgt))
    return wps, used_lens


# ------------------------------------------------------------------ individual plans
def _plan_track(members, movers, size, aspect, flip, mode, kind_note):
    lead = max(movers, key=lambda m: m.travel())
    d = _unit((lead.x1 - lead.x0, lead.y1 - lead.y0))
    p1, p2 = (-d[1], d[0]), (d[1], -d[0])
    if abs(p1[1] - p2[1]) < 1e-6:
        p = p1 if p1[0] > 0 else p2
    else:
        p = p1 if p1[1] < p2[1] else p2          # side that faces the audience
    az = math.degrees(math.atan2(p[0], -p[1]))
    cross = p[0] * d[1] - p[1] * d[0]
    az += 22.0 * (1 if cross > 0 else -1)         # a little ahead of the walker = 3/4 view
    subject = movers if len(movers) == len(members) or len(movers) > 1 else members
    wps, lens = _orbit_push(subject, size, aspect, az, mode=mode, linear=True)
    note = f"{kind_note}, {SIZES[size]['label']}, camera travels with the character"
    return Plan("track", size, lens, wps, True, note)


def _plan_ots(members, idx, aspect):
    a, b = members[0], members[1]
    if idx % 2 == 0:
        a, b = b, a                                # reverse shot every other time
    ax, ay = a.at(0.0)
    bx, by = b.at(0.0)
    u = _unit((bx - ax, by - ay))
    if u == (0.0, 0.0):
        u = (1.0, 0.0)
    l1, l2 = (-u[1], u[0]), (u[1], -u[0])
    lat = l1 if l1[1] < l2[1] else l2
    if abs(l1[1] - l2[1]) < 1e-6:
        lat = l1 if l1[0] > 0 else l2
    wps = []
    for t in (0.0, 1.0):
        ax, ay = a.at(t)
        bx, by = b.at(t)
        back = 0.85 - 0.25 * t                      # slow push toward the speaker
        cam = (ax - u[0] * back + lat[0] * 0.42, ay - u[1] * back + lat[1] * 0.42, 0.92 * a.height + 0.06)
        tgt = (bx, by, 0.90 * b.height)
        wps.append((t, cam, tgt))
    dist = math.hypot(wps[0][2][0] - wps[0][1][0], wps[0][2][1] - wps[0][1][1])
    th, tv = half_tans(50.0, aspect)
    want_fh = 0.62 * b.height
    tv_need = (want_fh / 2.0) / max(dist, 0.5)
    lens = SENSOR / 2.0 / (tv_need * (aspect if aspect >= 1.0 else 1.0))
    lens = max(35.0, min(85.0, lens))
    return Plan("ots", "mcu", lens, wps, False,
                f"Over-the-shoulder on {b.name} (looking past {a.name}), slow push-in")


# ------------------------------------------------------------------ the decision
MANUAL_LABEL = {
    "WIDE": "Wide shot", "FULL": "Full body", "MEDIUM": "Medium shot", "CLOSE": "Close-up",
    "OTS": "Over the shoulder", "TWO_SHOT": "Two-shot / group", "LOW": "Low angle",
    "HIGH": "High angle", "TRACK": "Tracking", "ORBIT": "Orbit",
}


def plan_shot(members, index=0, total=1, words=0, manual="AUTO", prev_kind="", rep=0, aspect=16 / 9.0):
    """Choose the camera for one shot. `rep` = how many shots in a row already used prev_kind."""
    if not members:
        members = [Member("?", 0.0, 0.0)]
    flip = 1 if index % 2 == 0 else -1
    n = len(members)
    cats = [m.category for m in members]
    movers = [m for m in members if m.travel() >= MOVE_MIN]
    first, last = index == 0, (index == total - 1 and total > 1)
    focus = members[index % n]

    if manual and manual != "AUTO":
        return _manual(manual, members, movers, focus, index, flip, aspect)

    # 1. somebody travels -> tracking shot
    if movers:
        main = max(movers, key=lambda m: m.travel())
        verb = "Running" if main.category == "run" else "Walking" if main.category == "walk" else "Moving"
        size = "wide" if main.category == "run" and n == 1 else "full"
        mode = "lowmid" if main.category == "run" else "eye"
        if len(members) > len(movers) and n > 2:
            size = "wide"
        return _plan_track(members, movers, size, aspect, flip, mode,
                           f"Auto: {verb} {main.travel():.1f} m -> tracking shot")

    # 2. big body action
    big = [m for m in members if m.category in BIG_ACTION]
    if big:
        c = big[0].category
        base = "wide" if n > 1 else "full"
        if c == "jump":
            wps, lens = _orbit_push(members, "wide" if n == 1 else base, aspect, 25 * flip, push=0.93,
                                    mode="low", fh_scale=0.9)
            return Plan("jump", base, lens, wps, False, "Auto: jump -> low angle, tall frame so the leap fits")
        if c == "fall":
            wps, lens = _orbit_push(members, base, aspect, 20 * flip, push=1.0, mode="high")
            return Plan("fall", base, lens, wps, False, "Auto: fall -> high angle, full body")
        if c == "fight":
            wps, lens = _orbit_push(members, base, aspect, -28 * flip, 28 * flip, push=0.95, mode="lowmid")
            return Plan("fight", base, lens, wps, False, "Auto: fight -> full body, slow orbit, slightly low")
        wps, lens = _orbit_push(members, base, aspect, -22 * flip, 22 * flip, push=0.96)
        return Plan("dance", base, lens, wps, False, f"Auto: {c} -> full body, slow orbit")

    # 3. opening shot establishes the place
    if first and total > 1:
        size = "wide" if n > 1 else "full"
        wps, lens = _orbit_push(members, size, aspect, 12 * flip, push=0.88, mode="eye")
        return Plan("establish", size, lens, wps, False, "Auto: first shot -> establishing wide, slow push-in")

    # 4. dialogue / quiet acting
    soft = cats[0] if cats else "idle"
    if n == 1:
        m = members[0]
        size = "mcu" if words <= 18 else "medium"
        mode, push, tag = "eye", 0.88, "slow push-in"
        if soft == "sad":
            mode, push, tag = "high", 0.92, "slightly high angle (feels small / sad)"
        elif soft == "angry":
            mode, push, tag = "lowmid", 0.90, "slightly low angle (strong / angry)"
        elif soft in ("laugh", "wave"):
            size, push, tag = "medium", 1.0, "steady medium"
        elif soft == "sit":
            size, mode, push, tag = "medium", "high", 1.0, "slightly high, seated"
        elif words == 0 and soft == "idle":
            size, push, tag = "full", 1.0, "steady full body (standing still)"
        elif soft in LOCOMOTION:
            size, push, tag = "full", 1.0, "steady full body (walking in place)"
        kind = "single_" + size + mode
        if kind == prev_kind or rep:
            size = tighter(size, 1) if rep % 2 == 0 and size not in ("close",) else size
            flip = -flip
        if last:
            push, tag = 1.14, "slow pull-out (last shot)"
        wps, lens = _orbit_push(members, size, aspect, 18 * flip, push=push, mode=mode)
        return Plan(kind, size, lens, wps, False, f"Auto: {m.name} {soft} -> {SIZES[size]['label']}, {tag}")

    if n == 2:
        gap = math.hypot(members[0].x0 - members[1].x0, members[0].y0 - members[1].y0)
        slot = index % 3
        if words == 0 or gap > 4.5 or slot == 0:
            size = "medium" if gap <= 4.5 else "full"
            push = 1.14 if last else 0.92
            wps, lens = _orbit_push(members, size, aspect, 14 * flip, push=push, mode="eye")
            tag = "pull-out (last shot)" if last else "slow push-in"
            return Plan("two", size, lens, wps, False,
                        f"Auto: two characters -> two-shot, {SIZES[size]['label']}, {tag}")
        plan = _plan_ots(members, index, aspect)
        plan.note = "Auto: dialogue between two -> " + plan.note
        return plan

    size = "full" if index % 2 == 0 else "medium"
    push = 1.14 if last else 0.93
    wps, lens = _orbit_push(members, size, aspect, 12 * flip, push=push, mode="eye")
    return Plan("group", size, lens, wps, False,
                f"Auto: {n} characters -> group shot, {SIZES[size]['label']}, everyone kept in frame")


def _manual(manual, members, movers, focus, index, flip, aspect):
    n = len(members)
    label = MANUAL_LABEL.get(manual, manual.title())
    if manual == "WIDE":
        wps, lens = _orbit_push(members, "wide", aspect, 15 * flip, push=0.95)
        return Plan("m_wide", "wide", lens, wps, False, f"Manual: {label}")
    if manual == "FULL":
        wps, lens = _orbit_push(members, "full", aspect, 12 * flip)
        return Plan("m_full", "full", lens, wps, False, f"Manual: {label}")
    if manual == "MEDIUM":
        wps, lens = _orbit_push(members, "medium", aspect, 10 * flip, push=0.95)
        return Plan("m_medium", "medium", lens, wps, False, f"Manual: {label}")
    if manual == "CLOSE":
        wps, lens = _orbit_push([focus], "close", aspect, 15 * flip, push=0.92)
        return Plan("m_close", "close", lens, wps, False, f"Manual: {label} on {focus.name}")
    if manual == "OTS":
        if n >= 2:
            p = _plan_ots(members, index, aspect)
            p.note = "Manual: " + p.note
            return p
        wps, lens = _orbit_push(members, "mcu", aspect, 25 * flip, push=0.92)
        return Plan("m_ots", "mcu", lens, wps, False, "Manual: over-the-shoulder needs 2 characters, used medium close-up")
    if manual == "TWO_SHOT":
        wps, lens = _orbit_push(members, "medium" if n < 3 else "full", aspect, 12 * flip, push=0.95)
        return Plan("m_two", "medium", lens, wps, False, f"Manual: {label}, everyone in frame")
    if manual == "LOW":
        wps, lens = _orbit_push(members, "full", aspect, 12 * flip, push=0.95, mode="low", lens=28.0)
        return Plan("m_low", "full", lens, wps, False, f"Manual: {label}, heroic")
    if manual == "HIGH":
        wps, lens = _orbit_push(members, "full" if n > 1 else "medium", aspect, 12 * flip, push=0.95, mode="tophigh")
        return Plan("m_high", "full", lens, wps, False, f"Manual: {label}")
    if manual == "TRACK":
        use = movers or []
        if not use:
            lead = members[0]
            use = [Member(lead.name, lead.x0, lead.y0, lead.x0 + 0.01, lead.y0, lead.height, lead.category)]
            fake = [lead]
            wps, lens = _orbit_push(fake, "full", aspect, 80 * flip)
            return Plan("m_track", "full", lens, wps, True, "Manual: Tracking (nobody moves, side view)")
        return _plan_track(members, use, "full", aspect, flip, "eye", "Manual: Tracking")
    if manual == "ORBIT":
        wps, lens = _orbit_push(members, "full" if n == 1 else "wide", aspect, -35 * flip, 35 * flip, push=0.95)
        return Plan("m_orbit", "full", lens, wps, False, f"Manual: {label} around the characters")
    wps, lens = _orbit_push(members, "medium", aspect, 0)
    return Plan("m_default", "medium", lens, wps, False, "Manual: medium")


# ------------------------------------------------------------------ helpers for tests / checks
def look_at_basis(cam, tgt):
    f = [tgt[i] - cam[i] for i in range(3)]
    n = math.sqrt(sum(c * c for c in f)) or 1.0
    f = [c / n for c in f]
    up = (0.0, 0.0, 1.0)
    r = (f[1] * up[2] - f[2] * up[1], f[2] * up[0] - f[0] * up[2], f[0] * up[1] - f[1] * up[0])
    rn = math.sqrt(sum(c * c for c in r)) or 1.0
    r = [c / rn for c in r]
    u = (r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0])
    return f, r, u


def project(cam, tgt, lens, aspect, point):
    """Normalised screen position (-1..1) of a world point, or None when behind the camera."""
    f, r, u = look_at_basis(cam, tgt)
    v = [point[i] - cam[i] for i in range(3)]
    depth = sum(v[i] * f[i] for i in range(3))
    if depth <= 0.01:
        return None
    th, tv = half_tans(lens, aspect)
    x = sum(v[i] * r[i] for i in range(3)) / depth / th
    y = sum(v[i] * u[i] for i in range(3)) / depth / tv
    return x, y
