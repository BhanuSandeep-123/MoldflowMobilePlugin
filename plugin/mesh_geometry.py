"""
mesh_geometry.py
----------------
Pure-Python geometry on a Moldflow .udm node cloud: bounding box + detection of
cylindrical through-holes. No COM here, so it is unit-testable offline.

Hole-detection strategy (works for through-holes whose axis is the part's
thinnest direction, e.g. holes drilled through a plate):
  1. Thickness axis = the smallest bounding-box dimension.
  2. Keep only "wall" nodes: those in the mid-thickness band (away from both
     flat faces). These lie on vertical walls = the outer rim + the hole walls.
  3. Cluster the wall nodes in the in-plane (2D) projection. The outer rim forms
     one big non-circular cluster; each hole forms its own circular cluster.
  4. Fit a circle (Kasa least-squares) to each cluster. Accept clusters whose
     fit residual is small relative to the radius -> those are holes. The
     rectangular rim is rejected automatically by the circularity test.

All coordinates in and out are in the file's native units (meters for .udm);
the caller scales to display units.
"""

from __future__ import annotations

import math
from collections import defaultdict

AXES = ("x", "y", "z")
_IDX = {"x": 0, "y": 1, "z": 2}


def parse_udm(path):
    """
    Parse a Moldflow .udm export.

    Returns (coords, tets, tris, bbox):
      coords : dict node_label -> (x, y, z)     (all nodes)
      tets   : list of (n1, n2, n3, n4) labels  (3D tetra elements)
      tris   : list of (n1, n2, n3) labels      (fusion/midplane triangles)
      bbox   : {axis: (min, max)} over all nodes

    Record token layouts (after replacing {/} with spaces and splitting), from
    Autodesk's own SmartSplit parsers:
      NODE{ID ... X Y Z}      -> ID = tok[1]; X,Y,Z = last 3
      TET4{ID ... n1 n2 n3 n4}-> node labels = last 4
      TRI3{ID ... n1 n2 n3}   -> node labels = last 3
    """
    coords = {}
    tets = []
    tris = []
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]

    with open(path, "r", errors="ignore") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("NODE{"):
                t = s.replace("{", " ").replace("}", " ").split()
                try:
                    label = int(t[1])
                    x = float(t[-3]); y = float(t[-2]); z = float(t[-1])
                except (ValueError, IndexError):
                    continue
                coords[label] = (x, y, z)
                if x < lo[0]: lo[0] = x
                if x > hi[0]: hi[0] = x
                if y < lo[1]: lo[1] = y
                if y > hi[1]: hi[1] = y
                if z < lo[2]: lo[2] = z
                if z > hi[2]: hi[2] = z
            elif s.startswith("TET4{"):
                t = s.replace("{", " ").replace("}", " ").split()
                try:
                    tets.append(tuple(int(v) for v in t[-4:]))
                except ValueError:
                    continue
            elif s.startswith("TRI3{"):
                t = s.replace("{", " ").replace("}", " ").split()
                try:
                    tris.append(tuple(int(v) for v in t[-3:]))
                except ValueError:
                    continue

    bbox = {a: (lo[_IDX[a]], hi[_IDX[a]]) for a in AXES}
    return coords, tets, tris, bbox


def parse_udm_nodes(path):
    """Back-compat: return (nodes_list, bbox) of all node coordinates."""
    coords, _tets, _tris, bbox = parse_udm(path)
    return list(coords.values()), bbox


def boundary_node_labels(tets):
    """
    Labels of nodes on the boundary surface of a tetra mesh: a triangular face
    is on the boundary iff it belongs to exactly one tetrahedron.
    """
    face_count = defaultdict(int)
    for (a, b, c, d) in tets:
        for face in ((a, b, c), (a, b, d), (a, c, d), (b, c, d)):
            face_count[tuple(sorted(face))] += 1
    boundary = set()
    for face, cnt in face_count.items():
        if cnt == 1:
            boundary.update(face)
    return boundary


def boundary_node_normals(coords, tets):
    """
    node_label -> unit OUTWARD surface normal for every boundary node of a
    tetra mesh.

    A boundary face belongs to exactly one tet; its outward normal is the
    face normal flipped away from the tet's fourth (interior) vertex -- the
    same construction Autodesk's injpts_3d.vbs uses for 3D injection points,
    where the injection direction must point out of the part for the solver
    to resolve the entrance. Per-node normals average the adjacent boundary
    face normals.
    """
    face_info = {}
    for (a, b, c, d) in tets:
        for face, opp in (((a, b, c), d), ((a, b, d), c),
                          ((a, c, d), b), ((b, c, d), a)):
            key = tuple(sorted(face))
            if key in face_info:
                face_info[key] = None          # shared face -> interior
            else:
                face_info[key] = (face, opp)

    sums = {}
    for info in face_info.values():
        if info is None:
            continue
        (a, b, c), opp = info
        if a not in coords or b not in coords or c not in coords \
                or opp not in coords:
            continue
        ax, ay, az = coords[a]
        ux = coords[b][0] - ax; uy = coords[b][1] - ay; uz = coords[b][2] - az
        vx = coords[c][0] - ax; vy = coords[c][1] - ay; vz = coords[c][2] - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        mag = math.sqrt(nx * nx + ny * ny + nz * nz)
        if mag <= 0.0:
            continue
        nx /= mag; ny /= mag; nz /= mag
        # Flip so the normal points AWAY from the interior vertex.
        wx = coords[opp][0] - ax; wy = coords[opp][1] - ay; wz = coords[opp][2] - az
        if nx * wx + ny * wy + nz * wz > 0.0:
            nx, ny, nz = -nx, -ny, -nz
        for lbl in (a, b, c):
            sx, sy, sz = sums.get(lbl, (0.0, 0.0, 0.0))
            sums[lbl] = (sx + nx, sy + ny, sz + nz)

    normals = {}
    for lbl, (sx, sy, sz) in sums.items():
        mag = math.sqrt(sx * sx + sy * sy + sz * sz)
        if mag > 0.0:
            normals[lbl] = (sx / mag, sy / mag, sz / mag)
    return normals


def triangle_node_normals(coords, tris):
    """
    node_label -> unit surface normal for a Dual Domain / midplane mesh.

    `boundary_node_normals` only works on tets, so a Dual Domain study had no
    normals at all and every gate fell back to +Z -- which is why the injection
    cone sat at an arbitrary angle to the surface instead of standing on it.

    A triangle mesh has no interior vertex to orient against, so the face
    normal comes straight from the winding, which Moldflow keeps consistent
    across the shell (an inconsistent one is what the mesh diagnostics report
    as "unoriented elements"). Per-node normals average the adjacent faces,
    weighted by triangle area via the un-normalised cross product, so large
    faces dominate a node shared with slivers.
    """
    sums = {}
    for tri in tris:
        try:
            a, b, c = tri[0], tri[1], tri[2]
        except (IndexError, TypeError):
            continue
        if a not in coords or b not in coords or c not in coords:
            continue
        ax, ay, az = coords[a]
        ux = coords[b][0] - ax; uy = coords[b][1] - ay; uz = coords[b][2] - az
        vx = coords[c][0] - ax; vy = coords[c][1] - ay; vz = coords[c][2] - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        if nx == 0.0 and ny == 0.0 and nz == 0.0:
            continue
        for lbl in (a, b, c):
            sx, sy, sz = sums.get(lbl, (0.0, 0.0, 0.0))
            sums[lbl] = (sx + nx, sy + ny, sz + nz)

    normals = {}
    for lbl, (sx, sy, sz) in sums.items():
        mag = math.sqrt(sx * sx + sy * sy + sz * sz)
        if mag > 0.0:
            normals[lbl] = (sx / mag, sy / mag, sz / mag)
    return normals


def surface_coords(coords, tets, tris):
    """
    Return the list of coordinates that lie on the model's outer surface.
      * 3D tetra mesh -> extract the boundary (faces used once).
      * fusion/midplane (triangles, or no tets) -> every node is already on the
        surface.
    """
    if tets:
        labels = boundary_node_labels(tets)
        return [coords[l] for l in labels if l in coords]
    if tris:
        used = set()
        for tri in tris:
            used.update(tri)
        return [coords[l] for l in used if l in coords]
    return list(coords.values())


def bbox_sizes(bbox):
    return {a: bbox[a][1] - bbox[a][0] for a in AXES}


# --------------------------------------------------------------------------- #
# Circle fitting (Kasa algebraic least squares) + 3x3 solve via Cramer's rule
# --------------------------------------------------------------------------- #
def _solve3(m, b):
    def det3(a):
        return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    d = det3(m)
    if abs(d) < 1e-20:
        return None
    out = []
    for c in range(3):
        mc = [row[:] for row in m]
        for r in range(3):
            mc[r][c] = b[r]
        out.append(det3(mc) / d)
    return out


def fit_circle(pts):
    """Fit a circle to 2D pts -> (cx, cy, r, rms_residual) or None."""
    n = len(pts)
    if n < 3:
        return None
    Sx = Sy = Sxx = Syy = Sxy = Sz = Sxz = Syz = 0.0
    for (x, y) in pts:
        z = x * x + y * y
        Sx += x; Sy += y
        Sxx += x * x; Syy += y * y; Sxy += x * y
        Sz += z; Sxz += x * z; Syz += y * z
    m = [[Sxx, Sxy, Sx], [Sxy, Syy, Sy], [Sx, Sy, float(n)]]
    sol = _solve3(m, [Sxz, Syz, Sz])
    if sol is None:
        return None
    A, B, C = sol
    cx, cy = A / 2.0, B / 2.0
    r2 = C + cx * cx + cy * cy
    if r2 <= 0:
        return None
    r = math.sqrt(r2)
    ss = 0.0
    for (x, y) in pts:
        ss += (math.hypot(x - cx, y - cy) - r) ** 2
    return cx, cy, r, math.sqrt(ss / n)


# --------------------------------------------------------------------------- #
# Grid-based clustering (connected components within eps)
# --------------------------------------------------------------------------- #
def estimate_edge_length(nodes3d):
    """
    Median nearest-neighbour distance in 3D = a robust estimate of the mesh
    surface edge length. Measured in 3D (not the 2D projection) so that
    vertical-wall nodes stacked across the thickness -- which collapse onto
    nearly the same 2D point -- do NOT corrupt the estimate.

    Each sampled point's nearest neighbour is searched against the full set.
    """
    n = len(nodes3d)
    if n < 2:
        return 0.0
    step = max(1, n // 300)
    nn = []
    for i in range(0, n, step):
        ax, ay, az = nodes3d[i]
        best = math.inf
        for j in range(n):
            if j == i:
                continue
            bx, by, bz = nodes3d[j]
            d = (ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2
            if 0.0 < d < best:
                best = d
        if best < math.inf:
            nn.append(math.sqrt(best))
    if not nn:
        return 0.0
    nn.sort()
    return nn[len(nn) // 2]


def _cluster(pts, eps):
    """Union-find connected components; two pts linked if within eps. Grid-accel."""
    if eps <= 0:
        return [list(range(len(pts)))] if pts else []
    parent = list(range(len(pts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    cell = eps
    grid = {}
    for i, (x, y) in enumerate(pts):
        grid.setdefault((int(x // cell), int(y // cell)), []).append(i)

    eps2 = eps * eps
    for (cx, cy), members in grid.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), ()):
                    for i in members:
                        if i < j:
                            ax, ay = pts[i]; bx, by = pts[j]
                            if (ax - bx) ** 2 + (ay - by) ** 2 <= eps2:
                                union(i, j)
    groups = {}
    for i in range(len(pts)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


# --------------------------------------------------------------------------- #
def detect_holes(nodes, bbox, face_frac=0.15, resid_frac=0.20, min_pts=5):
    """
    Detect cylindrical through-holes. Returns a dict:
      { 'axis': thickness-axis name, 'planar': (a,b) axis names,
        'edge_len':.., 'eps':.., 'wall_nodes':.., 'unique_wall':..,
        'clusters':.., 'cluster_info':[...],
        'holes': [ {'center': {a:.., b:..}, 'radius':.., 'diameter':..,
                    'npts':.., 'residual':..}, ... ] }
    Coordinates are in the same units as `nodes`.
    """
    sizes = bbox_sizes(bbox)
    taxis = min(sizes, key=sizes.get)
    planar = tuple(a for a in AXES if a != taxis)
    ti = _IDX[taxis]
    pa, pb = _IDX[planar[0]], _IDX[planar[1]]
    tmin, tmax = bbox[taxis]
    tsize = tmax - tmin
    ftol = face_frac * tsize

    # Mesh edge length from the full 3D surface (robust to wall stacking).
    edge = estimate_edge_length(nodes)
    pspan = max(sizes[planar[0]], sizes[planar[1]]) or 1.0
    if edge <= 0:
        edge = pspan * 1e-3
    dtol = 0.5 * edge      # collapse thickness-stacked wall nodes to one point
    eps = 2.5 * edge       # link neighbours around a hole ring

    raw_wall = [(n[pa], n[pb]) for n in nodes
                if (n[ti] - tmin) > ftol and (tmax - n[ti]) > ftol]

    seen = set()
    wall = []
    for (a, b) in raw_wall:
        key = (round(a / dtol), round(b / dtol))
        if key not in seen:
            seen.add(key)
            wall.append((a, b))

    result = {"axis": taxis, "planar": planar, "edge_len": edge, "eps": eps,
              "wall_nodes": len(raw_wall), "unique_wall": len(wall),
              "clusters": 0, "holes": []}
    if len(wall) < min_pts:
        return result

    clusters = _cluster(wall, eps)
    result["clusters"] = len(clusters)
    result["eps"] = eps

    holes = []
    diag = []   # per-cluster diagnostics (for tuning)
    for members in clusters:
        info = {"npts": len(members)}
        if len(members) < min_pts:
            info["reject"] = "too_few_points"
            diag.append(info)
            continue
        pts = [wall[i] for i in members]
        fit = fit_circle(pts)
        if not fit:
            info["reject"] = "no_circle_fit"
            diag.append(info)
            continue
        cx, cy, r, rms = fit
        info.update({"radius": r, "residual": rms,
                     "resid_ratio": (rms / r if r > 0 else None),
                     "center": {planar[0]: cx, planar[1]: cy}})
        if r <= 0:
            info["reject"] = "nonpositive_radius"
            diag.append(info)
            continue
        if r > 0.55 * pspan:      # bigger than the part -> a rim/edge arc, not a hole
            info["reject"] = "too_large"
            diag.append(info)
            continue
        if rms / r >= resid_frac:
            info["reject"] = f"not_circular(resid_ratio={rms / r:.3f}>={resid_frac})"
            diag.append(info)
            continue
        info["reject"] = None
        diag.append(info)
        holes.append({
            "center": {planar[0]: cx, planar[1]: cy},
            "radius": r, "diameter": 2 * r,
            "npts": len(pts), "residual": rms,
        })
    result["cluster_info"] = diag
    # stable order: by first planar axis then second
    holes.sort(key=lambda h: (h["center"][planar[0]], h["center"][planar[1]]))
    result["holes"] = holes
    return result
