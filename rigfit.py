"""Rig-fitting maths. Pure Python (no Blender imports) so it can be tested anywhere.

  * blend_weights   - smooth skin weights for one vertex (replaces "nearest bone gets 100%")
  * detect_landmarks - find the crotch height, neck height and leg spacing of a body from its points
  * fit_knots / remap_z - move a human template skeleton so its joints land on THIS body
"""
import math

# Template landmark helpers: a Mixamo hip joint sits a little above the crotch.
CROTCH_BELOW_HIP = 0.045


# ------------------------------------------------------------------ skin weights
def prepare_bones(bones):
    """bones: [(name, head(x,y,z), tail(x,y,z))] -> fast tuples for blend_weights."""
    out = []
    for name, h, t in bones:
        dx, dy, dz = t[0] - h[0], t[1] - h[1], t[2] - h[2]
        out.append((name, h[0], h[1], h[2], dx, dy, dz, dx * dx + dy * dy + dz * dz))
    return out


def blend_weights(p, prepared, scale, k=3, falloff=2.2, power=2.0, min_w=0.05):
    """Weights {bone_name: w} (sum = 1) for a point.

    Deep inside a limb only its own bone is close, so it gets ~100%.
    Near a joint two bones are about equally close, so they share the weight and the joint bends
    smoothly instead of tearing.  'scale' is the character height (sets how wide a joint blend is)."""
    px, py, pz = p
    ds = []
    for name, hx, hy, hz, dx, dy, dz, l2 in prepared:
        if l2 <= 0.0:
            t = 0.0
        else:
            t = ((px - hx) * dx + (py - hy) * dy + (pz - hz) * dz) / l2
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        cx, cy, cz = hx + dx * t - px, hy + dy * t - py, hz + dz * t - pz
        ds.append((math.sqrt(cx * cx + cy * cy + cz * cz), name))
    ds.sort()
    margin = 0.01 * scale
    limit = ds[0][0] * falloff + margin
    sel = [x for x in ds[:k] if x[0] <= limit]
    eps = margin * 0.5 + 1e-9
    raw = [(name, (1.0 / (d + eps)) ** power) for d, name in sel]
    tot = sum(w for _n, w in raw)
    out = {n: w / tot for n, w in raw}
    small = [n for n, w in out.items() if w < min_w]
    if small and len(small) < len(out):
        for n in small:
            del out[n]
        tot = sum(out.values())
        out = {n: w / tot for n, w in out.items()}
    return out


# ------------------------------------------------------------------ body landmarks
def _median(v):
    v = sorted(v)
    return v[len(v) // 2] if v else 0.0


def detect_landmarks(points, bins=64):
    """points: iterable of (x, y, z). Returns a dict that may contain:
         'crotch'        height (0..1 of the body) where the two legs join
         'neck'          height (0..1) of the narrowest part between head and shoulders
         'leg_half_sep'  half the distance between the leg centres, as a fraction of body height
    Anything that cannot be found reliably is simply left out."""
    pts = list(points)
    if len(pts) < 300:
        return {}
    zmin = min(p[2] for p in pts)
    zmax = max(p[2] for p in pts)
    H = zmax - zmin
    if H < 1e-9:
        return {}
    mid = [p[0] for p in pts if 0.25 <= (p[2] - zmin) / H <= 0.75]
    cx = _median(mid) if mid else _median([p[0] for p in pts])
    per_bin = [[] for _ in range(bins)]
    for x, _y, z in pts:
        i = min(bins - 1, max(0, int((z - zmin) / H * bins)))
        per_bin[i].append(x)
    out = {}

    # ---- neck: narrowest slice of the central band, between two wider parts (torso below, head above)
    band = 0.2 * H
    width = []
    for xs in per_bin:
        c = [x for x in xs if abs(x - cx) <= band]
        width.append((max(c) - min(c)) if len(c) >= 3 else 0.0)
    sm = [sum(width[max(0, i - 1):i + 2]) / len(width[max(0, i - 1):i + 2]) for i in range(bins)]
    lo_i, hi_i = int(0.55 * bins), int(0.95 * bins)
    cand = [i for i in range(lo_i, hi_i) if sm[i] > 0]
    if cand:
        j = min(cand, key=lambda i: sm[i])
        above = [sm[i] for i in range(j + 1, bins) if sm[i] > 0]
        below = [sm[i] for i in range(int(0.4 * bins), j) if sm[i] > 0]
        if above and below and sm[j] <= 0.8 * min(max(above), max(below)):
            a = b = j
            while a - 1 >= lo_i and sm[a - 1] <= sm[j] * 1.15:
                a -= 1
            while b + 1 < hi_i and sm[b + 1] <= sm[j] * 1.15:
                b += 1
            out["neck"] = (a + b + 1) / 2.0 / bins

    # ---- crotch: legs show as two separate groups of points straddling the centre line
    gap_min = 0.025 * H
    tol = 0.04 * H
    gaps = []
    for xs in per_bin:
        xs = sorted(xs)
        found = None
        if len(xs) >= 20:
            for k in range(len(xs) - 1):
                g = xs[k + 1] - xs[k]
                if g >= gap_min and xs[k] < cx + tol and xs[k + 1] > cx - tol and k + 1 >= 8 and len(xs) - k - 1 >= 8:
                    if found is None or g > found[0]:
                        found = (g, k)
        if found:
            k = found[1]
            gaps.append((_median(xs[:k + 1]), _median(xs[k + 1:])))
        else:
            gaps.append(None)
    start = next((i for i in range(int(0.25 * bins)) if gaps[i]), None)
    if start is not None:
        end, hole = start, 0
        for i in range(start + 1, int(0.75 * bins)):
            if gaps[i]:
                end, hole = i, 0
            else:
                hole += 1
                if hole > 1:
                    break
        if end - start + 1 >= 0.08 * bins:                 # legs must be a real part of the body
            out["crotch"] = (end + 1) / float(bins)
        use = [gaps[i] for i in range(start, max(start, end - 1) + 1) if gaps[i]]
        if use and "crotch" in out:
            out["leg_half_sep"] = _median([(r - l) / 2.0 for l, r in use]) / H
    return out


# ------------------------------------------------------------------ fitting the template
def fit_knots(lm, tpl_crotch, tpl_neck):
    """(template_height, mesh_height) pairs, all heights 0..1. Landmarks that look wrong are dropped."""
    knots = []
    c, n = lm.get("crotch"), lm.get("neck")
    if c is not None and not (0.2 <= c <= 0.62):
        c = None
    if n is not None and not (0.55 <= n <= 0.95):
        n = None
    if c is not None and n is not None and n - c < 0.1:
        c = n = None
    if c is not None and tpl_crotch is not None:
        knots.append((tpl_crotch, c))
    if n is not None and tpl_neck is not None:
        knots.append((tpl_neck, n))
    knots.sort()
    for a, b in zip(knots, knots[1:]):
        if b[1] <= a[1] + 0.02 or b[0] <= a[0] + 0.02:
            return []
    return knots


def remap_z(z, knots):
    """Piecewise-linear height remap through the knots (0,0) ... (1,1)."""
    pts = [(0.0, 0.0)] + list(knots) + [(1.0, 1.0)]
    if z <= 0.0:
        return z
    if z >= 1.0:
        return 1.0 + (z - 1.0)
    for (a0, b0), (a1, b1) in zip(pts, pts[1:]):
        if z <= a1:
            return b0 + (z - a0) * (b1 - b0) / (a1 - a0) if a1 > a0 else b0
    return z


# ------------------------------------------------------------------ marker-driven warp
def warp_points(points, src, dst, power=2.5, use_y=False, eps=1e-9):
    """Move template points so the template markers 'src' land exactly on the user's markers 'dst'.
    Every point is displaced by an inverse-distance blend of the marker displacements, so points
    near a marker follow it (the marker itself lands exactly) and points in between are
    interpolated smoothly.  All positions are (x, y, z) in the same units.
    use_y=False keeps depth (y) as it was - markers are placed in the front view."""
    disp = []
    for s, d in zip(src, dst):
        disp.append((d[0] - s[0], (d[1] - s[1]) if use_y else 0.0, d[2] - s[2]))
    out = []
    for p in points:
        num = [0.0, 0.0, 0.0]
        den = 0.0
        exact = None
        for s, dv in zip(src, disp):
            dist2 = (p[0] - s[0]) ** 2 + (p[1] - s[1]) ** 2 + (p[2] - s[2]) ** 2
            if dist2 < eps:
                exact = dv
                break
            w = 1.0 / (dist2 ** (power / 2.0))
            num[0] += w * dv[0]
            num[1] += w * dv[1]
            num[2] += w * dv[2]
            den += w
        if exact is not None:
            dv = exact
        elif den > 0:
            dv = (num[0] / den, num[1] / den, num[2] / den)
        else:
            dv = (0.0, 0.0, 0.0)
        out.append((p[0] + dv[0], p[1] + dv[1], p[2] + dv[2]))
    return out
