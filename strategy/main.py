"""MechMania 32 combat-first strategy.

Replacement preserving the supplied engine API and local tuning commands.
Uses staged economy, threat-based defense, obstacle-aware targeting and
reachable healer assignments. Match performance must be checked in the engine.
"""

from __future__ import annotations

# The engine injects Vec2, GameState, BotClass, MAP_SIZE and friends into the package.
# Running this file directly (for tuning) has no package, so the star import fails --
# that is expected and harmless, because nothing outside Brain needs those names at
# import time.  `from __future__ import annotations` is what keeps the type hints from
# being evaluated when the engine is absent.
try:
    from . import *  # noqa: F403
    _ENGINE = True
except ImportError:  # running as a plain script, for tuning
    _ENGINE = False

import json as _json
import math
import os as _os
import random
from dataclasses import asdict, dataclass, fields

# ═══════════════════════════════════════════════════════════════════════════════
#  LOCAL TESTING SWITCH -- READ THIS BEFORE YOU SUBMIT
# ═══════════════════════════════════════════════════════════════════════════════
#  `mm-cli run` plays the bot against itself, so with one strategy both sides make
#  the same decisions and the match tells you nothing.  While this is True, team 1
#  runs `sparring_strategy` instead -- a deliberately mediocre, randomised bot -- so
#  a local match measures whether the real strategy actually wins.
#
#  SET IT TO False BEFORE `mm-cli submit`.  The engine mirrors the world for the
#  top-right team, so in a real match both sides must run `Brain.act`; leaving this
#  on means roughly half the tournament is played by the sparring partner.
#
#  The tuner needs it False as well: it plays candidate against candidate, and the
#  sparring partner is only one of its opponents.  `selftest` refuses to run with
#  this on.
# ═══════════════════════════════════════════════════════════════════════════════
SPARRING_MODE = False


# ───────────────────────────────── tuning ──────────────────────────────────────
# Everything here is a preference; every *rule* comes from `get_config()`.
#
# These used to be module-level constants.  They hang off the Brain instead because
# the engine runs both teams inside one process: module globals are shared, so the
# tuner could not have team 0 play parameter set A while team 1 plays set B.


@dataclass
class Params:
    """One fleet's worth of preferences.  Defaults are the hand-tuned values."""

    # ── economy ────────────────────────────────────────────────────────────────
    # Deposit slots are SHARED with the enemy, so every slot we leave empty is one
    # they can mine from.  Take the lot; the code caps this at `extractor_cap`.
    extractors_wanted: int = 5
    battle_per_healer: int = 3          # favor shooters; healers support a line rather than replace it
    max_healers: int = 8
    extractor_build_stop: float = 0.55  # match fraction after which a new miner cannot pay off

    # ── holding the point ──────────────────────────────────────────────────────
    # One body inside the circle is the entire requirement; a second is insurance
    # against losing the first, and a third is a bot not shooting anybody.
    anchors_wanted: int = 2
    anchor_radius: float = 2.05         # inside `capture_radius`, outside the payload hull
    line_radius: float = 4.60           # the shooting line: well inside blaster range
    line_spacing: float = 1.30          # gap between the line's rings when it gets crowded
    line_per_ring: int = 10
    line_arc: float = 110.0             # how wide the line fans across the side it screens
    threat_range: float = 14.0          # enemies this close to the payload set the front

    # ── giving ground ──────────────────────────────────────────────────────────
    rally_back: float = 0.20            # how far back down the path a beaten line regroups
    outnumber_margin: int = 1           # enemy surplus at the point that triggers a regroup
    # Regrouping concedes the circle, which is only affordable while the capture race is
    # close.  Below this capture value we are losing and every body contests instead.
    # Once the payload crosses the midpoint, a regroup gifts the opponent a
    # compounding positional advantage.  Keep the circle contested instead.
    rally_min_capture: float = -0.05
    # A small fleet that is behind stops trying to win fights and freezes the payload:
    # a contested circle moves for nobody, and frozen beats losing.
    freeze_fleet: int = 4
    freeze_below: float = -0.05

    # ── defending the deposit ──────────────────────────────────────────────────
    # A standing garrison, not bots recruited by proximity once the raid has landed.
    # The old rule only considered battle bots already closer to the deposit than to
    # the payload, which is the empty set exactly when the line is forward.
    # A raid that reaches the miners is decisively expensive: extractors are
    # stationary, clustered, and the token income is needed to replace losses.
    # Keep a real picket and reinforce it before raiders have firing position.
    garrison_base: int = 0              # a picket; the full response is raid-triggered
    garrison_per_raider: float = 1.5    # extra bodies per detected raider
    garrison_max: int = 8
    garrison_min_battle: int = 5        # start protecting income before the line is large
    home_healers: int = 2               # sustain the garrison through a real raid
    raid_radius: float = 12.0           # detect and intercept before blaster range

    # ── raiding theirs ─────────────────────────────────────────────────────────
    raid_squad: int = 2                 # never hollow out the payload line for a raid
    raid_start: float = 0.08            # match fraction before which we do not bother
    raid_stop: float = 0.62             # ... and after which their tokens no longer matter
    raid_min_battle: int = 14            # only with a fleet big enough to hold and raid

    # ── shooting ───────────────────────────────────────────────────────────────
    aim_slack: float = 0.04             # shaved off the target hull so a marginal shot is not taken
    wounded_pullback: float = 4.0       # health at or below which a body drifts to the healers
    pullback_min_inside: int = 3        # ... but only while this many bodies hold the circle
    # Blaster damage is 3 of 10 health, so a miner that waits for half health has already
    # taken two hits and cannot outrun a blaster with three times its reach.  Leave on the
    # first hit; the garrison is what actually saves it.
    miner_flee_health: float = 7.0

    # `_pick_target` scoring weights, lowest score wins.  The old lexicographic tuple
    # asserted that one point of health always outranks any distance difference; a
    # weighted sum over the same features lets that trade-off be measured instead.
    w_shielded: float = 40.0            # still inside its invulnerability window
    w_on_point: float = 6.0             # standing off the capture circle
    w_health: float = 1.0               # finishing a wounded body beats chipping a fresh one
    w_gap: float = 0.10                 # prefer the nearer, more reliable shot
    w_guarded: float = 12.0             # per enemy healer in range: the shot gets out-healed
    w_is_healer: float = -14.0          # killing the healer is how the stack comes apart

    cheap_budget: int = 1200            # compute bank remaining below which we cut corners

    #: fields the engine needs as whole numbers
    _INTS = (
        "extractors_wanted", "battle_per_healer", "max_healers", "anchors_wanted",
        "line_per_ring", "pullback_min_inside", "outnumber_margin", "freeze_fleet",
        "garrison_base", "garrison_max", "garrison_min_battle", "home_healers",
        "raid_squad", "raid_min_battle", "cheap_budget",
    )

    def __post_init__(self) -> None:
        # The tuner searches in continuous space and hands us floats.
        for name in self._INTS:
            setattr(self, name, int(round(getattr(self, name))))

    @classmethod
    def from_mapping(cls, data) -> "Params":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dict(data).items() if k in known})

    @classmethod
    def from_env(cls, team: int) -> "Params":
        """MM_TUNE_PARAMS_<team> is JSON written by the tuner; absent in a real match."""
        raw = _os.environ.get(f"MM_TUNE_PARAMS_{int(team)}")
        if not raw:
            return cls()
        try:
            return cls.from_mapping(_json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            print(f"[strategy] ignoring bad MM_TUNE_PARAMS_{team}: {exc!r}")
            return cls()

    def to_dict(self) -> dict:
        return asdict(self)


# Degrees off the rear axis.  Deliberately not 0: a bot parked dead behind the payload is
# in cover, but the payload blocks its own shots too, and a stalemate at nil-nil is a draw.
# A shape rather than a scalar, so it is not part of the search.
ANCHOR_ANGLES = (70.0, -70.0, 110.0, -110.0, 45.0, -45.0, 135.0, -135.0, 20.0, -20.0)


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _sorted_by(bots, key):
    return sorted(bots, key=key)


def _inside(p):
    """Anything we hand to `navigate_to` has to be somewhere on the map."""
    edge = 0.6
    return Vec2(_clamp(p.x, edge, MAP_SIZE - edge), _clamp(p.y, edge, MAP_SIZE - edge))  # noqa: F405


class Brain:
    """One match's worth of state.  `act` is the strategy the engine calls each tick."""

    def __init__(self, params: "Params | None" = None) -> None:
        self.p = params or Params()
        self._conf = None
        self._spots = None
        self._home = None
        self._announced = False
        self._can_read_class = True  # flipped off if BotState exposes no `class_` for enemies

    # ────────────────────────────── plumbing ───────────────────────────────

    @property
    def conf(self):
        if self._conf is None:
            self._conf = get_config()  # noqa: F405
        return self._conf

    @property
    def home(self):
        """Our own spawn corner -- where `spawn_bot` puts every new body."""
        if self._home is None:
            r = self.conf.bot.radius
            self._home = Vec2(r + 0.1, float(MAP_SIZE) - r - 0.1)  # noqa: F405
        return self._home

    @property
    def enemy_home(self):
        """Their spawn corner: `spawn_bot` mirrors our own, so it is ours rotated."""
        return Vec2(float(MAP_SIZE) - self.home.x, float(MAP_SIZE) - self.home.y)  # noqa: F405

    def act(self, state):
        """Never let a bug cost the match: a raised exception would kill the process."""
        try:
            return self._act(state)
        except Exception as exc:  # noqa: BLE001 -- last line of defence
            print(f"[strategy] tick {state.tick}: {exc!r}")
            return self._fallback(state)

    def _fallback(self, state):
        """Contest the payload and keep building.  Correct, if not clever."""
        action = FleetAction.new()  # noqa: F405
        try:
            payload = state.payload_pos()
            for bot in state.fleet_me:
                ba = action.bots[bot.id]
                ba.move_action = move_bot(navigate_to(bot.pos, payload))  # noqa: F405
                ba.turn_action = turn_towards(payload)  # noqa: F405
            action.fabricator_next = int(BotClass.Battle)  # noqa: F405
            action.rush_order = (
                state.fabricator_me.tokens >= self.conf.fabricator.rush_cost
                and not state.fleet_me.is_full()
            )
        except Exception:  # noqa: BLE001
            pass
        return action

    # ─────────────────────────────── the tick ──────────────────────────────

    def _act(self, state):
        conf = self.conf
        p = self.p
        action = FleetAction.new()  # noqa: F405

        if not self._announced:
            self._announced = True
            print(
                f"[strategy] online: {conf.max_ticks} ticks, endgame at "
                f"{conf.max_ticks - conf.endgame_ticks}"
            )

        tick = state.tick
        phase = tick / float(max(1, conf.max_ticks))
        endgame = tick >= conf.max_ticks - conf.endgame_ticks
        cheap = get_budget().remaining < p.cheap_budget  # noqa: F405

        payload = state.payload_pos()
        allies = list(state.fleet_me)
        enemies = list(state.fleet_other)
        # A bot's `vel` is what it actually moved last tick, the best one-tick predictor
        # we have -- and the engine resolves shots *after* everyone has moved.
        enemies_pred = [(e, e.pos + e.vel) for e in enemies]

        # 1 ─ economy ────────────────────────────────────────────────────────
        action.fabricator_next = int(self._next_class(state, endgame))
        action.rush_order = (
            not endgame
            and not state.fleet_me.is_full()
            and state.fabricator_me.tokens >= conf.fabricator.rush_cost
        )

        if not allies:
            return action

        # Healer bookkeeping, done once per tick rather than once per shooter.  A target
        # with `heal_stack_cap` healers in range restores as much per cooldown cycle as
        # the same number of shooters take off, so shooting it throws the volley away.
        enemy_healers, guards = self._heal_support(enemies)
        healer_ids = {int(h.id) for h in enemy_healers}

        # 2 ─ roles ──────────────────────────────────────────────────────────
        miners = [b for b in allies if b.class_ == BotClass.Extractor]  # noqa: F405
        healers = [b for b in allies if b.class_ == BotClass.Healer]  # noqa: F405
        battle = [b for b in allies if b.class_ == BotClass.Battle]  # noqa: F405

        deposit = state.deposit_me.pos
        their_deposit = state.deposit_other.pos
        self._shot_deposits = (deposit, their_deposit)
        raiders = [e for e in enemies
                   if self._class_of(e) in (BotClass.Battle, None)
                   and e.pos.dist(deposit) <= p.raid_radius]

        # Garrison.  A standing reserve, chosen by distance to the deposit but NOT
        # conditioned on already being nearer the deposit than the payload -- that old
        # test was the empty set exactly when the line was forward and the miners were
        # dying, which is how a fleet loses six extractors without ever answering.
        garrison = []
        if not endgame and len(battle) >= p.garrison_min_battle:
            want = p.garrison_base + int(round(p.garrison_per_raider * len(raiders)))
            want = min(want, p.garrison_max, max(0, len(battle) - p.anchors_wanted))
            if want > 0:
                garrison = _sorted_by(
                    battle, key=lambda b: (b.pos.dist_sq(deposit), b.id)
                )[:want]
        garrison_ids = {b.id for b in garrison}

        # Strike squad.  Their miners are worth more than our place on the line: a fleet
        # with no token income cannot rush a replacement for anything it loses, and every
        # fight after that is one-sided.
        strike = []
        if (
            not endgame
            and p.raid_squad > 0
            and state.capture >= 0.0
            and not raiders
            and len([b for b in battle if b.pos.dist(payload) <= conf.bot.blaster_range])
                >= len([e for e in enemies
                        if self._class_of(e) in (BotClass.Battle, None)
                        and e.pos.dist(payload) <= conf.bot.blaster_range]) + p.raid_squad + 3
            and p.raid_start <= phase <= p.raid_stop
            and len(battle) - len(garrison) >= p.raid_min_battle
        ):
            spare = [b for b in battle if b.id not in garrison_ids]
            want = min(p.raid_squad, max(0, len(spare) - p.anchors_wanted))
            if want > 0:
                strike = _sorted_by(
                    spare, key=lambda b: (b.pos.dist_sq(their_deposit), b.id)
                )[:want]
        strike_ids = {b.id for b in strike}

        # In the endgame, extractors still cannot fire.  Sending the whole mining
        # corps into a stronger battle line turns it into free eliminations; keep most
        # of them safe as a reserve and use at most one to provide point insurance.
        bodies = [b for b in battle if b.id not in garrison_ids and b.id not in strike_ids]
        if endgame:
            bodies = bodies + miners[:1]
            working_miners = miners[1:]
        else:
            working_miners = miners

        # Freeze.  A contested circle moves for nobody.  Down to a handful of bots and
        # behind on capture, standing in the circle is worth more than any shot we could
        # take from outside it -- match 216 was lost by a trickle of survivors wandering
        # while the enemy walked the payload home uncontested for three thousand ticks.
        freeze = (
            not endgame
            and len(allies) <= p.freeze_fleet
            and state.capture < p.freeze_below
        )

        # Last-bot insurance.  An empty fleet during the endgame loses on the spot, and
        # that is the one way a won position can still be thrown away: if the payload is
        # on their side of the middle we take the tiebreak, so the fleet must not hit zero.
        # One body peels off and sits in the corner rather than trading to the last bot.
        survivor_id = -1
        if endgame and allies:
            ahead_on_tiebreak = state.capture > 0.0
            outgunned = len(enemies) >= 2 * len(allies)
            if len(allies) <= (6 if ahead_on_tiebreak else 4) or (outgunned and len(allies) <= 8):
                enemy_mass = self._front(payload, enemies)
                survivor_id = int(
                    max(allies, key=lambda b: (b.pos.dist(enemy_mass), b.health, -b.id)).id
                )

        # 3 ─ movement and facing ────────────────────────────────────────────
        self._orders_miners(state, action, working_miners, enemies, raiders, survivor_id)
        aims = self._orders_bodies(
            state, action, bodies, enemies_pred, payload, endgame, survivor_id, cheap,
            freeze, guards, healer_ids,
        )
        aims.update(
            self._orders_garrison(state, action, garrison, raiders, enemies_pred, deposit,
                                  cheap, guards, healer_ids)
        )
        aims.update(
            self._orders_strike(state, action, strike, enemies, enemies_pred, their_deposit,
                                cheap, guards, healer_ids)
        )
        home_ids = garrison_ids | {b.id for b in miners}
        self._orders_healers(
            state, action, healers, allies, enemies, payload, survivor_id,
            home_ids, bool(raiders), deposit,
        )

        # 4 ─ fire control ───────────────────────────────────────────────────
        self._fire_control(state, action, aims, enemies_pred)

        return action

    # ───────────────────────────── healer support ──────────────────────────

    def _class_of(self, bot):
        """`class_` on an enemy BotState, or None if the engine does not expose it.

        Probed once: if the attribute is missing the healer features simply go quiet
        rather than raising into the per-tick fallback every tick.
        """
        if not self._can_read_class:
            return None
        try:
            return bot.class_
        except Exception:  # noqa: BLE001
            self._can_read_class = False
            return None

    def _heal_support(self, enemies):
        """Enemy healers, and how many of them are in range of each enemy bot.

        `heal_per_tick * heal_stack_cap` per tick against `blaster_damage` per
        `blaster_cooldown` means a fully stacked target out-heals the same number of
        shooters exactly, so this count is what tells `_pick_target` when a volley would
        be absorbed rather than banked.
        """
        conf = self.conf
        cap = max(1, int(conf.bot.heal_stack_cap))
        reach = conf.bot.base_heal_range
        healers = [e for e in enemies if self._class_of(e) == BotClass.Healer]  # noqa: F405
        if not healers:
            return [], {}
        guards = {}
        for enemy in enemies:
            n = 0
            for h in healers:
                if h.id != enemy.id and h.pos.dist(enemy.pos) <= reach:
                    n += 1
                    if n >= cap:
                        break
            if n:
                guards[int(enemy.id)] = n
        return healers, guards

    # ───────────────────────────── the fabricator ──────────────────────────

    def _next_class(self, state, endgame: bool):
        """What the next body should be: economy first, then a battle line, then the
        healers that keep it standing."""
        conf = self.conf
        p = self.p

        miners = healers = battle = 0
        for bot in state.fleet_me:
            cls = bot.class_
            if cls == BotClass.Extractor:  # noqa: F405
                miners += 1
            elif cls == BotClass.Healer:  # noqa: F405
                healers += 1
            else:
                battle += 1

        if endgame:
            return BotClass.Battle

        # Reach 10 fighters / 3 miners / 3 healers in an interleaved opening.
        # Live counts also make recovery prioritize fighters after combat losses.
        if battle < 2:
            return BotClass.Battle
        deposit = state.deposit_me.pos
        threats = [e for e in state.fleet_other
                   if self._class_of(e) in (BotClass.Battle, None)
                   and e.pos.dist(deposit) <= p.raid_radius]
        mining_safe = not threats
        cap = min(p.extractors_wanted, int(conf.deposit.extractor_cap))
        travel_ticks = self.home.dist(deposit) / max(conf.bot.speed, 1e-6)
        payback_ticks = conf.fabricator.rush_cost / max(conf.bot.extract_rate, 1e-6)
        remaining = conf.max_ticks - conf.endgame_ticks - state.tick
        mining_window = (state.tick < conf.max_ticks * p.extractor_build_stop
                         and remaining > travel_ticks + payback_ticks)
        milestones = ((2, 1, 0), (4, 1, 1), (6, 2, 1),
                      (8, 2, 2), (10, 3, 3))
        for fighters, workers, medics in milestones:
            if battle < fighters:
                return BotClass.Battle
            if mining_safe and mining_window and miners < min(workers, cap):
                return BotClass.Extractor
            if healers < min(medics, p.max_healers):
                return BotClass.Healer

        if healers < min(p.max_healers, battle // max(1, p.battle_per_healer)):
            return BotClass.Healer
        # Add workers only after the frontline can support them.
        supported_workers = min(cap, 3 + max(0, battle - 10) // 3)
        if mining_safe and mining_window and miners < supported_workers:
            return BotClass.Extractor
        return BotClass.Battle

    # ──────────────────────────────── miners ───────────────────────────────

    def _mine_spots(self, state):
        """Places a bot can stand, see our deposit from, and mine it.

        Worked out once -- the deposit never moves and neither do the walls.  Close rings
        first: the nearer the ring, the wider the aiming error the extraction ray forgives.
        """
        if self._spots is not None:
            return self._spots

        conf = self.conf
        deposit = state.deposit_me.pos
        reach = conf.bot.base_extract_range * 0.85
        spots = []

        for radius in (1.5, 2.1, 2.7, 3.4, 4.0):
            if radius > reach:
                break
            for step in range(24):
                p = deposit + Vec2.from_angle_deg(step * 15.0) * radius  # noqa: F405
                if not (0.5 < p.x < MAP_SIZE - 0.5 and 0.5 < p.y < MAP_SIZE - 0.5):  # noqa: F405
                    continue
                if any(p.dist_sq(q) < 0.49 for q in spots):
                    continue  # splash is measured to the hull: do not park bots on top of each other
                if not point_free(p):  # noqa: F405
                    continue
                if not line_of_sight(p, deposit):  # noqa: F405
                    continue
                spots.append(p)
            if len(spots) >= int(conf.deposit.extractor_cap):
                break

        if not spots:  # nothing legal found: stand off the hull and let collision sort it
            spots = [deposit + Vec2(0.0, conf.deposit.radius + conf.bot.radius + 0.1)]  # noqa: F405

        self._spots = spots
        return spots

    def _orders_miners(self, state, action, miners, enemies, raiders, survivor_id: int) -> None:
        if not miners:
            return

        conf = self.conf
        deposit = state.deposit_me.pos
        spots = self._mine_spots(state)

        # Under a raid, work the far side of the deposit.  The deposit is a solid disc
        # that stops shots, so the spots behind it are the ones that keep mining.
        if raiders:
            threat_centre = self._front(deposit, raiders)
            spots = _sorted_by(spots, key=lambda s: -s.dist_sq(threat_centre))

        # Stable by id, so nobody swaps spots with a neighbour every tick.
        for index, bot in enumerate(sorted(miners, key=lambda b: b.id)):
            ba = action.bots[bot.id]
            # Free to ask every tick: out of range or out of sight it simply does not land.
            ba.special_action = SpecialAction.Extractor(mine=True)  # noqa: F405
            ba.turn_action = turn_towards(deposit)  # noqa: F405

            if bot.id == survivor_id:
                ba.move_action = move_bot(navigate_to(bot.pos, self.home))  # noqa: F405
                continue

            threat = self._nearest(bot.pos, enemies)
            if (
                bot.health <= self.p.miner_flee_health
                and threat is not None
                and bot.pos.dist(threat.pos) <= conf.bot.blaster_range + 1.0
            ):
                # The body is worth a rush order; the tokens it would have mined are not.
                # Put the deposit between it and the shooter rather than running into the
                # open: a miner at 0.05 speed cannot outrun a blaster reaching 10 units.
                cover = deposit + (deposit - threat.pos).normalize_or_zero() * (
                    conf.deposit.radius + conf.bot.radius + 0.3
                )
                ba.move_action = move_bot(navigate_to(bot.pos, _inside(cover)))  # noqa: F405
                continue

            spot = spots[index % len(spots)]
            if bot.pos.dist(spot) > 0.35:
                ba.move_action = move_bot(navigate_to(bot.pos, spot))  # noqa: F405
            # else: hold still -- a stationary miner keeps its ray on the deposit.

    # ───────────────────────── bodies on the payload ───────────────────────

    def _front_angle(self, payload, enemies) -> float:
        """The heading from the payload towards wherever the enemy is coming from.

        Their standing bodies if any are close, otherwise their spawn corner -- every bot
        they build appears there, so with the point clear that heading is the lane their
        reinforcements have to walk up.  Screening it is what stops the last stretch of a
        push from stalling on a body that pops out next to their own goal.
        """
        close = [e for e in enemies if e.pos.dist(payload) <= self.p.threat_range]
        reference = self._front(payload, close) if close else self.enemy_home
        delta = reference - payload
        if delta.norm_sq() < 1e-8:
            return 0.0
        return delta.angle_deg()

    def _hold_slots(self, state, payload, count: int, front: float, cheap: bool,
                    anchors_override: int = -1):
        """`count` standing places: anchors on the point, then a firing line behind it.

        The anchors sit inside the capture circle -- that is the whole game, since one
        body in there is all it takes to stop an enemy push.  They sit on the far side of
        the payload from the enemy, but off its flanks rather than dead behind it: behind
        is cover, but the payload stops their own shots as well, and two fleets in mutual
        cover is a nil-nil draw.

        Everyone else forms a line at `line_radius` across the side the enemy is coming
        from, so the reinforcement walking at the point is shot before it arrives.  The
        blaster reaches four times further than the capture circle is wide, so the line
        covers the whole circle without standing in the scrum.

        `anchors_override` puts every body inside the circle -- used by the freeze.
        """
        p = self.p
        rear = front + 180.0
        out = []

        want = p.anchors_wanted if anchors_override < 0 else anchors_override
        anchors = min(count, want)
        for k in range(anchors):
            angle = ANCHOR_ANGLES[k % len(ANCHOR_ANGLES)]
            out.append(self._legal_slot(payload, rear + angle, p.anchor_radius, cheap))

        span = p.line_arc * 2.0 / max(1, p.line_per_ring - 1)
        for k in range(count - anchors):
            ring, seat = divmod(k, max(1, p.line_per_ring))
            side = 1.0 if seat % 2 == 0 else -1.0
            angle = front + ((seat + 1) // 2) * span * side
            out.append(
                self._legal_slot(payload, angle, p.line_radius + ring * p.line_spacing, cheap)
            )
        return out

    def _legal_slot(self, payload, angle: float, radius: float, cheap: bool):
        p = _inside(payload + Vec2.from_angle_deg(angle) * radius)  # noqa: F405
        if cheap or point_free(p):  # noqa: F405
            return p
        for nudge in (18.0, -18.0, 36.0, -36.0, 60.0, -60.0):
            q = _inside(payload + Vec2.from_angle_deg(angle + nudge) * radius)  # noqa: F405
            if point_free(q):  # noqa: F405
                return q
        return payload

    def _combat_balance(self, fighters, enemies_pred, payload):
        # Include our entire shooting line, and only count combat-capable enemies.
        radius = max(self.p.line_radius + 3.0, self.conf.bot.blaster_range)
        enemy_fighters = [(e, ep) for e, ep in enemies_pred
                          if self._class_of(e) in (BotClass.Battle, None)]
        local_us = [b for b in fighters if b.pos.dist(payload) <= radius]
        local_them = [(e, ep) for e, ep in enemy_fighters if ep.dist(payload) <= radius]
        # Nearby reinforcements count when they have a lane to an engaged enemy.
        support = [b for b in fighters if b not in local_us and any(
            b.pos.dist(ep) <= self.conf.bot.blaster_range and line_of_sight(b.pos, ep)
            for _, ep in local_them)]
        ours = sum(0.5 + 0.5 * b.health / self.conf.bot.health
                   for b in local_us + support)
        theirs = sum(0.5 + 0.5 * e.health / self.conf.bot.health
                     for e, _ in local_them)
        return ours, theirs, local_us

    def _retreat_state(self, tick, ours, theirs, allowed):
        if not hasattr(self, "_retreat_until"):
            self._retreat_until = -1
            self._bad_since = None
            self._retreat_cooldown = -1
        bad = theirs >= 4 and theirs > ours * 1.45 + 1.0
        if not allowed:
            self._retreat_until = -1
            self._bad_since = None
            return False
        if self._retreat_until >= tick:
            if ours >= theirs * 1.1:
                self._retreat_until = -1
                self._retreat_cooldown = tick + 120
                return False
            return True
        if self._retreat_until >= 0:
            self._retreat_until = -1
            self._retreat_cooldown = tick + 120
            self._bad_since = None
        if bad and tick >= self._retreat_cooldown:
            if self._bad_since is None:
                self._bad_since = tick
            if tick - self._bad_since >= 35:
                self._retreat_until = tick + 120
                self._bad_since = None
                return True
        else:
            self._bad_since = None
        return False

    def _separated_step(self, bot, target, peers, cheap=False):
        step = (navigate_to(bot.pos, target) if bot.pos.dist(target) > 0.18
                else Vec2(0.0, 0.0))
        push = Vec2(0.0, 0.0)
        # A local repulsion acts on the route as well as at the destination.
        for other in peers:
            if other.id == bot.id:
                continue
            delta = bot.pos - other.pos
            distance = delta.norm()
            if distance >= 0.95:
                continue
            if distance < 1e-5:
                lo, hi = sorted((int(bot.id), int(other.id)))
                delta = Vec2.from_angle_deg((lo * 97 + hi * 53) % 360)
                if int(bot.id) != lo:
                    delta = delta * -1.0
            else:
                delta = delta * (1.0 / distance)
            push = push + delta * (0.95 - distance)
        mixed = step + push * 1.5
        if mixed.norm_sq() < 1e-8:
            return step
        candidate = mixed.normalize_or_zero()
        if point_free(bot.pos + candidate * self.conf.bot.speed):
            return candidate
        return step

    def _orders_bodies(
        self, state, action, bodies, enemies_pred, payload, endgame, survivor_id, cheap,
        freeze, guards, healer_ids,
    ) -> dict:
        aims = {}
        if not bodies:
            return aims
        conf, p = self.conf, self.p
        fighters = [b for b in bodies if b.class_ == BotClass.Battle
                    and b.id != survivor_id]
        active_ids = {int(b.id) for b in fighters}
        if not hasattr(self, "_target_memory"):
            self._target_memory = {}
        self._target_memory = {k: v for k, v in self._target_memory.items()
                               if k in active_ids}
        desired_front = self._front_angle(payload, [e for e, _ in enemies_pred])
        last_front = getattr(self, "_formation_front", desired_front)
        front = normalize_degrees(last_front + _clamp(
            diff_degrees(desired_front, last_front), -1.5, 1.5))
        self._formation_front = front
        ours, theirs, local = self._combat_balance(fighters, enemies_pred, payload)
        retreat = self._retreat_state(state.tick, ours, theirs,
                                      not freeze and not endgame and state.capture > -0.65)
        # Keep anchors stable instead of reassigning every time distances cross.
        previous = getattr(self, "_anchor_ids", [])
        anchors = [bid for bid in previous if bid in active_ids]
        for b in sorted(fighters, key=lambda b: (b.pos.dist_sq(payload), b.id)):
            if len(anchors) >= p.anchors_wanted:
                break
            if int(b.id) not in anchors:
                anchors.append(int(b.id))
        self._anchor_ids = anchors
        by_id = {int(b.id): b for b in bodies}
        holders = [by_id[bid] for bid in anchors]
        holders += sorted([b for b in bodies if int(b.id) not in anchors], key=lambda b: b.id)
        slots = self._hold_slots(state, payload, len(holders), front, cheap,
                                 anchors_override=len(holders) if freeze else -1)
        # A short retreat retains supporting distance; every bot gets its own slot.
        if retreat and not getattr(self, "_was_retreating", False):
            self._rally_center = payload_pos(_clamp(state.capture - 0.065, -1.0, 1.0))
        self._was_retreating = retreat
        rally = getattr(self, "_rally_center", payload)
        # Stage near the leading group, never farther forward than that group.
        leading = sorted(fighters, key=lambda b: b.pos.dist_sq(payload))[:6]
        if leading:
            stage = Vec2(sum(b.pos.x for b in leading) / len(leading),
                         sum(b.pos.y for b in leading) / len(leading))
        else:
            stage = payload
        for index, bot in enumerate(holders):
            ba = action.bots[bot.id]
            anchor = int(bot.id) in anchors or freeze
            target = slots[index]
            enemy_close = any(self._class_of(e) in (BotClass.Battle, None)
                              and bot.pos.dist(ep) <= conf.bot.blaster_range
                              for e, ep in enemies_pred)
            support = sum(b.id != bot.id and b.pos.dist(bot.pos) <= 5.0 for b in fighters)
            staging = False
            if bot.id == survivor_id:
                target = self.home
            elif retreat:
                target = self._legal_slot(rally, front + 90.0 + (index % 7) * 30.0,
                                          1.4 + (index // 7) * 1.1, False)
                staging = True
            elif not freeze and not endgame:
                # Do not send an unsupported anchor into the enemy formation.
                if anchor and enemy_close and support < 3 and len(fighters) >= 4:
                    target = self._legal_slot(stage, front + 120.0 + index * 45.0, 1.2, False)
                    staging = True
                elif bot.pos.dist(stage) > 6.0 and theirs > ours and len(local) >= 3:
                    target = self._legal_slot(stage, front + 150.0 + (index % 5) * 15.0,
                                              2.0 + (index // 5) * 0.8, False)
                    staging = True
            mark = self._pick_target(bot.pos, enemies_pred, payload, cheap,
                                     bot.next_fire_tick <= state.tick
                                     if bot.class_ == BotClass.Battle else False,
                                     state.tick, guards, healer_ids, shooter=bot)
            if (mark is not None and not anchor and not staging
                    and bot.id != survivor_id):
                # Stop a supporting shooter walking away from a usable firing lane.
                if (bot.pos.dist(mark[1]) <= conf.bot.blaster_range - 0.5
                        and bot.pos.dist(payload) <= p.line_radius + 2.0):
                    target = bot.pos
            elif (mark is None and not anchor and not staging and not cheap
                  and bot.id != survivor_id):
                # Find a nearby firing lane instead of circling a blocked target.
                for sign in (1.0, -1.0):
                    probe = self._legal_slot(bot.pos, front + sign * 90.0, 1.4, False)
                    if probe.dist(payload) > max(p.line_radius + 3.0, bot.pos.dist(payload) + 0.2):
                        continue
                    if self._pick_target(probe, enemies_pred, payload, False, True,
                                         state.tick, guards, healer_ids) is not None:
                        target = probe
                        break
            step = self._separated_step(bot, target, bodies, cheap)
            ba.move_action = move_bot(step)
            here = bot.pos + self._applied(step, conf.bot.speed)
            if bot.class_ != BotClass.Battle:
                ba.turn_action = turn_towards(payload)
                ba.special_action = SpecialAction.Extractor(mine=True)
                continue
            ba.special_action = SpecialAction.Battle(fire=False)
            mark = self._pick_target(here, enemies_pred, payload, cheap,
                                     bot.next_fire_tick <= state.tick, state.tick,
                                     guards, healer_ids, shooter=bot)
            if mark is None:
                nearest = min(enemies_pred, key=lambda item: here.dist_sq(item[1]), default=None)
                ba.turn_action = turn_towards(nearest[1] if nearest else payload)
                self._target_memory.pop(int(bot.id), None)
                continue
            enemy, mark_pos = mark
            self._target_memory[int(bot.id)] = int(enemy.id)
            ba.turn_action = turn_towards(mark_pos)
            aims[int(bot.id)] = (bot, here, self._after_turn(bot, here, mark_pos))
        return aims

    # ──────────────────────────────── garrison ─────────────────────────────

    def _orders_garrison(self, state, action, garrison, raiders, enemies_pred, deposit,
                         cheap, guards, healer_ids) -> dict:
        """The standing home guard.

        With raiders present it engages them; with the deposit quiet it pickets the lane
        a raid would walk up, which is also where it can shoot one before it reaches the
        miners.  It is chosen every tick, so a bot that drifts forward gets replaced by
        whoever is nearer -- the role stays filled even as bodies come and go.
        """
        aims = {}
        if not garrison:
            return aims

        conf = self.conf
        speed = conf.bot.speed
        payload = state.payload_pos()
        approach = (self.enemy_home - deposit).normalize_or_zero()
        if approach.norm_sq() < 1e-8:
            approach = Vec2(1.0, 0.0)  # noqa: F405
        flank = Vec2(-approach.y, approach.x)  # noqa: F405

        for slot, bot in enumerate(sorted(garrison, key=lambda b: b.id)):
            ba = action.bots[bot.id]
            mark = self._nearest(bot.pos, raiders) if raiders else None

            if mark is not None:
                mark_pos = mark.pos + mark.vel
                # Close to a range where the shot is forgiving, then stand and shoot.
                step = (
                    navigate_to(bot.pos, mark_pos)  # noqa: F405
                    if bot.pos.dist(mark_pos) > 4.0
                    else Vec2(0.0, 0.0)  # noqa: F405
                )
            else:
                # Picket: spread across the approach so one splash cannot clip two.
                side = 1.0 if slot % 2 == 0 else -1.0
                stand = _inside(
                    deposit + approach * 2.4 + flank * (side * (1.0 + 0.9 * (slot // 2)))
                )
                step = (
                    navigate_to(bot.pos, stand)  # noqa: F405
                    if bot.pos.dist(stand) > 0.3
                    else Vec2(0.0, 0.0)  # noqa: F405
                )

            ba.move_action = move_bot(step)  # noqa: F405
            here = bot.pos + self._applied(step, speed)

            aim = self._pick_target(
                here, enemies_pred, payload, cheap, bot.next_fire_tick <= state.tick,
                state.tick, guards, healer_ids,
            )
            ba.special_action = SpecialAction.Battle(fire=False)  # noqa: F405
            if aim is None:
                ba.turn_action = turn_towards(_inside(deposit + approach * 8.0))  # noqa: F405
                continue
            _, aim_pos = aim
            ba.turn_action = turn_towards(aim_pos)  # noqa: F405
            aims[int(bot.id)] = (bot, here, self._after_turn(bot, here, aim_pos))
        return aims

    # ────────────────────────────── strike squad ───────────────────────────

    def _orders_strike(self, state, action, strike, enemies, enemies_pred, their_deposit,
                       cheap, guards, healer_ids) -> dict:
        """Bodies sent at the enemy deposit.

        Their extractors are the target.  A miner holds still to keep its ray on the
        deposit, which makes it the easiest thing on the map to hit, and a fleet with no
        income cannot rush a replacement for anything it loses afterwards.
        """
        aims = {}
        if not strike:
            return aims

        conf = self.conf
        p = self.p
        speed = conf.bot.speed
        payload = state.payload_pos()

        # Prefer their miners, then anything else loitering at their deposit.
        near = [e for e in enemies if e.pos.dist(their_deposit) <= p.raid_radius]
        prey = [e for e in near if self._class_of(e) == BotClass.Extractor] or near  # noqa: F405

        # Approach on a fanned arc rather than single file, so the first defender's
        # splash cannot catch the whole squad.
        inbound = (self.home - their_deposit).normalize_or_zero()
        if inbound.norm_sq() < 1e-8:
            inbound = Vec2(1.0, 0.0)  # noqa: F405
        side_vec = Vec2(-inbound.y, inbound.x)  # noqa: F405

        for slot, bot in enumerate(sorted(strike, key=lambda b: b.id)):
            ba = action.bots[bot.id]
            mark = self._nearest(bot.pos, prey) if prey else None

            if mark is not None:
                mark_pos = mark.pos + mark.vel
                step = (
                    navigate_to(bot.pos, mark_pos)  # noqa: F405
                    if bot.pos.dist(mark_pos) > 4.0
                    else Vec2(0.0, 0.0)  # noqa: F405
                )
            else:
                side = 1.0 if slot % 2 == 0 else -1.0
                stand = _inside(
                    their_deposit
                    + inbound * (conf.bot.blaster_range * 0.55)
                    + side_vec * (side * (1.2 + 1.1 * (slot // 2)))
                )
                step = (
                    navigate_to(bot.pos, stand)  # noqa: F405
                    if bot.pos.dist(stand) > 0.3
                    else Vec2(0.0, 0.0)  # noqa: F405
                )

            ba.move_action = move_bot(step)  # noqa: F405
            here = bot.pos + self._applied(step, speed)

            aim = self._pick_target(
                here, enemies_pred, payload, cheap, bot.next_fire_tick <= state.tick,
                state.tick, guards, healer_ids,
            )
            ba.special_action = SpecialAction.Battle(fire=False)  # noqa: F405
            if aim is None:
                ba.turn_action = turn_towards(their_deposit)  # noqa: F405
                continue
            _, aim_pos = aim
            ba.turn_action = turn_towards(aim_pos)  # noqa: F405
            aims[int(bot.id)] = (bot, here, self._after_turn(bot, here, aim_pos))
        return aims

    # ──────────────────────────────── healers ──────────────────────────────

    def _orders_healers(
        self, state, action, healers, allies, enemies, payload, survivor_id,
        home_ids, raided, deposit,
    ) -> None:
        if not healers:
            return

        conf = self.conf
        p = self.p
        full = conf.bot.health
        reach = conf.bot.base_heal_range
        stack = max(1, int(conf.bot.heal_stack_cap))  # a fourth healer on one bot is wasted

        ordered = sorted(healers, key=lambda b: (b.pos.dist_sq(deposit), b.id))
        # Healers filter patients by distance, so one standing at the payload will never
        # pick up a miner bleeding at the deposit.  Reserve some explicitly while raided.
        home_want = min(p.home_healers if raided else 0, max(0, len(ordered) - 1))
        home_healer_ids = {b.id for b in ordered[:home_want]}

        def patients(for_home: bool):
            pool = [
                b for b in allies
                if b.health < full - 1e-3
                and ((b.id in home_ids) if for_home else True)
            ]
            if not pool:  # keep the healers themselves standing
                pool = [
                    b for b in allies
                    if b.health < full - 1e-3
                    and ((b.id in home_ids) if for_home else True)
                ]
            return _sorted_by(pool, key=lambda b: (b.health, b.pos.dist_sq(payload)))

        wounded_home = patients(True) if home_want else []
        wounded_all = patients(False)

        load = {}
        for healer_index, healer in enumerate(ordered):
            ba = action.bots[healer.id]
            at_home = healer.id in home_healer_ids

            if healer.id == survivor_id:
                ba.move_action = move_bot(navigate_to(healer.pos, self.home))  # noqa: F405
                ba.turn_action = turn_towards(payload)  # noqa: F405
                ba.special_action = SpecialAction.Healer(fire=False, target=int(healer.id))  # noqa: F405
                continue

            patient = None
            candidates = wounded_home if at_home else wounded_all
            candidates = sorted(candidates, key=lambda b: (
                healer.pos.dist(b.pos) > reach,
                b.health + 0.6 * healer.pos.dist(b.pos), b.id))
            for hurt in candidates:
                if hurt.id == healer.id:  # a healer cannot heal itself
                    continue
                if load.get(hurt.id, 0) >= stack:
                    continue
                if healer.pos.dist(hurt.pos) > reach * 1.7:
                    continue
                if not line_of_sight(healer.pos, hurt.pos):
                    continue
                patient = hurt
                break

            if patient is None:
                # Nobody to mend: home healers wait at the deposit, the rest sit inside
                # the capture circle so the body still counts against an enemy push.
                if at_home:
                    lane = (self.enemy_home - deposit).normalize_or_zero()
                    anchor = _inside(deposit + lane * 1.6) if lane.norm_sq() > 1e-8 else deposit
                    face = self.enemy_home
                else:
                    back = (payload - self._front(payload, enemies)).normalize_or_zero()
                    rear = back.angle_deg() if back.norm_sq() > 1e-8 else 180.0
                    offset = ANCHOR_ANGLES[healer_index % len(ANCHOR_ANGLES)] * 0.6
                    anchor = self._legal_slot(payload, rear + offset, 2.0, False)
                    face = payload
                step = (
                    navigate_to(healer.pos, anchor)  # noqa: F405
                    if healer.pos.dist(anchor) > 0.3
                    else Vec2(0.0, 0.0)  # noqa: F405
                )
                ba.move_action = move_bot(step)  # noqa: F405
                ba.turn_action = turn_towards(face)  # noqa: F405
                ba.special_action = SpecialAction.Healer(fire=False, target=int(healer.id))  # noqa: F405
                continue

            load[patient.id] = load.get(patient.id, 0) + 1
            patient_pos = patient.pos + patient.vel

            # Stand off on the side of the patient facing away from the nearest enemy: in
            # range, inside the arc, and not the closest thing for the enemy to shoot.
            threat = self._nearest(patient.pos, enemies)
            away = (
                (patient.pos - threat.pos).normalize_or_zero()
                if threat is not None
                else (patient.pos - payload).normalize_or_zero()
            )
            if away.norm_sq() < 1e-8:
                away = Vec2(0.0, 1.0)  # noqa: F405
            offset = (healer_index % 3 - 1) * 35.0
            stand = self._legal_slot(patient_pos, away.angle_deg() + offset,
                                     reach * 0.55, False)

            step = (
                navigate_to(healer.pos, stand)  # noqa: F405
                if healer.pos.dist(stand) > 0.3
                else Vec2(0.0, 0.0)  # noqa: F405
            )
            ba.move_action = move_bot(step)  # noqa: F405
            ba.turn_action = turn_towards(patient_pos)  # noqa: F405
            # Asking always is free: an out-of-range or out-of-arc channel simply does not
            # land, and there is no heal cooldown to burn.
            ba.special_action = SpecialAction.Healer(fire=True, target=int(patient.id))  # noqa: F405

    # ────────────────────────────── fire control ───────────────────────────

    def _fire_control(self, state, action, aims: dict, enemies_pred) -> None:
        """Allocate reachable shots; a duplicate aim can use a different target.

        Only consider angles the shooter can reach during this tick. Recheck the
        first ray hit, obstacles, invulnerability and splash before reserving victims.
        """
        if not aims or not enemies_pred:
            return
        conf = self.conf
        tick = state.tick
        splash = conf.bot.base_blaster_splash_radius + conf.bot.radius
        deposits = (state.deposit_me.pos, state.deposit_other.pos)
        payload = state.payload_pos()
        claimed = set()
        choices = []
        for bot_id, (bot, here, angle) in aims.items():
            if bot.next_fire_tick > tick:
                continue
            # Preserve the chosen aim, plus alternative targets reachable now.
            candidates = [(angle, None)]
            reachable = []
            for enemy, pos in enemies_pred:
                distance = here.dist(pos)
                if distance > conf.bot.blaster_range or distance < 1e-6:
                    continue
                error = abs(diff_degrees((pos - here).angle_deg(), bot.angle))
                tolerance = math.degrees(math.asin(min(1.0,
                    max(0.0, conf.bot.radius - self.p.aim_slack) / distance)))
                if error <= conf.bot.turn_speed + tolerance:
                    reachable.append((enemy.health, distance, int(enemy.id), pos))
            for _, _, _, pos in sorted(reachable)[:6]:
                candidates.append((self._after_turn(bot, here, pos), pos))
            options = []
            seen = set()
            for candidate_angle, aim_pos in candidates:
                hit = self._ray_hit(here, candidate_angle, enemies_pred, payload, deposits)
                if hit is None:
                    continue
                enemy, impact, gap = hit
                if int(enemy.id) in seen:
                    continue
                seen.add(int(enemy.id))
                victims = [e for e, ep in enemies_pred if ep.dist(impact) <= splash
                           and e.invulnerable_until_tick <= tick]
                if victims:
                    options.append((victims, gap, aim_pos, int(enemy.id)))
            if options:
                choices.append((len(options), bot_id, options))
        # Shooters with only one firing lane get it first; flexible ones retarget.
        for _, bot_id, options in sorted(choices, key=lambda x: (x[0], x[1])):
            best = None
            best_score = -1.0
            for victims, gap, aim_pos, hit_id in options:
                fresh = [e for e in victims if int(e.id) not in claimed]
                if not fresh:
                    continue
                score = sum(3.0 + (5.0 if e.health <= conf.bot.blaster_damage else 0.0)
                            + (2.0 if self._class_of(e) == BotClass.Healer else 0.0)
                            for e in fresh) - gap * 0.02
                if score > best_score:
                    best_score = score
                    best = (fresh, aim_pos, hit_id)
            if best is None:
                continue
            victims, aim_pos, hit_id = best
            if aim_pos is not None:
                action.bots[bot_id].turn_action = turn_towards(aim_pos)
            action.bots[bot_id].special_action = SpecialAction.Battle(fire=True)
            claimed.update(int(e.id) for e in victims)
            if hasattr(self, "_target_memory"):
                self._target_memory[int(bot_id)] = hit_id

    def _ray_hit(self, origin, angle: float, enemies_pred, payload, deposits):
        """The enemy a shot from `origin` along `angle` would stop on, or `None`.

        Mirrors `step_blasters`: the ray runs out to `blaster_range`, allies are
        transparent, and walls, the payload and the deposits stop it short.
        """
        conf = self.conf
        reach = conf.bot.blaster_range
        hull = conf.bot.radius - self.p.aim_slack

        best = None
        best_gap = reach + 1.0
        for enemy, pos in enemies_pred:
            delta = pos - origin
            gap = delta.norm()
            if gap > reach or gap < 1e-4 or gap >= best_gap:
                continue
            error = abs(diff_degrees(delta.angle_deg(), angle))  # noqa: F405
            if error >= 90.0:
                continue
            if gap * math.sin(math.radians(error)) > hull:
                continue  # the ray would slide past the hull
            if self._disc_blocks(origin, pos, payload, conf.payload.radius):
                continue
            if any(self._disc_blocks(origin, pos, d, conf.deposit.radius) for d in deposits):
                continue
            if not line_of_sight(origin, pos):  # noqa: F405
                continue
            best, best_gap = (enemy, pos, gap), gap
        return best

    @staticmethod
    def _disc_blocks(origin, target, centre, radius: float) -> bool:
        """Whether a solid disc sits on the segment between the two points."""
        if radius <= 0.0:
            return False
        if (centre - origin).dot(target - origin) <= 0.0:
            return False  # behind the shooter
        if origin.dist(centre) - radius >= origin.dist(target):
            return False  # past the target
        return point_seg_dist(centre, origin, target) < radius  # noqa: F405

    # ──────────────────────────────── helpers ──────────────────────────────

    def _pick_target(self, here, enemies_pred, payload, cheap: bool, ready: bool, tick: int,
                     guards: dict, healer_ids: set, shooter=None):
        """Who this bot points at.  Lowest score wins.

        Out of range is a hard fact and dominates everything.  Below that the features
        are summed with learnable weights rather than ordered lexicographically: a
        lexicographic tuple asserts that one point of health outranks any distance
        difference, which is a strong claim nobody checked.

        Two features come from the healer arithmetic.  A target with healers in range
        restores `heal_per_tick * n` per tick while one shooter deals `blaster_damage`
        per `blaster_cooldown`, so at `heal_stack_cap` guards the volley is absorbed
        outright -- `w_guarded` steers off it.  `w_is_healer` steers onto the healers
        instead, because killing one is what makes every other shot start landing.

        A bot with a loaded blaster also skips anything still inside its invulnerability
        window: that blast would be absorbed for nothing and cost a full cooldown.
        """
        conf = self.conf
        p = self.p
        reach = conf.bot.blaster_range
        capture = conf.payload.capture_radius

        ranked = []
        for enemy, pos in enemies_pred:
            gap = here.dist(pos)
            if gap > reach + 6.0:
                continue
            eid = int(enemy.id)
            shielded = 1.0 if (ready and enemy.invulnerable_until_tick > tick) else 0.0
            on_point = 0.0 if pos.dist(payload) <= capture + 1.0 else 1.0
            score = (
                (0.0 if gap <= reach else 1000.0)
                + shielded * p.w_shielded
                + on_point * p.w_on_point
                + enemy.health * p.w_health
                + gap * p.w_gap
                + guards.get(eid, 0) * p.w_guarded
                + (p.w_is_healer if eid in healer_ids else 0.0)
            )
            if shooter is not None:
                desired = (pos - here).angle_deg()
                turn_ticks = abs(diff_degrees(desired, shooter.angle)) / max(conf.bot.turn_speed, 1e-6)
                score += 0.15 * turn_ticks
                if getattr(self, "_target_memory", {}).get(int(shooter.id)) == eid:
                    score -= 4.0
            ranked.append((score, enemy, pos))
        if not ranked:
            return None
        ranked.sort(key=lambda r: r[0])

        for _, enemy, pos in ranked:
            if here.dist(pos) > reach:
                continue
            if self._disc_blocks(here, pos, payload, conf.payload.radius):
                continue
            if any(self._disc_blocks(here, pos, d, conf.deposit.radius)
                   for d in getattr(self, "_shot_deposits", ())):
                continue
            if line_of_sight(here, pos):  # noqa: F405
                return enemy, pos
        return None

    def _after_turn(self, bot, here, target) -> float:
        """The facing the engine will give this bot once this tick's turn is applied.

        `eval_tick` moves first and turns second, so the aim is taken from where the bot
        will be standing, not where it is now -- and the turn is capped at `turn_speed`.
        """
        limit = self.conf.bot.turn_speed
        wanted = (target - here).angle_deg()
        return normalize_degrees(  # noqa: F405
            bot.angle + _clamp(diff_degrees(wanted, bot.angle), -limit, limit)  # noqa: F405
        )

    @staticmethod
    def _applied(direction, speed: float):
        """What `MoveAction::sanitize` does to our order, so we can predict our own step."""
        length = direction.norm()
        if length > 1.0:
            direction = direction / length
        return direction * speed

    @staticmethod
    def _nearest(point, bots):
        best = None
        best_gap = 0.0
        for bot in bots:
            gap = point.dist_sq(bot.pos)
            if best is None or gap < best_gap:
                best, best_gap = bot, gap
        return best

    @staticmethod
    def _front(payload, enemies):
        """Where the pressure is coming from -- the enemy centre of mass."""
        if not enemies:
            return payload + Vec2(1.0, 0.0)  # noqa: F405
        total = Vec2(0.0, 0.0)  # noqa: F405
        for enemy in enemies:
            total = total + enemy.pos
        return total / float(len(enemies))


# ───────────────────────── the sparring partner ────────────────────────────────
#  Used locally as team 1 while `SPARRING_MODE` is on, and as the tuner's fixed
#  baseline opponent.  It is meant to be a plausible mid-table opponent, not a good
#  one: it mines a little, walks at the payload, chases whoever is nearest, and
#  shoots on the crude "in range and visible" test most first-day bots use.  The
#  randomness matters -- a deterministic opponent gets beaten the same way every
#  match and hides the holes in a real strategy.


def sparring_strategy(state):
    conf = get_config()  # noqa: F405
    action = FleetAction.new()  # noqa: F405
    payload = state.payload_pos()
    fleet = list(state.fleet_me)
    enemies = list(state.fleet_other)

    miners = sum(1 for b in fleet if b.class_ == BotClass.Extractor)  # noqa: F405
    if miners < random.choice((2, 3, 4)):
        action.fabricator_next = int(BotClass.Extractor)  # noqa: F405
    elif random.random() < 0.2:
        action.fabricator_next = int(BotClass.Healer)  # noqa: F405
    else:
        action.fabricator_next = int(BotClass.Battle)  # noqa: F405

    in_endgame = state.tick >= conf.max_ticks - conf.endgame_ticks
    action.rush_order = (
        not in_endgame
        and state.fabricator_me.tokens >= conf.fabricator.rush_cost
        and random.random() < 0.8
    )

    deposit = state.deposit_me.pos
    mining_spot = deposit + Vec2(0.0, conf.deposit.radius + conf.bot.radius + 1.0)  # noqa: F405

    for bot in fleet:
        ba = action.bots[bot.id]

        if bot.class_ == BotClass.Extractor:  # noqa: F405
            ba.move_action = move_bot(navigate_to(bot.pos, mining_spot))  # noqa: F405
            ba.turn_action = turn_towards(deposit)  # noqa: F405
            ba.special_action = SpecialAction.Extractor(mine=True)  # noqa: F405
            continue

        mark = None
        for enemy in enemies:
            if mark is None or bot.pos.dist_sq(enemy.pos) < bot.pos.dist_sq(mark.pos):
                mark = enemy

        if bot.class_ == BotClass.Healer:  # noqa: F405
            hurt = None
            for ally in fleet:
                if ally.id == bot.id or ally.health >= conf.bot.health:
                    continue
                if hurt is None or ally.health < hurt.health:
                    hurt = ally
            if hurt is None:
                ba.move_action = move_bot(navigate_to(bot.pos, payload))  # noqa: F405
                ba.turn_action = turn_towards(payload)  # noqa: F405
                ba.special_action = SpecialAction.Healer(fire=False, target=int(bot.id))  # noqa: F405
            else:
                ba.move_action = move_bot(navigate_to(bot.pos, hurt.pos))  # noqa: F405
                ba.turn_action = turn_towards(hurt.pos)  # noqa: F405
                ba.special_action = SpecialAction.Healer(fire=True, target=int(hurt.id))  # noqa: F405
            continue

        # Battle: half the fleet sits on the point, the rest chases, and a wandering
        # offset keeps them from filing into a single stack.
        if mark is None or bot.id % 2 == 0:
            goal = payload + Vec2.from_angle_deg(random.uniform(0.0, 360.0)) * 1.8  # noqa: F405
        else:
            goal = mark.pos

        ba.move_action = move_bot(navigate_to(bot.pos, _inside(goal)))  # noqa: F405
        if mark is None:
            ba.turn_action = turn_towards(payload)  # noqa: F405
            ba.special_action = SpecialAction.Battle(fire=False)  # noqa: F405
            continue

        ba.turn_action = turn_towards(mark.pos)  # noqa: F405
        can_shoot = (
            bot.pos.dist(mark.pos) <= conf.bot.blaster_range
            and line_of_sight(bot.pos, mark.pos)  # noqa: F405
        )
        ba.special_action = SpecialAction.Battle(fire=can_shoot)  # noqa: F405

    return action


def get_strategy(team: int):
    """A fresh Brain per call.

    The old module-level singleton cached `_conf`, `_spots` and `_home` for the life of
    the process.  That is fine for one `mm-cli run`, but the tuner plays thousands of
    matches back to back and a cache from match 1 would silently corrupt match 2.

    Parameters come from MM_TUNE_PARAMS_<team> while the tuner is driving, and from the
    dataclass defaults otherwise -- so a submitted bot is unaffected by any of this.
    """
    if SPARRING_MODE and team == 1:
        print("[strategy] *** SPARRING MODE: team 1 is the practice dummy. "
              "Set SPARRING_MODE = False before submitting. ***")
        return sparring_strategy

    if SPARRING_MODE:
        print("[strategy] *** SPARRING MODE is ON -- do not submit like this. ***")

    brain = Brain(Params.from_env(team))
    print(f"[strategy] team {team} reporting in")
    return brain.act


# ═══════════════════════════════════════════════════════════════════════════════
#  TUNER -- everything below this line runs only from the command line.
# ═══════════════════════════════════════════════════════════════════════════════
#
#  Why a cross-entropy search rather than PPO or DQN: the decision rules above are
#  already derived from the engine and are correct.  What is unknown is ~36 scalars.
#  CEM is gradient-free, tolerates the noisy win/loss signal, parallelises perfectly,
#  needs nothing installed, and costs zero compute at match time -- the result is a
#  set of literals pasted back into `Params`.
#
#  Three things keep it honest:
#
#    * Common random numbers.  Every candidate in a generation plays the same seeds
#      against the same opponents, so most of the variance between candidates is the
#      candidates rather than the dice.  Largest available win, and it is free.
#    * Mirrored sampling.  Perturbations come in +d/-d pairs, cancelling the
#      first-order noise term in the elite mean.
#    * An opponent archive.  Tuning against one fixed dummy teaches you to beat that
#      dummy.  Snapshots of past means go into a pool and candidates are scored
#      against a sample of it -- cheap fictitious self-play.
#
#  `ablate` is the companion to all this: the search tells you the package got better,
#  the ablation tells you which part earned it.

# name, low, high.  Bounds are judgement, not guesses: `anchor_radius` has to stay
# inside `capture_radius` and outside the payload hull; `line_radius` has to stay well
# inside `blaster_range`.  Check both against the real config before a long run --
# match 216 ran with capture_radius 2.5 and blaster_range 10.
SPEC = [
    ("extractors_wanted",    2.0,   8.0),
    ("battle_per_healer",    2.0,   6.0),
    ("max_healers",          2.0,  12.0),
    ("extractor_build_stop", 0.20,  0.80),

    ("anchors_wanted",       1.0,   6.0),
    ("anchor_radius",        1.40,  2.40),
    ("line_radius",          3.00,  7.00),
    ("line_spacing",         0.90,  2.00),
    ("line_per_ring",        4.0,  14.0),
    ("line_arc",            40.0, 170.0),
    ("threat_range",         6.0,  22.0),

    ("rally_back",           0.05,  0.50),
    ("outnumber_margin",     0.0,   3.0),
    ("rally_min_capture",   -0.80,  0.40),
    ("freeze_fleet",         0.0,  10.0),
    ("freeze_below",        -0.60,  0.30),

    ("garrison_base",        0.0,   5.0),
    ("garrison_per_raider",  0.0,   2.0),
    ("garrison_max",         1.0,  10.0),
    ("garrison_min_battle",  2.0,  14.0),
    ("home_healers",         0.0,   3.0),
    ("raid_radius",          4.0,  16.0),

    ("raid_squad",           0.0,   7.0),
    ("raid_start",           0.02,  0.50),
    ("raid_stop",            0.30,  0.95),
    ("raid_min_battle",      4.0,  18.0),

    ("aim_slack",            0.00,  0.15),
    ("wounded_pullback",     1.0,   9.0),
    ("pullback_min_inside",  1.0,   6.0),
    ("miner_flee_health",    1.0,  10.0),

    ("w_shielded",           0.0, 100.0),
    ("w_on_point",         -20.0,  20.0),
    ("w_health",            -3.0,   3.0),
    ("w_gap",               -1.0,   1.0),
    ("w_guarded",          -10.0,  60.0),
    ("w_is_healer",        -60.0,  20.0),
]
DIM = len(SPEC)

# ADAPT ME (1/3): where the repo root is, relative to this file.
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_MM_CLI = _os.environ.get("MM_CLI", "mm-cli")
_MATCH_TIMEOUT = float(_os.environ.get("MM_MATCH_TIMEOUT", "120"))


def _match_command(seed: int, out_dir: str):
    """ADAPT ME (2/3): the command that plays one match."""
    return [
        _MM_CLI, "run",
        "--seed", str(seed),
        "--output", _os.path.join(out_dir, "gamelog.json"),
    ]


# ADAPT ME (3/3): how a finished match reports itself.  `probe` shows you the raw
# output so you can fix these without guessing.  Match 216's log ends with a commented
# `# result: {"reason":"payload","tick":5040,"winner":"A"}` line and names the winner
# by letter, so both the letter and the numeric forms are handled.
_RESULT_PATTERNS = [
    (r'"winner"\s*:\s*"?([AB01])"?', "winner"),
    (r"winner[:\s]+team\s*([01])", "winner"),
    (r"team\s*([01])\s+wins", "winner"),
    (r'"capture"\s*:\s*(-?\d+\.?\d*)', "capture"),
    (r"final\s+capture[:\s]+(-?\d+\.?\d*)", "capture"),
]
_WINNER_MAP = {"A": 0, "B": 1, "0": 0, "1": 1}


def _parse_result(stdout: str, out_dir: str):
    """Returns (winner, capture, ok, note), always from team 0's point of view."""
    import re

    log = _os.path.join(out_dir, "gamelog.json")
    if _os.path.exists(log):
        try:
            with open(log) as fh:
                text = fh.read()
            # The log may be one JSON document, or newline-delimited tick records with a
            # trailing `# result: {...}` comment.  Take the last result we can find, and
            # fall back to parsing the whole file as one object.
            found = None
            for found in re.finditer(r"result\s*:?\s*(\{[^\n]*\})", text):
                pass
            data = _json.loads(found.group(1)) if found else _json.loads(text)
            winner = data.get("winner")
            if isinstance(winner, dict):
                winner = winner.get("team")
            capture = data.get("final_capture", data.get("capture", 0.0))
            w = _WINNER_MAP.get(str(winner).upper())
            if w is not None or capture:
                return w, float(capture or 0.0), True, ""
        except Exception as exc:  # noqa: BLE001
            return None, 0.0, False, f"gamelog unparsable: {exc!r}"

    winner, capture, found_any = None, 0.0, False
    for pattern, kind in _RESULT_PATTERNS:
        m = re.search(pattern, stdout, re.I | re.M)
        if not m:
            continue
        found_any = True
        if kind == "winner":
            winner = _WINNER_MAP.get(m.group(1).upper())
        else:
            capture = float(m.group(1))
    if not found_any:
        return None, 0.0, False, "no result found in output"
    return winner, capture, True, ""


def _score(winner, capture, ok, win_weight: float = 3.0) -> float:
    """Dense fitness for team 0.

    Win or loss alone is one bit per match, far too noisy for a population of 24.  The
    final capture position says *how close* a loss was, and that margin carries most of
    the usable signal.
    """
    if not ok:
        return -win_weight - 1.0  # a crash is worse than any loss
    result = 0.0 if winner is None else (1.0 if winner == 0 else -1.0)
    return win_weight * result + _clamp(capture, -1.0, 1.0)


def _run_match(params_0: dict, params_1: dict, seed: int):
    """Play one match.  Parameters reach the strategy through the environment, which is
    why `get_strategy` reads MM_TUNE_PARAMS_<team>."""
    import shutil
    import subprocess
    import tempfile

    env = dict(_os.environ)
    env["MM_TUNE_PARAMS_0"] = _json.dumps(params_0)
    env["MM_TUNE_PARAMS_1"] = _json.dumps(params_1)
    env["PYTHONHASHSEED"] = str(seed)  # remove one source of run-to-run drift

    out_dir = tempfile.mkdtemp(prefix="mm-")
    try:
        proc = subprocess.run(
            _match_command(seed, out_dir),
            cwd=_REPO, env=env, capture_output=True, text=True, timeout=_MATCH_TIMEOUT,
        )
        if proc.returncode != 0:
            return None, 0.0, False, f"exit {proc.returncode}: {proc.stderr[-400:]}"
        return _parse_result(proc.stdout, out_dir)
    except subprocess.TimeoutExpired:
        return None, 0.0, False, "timeout"
    except FileNotFoundError:
        return None, 0.0, False, f"{_MM_CLI} not on PATH"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _duel(job):
    """Mean score for `a` against `b`, playing both sides of the same seed.

    The engine mirrors the world, so side bias should be zero -- playing both sides
    costs one extra match and removes it from the measurement anyway.
    """
    a, b, seed, swap = job
    winner, capture, ok, _note = _run_match(a, b, seed)
    total = _score(winner, capture, ok)
    if not swap:
        return total
    winner, capture, ok, _note = _run_match(b, a, seed)
    return (total - _score(winner, capture, ok)) / 2.0


def _to_params(z) -> dict:
    """Normalised [0,1] vector -> parameter dict, merged onto the defaults."""
    out = Params().to_dict()
    for value, (name, low, high) in zip(z, SPEC):
        out[name] = low + _clamp(value, 0.0, 1.0) * (high - low)
    return out


def _to_vector(params: dict):
    return [_clamp((params[n] - lo) / (hi - lo), 0.0, 1.0) for n, lo, hi in SPEC]


def _tune(args) -> None:
    import multiprocessing as mp
    import statistics
    import time

    run_dir = _os.path.join("runs", args.name)
    _os.makedirs(run_dir, exist_ok=True)

    rng = random.Random(args.seed)
    mu = _to_vector(Params().to_dict()) if args.warm_start else [0.5] * DIM
    sigma = [args.sigma] * DIM

    baseline = Params().to_dict()
    archive = [baseline]
    history, best = [], {"score": -1e9, "params": baseline, "gen": -1}

    methods = mp.get_all_start_methods()
    ctx = mp.get_context("fork" if "fork" in methods else "spawn")
    pool = ctx.Pool(args.workers) if args.workers > 1 else None
    elite_n = max(2, int(round(args.pop * args.elite_frac)))

    try:
        for gen in range(args.gens):
            started = time.time()

            # Mirrored sampling: half the population is the negation of the other half.
            half = max(1, args.pop // 2)
            deltas = [[rng.gauss(0.0, 1.0) for _ in range(DIM)] for _ in range(half)]
            zs = []
            for d in deltas:
                zs.append([mu[i] + sigma[i] * d[i] for i in range(DIM)])
                zs.append([mu[i] - sigma[i] * d[i] for i in range(DIM)])
            zs = zs[: args.pop]
            zs[0] = list(mu)  # carry the incumbent, re-measured on this generation's seeds

            population = [_to_params(z) for z in zs]

            # Common random numbers: one seed set for the whole generation.
            seeds = [rng.randrange(1, 2**31) for _ in range(args.seeds_per_gen)]
            opponents = [baseline]
            if len(archive) > 1:
                want = min(args.opponents - 1, len(archive) - 1)
                opponents.append(archive[-1])
                if want > 1:
                    opponents += rng.sample(archive[:-1], min(want - 1, len(archive) - 1))

            jobs, owner = [], []
            for idx, candidate in enumerate(population):
                for opponent in opponents:
                    for seed in seeds:
                        jobs.append((candidate, opponent, seed, not args.no_swap))
                        owner.append(idx)

            raw = pool.map(_duel, jobs, chunksize=1) if pool else [_duel(j) for j in jobs]

            buckets = [[] for _ in population]
            for idx, value in zip(owner, raw):
                buckets[idx].append(value)
            scores = [statistics.fmean(b) if b else -99.0 for b in buckets]

            order = sorted(range(len(zs)), key=lambda i: scores[i], reverse=True)
            elite = [zs[i] for i in order[:elite_n]]

            new_mu = [statistics.fmean(e[i] for e in elite) for i in range(DIM)]
            new_sd = [statistics.pstdev([e[i] for e in elite]) for i in range(DIM)]

            # Smoothed update.  Raw CEM collapses sigma to zero within a handful of noisy
            # generations and then stops searching; the floor and the momentum keep it alive.
            mu = [args.alpha * new_mu[i] + (1 - args.alpha) * mu[i] for i in range(DIM)]
            sigma = [
                max(args.sigma_floor, args.alpha * new_sd[i] + (1 - args.alpha) * sigma[i])
                for i in range(DIM)
            ]

            top = scores[order[0]]
            if top > best["score"]:
                best = {"score": top, "params": population[order[0]], "gen": gen}

            if gen % args.archive_every == 0:
                archive.append(_to_params(mu))
                if len(archive) > args.archive_cap:
                    archive = [archive[0]] + archive[-(args.archive_cap - 1):]

            row = {
                "gen": gen, "best": top, "mean": statistics.fmean(scores),
                "incumbent": scores[0], "sigma": statistics.fmean(sigma),
                "secs": round(time.time() - started, 1), "mu": _to_params(mu),
            }
            history.append(row)
            print(
                f"gen {gen:3d}  best {top:+6.2f}  mean {row['mean']:+6.2f}  "
                f"incumbent {scores[0]:+6.2f}  sigma {row['sigma']:.3f}  "
                f"{row['secs']:5.1f}s  opponents {len(opponents)}"
            )

            for name, blob in (("history", history), ("best", best), ("mu", _to_params(mu))):
                with open(_os.path.join(run_dir, name + ".json"), "w") as fh:
                    _json.dump(blob, fh, indent=2)
    except KeyboardInterrupt:
        print("\ninterrupted -- best so far is saved")
    finally:
        if pool:
            pool.close()
            pool.join()

    print(f"\nbest score {best['score']:+.2f} from generation {best['gen']}")
    print(f"python strategy/main.py emit {_os.path.join(run_dir, 'best.json')}")


def _emit(path: str) -> None:
    """Print a paste-ready `Params` body.

    Bake the numbers into the dataclass before you submit.  The environment variable is
    a tuning convenience and will not exist on the judge machine.
    """
    with open(path) as fh:
        blob = _json.load(fh)
    params = blob.get("params", blob)
    ints = set(Params._INTS)
    default = Params()
    print("@dataclass\nclass Params:")
    for f in fields(Params):
        value = params.get(f.name, getattr(default, f.name))
        if f.name in ints:
            print(f"    {f.name}: int = {int(round(value))}")
        else:
            print(f"    {f.name}: float = {float(value):.4g}")
    print(f"\n    _INTS = {Params._INTS!r}")
    print("\n    # keep __post_init__, from_mapping, from_env and to_dict as they are")


def _probe() -> None:
    """Run one match and dump everything, so the parser can be wired up by looking."""
    import shutil
    import subprocess
    import tempfile

    p = Params().to_dict()
    env = dict(_os.environ)
    env["MM_TUNE_PARAMS_0"] = _json.dumps(p)
    env["MM_TUNE_PARAMS_1"] = _json.dumps(p)
    out_dir = tempfile.mkdtemp(prefix="mm-probe-")
    cmd = _match_command(1, out_dir)
    print("$", " ".join(cmd), "\n")
    proc = subprocess.run(
        cmd, cwd=_REPO, env=env, capture_output=True, text=True, timeout=_MATCH_TIMEOUT
    )
    print("── exit", proc.returncode)
    print("── stdout (last 3000 chars)\n", proc.stdout[-3000:])
    print("── stderr (last 1500 chars)\n", proc.stderr[-1500:])
    print("── files produced:", _os.listdir(out_dir))
    print("\n── _parse_result says:", _parse_result(proc.stdout, out_dir))
    print("\nIf that comes back ok=False, fix _RESULT_PATTERNS / _parse_result above.")
    shutil.rmtree(out_dir, ignore_errors=True)


def _selftest() -> int:
    """A crippled fleet must lose.  If it does not, the harness is measuring noise and
    every generation after that is theatre."""
    if SPARRING_MODE:
        print("SPARRING_MODE is on -- team 1 would ignore its parameters. Set it False.")
        return 1

    good = Params().to_dict()
    bad = dict(good)
    bad.update(extractors_wanted=0, anchors_wanted=0, line_radius=14.0, max_healers=0,
               garrison_base=0, garrison_max=1, raid_squad=0)

    scores = [_duel((good, bad, seed, True)) for seed in (1, 2, 3, 4)]
    mean = sum(scores) / len(scores)
    print(f"default vs crippled over {len(scores)} seeds: "
          f"{[round(s, 2) for s in scores]} -> mean {mean:+.2f}")
    if mean > 1.0:
        print("PASS -- the harness has signal. Start tuning.")
        return 0
    print("FAIL -- either the parser is wrong, or the parameters are not reaching the\n"
          "strategy. Check that get_strategy is being called per team and that\n"
          "MM_TUNE_PARAMS_0 is visible to the engine process.")
    return 1


def _ablate(args) -> int:
    """Measure one change at a time against the current defaults.

    Tuning 36 parameters at once tells you the package is better; it does not tell you
    which part earned it.  This plays the defaults against a copy with one group
    reverted, so a change that is doing nothing -- or is actively hurting -- shows up
    as a number near zero or below it rather than hiding inside a good aggregate.
    """
    import statistics

    base = Params().to_dict()
    variants = {
        "no garrison":      dict(garrison_base=0, garrison_max=0, garrison_per_raider=0.0),
        "no strike squad":  dict(raid_squad=0),
        "4 anchors (old)":  dict(anchors_wanted=4),
        "6 miners (old)":   dict(extractors_wanted=6),
        "flee at 4 (old)":  dict(miner_flee_health=4.0),
        "healer-blind aim": dict(w_guarded=0.0, w_is_healer=0.0),
        "always rally":     dict(rally_min_capture=-1.0, freeze_fleet=0),
    }
    seeds = list(range(1, args.seeds + 1))
    print(f"defaults vs each variant, {len(seeds)} seeds each "
          f"(positive means the change is earning its keep)\n")
    for label, patch in variants.items():
        other = dict(base)
        other.update(patch)
        s = [_duel((base, other, seed, True)) for seed in seeds]
        print(f"  {label:18s} {statistics.fmean(s):+6.2f}   {[round(x, 1) for x in s]}")
    return 0


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="tune the Deliverables bot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="run one match and dump the raw output")
    sub.add_parser("selftest", help="check the harness measures something real")

    a = sub.add_parser("ablate", help="measure each change against the defaults")
    a.add_argument("--seeds", type=int, default=6)

    e = sub.add_parser("emit", help="print a paste-ready Params block")
    e.add_argument("path")

    t = sub.add_parser("tune", help="run the cross-entropy search")
    t.add_argument("--name", default="default")
    t.add_argument("--gens", type=int, default=60)
    t.add_argument("--pop", type=int, default=24)
    t.add_argument("--elite-frac", type=float, default=0.25)
    t.add_argument("--seeds-per-gen", type=int, default=2)
    t.add_argument("--opponents", type=int, default=3)
    t.add_argument("--archive-every", type=int, default=5)
    t.add_argument("--archive-cap", type=int, default=8)
    t.add_argument("--sigma", type=float, default=0.25)
    t.add_argument("--sigma-floor", type=float, default=0.03)
    t.add_argument("--alpha", type=float, default=0.7, help="update smoothing")
    t.add_argument("--workers", type=int, default=8)
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--no-swap", action="store_true", help="skip the side-swapped replay")
    t.add_argument("--warm-start", action="store_true", default=True,
                   help="start from the current hand-tuned values")
    t.add_argument("--cold-start", dest="warm_start", action="store_false")

    args = ap.parse_args()

    if args.cmd == "probe":
        _probe()
        return 0
    if args.cmd == "selftest":
        return _selftest()
    if args.cmd == "ablate":
        return _ablate(args)
    if args.cmd == "emit":
        _emit(args.path)
        return 0

    per_gen = args.pop * args.opponents * args.seeds_per_gen * (1 if args.no_swap else 2)
    print(f"{args.gens} generations x ~{per_gen} matches = ~{args.gens * per_gen} matches")
    _tune(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())