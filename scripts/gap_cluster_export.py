#!/usr/bin/env python3
"""
gap_cluster_export.py — Group gap/extrapolation candidates into survey clusters.
=================================================================================
Candidates are undiscovered, so they have no coordinates of their own. But a
candidate's boxel (subsector + mass code + boxel number, e.g. "AS-S c17") is a
fixed cube of space, and the sector DB usually already holds known systems in
that same boxel. Each candidate boxel is placed at the centroid of those known
systems, then boxels are grouped into nested proximity groups for field-survey
planning:

  region   complete-linkage group, at most --region-ly across (default 450)
  cluster  complete-linkage group inside a region, at most --cluster-ly
           across (default 150)

Both levels come from the same complete-linkage tree, so clusters always nest
inside regions. Regions, then clusters within each region, are numbered in a
suggested visiting order (greedy nearest-neighbour + 2-opt, starting from the
region nearest --start-system, or Sol). Inside each cluster the boxels are the
stops, routed the same way from the boxel nearest the cluster's "plot to"
anchor; candidates at a stop list bracketed gaps first, then extrapolated
ones nearest their known edge. Every candidate gets a visit_order number, so
the route survives filtering and re-sorting in a spreadsheet.

Grouping on bare subsector letters ("AS-S") would be wrong: the same letters
recur for every mass code, hundreds of ly apart.

For procedurally named sectors the boxel cube itself is also decoded from the
name and reported (cube_* columns) once the sector's known systems confirm the
grid origin; hand-authored sectors (e.g. "NGC 1528 Sector") don't follow it,
and those columns are left blank.

Pure stdlib: the clustering stores only pairs within the cut distance, so
memory stays proportional to local density rather than n².

Outputs (in --out-dir):
  <stem>_survey_clusters.md / .csv / .svg   region + cluster report, table, map
  <stem>_candidates_clustered.csv           every candidate with its region/cluster

Usage:
  python scripts/gap_cluster_export.py --db data/sector_library/sector_Hypiae_Aim.sqlite \\
      --sector "Hypiae Aim" --files out/hypiae_aim_gap_full_validated.csv \\
      out/hypiae_aim_extrap_backward_validated.csv
"""

from __future__ import annotations

import argparse
import csv
import heapq
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from scripts import aggregate_gap_master_list, gap_naming
except ImportError:
    import aggregate_gap_master_list
    import gap_naming

DEFAULT_REGION_LY = 450.0
DEFAULT_CLUSTER_LY = 150.0

# Boxel cube edge length per mass code, ly.
CUBE_SIZE = {"a": 10, "b": 20, "c": 40, "d": 80, "e": 160, "f": 320, "g": 640, "h": 1280}
# Procedural sectors sit on a 1280 ly grid anchored here.
GALACTIC_GRID_ORIGIN = (-49985.0, -40985.0, -24105.0)
# Known-system coordinates are stored to 1/32 ly.
COORD_TOLERANCE = 0.05
# Row order within a boxel: strongest evidence first.
CANDIDATE_RANK = {
    "bracketed_gap": 0, "spatial_gap": 0,
    "backward_extrap": 1, "spatial_backward_extrap": 1,
    "forward_extrap": 2,
}
_COORDS_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$")

Point = tuple[float, float, float]
BoxelKey = tuple[str, str]  # (family lower-cased, mass prefix e.g. "c17")


class NoCandidatesError(RuntimeError):
    """Raised by run() when the given files hold no not_in_edsm candidates."""


class StartNotFoundError(ValueError):
    """Raised by resolve_start() when a start system can't be located."""


def resolve_start(text: str, search_dbs: list[Path]) -> tuple[Point, str]:
    """Where to start the visiting order: "x, y, z" coordinates, "Sol", or a
    system name looked up in search_dbs (any extracted sectors, not just the
    one being surveyed -- a carrier or colony is often elsewhere). Exact
    names are checked in every DB; a case-insensitive match is also tried in
    the DB for the name's own sector. Returns (position, label)."""
    text = text.strip()
    m = _COORDS_RE.match(text)
    if m:
        point = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
        return point, _xyz(point)
    if text.lower() == "sol":
        return (0.0, 0.0, 0.0), "Sol"

    dbs = list(dict.fromkeys(p.resolve() for p in search_dbs if p.is_file()))
    tokens = text.split()
    idx = gap_naming.find_subsector_index(tokens)
    sector_key = re.sub(r"[^a-z0-9]", "", "sector" + " ".join(tokens[:idx]).lower()) if idx else None
    for exact in (True, False):
        for db in dbs:
            if not exact and re.sub(r"[^a-z0-9]", "", db.stem.lower()) != sector_key:
                continue
            conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
            try:
                sql = ("SELECT name, x, y, z FROM systems WHERE name = ?" if exact else
                       "SELECT name, x, y, z FROM systems WHERE LOWER(name) = LOWER(?)")
                hit = conn.execute(sql, (text,)).fetchone()
            finally:
                conn.close()
            if hit and None not in hit[1:]:
                return (float(hit[1]), float(hit[2]), float(hit[3])), hit[0]
    raise StartNotFoundError(
        f"Start system {text!r} isn't in any extracted sector. Enter its coordinates "
        "instead as x, y, z (EDSM and Spansh both show them)."
    )


# =============================================================================
# Boxel geometry
# =============================================================================

def boxel_key(family: str, mass_prefix: str) -> BoxelKey:
    return family.strip().lower(), mass_prefix.strip().lower().rstrip("-")


def boxel_grid_index(subsector: str, mass_prefix: str) -> Optional[tuple[int, int, int]]:
    """Grid position of a boxel within its sector, in units of its cube size.

    The name encodes one index: L1 + L2*26 + L3*26² + n1*26³ ("AB-C c17":
    L1=A, L2=B, L3=C, n1=17), laid out on a fixed 128-wide stride regardless
    of mass code."""
    sub = subsector.upper()
    m = re.match(r"^([a-h])(\d+)$", mass_prefix.lower().rstrip("-"))
    if not m or not re.match(r"^[A-Z]{2}-[A-Z]$", sub):
        return None
    idx = (ord(sub[0]) - 65) + (ord(sub[1]) - 65) * 26 + (ord(sub[3]) - 65) * 676 + int(m.group(2)) * 17576
    return idx % 128, (idx // 128) % 128, idx // 16384


def infer_sector_origin(known: list[tuple[str, str, Point]]) -> Optional[Point]:
    """Sector-grid origin consistent with every known (subsector, mass_prefix,
    position), or None if no single origin fits (hand-authored sectors).

    Each known system bounds the origin on each axis to
    (pos - (index+1)*size, pos - index*size]; the origin is the intersection."""
    lo = [-math.inf] * 3
    hi = [math.inf] * 3
    seen = False
    for subsector, mass_prefix, pos in known:
        grid = boxel_grid_index(subsector, mass_prefix)
        if grid is None:
            continue
        size = CUBE_SIZE[mass_prefix[0].lower()]
        seen = True
        for axis in range(3):
            lo[axis] = max(lo[axis], pos[axis] - (grid[axis] + 1) * size)
            hi[axis] = min(hi[axis], pos[axis] - grid[axis] * size)
    if not seen or any(lo[a] > hi[a] + COORD_TOLERANCE for a in range(3)):
        return None
    origin = []
    for axis in range(3):
        # Prefer the exact 1280 ly grid point when it fits.
        g0 = GALACTIC_GRID_ORIGIN[axis]
        snapped = g0 + round((hi[axis] - g0) / 1280) * 1280
        inside = lo[axis] - COORD_TOLERANCE <= snapped <= hi[axis] + COORD_TOLERANCE
        origin.append(snapped if inside else hi[axis])
    return origin[0], origin[1], origin[2]


# =============================================================================
# Sparse complete-linkage clustering
# =============================================================================

def _neighbor_pairs(points: list[Point], radius: float) -> list[dict[int, float]]:
    """For each point, {other index: distance} for every other point within
    radius, found via a grid of radius-sized cells (27-cell neighbourhood)."""
    cells: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for i, p in enumerate(points):
        cells[(math.floor(p[0] / radius), math.floor(p[1] / radius), math.floor(p[2] / radius))].append(i)
    nb: list[dict[int, float]] = [{} for _ in points]
    for (cx, cy, cz), members in cells.items():
        nearby = [
            j
            for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
            for j in cells.get((cx + dx, cy + dy, cz + dz), ())
        ]
        for i in members:
            pi = points[i]
            for j in nearby:
                if j > i:
                    d = math.dist(pi, points[j])
                    if d <= radius:
                        nb[i][j] = d
                        nb[j][i] = d
    return nb


def complete_linkage(n: int, nb: list[dict[int, float]]) -> list[list[int]]:
    """Agglomerative complete-linkage clustering of n items, given only the
    pairwise distances at or below the cut (nb[i][j] == nb[j][i]). Pairs
    absent from nb are treated as beyond the cut. Returns the groups that
    exist once every merge at or below the cut is done -- the same partition
    as cutting a full complete-linkage tree at that height.

    Complete linkage takes the max over member pairs, so a merged group's
    in-range neighbours are exactly the neighbours both halves shared."""
    nb = [dict(d) for d in nb]
    members = [[i] for i in range(n)]
    active = [True] * n
    heap = [(d, i, j) for i in range(n) for j, d in nb[i].items() if i < j]
    heapq.heapify(heap)
    while heap:
        d, i, j = heapq.heappop(heap)
        if not (active[i] and active[j]) or nb[i].get(j) != d:
            continue  # stale entry
        ni, nj = nb[i], nb[j]
        merged = {k: max(dk, nj[k]) for k, dk in ni.items() if k in nj}
        for k in ni:
            if k != j:
                del nb[k][i]
        for k in nj:
            if k != i:
                del nb[k][j]
        for k, dk in merged.items():
            nb[k][i] = dk
            heapq.heappush(heap, (dk, min(i, k), max(i, k)))
        nb[i] = merged
        nb[j] = {}
        active[j] = False
        members[i].extend(members[j])
        members[j] = []
    return [sorted(m) for m in members if m]


def _group_max_distance(a: list[Point], b: list[Point], cut: float) -> Optional[float]:
    """Complete-linkage distance between two point groups, or None as soon as
    any pair exceeds cut."""
    worst = 0.0
    for p in a:
        for q in b:
            d = math.dist(p, q)
            if d > cut:
                return None
            if d > worst:
                worst = d
    return worst


def nested_complete_linkage(
    points: list[Point], cluster_ly: float, region_ly: float
) -> tuple[list[list[int]], list[list[int]]]:
    """Returns (clusters, regions): clusters are lists of point indices cut at
    cluster_ly, regions are lists of cluster indices cut at region_ly. Merges
    happen in increasing-distance order, so continuing from the cluster_ly
    partition reproduces the region_ly cut of the same tree."""
    clusters = complete_linkage(len(points), _neighbor_pairs(points, cluster_ly))
    groups = [[points[i] for i in c] for c in clusters]
    centroids = [centroid(g) for g in groups]
    # A group centroid gap never exceeds the max member-pair distance, so
    # centroid proximity is a safe prefilter for the region-level pairs.
    candidate_pairs = _neighbor_pairs(centroids, region_ly)
    nb: list[dict[int, float]] = [{} for _ in clusters]
    for i, near in enumerate(candidate_pairs):
        for j in near:
            if j > i:
                d = _group_max_distance(groups[i], groups[j], region_ly)
                if d is not None:
                    nb[i][j] = d
                    nb[j][i] = d
    regions = complete_linkage(len(clusters), nb)
    return clusters, regions


def centroid(points: list[Point]) -> Point:
    n = len(points)
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n, sum(p[2] for p in points) / n)


def _diameter(points: list[Point]) -> float:
    return max((math.dist(p, q) for i, p in enumerate(points) for q in points[i + 1:]), default=0.0)


# =============================================================================
# Visiting order
# =============================================================================

def visiting_order(points: list[Point], start: int) -> list[int]:
    """Open path through all points from start: greedy nearest neighbour,
    then 2-opt until no segment reversal shortens it."""
    left = set(range(len(points))) - {start}
    order = [start]
    while left:
        cur = points[order[-1]]
        nxt = min(left, key=lambda k: math.dist(points[k], cur))
        order.append(nxt)
        left.remove(nxt)

    def d(a: int, b: int) -> float:
        return math.dist(points[order[a]], points[order[b]])

    improved = True
    while improved:
        improved = False
        for i in range(1, len(order) - 1):
            for j in range(i + 1, len(order)):
                before = d(i - 1, i) + (d(j, j + 1) if j + 1 < len(order) else 0.0)
                after = d(i - 1, j) + (d(i, j + 1) if j + 1 < len(order) else 0.0)
                if after < before - 1e-6:
                    order[i:j + 1] = order[i:j + 1][::-1]
                    improved = True
    return order


# =============================================================================
# Main pipeline
# =============================================================================

def load_known_by_boxel(db_path: Path) -> tuple[dict[BoxelKey, list[tuple[str, Point]]], list[Point], Optional[Point]]:
    """Known systems grouped by boxel, every known position (for the map),
    and the inferred sector-grid origin (None if the names don't follow it)."""
    by_boxel: dict[BoxelKey, list[tuple[str, Point]]] = defaultdict(list)
    all_points: list[Point] = []
    origin_samples: list[tuple[str, str, Point]] = []
    conn = sqlite3.connect(str(db_path))
    try:
        for name, x, y, z in conn.execute(
            "SELECT name, x, y, z FROM systems WHERE x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL"
        ):
            pos = (float(x), float(y), float(z))
            all_points.append(pos)
            parsed = gap_naming.parse_sequence_name(name)
            if parsed is None:
                continue
            family, prefix, _number = parsed
            key = boxel_key(family, prefix)
            by_boxel[key].append((name, pos))
            origin_samples.append((gap_naming.extract_subsector(family), key[1], pos))
    finally:
        conn.close()
    return by_boxel, all_points, infer_sector_origin(origin_samples)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def run(
    db_path: Path,
    sector: str,
    candidate_files: list[Path],
    out_dir: Path,
    region_ly: float = DEFAULT_REGION_LY,
    cluster_ly: float = DEFAULT_CLUSTER_LY,
    start_point: Optional[Point] = None,
    label: Optional[str] = None,
    start_label: Optional[str] = None,
) -> list[Path]:
    """Cluster the not_in_edsm candidates in candidate_files. Returns the files
    written. label (default: sector) names the output files and report title,
    e.g. a spatial search's "<sector> spatial <center>"."""
    label = (label or sector).strip()
    if start_point is None:
        start_point, start_label = (0.0, 0.0, 0.0), "Sol"
    print("Gap Survey Clusters")
    print(f"  Source DB      : {db_path}")
    print(f"  Sector         : {sector!r}")
    print(f"  Candidate files: {len(candidate_files)}")
    print(f"  Region / cluster diameter: {region_ly:g} / {cluster_ly:g} ly")
    print()

    rows = aggregate_gap_master_list.load_rows_from_files(candidate_files)
    if not rows:
        raise NoCandidatesError(
            f"No not_in_edsm candidates for {label!r} (dry-run output has none to cluster)."
        )

    known, all_known, origin = load_known_by_boxel(db_path)

    by_boxel: dict[BoxelKey, list] = defaultdict(list)
    for row in rows:
        by_boxel[boxel_key(row.family, row.mass_prefix)].append(row)

    boxels: list[BoxelKey] = []
    positions: list[Point] = []
    unplaced: list[BoxelKey] = []
    for key in sorted(by_boxel):
        mates = known.get(key)
        if mates:
            boxels.append(key)
            positions.append(centroid([p for _, p in mates]))
        elif origin is not None:
            # No known boxel-mates, but the grid is confirmed: use the cube centre.
            row = by_boxel[key][0]
            grid = boxel_grid_index(row.subsector, key[1])
            if grid is None:
                unplaced.append(key)
                continue
            size = CUBE_SIZE[key[1][0]]
            boxels.append(key)
            positions.append(tuple(origin[a] + (grid[a] + 0.5) * size for a in range(3)))
        else:
            unplaced.append(key)

    if not boxels:
        raise NoCandidatesError(f"No candidate boxel for {label!r} could be located from the sector DB.")

    print(f"  {len(rows)} candidates in {len(by_boxel)} boxels "
          f"({len(unplaced)} boxel(s) could not be located)")
    print(f"  Boxel-cube decoding: {'grid origin ' + str(origin) if origin else 'not available (non-procedural naming)'}")

    clusters, regions = nested_complete_linkage(positions, cluster_ly, region_ly)

    # --- order regions from start_point, then clusters within each region,
    #     each region entered nearest the previous region's last cluster ---
    cluster_points = [[positions[i] for i in c] for c in clusters]
    cluster_cen = [centroid(pts) for pts in cluster_points]
    region_cen = [centroid([p for ci in r for p in cluster_points[ci]]) for r in regions]
    first = min(range(len(regions)), key=lambda r: math.dist(region_cen[r], start_point))
    region_order = visiting_order(region_cen, first)

    cluster_name: dict[int, str] = {}
    region_name: dict[int, str] = {}
    ordered_clusters: list[int] = []
    prev = start_point
    width = max(2, len(str(len(regions))))
    for rn, r in enumerate(region_order, 1):
        region_name[r] = f"R{rn:0{width}d}"
        members = regions[r]
        pts = [cluster_cen[c] for c in members]
        entry = min(range(len(members)), key=lambda k: math.dist(pts[k], prev))
        for cn, k in enumerate(visiting_order(pts, entry), 1):
            c = members[k]
            cluster_name[c] = f"{region_name[r]}-C{cn:02d}"
            ordered_clusters.append(c)
        prev = cluster_cen[ordered_clusters[-1]]
    region_of_cluster = {c: r for r, members in enumerate(regions) for c in members}

    # --- per-boxel and per-cluster details ---
    boxel_cluster = {i: c for c, idxs in enumerate(clusters) for i in idxs}

    def anchor(keys: list[BoxelKey], target: Point) -> tuple[str, Optional[Point]]:
        mates = [m for k in keys for m in known.get(k, ())]
        if not mates:
            return "", None
        return min(mates, key=lambda m: math.dist(m[1], target))

    # --- route through each cluster's boxels: start at the boxel nearest the
    #     cluster's "plot to" anchor, then nearest-neighbour + 2-opt. Every
    #     candidate in a boxel shares one estimated position, so boxels (not
    #     candidates) are the stops. ---
    boxel_stops: dict[int, list[int]] = {}
    cluster_anchor: dict[int, tuple[str, Optional[Point]]] = {}
    for c in ordered_clusters:
        idxs = clusters[c]
        cluster_anchor[c] = anchor([boxels[i] for i in idxs], cluster_cen[c])
        entry_point = cluster_anchor[c][1] or cluster_cen[c]
        pts = [positions[i] for i in idxs]
        first_stop = min(range(len(idxs)), key=lambda k: math.dist(pts[k], entry_point))
        boxel_stops[c] = [idxs[k] for k in visiting_order(pts, first_stop)]

    def cube(key: BoxelKey, subsector: str) -> tuple[str, str, str, str]:
        grid = boxel_grid_index(subsector, key[1]) if origin else None
        if grid is None:
            return "", "", "", ""
        size = CUBE_SIZE[key[1][0]]
        return tuple(f"{origin[a] + grid[a] * size:.1f}" for a in range(3)) + (str(size),)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_slug(label)}"
    clusters_csv = out_dir / f"{stem}_survey_clusters.csv"
    candidates_csv = out_dir / f"{stem}_candidates_clustered.csv"
    report_md = out_dir / f"{stem}_survey_clusters.md"
    map_svg = out_dir / f"{stem}_survey_clusters.svg"

    def _int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def evidence_order(row) -> tuple:
        # Within a boxel: bracketed gaps (strongest evidence) first, then
        # extrapolated candidates nearest their known edge first.
        return (CANDIDATE_RANK.get(row.candidate_type, 9), _int(row.steps_from_edge), _int(row.number))

    cand_out = []
    for c in ordered_clusters:
        for stop, i in enumerate(boxel_stops[c], 1):
            key = boxels[i]
            pos = positions[i]
            boxel_rows = sorted(by_boxel[key], key=evidence_order)
            cube_x, cube_y, cube_z, cube_size = cube(key, boxel_rows[0].subsector)
            anchor_name, _ = anchor([key], pos)
            for row in boxel_rows:
                cand_out.append({
                    "visit_order": len(cand_out) + 1,
                    "region": region_name[region_of_cluster[c]],
                    "cluster": cluster_name[c],
                    "cluster_stop": stop,
                    "boxel_id": f"{row.subsector} {key[1]}",
                    "system_name": row.system_name,
                    "candidate_type": row.candidate_type,
                    "steps_from_edge": row.steps_from_edge,
                    "subsector": row.subsector,
                    "mass_code": row.mass_code,
                    "mass_prefix": key[1],
                    "number": row.number,
                    "boxel_known_count": len(known.get(key, ())),
                    "boxel_x": f"{pos[0]:.1f}", "boxel_y": f"{pos[1]:.1f}", "boxel_z": f"{pos[2]:.1f}",
                    "cube_min_x": cube_x, "cube_min_y": cube_y, "cube_min_z": cube_z,
                    "cube_size_ly": cube_size,
                    "anchor_known_system": anchor_name,
                })
    # Unlocatable boxels go last, with no visit_order.
    for key in unplaced:
        for row in sorted(by_boxel[key], key=evidence_order):
            cand_out.append({
                "region": "", "cluster": "", "boxel_id": f"{row.subsector} {key[1]}",
                "system_name": row.system_name, "candidate_type": row.candidate_type,
                "steps_from_edge": row.steps_from_edge, "subsector": row.subsector,
                "mass_code": row.mass_code, "mass_prefix": key[1], "number": row.number,
                "boxel_known_count": 0,
            })

    cand_fields = [
        "visit_order", "region", "cluster", "cluster_stop", "boxel_id", "system_name",
        "candidate_type", "steps_from_edge",
        "subsector", "mass_code", "mass_prefix", "number", "boxel_known_count",
        "boxel_x", "boxel_y", "boxel_z", "cube_min_x", "cube_min_y", "cube_min_z",
        "cube_size_ly", "anchor_known_system",
    ]
    with candidates_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, cand_fields, restval="")
        writer.writeheader()
        writer.writerows(cand_out)

    cluster_rows = []
    first_visit = {}
    for row in cand_out:
        first_visit.setdefault(row["cluster"], row.get("visit_order", ""))
    for c in ordered_clusters:
        keys = [boxels[i] for i in boxel_stops[c]]
        crows = [r for k in keys for r in by_boxel[k]]
        cen = cluster_cen[c]
        anchor_name, anchor_pos = cluster_anchor[c]
        anchor_dist = math.dist(anchor_pos, cen) if anchor_pos else 0.0
        types = Counter(r.candidate_type for r in crows)
        cluster_rows.append({
            "cluster": cluster_name[c],
            "region": region_name[region_of_cluster[c]],
            "first_visit_order": first_visit[cluster_name[c]],
            "centroid_x": f"{cen[0]:.1f}", "centroid_y": f"{cen[1]:.1f}", "centroid_z": f"{cen[2]:.1f}",
            "diameter_ly": f"{_diameter(cluster_points[c]):.1f}",
            "boxels": len(keys),
            "candidates": len(crows),
            "bracketed_gap": types.get("bracketed_gap", 0) + types.get("spatial_gap", 0),
            "extrapolated": len(crows) - types.get("bracketed_gap", 0) - types.get("spatial_gap", 0),
            "mass_codes": " ".join(f"{m}:{n}" for m, n in sorted(Counter(k[1][0] for k in keys).items())),
            "anchor_known_system": anchor_name,
            "anchor_dist_ly": f"{anchor_dist:.1f}",
            "boxel_ids_in_route_order": "; ".join(f"{by_boxel[k][0].subsector} {k[1]}" for k in keys),
        })
    with clusters_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, list(cluster_rows[0]))
        writer.writeheader()
        writer.writerows(cluster_rows)

    region_rows = []
    for r in region_order:
        name = region_name[r]
        crs = [x for x in cluster_rows if x["region"] == name]
        region_rows.append({
            "region": name,
            "centroid": region_cen[r],
            "diameter": _diameter([p for c in regions[r] for p in cluster_points[c]]),
            "clusters": len(crs),
            "boxels": sum(x["boxels"] for x in crs),
            "candidates": sum(x["candidates"] for x in crs),
            "bracketed_gap": sum(x["bracketed_gap"] for x in crs),
        })

    unplaced_candidates = sum(len(by_boxel[k]) for k in unplaced)
    _write_report(
        report_md, label, db_path, rows, boxels, unplaced, unplaced_candidates, cluster_rows, region_rows,
        region_ly, cluster_ly, origin, start_label or _xyz(start_point), candidates_csv, clusters_csv, map_svg,
    )
    _write_svg(map_svg, label, all_known, positions, boxels, by_boxel, boxel_cluster,
               cluster_name, region_name, region_of_cluster, region_rows, region_ly)

    print(f"  {len(cluster_rows)} survey clusters in {len(region_rows)} regions")
    print(f"  Report: {report_md}")
    return [report_md, clusters_csv, candidates_csv, map_svg]


def _write_report(path, label, db_path, rows, boxels, unplaced, unplaced_candidates, cluster_rows, region_rows,
                  region_ly, cluster_ly, origin, start_label, candidates_csv, clusters_csv, map_svg) -> None:
    xyz = _xyz

    with path.open("w", encoding="utf-8") as f:
        f.write(f"# {label} — Survey Clusters\n\n")
        f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}  \n")
        f.write(f"Source: `{db_path.name}`  \n")
        f.write(f"Candidates (not in EDSM): {len(rows)} in {len(boxels) + len(unplaced)} boxels → "
                f"{len(cluster_rows)} survey clusters (≤{cluster_ly:g} ly across) in "
                f"{len(region_rows)} regions (≤{region_ly:g} ly across)  \n")
        f.write(f"Files: `{candidates_csv.name}` (every candidate), `{clusters_csv.name}`, `{map_svg.name}` (map)\n\n")
        f.write(
            "Each candidate boxel (subsector + mass code + boxel number, e.g. `AS-S c17`) is placed at the "
            "centroid of the known systems in that same boxel; diameters are measured between those "
            "positions. An undiscovered system can sit anywhere in its boxel's cube "
            "(edge: b 20 ly, c 40 ly, d 80 ly, e 160 ly), so treat cluster outlines as approximate. "
            "**Plot to** is a known system near the cluster centre to set as a route destination.\n\n"
        )
        f.write(
            f"**How to fly it:** work through `{candidates_csv.name}` in `visit_order`. Within each "
            "cluster, plot to its *Plot to* system, then visit its stops (`cluster_stop`) in order; "
            "every candidate at a stop shares one estimated position, strongest evidence (bracketed "
            "gaps) first. Skip any candidate that turns out not to exist — the next stop is still "
            "nearby.\n\n"
        )
        if origin is None:
            f.write("> Boxel cubes could not be decoded for this sector (its names don't follow the "
                    "procedural grid), so the `cube_*` columns are blank; positions come from known "
                    "boxel-mates only.\n\n")
        if unplaced:
            f.write(f"> {unplaced_candidates} candidate(s) in {len(unplaced)} "
                    "boxel(s) have no known boxel-mates and could not be located; they are listed in "
                    "the candidates CSV with no region/cluster.\n\n")
        f.write(f"## Regions (suggested visiting order, starting nearest {start_label})\n\n")
        f.write("| Region | Centroid (x, y, z) | Across ly | Clusters | Boxels | Candidates | Bracketed gaps | Leg from previous ly |\n")
        f.write("|---|---|--:|--:|--:|--:|--:|--:|\n")
        prev = None
        for r in region_rows:
            leg = "" if prev is None else f"{math.dist(r['centroid'], prev):.0f}"
            f.write(f"| {r['region']} | {xyz(r['centroid'])} | {r['diameter']:.0f} | {r['clusters']} | "
                    f"{r['boxels']} | {r['candidates']} | {r['bracketed_gap']} | {leg} |\n")
            prev = r["centroid"]
        for r in region_rows:
            f.write(f"\n## {r['region']}\n\n")
            f.write("| Cluster | Centroid (x, y, z) | Across ly | Boxels | Candidates | Gap | Extrap | Mass codes | Plot to |\n")
            f.write("|---|---|--:|--:|--:|--:|--:|---|---|\n")
            for c in cluster_rows:
                if c["region"] == r["region"]:
                    cen = (float(c["centroid_x"]), float(c["centroid_y"]), float(c["centroid_z"]))
                    f.write(f"| {c['cluster']} | {xyz(cen)} | {float(c['diameter_ly']):.0f} | {c['boxels']} | "
                            f"{c['candidates']} | {c['bracketed_gap']} | {c['extrapolated']} | "
                            f"{c['mass_codes']} | {c['anchor_known_system']} |\n")


def _xyz(p: Point) -> str:
    return f"{p[0]:.0f}, {p[1]:.0f}, {p[2]:.0f}"


# Okabe-Ito-derived categorical palette, distinguishable on white.
_PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00",
            "#7A5195", "#8C6D31", "#2F4B7C", "#B8860B", "#3F7F3F", "#A0306E"]


def _region_colors(region_rows, near_ly: float) -> dict[str, str]:
    """Greedy palette assignment so regions whose centroids are within
    near_ly of each other never share a colour (while the palette allows)."""
    colors: dict[str, str] = {}
    for r in region_rows:
        nearby = {colors[o["region"]] for o in region_rows
                  if o["region"] in colors and math.dist(o["centroid"], r["centroid"]) <= near_ly}
        free = [c for c in _PALETTE if c not in nearby]
        use = Counter(colors.values())
        colors[r["region"]] = min(free or _PALETTE, key=lambda c: use[c])
    return colors


def _write_svg(path, label, all_known, positions, boxels, by_boxel, boxel_cluster,
               cluster_name, region_name, region_of_cluster, region_rows, region_ly) -> None:
    """Plan (x–z, galactic-centre up) and side (x–y) views of the candidate
    boxels coloured by region, over the sector's known systems."""
    panel, pad, gap, top = 700, 40, 60, 60
    pts = list(all_known) + positions
    lo = [min(p[a] for p in pts) for a in range(3)]
    hi = [max(p[a] for p in pts) for a in range(3)]
    span = max(hi[a] - lo[a] for a in range(3)) or 1.0
    scale = (panel - 2 * pad) / span

    def to_px(p, vertical_axis, x0):
        px = x0 + pad + (p[0] - lo[0]) * scale
        py = top + panel - pad - (p[vertical_axis] - lo[vertical_axis]) * scale
        return px, py

    color = _region_colors(region_rows, region_ly)
    step = max(1, len(all_known) // 12000)  # thin the background on very large sectors
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{2 * panel + gap}" height="{panel + top + 20}" '
        f'font-family="Segoe UI, Arial, sans-serif" font-size="12">',
        f'<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{pad}" y="24" font-size="16" font-weight="600">{_esc(label)} — survey regions</text>',
        f'<text x="{pad}" y="44" fill="#555">Grey: known systems. Dots: candidate boxels coloured by '
        f'region, size ∝ candidates. Dashed: suggested region order.</text>',
    ]
    for x0, vertical, title in ((0, 2, "Plan view (x right, z up = toward galactic centre)"),
                                (panel + gap, 1, "Side view (x right, y up)")):
        out.append(f'<rect x="{x0 + 0.5}" y="{top + 0.5}" width="{panel - 1}" height="{panel - 1}" '
                   f'fill="none" stroke="#ccc"/>')
        out.append(f'<text x="{x0 + 8}" y="{top + 18}" fill="#333">{title}</text>')
        out.append('<g fill="#d0d0d0">')
        for p in all_known[::step]:
            px, py = to_px(p, vertical, x0)
            out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="0.8"/>')
        out.append("</g>")
        for i, p in enumerate(positions):
            reg = region_name[region_of_cluster[boxel_cluster[i]]]
            px, py = to_px(p, vertical, x0)
            r = 1.8 + 1.2 * math.sqrt(len(by_boxel[boxels[i]]))
            out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r:.1f}" fill="{color[reg]}" '
                       f'fill-opacity="0.75"><title>{_esc(cluster_name[boxel_cluster[i]])} '
                       f'{_esc(by_boxel[boxels[i]][0].subsector)} {boxels[i][1]}: '
                       f'{len(by_boxel[boxels[i]])} candidate(s)</title></circle>')
        route = " ".join("{:.1f},{:.1f}".format(*to_px(r["centroid"], vertical, x0)) for r in region_rows)
        out.append(f'<polyline points="{route}" fill="none" stroke="#333" stroke-width="1" '
                   f'stroke-dasharray="4 3" opacity="0.6"/>')
        for r in region_rows:
            px, py = to_px(r["centroid"], vertical, x0)
            out.append(f'<text x="{px:.1f}" y="{py:.1f}" text-anchor="middle" dominant-baseline="middle" '
                       f'font-weight="700" stroke="#fff" stroke-width="3" paint-order="stroke">'
                       f'{r["region"]}</text>')
    out.append("</svg>")
    path.write_text("\n".join(out), encoding="utf-8")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Group gap/extrapolation candidates into nested survey regions and clusters.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, required=True, help="Path to sector library SQLite file")
    parser.add_argument("--sector", required=True, help="Sector name (for labeling/filenames)")
    parser.add_argument("--files", type=Path, nargs="+", required=True,
                        help="Candidate CSVs to cluster (gap/extrapolation/spatial *_validated.csv)")
    parser.add_argument("--out-dir", type=Path, default=Path("out"), help="Output directory (default: out)")
    parser.add_argument("--region-ly", type=float, default=DEFAULT_REGION_LY,
                        help=f"Max region diameter, ly (default: {DEFAULT_REGION_LY:g})")
    parser.add_argument("--cluster-ly", type=float, default=DEFAULT_CLUSTER_LY,
                        help=f"Max cluster diameter, ly (default: {DEFAULT_CLUSTER_LY:g})")
    parser.add_argument("--start-system", default=None,
                        help="Where to start the visiting order: a system in --db or any other "
                             "sector_*.sqlite beside it, or 'x, y, z' coordinates (default: Sol)")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        return 1
    if not 0 < args.cluster_ly <= args.region_ly:
        print("ERROR: need 0 < --cluster-ly <= --region-ly", file=sys.stderr)
        return 1

    start_point, start_label = None, None
    if args.start_system:
        try:
            start_point, start_label = resolve_start(
                args.start_system, [args.db, *sorted(args.db.parent.glob("sector_*.sqlite"))]
            )
        except StartNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    try:
        run(args.db, args.sector, args.files, args.out_dir,
            region_ly=args.region_ly, cluster_ly=args.cluster_ly, start_point=start_point,
            start_label=start_label)
    except NoCandidatesError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
