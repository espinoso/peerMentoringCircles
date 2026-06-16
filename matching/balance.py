"""Deterministic repair pass over the LLM's proposed circles.

Hard guarantees, regardless of what the model returns:
  * every included applicant is placed in exactly one circle (no orphans);
  * every circle has between min_size and max_size members;
  * circles are filled toward `target` (default 6) when moves are needed.

The LLM does the smart affinity grouping; this just fixes constraint
violations, using a light affinity score to place/move people sensibly.
"""
from __future__ import annotations

import re
from collections import Counter

from .parser import Participant
from .prompt import Group, compute_meet_links

_WORD = re.compile(r"[a-záéíóúñü]+", re.IGNORECASE)

# Strong enough to dominate affinity, so a same-organization pairing is only
# chosen when no conflict-free option keeps every circle at 3–6 (unavoidable).
_ORG_PENALTY = 1000.0
_MEET_BONUS = 10.0


def _tokens(p: Participant) -> set[str]:
    text = f"{p.topic} {p.keywords}".lower()
    return {w for w in _WORD.findall(text) if len(w) > 3}


def _themes(p: Participant) -> set[str]:
    return set(p.groups)


def _objs(p: Participant) -> set[str]:
    return set(p.objectives)


def _affinity(p: Participant, members: list[Participant]) -> float:
    """How well `p` fits a set of members (higher = better)."""
    if not members:
        return 0.0
    pt, po, pk = _themes(p), _objs(p), _tokens(p)
    score = 0.0
    for m in members:
        score += 2.0 * len(pt & _themes(m))
        score += 1.0 * len(po & _objs(m))
        score += 0.3 * len(pk & _tokens(m))
    return score / len(members)


def _fit(p: Participant, members: list[Participant], wants: dict[int, set[int]]) -> float:
    """Affinity, minus a same-organization penalty, plus a named-request bonus."""
    score = _affinity(p, members)
    org = (p.organization or "").strip().lower()
    if org and any((m.organization or "").strip().lower() == org for m in members):
        score -= _ORG_PENALTY
    wanted = wants.get(p.id, set())
    for m in members:
        if m.id in wanted or p.id in wants.get(m.id, set()):
            score += _MEET_BONUS
    return score


def _org(p: Participant) -> str:
    return (p.organization or "").strip().lower()


def _org_dupes(member_ids: list[int], by_id: dict[int, Participant]) -> list[int]:
    """Member ids that share an organization with an earlier member of the list."""
    seen: set[str] = set()
    dupes: list[int] = []
    for i in member_ids:
        o = _org(by_id[i])
        if o and o in seen:
            dupes.append(i)
        elif o:
            seen.add(o)
    return dupes


def _count_org_conflicts(groups: list[Group], by_id: dict[int, Participant]) -> int:
    return sum(len(_org_dupes(g.member_ids, by_id)) for g in groups)


def _meet_clusters(
    wants: dict[int, set[int]], max_size: int
) -> tuple[list[list[int]], list[list[int]]]:
    """Connected components of the (undirected) 'wants to meet' graph.

    Returns (clusters that fit in a circle, clusters too big to all fit).
    A request from A to B is treated as a mutual must-link.
    """
    parent: dict[int, int] = {}

    def find(a: int) -> int:
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, bs in wants.items():
        for b in bs:
            union(a, b)

    comps: dict[int, list[int]] = {}
    for n in list(parent):
        comps.setdefault(find(n), []).append(n)

    clusters = [sorted(c) for c in comps.values() if len(c) > 1]
    fits = [c for c in clusters if len(c) <= max_size]
    too_big = [c for c in clusters if len(c) > max_size]
    return fits, too_big


def _resolve_org_conflicts(
    groups: list[Group],
    by_id: dict[int, Participant],
    wants: dict[int, set[int]],
    locked: set[int],
) -> int:
    """Swap members between circles to remove same-organization pairings.

    Only size-preserving swaps that create no new conflict are applied, so the
    3–6 sizes are untouched and any remaining pairing is truly unavoidable.
    Must-linked members (`locked`) are never moved, so a named request always
    takes precedence over the same-organization rule. A conflicting member with
    no valid swap is parked in `stuck`; any successful swap clears `stuck`.
    """
    swaps = 0
    stuck: set[int] = set()
    guard = 0
    while guard < 3000:
        guard += 1
        target = None
        for gi, g in enumerate(groups):
            for x in _org_dupes(g.member_ids, by_id):
                if x not in stuck and x not in locked:
                    target = (gi, x)
                    break
            if target:
                break
        if target is None:
            break

        gi, x = target
        g = groups[gi]
        ox = _org(by_id[x])
        g_rest = [m for m in g.member_ids if m != x]

        best = None  # (score, hk, y)
        for hk, h in enumerate(groups):
            if hk == gi:
                continue
            for y in h.member_ids:
                if y in locked:
                    continue
                oy = _org(by_id[y])
                h_rest = [m for m in h.member_ids if m != y]
                if ox and any(_org(by_id[m]) == ox for m in h_rest):
                    continue
                if oy and any(_org(by_id[m]) == oy for m in g_rest):
                    continue
                score = _fit(by_id[x], [by_id[m] for m in h_rest], wants) + _fit(
                    by_id[y], [by_id[m] for m in g_rest], wants
                )
                if best is None or score > best[0]:
                    best = (score, hk, y)

        if best is None:
            stuck.add(x)
            continue

        _, hk, y = best
        h = groups[hk]
        g.member_ids = [y if m == x else m for m in g.member_ids]
        h.member_ids = [x if m == y else m for m in h.member_ids]
        swaps += 1
        stuck.clear()
    return swaps


def balance_groups(
    groups: list[Group],
    participants: list[Participant],
    included_ids: list[int],
    min_size: int = 3,
    max_size: int = 6,
    target: int = 6,
) -> tuple[list[Group], list[str]]:
    """Return (repaired groups, human-readable notes about what changed)."""
    by_id = {p.id: p for p in participants}
    included = set(included_ids)
    wants = compute_meet_links([by_id[i] for i in included_ids if i in by_id])
    notes: list[str] = []

    # Named-request "must-link" clusters: people who asked to be grouped together
    # must end up in the same circle. Clusters larger than a circle can't all fit.
    meet_clusters, oversized_clusters = _meet_clusters(wants, max_size)
    locked = {i for c in meet_clusters for i in c}

    def members(g: Group) -> list[Participant]:
        return [by_id[i] for i in g.member_ids]

    # 1. Clean: keep only included ids, drop cross-group duplicates & empties.
    placed: set[int] = set()
    clean: list[Group] = []
    for g in groups:
        ids = [i for i in g.member_ids if i in included and i not in placed]
        placed.update(ids)
        if ids:
            g.member_ids = ids
            clean.append(g)
    groups = clean or [Group(name="Circle 1", member_ids=[])]

    # 2. Place any applicant the model left out, filling fuller circles first.
    orphans = [i for i in included if i not in placed]
    for oid in orphans:
        p = by_id[oid]
        # Prefer a group below the target size, then best fit, then the emptier
        # one — this spreads people toward the ideal size and reduces the chance
        # of forcing a same-organization pairing.
        candidates = [g for g in groups if len(g.member_ids) < max_size]
        if candidates:
            best = max(
                candidates,
                key=lambda g: (len(g.member_ids) < target, _fit(p, members(g), wants), -len(g.member_ids)),
            )
            best.member_ids.append(oid)
        else:
            groups.append(Group(name="New circle", member_ids=[oid]))
    if orphans:
        notes.append(f"Placed {len(orphans)} applicant(s) the model left unassigned.")

    # 3. Enforce named-request must-links: pull each cluster into one host circle.
    for cluster in meet_clusters:
        loc = {i: gi for gi, g in enumerate(groups) for i in g.member_ids if i in cluster}
        if not loc:
            continue
        host_gi = Counter(loc.values()).most_common(1)[0][0]
        host = groups[host_gi]
        for i in cluster:
            gi = loc.get(i)
            if gi is None or gi == host_gi:
                continue
            groups[gi].member_ids.remove(i)
            host.member_ids.append(i)
    groups = [g for g in groups if g.member_ids]

    # 4. Break up any oversized circle (move worst-fitting member out). Never
    #    evict a must-linked member unless the whole circle is must-linked.
    moved_over = 0
    guard = 0
    while any(len(g.member_ids) > max_size for g in groups) and guard < 1000:
        guard += 1
        big = next(g for g in groups if len(g.member_ids) > max_size)
        bm = members(big)
        evictable = [i for i in big.member_ids if i not in locked] or big.member_ids
        worst = min(evictable, key=lambda i: _affinity(by_id[i], [m for m in bm if m.id != i]))
        big.member_ids.remove(worst)
        targets = [g for g in groups if g is not big and len(g.member_ids) < max_size]
        if targets:
            best = max(
                targets,
                key=lambda g: (len(g.member_ids) < target, _fit(by_id[worst], members(g), wants), -len(g.member_ids)),
            )
            best.member_ids.append(worst)
        else:
            groups.append(Group(name="New circle", member_ids=[worst]))
        moved_over += 1
    if moved_over:
        notes.append(f"Rebalanced {moved_over} member(s) out of oversized circles.")

    # 5. Fill undersized circles to the minimum by pulling best-fit members
    #    from circles that can spare them; merge/redistribute if that fails.
    #    Never pull a must-linked member (would split their cluster).
    fixed_small = 0
    guard = 0
    while any(len(g.member_ids) < min_size for g in groups) and guard < 1000:
        guard += 1
        sg = next(g for g in groups if len(g.member_ids) < min_size)
        need = min_size - len(sg.member_ids)
        sgm = members(sg)

        donors_exist = any(
            g is not sg
            and len(g.member_ids) > min_size
            and any(m not in locked for m in g.member_ids)
            for g in groups
        )
        if donors_exist:
            for _ in range(need):
                best_mid, best_g, best_score = None, None, float("-inf")
                for g in groups:
                    if g is sg or len(g.member_ids) <= min_size:
                        continue
                    for mid in g.member_ids:
                        if mid in locked:
                            continue
                        sc = _fit(by_id[mid], sgm, wants)
                        if sc > best_score:
                            best_mid, best_g, best_score = mid, g, sc
                if best_mid is None:
                    break
                best_g.member_ids.remove(best_mid)
                sg.member_ids.append(best_mid)
                sgm = members(sg)
            fixed_small += 1
        else:
            # No spare donors: merge this circle into the best-fit one with room.
            others = [g for g in groups if g is not sg]
            mergeable = [g for g in others if len(g.member_ids) + len(sg.member_ids) <= max_size]
            if mergeable:
                tgt = max(mergeable, key=lambda g: sum(_fit(m, members(g), wants) for m in sgm))
                tgt.member_ids.extend(sg.member_ids)
                sg.member_ids = []
            else:
                for mid in list(sg.member_ids):
                    cand = [g for g in others if len(g.member_ids) < max_size]
                    if not cand:
                        break
                    tgt = max(cand, key=lambda g: _fit(by_id[mid], members(g), wants))
                    tgt.member_ids.append(mid)
                    sg.member_ids.remove(mid)
            groups = [g for g in groups if g.member_ids]
            fixed_small += 1
    if fixed_small:
        notes.append(f"Resized {fixed_small} circle(s) to meet the {min_size}-member minimum.")

    groups = [g for g in groups if g.member_ids]

    # 6. Resolve same-organization pairings via size-preserving swaps. Must-linked
    #    members are never moved, so a named request always wins over the org rule.
    resolved = _resolve_org_conflicts(groups, by_id, wants, locked)
    if resolved:
        notes.append(f"Separated {resolved} same-institution pairing(s) into different circles.")
    remaining = _count_org_conflicts(groups, by_id)
    if remaining:
        notes.append(
            f"{remaining} same-institution pairing(s) could not be avoided without "
            "breaking the size limits or a requested pairing — please review."
        )

    # 7. Report on named requests.
    if meet_clusters:
        gi_of = {i: gi for gi, g in enumerate(groups) for i in g.member_ids}
        honored = sum(1 for c in meet_clusters if len({gi_of.get(i) for i in c}) == 1)
        notes.append(f"Honored {honored}/{len(meet_clusters)} named collaboration request(s).")
    if oversized_clusters:
        n = len(oversized_clusters)
        notes.append(
            f"{n} named request group(s) were larger than {max_size} people and could "
            "not all be placed together."
        )

    # Final sanity note if constraints still can't hold (e.g. fewer than
    # min_size applicants total).
    bad = [g for g in groups if not (min_size <= len(g.member_ids) <= max_size)]
    if bad and len(included) >= min_size:
        notes.append("Some circles are still outside 3–6 — please adjust manually.")

    return groups, notes
