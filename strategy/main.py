"""MechMania 32 combat-first strategy.

Replacement preserving the supplied engine API and local tuning commands.
Uses staged economy, threat-based defense, obstacle-aware targeting and
reachable healer assignments. Match performance must be checked in the engine.

Revision notes, from reading friendly logs 1074 and 1026 tick by tick:

  * The supplied 1368 and 1414 logs are identical match data apart from elapsed time.
    Team B grew to seven extractors, team A held three, then B amassed a full fleet.
    This version expands economy in stages toward the eight-extractor cap.
  * Gang (log 1026) self-destructed all eight of its extractors at full health on tick
    5836 and spent a 1375-token bank on battle bots, entering the endgame 22/10/0 while
    Boneyard Creek carried four miners that cannot shoot.  It then wiped them for an
    elimination win from BEHIND on capture (+0.567 against it).  Tokens buy nothing
    after the endgame line and an extractor is a body that cannot fire, so the same
    conversion is now ours: see `_convert_miners`.
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
    # Expand toward eight when affordable; the supplied repeated match reached
    # seven opponent extractors by tick 750 while the losing side stayed at three.
    extractors_wanted: int = 8
    miner_lead: int = 1                 # fighters must outnumber miners by this before the next miner
    battle_per_healer: int = 2          # favor shooters; healers support a line rather than replace it
    max_healers: int = 8
    extractor_build_stop: float = 0.55  # match fraction after which a new miner cannot pay off

    # ── the pre-endgame conversion ─────────────────────────────────────────────
    # Nothing is built in the last `endgame_ticks`, so a token banked past that line is
    # worth only the third tiebreak, and an extractor standing there is a body that
    # cannot shoot.  Inside this window the miners are scrapped one per tick and the
    # freed slots are rushed as battle bots, which is how a fleet arrives at the
    # endgame with every slot holding a gun.
    convert_lead: int = 300             # ticks before the endgame line to start scrapping
    convert_min_tokens: float = 200.0   # a bank this deep converts even below the fleet cap

    # ── mustering ──────────────────────────────────────────────────────────────
    # A bot that walks at the payload the moment it is built arrives alone into whatever
    # is already standing there.  Friendly 1270 was lost exactly that way: we reached the
    # point FIRST, nine bodies to seven, and then fed the next twenty in one at a time --
    # forty-five deaths to their four, with a kill zone at (24, 14.5) they never had to
    # move from.  Reinforcements gather on our side of the path and go forward together.
    muster_back: float = 0.14           # how far back down the path the squad forms up
    muster_radius: float = 3.60         # counted as "formed up" inside this
    squad_size: int = 6                 # bodies that have to gather before the push goes in
    commit_hold: int = 90               # ticks a commitment stands before it may flip again

    # ── holding the point ──────────────────────────────────────────────────────
    # One body inside the circle is the entire requirement; a second is insurance
    # against losing the first, and a third is a bot not shooting anybody.
    anchors_wanted: int = 2
    anchor_radius: float = 2.05         # inside `capture_radius`, outside the payload hull
    line_radius: float = 3.65           # the shooting line: well inside blaster range
    line_spacing: float = 1.30          # gap between the line's rings when it gets crowded
    line_per_ring: int = 9
    line_arc: float = 90.0             # how wide the line fans across the side it screens
    threat_range: float = 14.0          # enemies this close to the payload set the front

    # ── giving ground ──────────────────────────────────────────────────────────
    rally_back: float = 0.20            # how far back down the path a beaten line regroups
    outnumber_margin: int = 1           # enemy surplus at the point that triggers a regroup
    # Regrouping concedes the circle, which is only affordable while the capture race is
    # close.  Below this capture value we are losing and every body contests instead.
    # The supplied opponent turned a tiny lead into total map control after our
    # losing side abandoned the centre. Regroup only with a substantial lead.
    rally_min_capture: float = 0.25
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
    garrison_per_raider: float = 1.0    # extra bodies per detected raider
    #  A body is only ever drafted for the garrison if it is ALREADY at home -- a fresh
    #  build standing in the spawn corner, or one still walking out.  Drafting by nearest
    #  distance with no limit is what made bodies peel off the middle of a fight and walk
    #  the length of the map: one raider took 1.5 fighters off the line, four took six,
    #  and the line lost them mid-engagement.  The pack goes forward together.
    garrison_home_radius: float = 11.0  # only bodies this close to the deposit may be drafted
    garrison_max: int = 8
    garrison_min_battle: int = 5        # start protecting income before the line is large
    sticky_roles: int = 1               # keep a drafted body in its role instead of re-picking
    healers_early: int = 4              # healers the opening must have before economy resumes
    #  Zero by default.  Sending healers home on a raid strips the line of the one thing
    #  keeping it alive: in friendlies 1330 and 1332 two of our three healers sat at the
    #  deposit from tick 1400 while the fighters died at the payload.
    home_healers: int = 0               # healers held back for the garrison
    raid_radius: float = 12.0           # detect and intercept before blaster range

    # ── raiding theirs ─────────────────────────────────────────────────────────
    # The strike squad is only detached with local fighter surplus, after establishing
    # the payload line, and only while enemy miners are actually at their deposit.
    raid_squad: int = 2                 # flank the enemy miners only with adequate payload cover
    raid_start: float = 0.10            # match fraction before which we do not bother
    raid_stop: float = 0.53             # ... and after which their tokens no longer matter
    raid_min_battle: int = 12            # only with a fleet big enough to hold and raid

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
    w_on_point: float = 9.0             # standing off the capture circle
    w_health: float = 1.0               # finishing a wounded body beats chipping a fresh one
    w_gap: float = 0.10                 # prefer the nearer, more reliable shot
    w_guarded: float = 9.0             # per enemy healer in range: the shot gets out-healed
    w_is_healer: float = -23.0          # killing the healer is how the stack comes apart

    cheap_budget: int = 1200            # compute bank remaining below which we cut corners

    #: fields the engine needs as whole numbers
    _INTS = (
        "extractors_wanted", "battle_per_healer", "max_healers", "anchors_wanted",
        "line_per_ring", "pullback_min_inside", "outnumber_margin", "freeze_fleet",
        "garrison_base", "garrison_max", "garrison_min_battle", "home_healers",
        "sticky_roles",
        "healers_early",
        "raid_squad", "raid_min_battle", "cheap_budget", "convert_lead",
        "squad_size", "miner_lead", "commit_hold",
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
        self._scrapped = set()       # miners already told to self-destruct

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

    def _converting(self, state) -> bool:
        """Inside the window where miners are scrapped for fighters.

        Deliberately a window and not a single tick: only one bot can be rushed per
        tick, so eight slots need eight ticks, and `convert_lead` leaves room for the
        bank to be thin.  Gang did the whole swap in one tick 164 ticks out and it cost
        them nothing, but one-per-tick never leaves the fleet short of bodies.
        """
        conf = self.conf
        line = conf.max_ticks - conf.endgame_ticks
        return line - self.p.convert_lead <= state.tick < line

    def _convert_miners(self, state, action, miners) -> set:
        """Scrap one miner per tick and let the rush order replace it with a gun.

        `step_self_destruct` runs last in the tick and does nothing but remove the bot,
        so the miner still banks this tick's tokens on its way out, and the fabricator
        sees the free slot next tick.  Returns the ids scrapped this tick so nothing
        else bothers giving them orders.
        """
        p = self.p
        conf = self.conf
        if not miners or not self._converting(state):
            return set()

        bank = state.fabricator_me.tokens
        # The replacement has to be affordable this tick, or the scrap is just a body
        # thrown away: `step_fabricators` runs at the top of the next tick and fills the
        # slot only if the tokens are there.
        if bank < conf.fabricator.rush_cost * 1.5:
            return set()
        # And the slot has to be what is actually scarce.  At the fleet cap a rush order
        # is refused, so the miner's slot is the only way to add a gun and the swap is
        # free -- that is the position Gang converted from.  Below the cap we are already
        # spending every token on new bodies, so scrapping a miner would cost one body
        # and buy nothing; only a bank too deep to spend any other way justifies it.
        if not (state.fleet_me.is_full() or bank >= p.convert_min_tokens):
            return set()

        # Farthest from the front first: that one has the longest walk if it lived, and
        # the closest miner is the one most likely to be useful as a late body.
        payload = state.payload_pos()
        victim = max(
            (b for b in miners if int(b.id) not in self._scrapped),
            key=lambda b: (b.pos.dist_sq(payload), b.id),
            default=None,
        )
        if victim is None:
            return set()
        self._scrapped.add(int(victim.id))
        ba = action.bots[victim.id]
        ba.self_destruct = True
        # It still acts on the tick it goes, so keep it mining and looking at the deposit.
        ba.special_action = SpecialAction.Extractor(mine=True)  # noqa: F405
        ba.turn_action = turn_towards(state.deposit_me.pos)  # noqa: F405
        return {int(victim.id)}

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

        # The pre-endgame conversion.  Done before anything else claims these bodies.
        scrapped = self._convert_miners(state, action, miners)
        if scrapped:
            miners = [b for b in miners if int(b.id) not in scrapped]
            allies = [b for b in allies if int(b.id) not in scrapped]

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
        # With the deposit cap taken in full this is eight bodies' worth of income,
        # which is most of a replacement fleet over a match.
        garrison = []
        if not endgame and len(battle) >= p.garrison_min_battle and miners:
            want = p.garrison_base + int(round(p.garrison_per_raider * len(raiders)))
            want = min(want, p.garrison_max, max(0, len(battle) - p.anchors_wanted))

            # Only bodies already at home may be drafted.  Anything forward stays
            # forward: a fighter recalled from the middle of an engagement is a gun the
            # line loses now and a gun that arrives home in four hundred ticks.
            eligible = [b for b in battle
                        if b.pos.dist(deposit) <= p.garrison_home_radius
                        or b.pos.dist(deposit) < b.pos.dist(payload)]

            # Sticky: whoever held the role last tick keeps it if they are still eligible,
            # so the draft does not churn a different body into the walk every tick.
            held = getattr(self, "_garrison_ids", set())
            keep = [b for b in eligible if int(b.id) in held]
            fresh = _sorted_by([b for b in eligible if int(b.id) not in held],
                               key=lambda b: (b.pos.dist_sq(deposit), b.id))
            garrison = (keep + fresh)[:max(0, want)] if want > 0 else []
        garrison_ids = {b.id for b in garrison}
        self._garrison_ids = {int(b.id) for b in garrison}

        # Strike squad.  Their miners are worth more than our place on the line: a fleet
        # with no token income cannot rush a replacement for anything it loses, and every
        # fight after that is one-sided.  Both winners in the friendly logs ran the full
        # eight-miner economy, so this is aimed squarely at the bots worth beating.
        strike = []
        their_workers = [
            e for e in enemies
            if self._class_of(e) == BotClass.Extractor
            and e.pos.dist(their_deposit) <= p.raid_radius
        ]
        our_local = sum(
            b.pos.dist(payload) <= conf.bot.blaster_range for b in battle
        )
        their_local = sum(
            e.pos.dist(payload) <= conf.bot.blaster_range
            for e in enemies if self._class_of(e) in (BotClass.Battle, None)
        )
        # A raid is not worth losing control of the capture circle.  But when
        # two spare guns exist, take the undefended miner zone instead of
        # donating them to the opponent's nine-healer death ball.
        raid_ok = (
            not endgame and p.raid_squad > 0 and their_workers
            and state.capture >= -0.06 and not raiders
            and p.raid_start <= phase <= p.raid_stop
            and len(battle) - len(garrison) >= p.raid_min_battle
            and our_local >= their_local + 2
            and len(battle) - len(garrison) - p.raid_squad >= p.anchors_wanted + 6
        )
        if raid_ok:
            spare = [b for b in battle if b.id not in garrison_ids]
            held = getattr(self, "_strike_ids", set())
            sticky = [b for b in spare if int(b.id) in held]
            fresh = _sorted_by(
                [b for b in spare if int(b.id) not in held],
                key=lambda b: (b.pos.dist_sq(their_deposit), b.id),
            )
            strike = (sticky + fresh)[:p.raid_squad]
        self._strike_ids = {int(b.id) for b in strike}
        strike_ids = {b.id for b in strike}

        # In the endgame, extractors still cannot fire.  Sending the whole mining
        # corps into a stronger battle line turns it into free eliminations; keep most
        # of them safe as a reserve and use at most one to provide point insurance.
        # After a clean conversion there are none of them left to argue about.
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
        # Log 1026 is exactly this loss: Boneyard Creek led on capture the whole match and
        # lost to elimination on tick 7077.
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
        be absorbed rather than banked.  ClankerBot ran nine healers on a twenty-four
        bot fleet in log 1074; against that, aim matters less than aiming at the right bot.
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
        """Expand economy early, without sacrificing the initial escort.

        The supplied match's winner grew from 3 to 7 extractors by tick 750,
        whereas the losing side never expanded beyond 3.  Use staged targets
        so a miner does not displace a crucial first fighter or medic.  Keep
        replenishing lost miners if there is enough time to recoup them.
        """
        conf, p = self.conf, self.p
        miners = healers = battle = 0
        for bot in state.fleet_me:
            if bot.class_ == BotClass.Extractor:
                miners += 1
            elif bot.class_ == BotClass.Healer:
                healers += 1
            else:
                battle += 1

        if endgame or self._converting(state):
            return BotClass.Battle
        if battle < 2:
            return BotClass.Battle

        danger = any(
            self._class_of(e) in (BotClass.Battle, None)
            and e.pos.dist(state.deposit_me.pos) <= p.raid_radius
            for e in state.fleet_other
        )
        cap = min(p.extractors_wanted, int(conf.deposit.extractor_cap))
        travel = self.home.dist(state.deposit_me.pos) / max(1e-6, conf.bot.speed)
        payback = conf.fabricator.rush_cost / max(1e-6, conf.bot.extract_rate)
        cutoff = conf.max_ticks - conf.endgame_ticks
        can_invest = (
            not danger and state.tick < conf.max_ticks * p.extractor_build_stop
            and cutoff - state.tick > travel + payback
        )

        # These stages are interleaved.  They do NOT force us to wait for the
        # whole economy before buying the first shooting line and healing.
        stages = (
            (2, 1, 4),
            (5, 2, 5),
            (8, 3, 7),
            (10, 4, cap),
        )
        for guns, medics, workers in stages:
            if battle < guns:
                return BotClass.Battle
            if healers < min(medics, p.max_healers):
                return BotClass.Healer
            if can_invest and miners < min(workers, cap):
                return BotClass.Extractor

        # If miners die during midgame, replace them; otherwise expand the
        # combat line.  Under a raid, battle/healer replenishment takes over.
        wounded = sum(
            1 for b in state.fleet_me
            if b.class_ == BotClass.Battle and b.health < conf.bot.health - 2.0
        )
        want_healers = min(
            p.max_healers,
            max(p.healers_early, battle // max(1, p.battle_per_healer)
                + (1 if wounded >= 5 else 0)),
        )
        if healers < want_healers and battle >= 5:
            return BotClass.Healer
        if can_invest and miners < cap and battle >= miners + p.miner_lead:
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
        ours, theirs, _local = self._combat_balance(fighters, enemies_pred, payload)
        retreat = self._retreat_state(state.tick, ours, theirs,
                                      not freeze and not endgame and state.capture >= p.rally_min_capture)
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

        # The muster point, and whether the squad has formed up.
        #
        # This used to be the centroid of our own six leading bodies, which is a feedback
        # loop rather than a position: bots that fall back drag the muster back with them,
        # so a fleet that loses one engagement stages further and further from the payload
        # and never returns.  In friendly 1270 our survivors sat at (26,14) from tick 900
        # to tick 2000 while the enemy walked the payload the whole length of the path.
        #
        # Anchoring it to the PATH instead fixes both halves: it cannot drift, and it is
        # always a place the squad can push forward from.
        muster = _inside(payload_pos(_clamp(state.capture - p.muster_back, -1.0, 1.0)))
        formed = sum(1 for b in fighters if b.pos.dist(muster) <= p.muster_radius)
        want_formed = min(p.squad_size, max(2, int(round(len(fighters) * 0.6))))

        # Is anyone of ours actually in a fight right now?  Once shots are being traded,
        # the squad is past the point where falling back is free: bodies that turn round
        # are shot in the back, and any body left behind is shot alone.
        engaged = False
        for b in fighters:
            for _, ep in enemies_pred:
                if b.pos.dist(ep) <= conf.bot.blaster_range:
                    engaged = True
                    break
            if engaged:
                break

        committed = getattr(self, "_committed", True)
        since = state.tick - getattr(self, "_commit_tick", -10 ** 6)
        if committed:
            # A squad only un-commits BEFORE contact, or after contact has been broken.
            # Flipping mid-fight is what strands the anchors: everyone else walks back to
            # the muster and the two bodies holding the circle are left to die alone.
            # `commit_hold` stops it flickering on a single tick of bad arithmetic.
            if not engaged and since >= p.commit_hold:
                committed = not retreat
        else:
            committed = (formed >= want_formed
                         or ours >= theirs
                         or endgame or freeze
                         or state.capture <= -0.35)
        if committed != getattr(self, "_committed", True):
            self._commit_tick = state.tick
        self._committed = committed
        for index, bot in enumerate(holders):
            ba = action.bots[bot.id]
            anchor = int(bot.id) in anchors or freeze
            target = slots[index]
            staging = False
            if bot.id == survivor_id:
                target = self.home
            elif retreat:
                target = self._legal_slot(rally, front + 90.0 + (index % 7) * 30.0,
                                          1.4 + (index // 7) * 1.1, False)
                staging = True
            elif not freeze and not endgame and not committed:
                # Form up on the path rather than walking into the fight one at a time.
                # No anchor exemption: the squad goes forward together and comes back
                # together.  Leaving two bodies on the point while the rest re-form hands
                # the enemy two free kills and does not hold the circle anyway -- they
                # die in well under the time it takes the squad to reassemble.  The
                # circle is conceded for those ticks, which is what `capture <= -0.35`
                # above exists to put a floor under.
                target = self._legal_slot(muster, front + 150.0 + (index % 5) * 24.0,
                                          1.2 + (index % 7) * 0.45, False)
                staging = True
            mark = self._pick_target(bot.pos, enemies_pred, payload, cheap,
                                     bot.next_fire_tick <= state.tick
                                     if bot.class_ == BotClass.Battle else False,
                                     state.tick, guards, healer_ids, shooter=bot)
            if (mark is not None and not anchor and not staging
                    and bot.id != survivor_id):
                # Stop a supporting shooter walking away from a usable firing lane.
                if (bot.pos.dist(mark[1]) <= conf.bot.blaster_range * 0.78
                        and bot.pos.dist(payload) <= p.line_radius + 0.1):
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

            # Raid-role target acquisition must not silently swap its miner
            # target for an unrelated shooter near the payload.
            aim = None
            if mark is not None and here.dist(mark_pos) <= conf.bot.blaster_range:
                if (line_of_sight(here, mark_pos)
                        and not self._disc_blocks(here, mark_pos, payload, conf.payload.radius)
                        and not any(self._disc_blocks(here, mark_pos, d, conf.deposit.radius)
                                    for d in self._shot_deposits)):
                    aim = (mark, mark_pos)
            if aim is None:
                aim = self._pick_target(
                    here, enemies_pred, payload, cheap, bot.next_fire_tick <= state.tick,
                    state.tick, guards, healer_ids, shooter=bot,
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
        """Healers shadow the shooters and stay inside heal range of them.

        The old rule picked the single most wounded ally anywhere within `base_heal_range
        * 1.7` and walked at it.  Three things went wrong with that, all visible in
        friendlies 1330 and 1332: the worst-hurt ally changes from tick to tick so healers
        oscillated between patients instead of healing either; a patient outside range
        meant a long walk during which nothing was healed at all; and with nobody in range
        the healer fell back to a slot near the payload, which is how ours ended up four
        to six units from the nearest bleeding bot with zero allies in range at ticks 800
        and 1000.

        Now the station comes first and the patient second.  A healer is posted to the
        wounded body's side of the firing line and never leaves heal range of the line,
        so whoever takes the next hit is already covered.  It heals the nearest reachable
        wounded ally, not the most wounded one on the map.
        """
        if not healers:
            return

        conf = self.conf
        p = self.p
        full = conf.bot.health
        reach = conf.bot.base_heal_range
        stack = max(1, int(conf.bot.heal_stack_cap))  # a fourth healer on one bot is wasted

        fighters = [b for b in allies
                    if b.class_ == BotClass.Battle and b.id != survivor_id]  # noqa: F405
        threat = self._front(payload, enemies) if enemies else self.enemy_home

        # The line, as the healers see it: the shooters nearest the enemy, since those are
        # the ones taking fire.  Anchoring the station to them keeps the healers moving
        # with the fight instead of with the payload.
        line = sorted(fighters, key=lambda b: b.pos.dist_sq(threat))[:max(3, len(fighters) // 2)]
        if line:
            centre = Vec2(sum(b.pos.x for b in line) / len(line),  # noqa: F405
                          sum(b.pos.y for b in line) / len(line))
        else:
            centre = payload
        back = (centre - threat).normalize_or_zero()
        if back.norm_sq() < 1e-8:
            back = Vec2(0.0, 1.0)  # noqa: F405

        ordered = sorted(healers, key=lambda b: b.id)
        home_want = min(p.home_healers if raided else 0, max(0, len(ordered) - 1))
        home_ids_set = {b.id for b in ordered[:home_want]}

        load = {}
        for index, healer in enumerate(ordered):
            ba = action.bots[healer.id]

            if healer.id == survivor_id:
                ba.move_action = move_bot(navigate_to(healer.pos, self.home))  # noqa: F405
                ba.turn_action = turn_towards(payload)  # noqa: F405
                ba.special_action = SpecialAction.Healer(fire=False, target=int(healer.id))  # noqa: F405
                continue

            if healer.id in home_ids_set:
                lane = (self.enemy_home - deposit).normalize_or_zero()
                stand = _inside(deposit + lane * 1.6) if lane.norm_sq() > 1e-8 else deposit
            else:
                # Just behind the line, fanned sideways so one splash cannot catch two,
                # and close enough that most of the line is inside `base_heal_range`.
                fan = (index % 3 - 1) * 40.0
                stand = self._legal_slot(centre, back.angle_deg() + fan, reach * 0.55, False)

            # A reachable wounded ally overrides the station only by a step: close the
            # last of the gap, never cross the formation.
            patient = None
            for hurt in sorted(
                (b for b in allies if b.health < full - 1e-3 and b.id != healer.id),
                key=lambda b: (0 if b.health <= conf.bot.blaster_damage else 1,
                               healer.pos.dist_sq(b.pos), b.health),
            ):
                if load.get(hurt.id, 0) >= stack:
                    continue
                if healer.pos.dist(hurt.pos) > reach * 1.25:
                    continue  # too far to be worth abandoning the station for
                if not line_of_sight(healer.pos, hurt.pos):  # noqa: F405
                    continue
                patient = hurt
                break

            if patient is not None and healer.pos.dist(patient.pos) > reach * 0.8:
                # Step towards the patient's covered side rather than onto it.
                away = (patient.pos - threat).normalize_or_zero()
                if away.norm_sq() < 1e-8:
                    away = back
                stand = self._legal_slot(patient.pos + patient.vel,
                                         away.angle_deg(), reach * 0.5, False)

            step = self._separated_step(healer, stand, healers, False)
            ba.move_action = move_bot(step)  # noqa: F405

            if patient is None:
                ba.turn_action = turn_towards(_inside(centre))  # noqa: F405
                ba.special_action = SpecialAction.Healer(fire=False, target=int(healer.id))  # noqa: F405
                continue

            load[patient.id] = load.get(patient.id, 0) + 1
            ba.turn_action = turn_towards(patient.pos + patient.vel)  # noqa: F405
            # Asking is free: out of range or out of arc it simply does not land, and
            # there is no heal cooldown to burn.
            ba.special_action = SpecialAction.Healer(fire=True, target=int(patient.id))  # noqa: F405

    # ────────────────────────────── fire control ───────────────────────────

    def _fire_control(self, state, action, aims: dict, enemies_pred) -> None:
        """Coordinate real, unobstructed shots and focus enough guns for kills.

        The old `claimed` set allowed only ONE blaster hit per enemy per tick.
        A single healer restores 3 HP in the 60 ticks between that bot's
        shots, exactly cancelling a solo blaster.  That made a protected enemy
        effectively immortal; multiple shooters must be allowed to fire at it.
        """
        if not aims or not enemies_pred:
            return
        conf, tick = self.conf, state.tick
        splash = conf.bot.base_blaster_splash_radius + conf.bot.radius
        deposits = (state.deposit_me.pos, state.deposit_other.pos)
        payload = state.payload_pos()
        enemy_by_id = {int(e.id): e for e, _ in enemies_pred}
        healers, guards = self._heal_support([e for e, _ in enemies_pred])
        healer_ids = {int(e.id) for e in healers}
        enemy_miners = {
            int(e.id) for e, ep in enemies_pred
            if self._class_of(e) == BotClass.Extractor
            and ep.dist(state.deposit_other.pos) <= self.p.raid_radius
        }
        # Sorting by fewest available rays first prevents slow-turning shooters
        # being forced to waste their rare shot on a target they cannot reach.
        choices = []
        for bot_id, (bot, here, desired_angle) in aims.items():
            if bot.next_fire_tick > tick:
                continue
            candidate_angles = [desired_angle]
            reachable = []
            for enemy, pos in enemies_pred:
                gap = here.dist(pos)
                if gap > conf.bot.blaster_range or gap < 1e-5:
                    continue
                angle = (pos - here).angle_deg()
                error = abs(diff_degrees(angle, bot.angle))
                tolerance = math.degrees(math.asin(
                    min(1.0, max(0.0, conf.bot.radius - self.p.aim_slack) / gap)
                ))
                if error <= conf.bot.turn_speed + tolerance:
                    reachable.append((gap, pos))
            for _, pos in sorted(reachable, key=lambda t: t[0])[:12]:
                candidate_angles.append(self._after_turn(bot, here, pos))

            options, seen = [], set()
            for angle in candidate_angles:
                hit = self._ray_hit(here, angle, enemies_pred, payload, deposits)
                if hit is None:
                    continue
                enemy, impact, gap = hit
                eid = int(enemy.id)
                if eid in seen:
                    continue
                seen.add(eid)
                victims = [e for e, ep in enemies_pred
                           if ep.dist(impact) <= splash
                           and e.invulnerable_until_tick <= tick]
                if victims:
                    options.append((victims, gap, angle, eid))
            if options:
                choices.append((len(options), bot_id, options))

        assigned_damage = {}
        struck = {}
        strike_ids = getattr(self, "_strike_ids", set())
        for _, bot_id, options in sorted(choices, key=lambda x: (x[0], x[1])):
            best = None
            best_score = -1e20
            for victims, gap, angle, primary in options:
                score = -gap * 0.045
                useful = False
                for e in victims:
                    eid = int(e.id)
                    planned = assigned_damage.get(eid, 0.0)
                    remaining = max(0.0, e.health - planned)
                    if remaining <= 0.0:
                        score -= 6.0  # do not waste an entire cooldown on a corpse
                        continue
                    useful = True
                    # A kill is more valuable than spreading 3 HP around an
                    # enemy with nine medics in formation.
                    score += 3.5 + (6.0 if remaining <= conf.bot.blaster_damage else 0.0)
                    score += 1.6 * min(3.0, struck.get(eid, 0))
                    if eid in healer_ids:
                        score += 6.0
                    if eid in enemy_miners and bot_id in strike_ids:
                        score += 16.0
                    if e.pos.dist(payload) <= conf.payload.capture_radius + 0.5:
                        score += 1.5
                    score -= 0.85 * guards.get(eid, 0)
                if useful and score > best_score:
                    best_score = score
                    best = (victims, angle, primary)
            if best is None:
                continue
            victims, angle, primary = best
            # Retain the selected final angle exactly.  `turn_towards` is
            # engine's turn controller, and an unreachable ray is excluded above.
            action.bots[bot_id].turn_action = turn_towards(
                here + Vec2.from_angle_deg(angle) * conf.bot.blaster_range
            )
            action.bots[bot_id].special_action = SpecialAction.Battle(fire=True)
            for e in victims:
                eid = int(e.id)
                assigned_damage[eid] = assigned_damage.get(eid, 0.0) + conf.bot.blaster_damage
                struck[eid] = struck.get(eid, 0) + 1
            if hasattr(self, "_target_memory"):
                self._target_memory[int(bot_id)] = primary

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




# ═══════════════════════════════════════════════════════════════════════════════
#  THE CHALLENGER -- the all-in pack formation, team 1's side of the war
# ═══════════════════════════════════════════════════════════════════════════════
#  A second, complete strategy, kept here so the two can be played head to head out of
#  one file.  `Brain` above is the incumbent: fixed line, muster on the path, give
#  ground when an engagement is lost, reform, come back.  `PackBrain` below never
#  leaves the payload at all.
#
#  What the challenger does differently:
#    * fixed composition -- seven miners, four healers, every other slot a gun
#    * the enemy is clustered into mobs, and the guns are split between those mobs in
#      proportion to how big each mob is
#    * every gun stands in a hexagonal shell around the payload, thickest on the side
#      the biggest mob comes from, and shoots from its seat
#    * healers split in the same proportion and ride inside their own sector
#    * no muster, no rally, no retreat, no garrison, no strike squad
#
#  Ring geometry is set by SPACING rather than by looking tight.  Splash is
#  `base_blaster_splash_radius + radius` = 0.55 measured to the hull, and a ray fired
#  into a blob hits something whatever the aim, so seats stay ~2.2 apart at every
#  radius: `ring_slots` seats on the first ring and `ring_slots` more on each ring out,
#  with `ring_step` equal to the arc between seats.  An earlier version packed nine
#  bodies inside one unit and lost all nine in a hundred ticks.
#
#  In my own harness the incumbent wins this matchup.  That is what the war is for --
#  the engine gets the final word, not a port of it.


@dataclass
class PackParams(Params):
    """`Params` plus the pack's geometry, and zeroes for everything it does without."""

    # fixed composition
    extractors_wanted: int = 7
    healers_wanted: int = 4

    # the shell
    ring_radius: float = 2.20           # first ring -- inside `capture_radius`, so all six contest
    ring_step: float = 2.20             # each further ring sits this much further out
    ring_slots: int = 6                 # seats on the first ring, and added per ring after
    min_gap: float = 1.70               # bodies hold at least this much apart, marching included
    healer_inset: float = 0.85          # healers ride this far inside their group's ring
    group_range: float = 13.0           # enemies this close to the payload are worth forming against
    group_link: float = 5.5             # enemies within this of each other are one mob

    # The ring breathes rather than breaking: outnumbered, the outer seats stand further
    # out instead of crowding into the enemy's splash.  The blaster reaches ten units, so
    # a wider ring still covers the whole circle and still surrounds the payload.
    breathe_ratio: float = 1.30         # enemy weight over ours that starts the widening
    breathe_out: float = 2.60           # how much further the outer seats stand

    # All-in: nothing is ever detached from the payload.
    garrison_base: int = 0
    garrison_per_raider: float = 0.0
    garrison_max: int = 0
    raid_squad: int = 0
    home_healers: int = 0

    _INTS = Params._INTS + ("healers_wanted", "ring_slots")


class PackBrain(Brain):
    """The all-in pack.

    Everything not overridden here is inherited from `Brain`: the fire control, the ray
    checks, the target scoring, the miners and the pre-endgame conversion are
    strategy-neutral machinery and both sides run the same copy of them.  What differs
    is only where the bodies stand and what gets built.
    """

    def __init__(self, params=None) -> None:
        super().__init__(params or PackParams())
    def _next_class(self, state, endgame: bool):
        """A fixed composition: seven miners, four healers, every other slot a gun.

        Ratios drift -- a ratio of healers to fighters keeps buying healers as the fleet
        grows, and a ratio of miners keeps buying miners after the economy is already
        built.  Fixed counts mean every body past the twelfth is a shooter, which is what
        the ring is made of.  The schedule interleaves so the line exists before the
        economy does: the opening 800 tokens buy roughly nine guns, seven miners and four
        healers inside the first twenty ticks.
        """
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

        # Nothing but guns once building stops mattering for anything else.
        if endgame or self._converting(state):
            return BotClass.Battle  # noqa: F405

        deposit = state.deposit_me.pos
        threats = [e for e in state.fleet_other
                   if self._class_of(e) in (BotClass.Battle, None)
                   and e.pos.dist(deposit) <= p.raid_radius]
        cap = min(p.extractors_wanted, int(conf.deposit.extractor_cap))
        travel_ticks = self.home.dist(deposit) / max(conf.bot.speed, 1e-6)
        payback_ticks = conf.fabricator.rush_cost / max(conf.bot.extract_rate, 1e-6)
        remaining = conf.max_ticks - conf.endgame_ticks - state.tick
        can_mine = (not threats
                    and state.tick < conf.max_ticks * p.extractor_build_stop
                    and remaining > travel_ticks + payback_ticks
                    and miners < cap
                    and miners + p.miner_lead <= battle)

        want_healers = min(p.healers_wanted, p.max_healers)
        for guns, workers, medics in ((2, 0, 0), (2, 3, 1), (6, 3, 1), (6, 5, 2),
                                      (9, 5, 2), (9, cap, want_healers)):
            if battle < guns:
                return BotClass.Battle  # noqa: F405
            if miners < min(workers, cap) and can_mine:
                return BotClass.Extractor  # noqa: F405
            if healers < min(medics, want_healers):
                return BotClass.Healer  # noqa: F405

        if can_mine:
            return BotClass.Extractor  # noqa: F405
        if healers < want_healers:
            return BotClass.Healer  # noqa: F405
        return BotClass.Battle  # noqa: F405

    def _front_angle(self, payload, enemies) -> float:
        """The heading from the payload towards wherever the enemy is coming from.

        Their standing bodies if any are close, otherwise their spawn corner -- every bot
        they build appears there, so with the point clear that heading is the lane their
        reinforcements have to walk up.
        """
        close = [e for e in enemies if e.pos.dist(payload) <= self.p.group_range]
        reference = self._front(payload, close) if close else self.enemy_home
        delta = reference - payload
        if delta.norm_sq() < 1e-8:
            return 0.0
        return delta.angle_deg()

    def _separated_step(self, bot, target, peers, cheap=False):
        step = (navigate_to(bot.pos, target) if bot.pos.dist(target) > 0.18
                else Vec2(0.0, 0.0))
        push = Vec2(0.0, 0.0)
        # A local repulsion acts on the route as well as at the destination: splash is
        # measured to the hull, so two bots sharing a spot share every blast.
        for other in peers:
            if other.id == bot.id:
                continue
            delta = bot.pos - other.pos
            distance = delta.norm()
            if distance >= self.p.min_gap:
                continue
            if distance < 1e-5:
                lo, hi = sorted((int(bot.id), int(other.id)))
                delta = Vec2.from_angle_deg((lo * 97 + hi * 53) % 360)
                if int(bot.id) != lo:
                    delta = delta * -1.0
            else:
                delta = delta * (1.0 / distance)
            push = push + delta * (self.p.min_gap - distance)
        mixed = step + push * 2.0
        if mixed.norm_sq() < 1e-8:
            return step
        candidate = mixed.normalize_or_zero()
        if point_free(bot.pos + candidate * self.conf.bot.speed):
            return candidate
        return step

    def _mobs(self, payload, enemies_pred):
        """The enemy, clustered into the mobs it actually arrives in.

        Single-linkage on `group_link`, then merged until nothing else touches, so two
        bodies walking together are one mob and two groups coming around opposite sides
        of the payload are two.  Healers count for less than shooters: the formation is
        sized against incoming damage, and a healer deals none.
        """
        p = self.p
        near = [(e, ep) for e, ep in enemies_pred if ep.dist(payload) <= p.group_range]
        if not near:
            return []

        mobs = []
        for e, ep in sorted(near, key=lambda t: t[1].dist(payload)):
            for m in mobs:
                if any(ep.dist(q) <= p.group_link for _, q in m):
                    m.append((e, ep))
                    break
            else:
                mobs.append([(e, ep)])

        merging = True
        while merging and len(mobs) > 1:
            merging = False
            for i in range(len(mobs)):
                for j in range(i + 1, len(mobs)):
                    if any(a.dist(b) <= p.group_link for _, a in mobs[i] for _, b in mobs[j]):
                        mobs[i] = mobs[i] + mobs.pop(j)
                        merging = True
                        break
                if merging:
                    break

        out = []
        for m in mobs:
            centre = Vec2(sum(q.x for _, q in m) / len(m), sum(q.y for _, q in m) / len(m))  # noqa: F405
            delta = centre - payload
            weight = sum(0.6 if self._class_of(e) == BotClass.Healer else 1.0  # noqa: F405
                         for e, _ in m)
            out.append({
                "centre": centre,
                "angle": delta.angle_deg() if delta.norm_sq() > 1e-8 else 0.0,
                "weight": weight,
                "members": m,
            })
        out.sort(key=lambda g: -g["weight"])
        return out

    @staticmethod
    def _split(total: int, mobs):
        """`total` bodies divided between the mobs in proportion to their weight.

        Largest remainder, then a pass that buys every mob at least one body out of the
        biggest share -- an unwatched mob walks onto the point for free, and one body in
        the circle is the whole requirement.
        """
        if total <= 0 or not mobs:
            return []
        weight = sum(g["weight"] for g in mobs) or 1.0
        exact = [total * g["weight"] / weight for g in mobs]
        share = [int(x) for x in exact]
        order = sorted(range(len(mobs)), key=lambda i: -(exact[i] - share[i]))
        for k in range(total - sum(share)):
            share[order[k % len(order)]] += 1
        for i in range(len(share)):
            if share[i] == 0:
                donor = max(range(len(share)), key=lambda j: share[j])
                if share[donor] > 1:
                    share[donor] -= 1
                    share[i] += 1
        return share

    def _ring_slots(self, base_angle: float, count: int):
        """Concentric rings of `ring_slots` seats -- an octagon by default, a hexagon at
        six -- with a seat pointed straight down `base_angle` so the pack has a vertex
        facing the threat rather than a gap.

        Returns (angle, radius, ring) triples; the first ring sits inside
        `capture_radius`, so every one of its seats contests the payload on its own.
        """
        p = self.p
        out = []
        ring = 0
        while len(out) < count and ring < 8:
            radius = p.ring_radius + ring * p.ring_step
            seats = max(3, p.ring_slots * (ring + 1))  # arc length per seat stays constant
            for k in range(seats):
                out.append((normalize_degrees(base_angle + 360.0 * k / seats), radius, ring))  # noqa: F405
            ring += 1
        return out

    def _orders_bodies(
        self, state, action, bodies, enemies_pred, payload, endgame, survivor_id, cheap,
        freeze, guards, healer_ids,
    ) -> dict:
        """Every gun stands in the ring around the payload and never leaves it.

        There is no muster, no staging, no rally and no retreat.  The pack forms up
        around the payload, thickest on the side the biggest mob is coming from, and if
        the enemy arrives from two directions the bodies are split between them in the
        same proportion as the mobs themselves.  A bot's seat is the only place it goes;
        it shoots from there.
        """
        aims = {}
        if not bodies:
            return aims
        conf, p = self.conf, self.p

        fighters = [b for b in bodies if b.class_ == BotClass.Battle  # noqa: F405
                    and b.id != survivor_id]
        others = [b for b in bodies if b not in fighters]
        active_ids = {int(b.id) for b in fighters}
        if not hasattr(self, "_target_memory"):
            self._target_memory = {}
        self._target_memory = {k: v for k, v in self._target_memory.items() if k in active_ids}

        # Which way the pack faces, smoothed so it cannot spin on a single tick's noise.
        mobs = self._mobs(payload, enemies_pred)
        desired_front = mobs[0]["angle"] if mobs else self._front_angle(
            payload, [e for e, _ in enemies_pred])
        last_front = getattr(self, "_formation_front", desired_front)
        front = normalize_degrees(last_front + _clamp(  # noqa: F405
            diff_degrees(desired_front, last_front), -2.5, 2.5))  # noqa: F405
        self._formation_front = front

        # Bodies to each mob, in proportion to the mob.
        if mobs:
            shares = self._split(len(fighters), mobs)
        else:
            mobs = [{"centre": payload + Vec2.from_angle_deg(front) * 8.0,  # noqa: F405
                     "angle": front, "weight": 1.0, "members": []}]
            shares = [len(fighters)]
        self._mob_plan = [(mobs[i]["angle"], shares[i]) for i in range(len(mobs))]

        # Seats: every mob takes the unclaimed seats nearest its heading, inner rings
        # first, so the circle is always manned before the outer rings fill.
        # How the fight is going, in bodies weighted by health, inside the ring's reach.
        reach = p.ring_radius + p.ring_step * 2.0
        ours = sum(0.5 + 0.5 * b.health / conf.bot.health
                   for b in fighters if b.pos.dist(payload) <= reach + 2.0)
        theirs = sum(0.5 + 0.5 * e.health / conf.bot.health
                     for g in mobs for e, ep in g.get("members", ())
                     if ep.dist(payload) <= reach + 2.0)
        breathing = theirs > ours * p.breathe_ratio + 0.5

        seats = self._ring_slots(front, max(1, len(fighters)))
        if breathing:
            seats = [(a, r + (0.0 if i < p.ring_slots else p.breathe_out), ring)
                     for i, (a, r, ring) in enumerate(seats)]
        taken = [False] * len(seats)
        plan = []  # (mob index, angle, radius)
        for index, share in enumerate(shares):
            heading = mobs[index]["angle"]
            for _ in range(share):
                best, best_score = -1, None
                for k, (angle, radius, ring) in enumerate(seats):
                    if taken[k]:
                        continue
                    score = abs(diff_degrees(angle, heading)) + 90.0 * ring  # noqa: F405
                    if best_score is None or score < best_score:
                        best, best_score = k, score
                if best < 0:
                    break
                taken[best] = True
                plan.append((index, seats[best][0], seats[best][1]))

        # Bots to seats: each mob's share goes to the bodies already standing nearest its
        # heading, then nearest seat first, so nobody crosses the formation to take a
        # place a neighbour could have filled.
        around = sorted(
            fighters,
            key=lambda b: diff_degrees((b.pos - payload).angle_deg(), front)  # noqa: F405
            if (b.pos - payload).norm_sq() > 1e-8 else 0.0,
        )
        assignment = {}
        pool = list(around)
        for index, share in enumerate(shares):
            heading = mobs[index]["angle"]
            pool.sort(key=lambda b: abs(diff_degrees(  # noqa: F405
                (b.pos - payload).angle_deg() if (b.pos - payload).norm_sq() > 1e-8 else 0.0,
                heading)))
            group = pool[:share]
            del pool[:share]
            places = [(a, r) for m, a, r in plan if m == index]
            for bot in sorted(group, key=lambda b: b.pos.dist_sq(payload)):
                if not places:
                    break
                here = bot.pos
                places.sort(key=lambda ar: here.dist_sq(
                    payload + Vec2.from_angle_deg(ar[0]) * ar[1]))  # noqa: F405
                angle, radius = places.pop(0)
                assignment[int(bot.id)] = (index, angle, radius)

        for bot in others + pool:  # anything unseated still stands on the point
            assignment.setdefault(int(bot.id), (0, front + 180.0, p.ring_radius))

        for bot in bodies:
            ba = action.bots[bot.id]
            mob_index, angle, radius = assignment.get(
                int(bot.id), (0, front + 180.0, p.ring_radius))

            if bot.id == survivor_id:
                target = self.home
            else:
                target = self._legal_slot(payload, angle, radius, cheap)

            step = self._separated_step(bot, target, bodies, cheap)
            ba.move_action = move_bot(step)  # noqa: F405
            here = bot.pos + self._applied(step, conf.bot.speed)

            if bot.class_ != BotClass.Battle:  # noqa: F405
                # A miner pressed into the ring in the endgame: a body, not a shooter.
                ba.turn_action = turn_towards(payload)  # noqa: F405
                ba.special_action = SpecialAction.Extractor(mine=True)  # noqa: F405
                continue

            ba.special_action = SpecialAction.Battle(fire=False)  # noqa: F405
            ready = bot.next_fire_tick <= state.tick

            # Shoot into your own mob first -- that is what the seat was assigned for --
            # and only look at the rest of the map when it offers nothing.
            own = mobs[mob_index]["members"] if mob_index < len(mobs) else []
            mark = None
            if own:
                mark = self._pick_target(here, own, payload, cheap, ready, state.tick,
                                         guards, healer_ids, shooter=bot)
            if mark is None:
                mark = self._pick_target(here, enemies_pred, payload, cheap, ready,
                                         state.tick, guards, healer_ids, shooter=bot)
            if mark is None:
                facing = mobs[mob_index]["centre"] if mob_index < len(mobs) else None
                ba.turn_action = turn_towards(facing if facing is not None else payload)  # noqa: F405
                self._target_memory.pop(int(bot.id), None)
                continue

            enemy, mark_pos = mark
            self._target_memory[int(bot.id)] = int(enemy.id)
            ba.turn_action = turn_towards(mark_pos)  # noqa: F405
            aims[int(bot.id)] = (bot, here, self._after_turn(bot, here, mark_pos))
        return aims

    def _orders_healers(
        self, state, action, healers, allies, enemies, payload, survivor_id,
        home_ids, raided, deposit,
    ) -> None:
        """Healers ride inside the ring, split between the mobs in the same proportion as
        the guns are.

        A healer that chases the worst-hurt bot across the formation spends its match
        walking and heals nobody; heal range is three units and the ring is barely wider
        than that, so standing in its own sector puts it inside range of the bodies it was
        assigned to and inside `capture_radius` as another body contesting the point.
        """
        if not healers:
            return

        conf = self.conf
        p = self.p
        full = conf.bot.health
        reach = conf.bot.base_heal_range
        stack = max(1, int(conf.bot.heal_stack_cap))  # a fourth healer on one bot is wasted

        plan = getattr(self, "_mob_plan", None) or [(getattr(self, "_formation_front", 0.0), 1)]
        ordered = sorted(healers, key=lambda b: b.id)
        shares = self._split(len(ordered), [{"weight": max(1, n)} for _, n in plan])
        if not shares:
            shares = [len(ordered)]

        # Same proportion as the guns: sector by sector, in order.
        sector_of = {}
        seat_of = {}
        cursor = 0
        for index, count in enumerate(shares):
            for seat in range(count):
                if cursor >= len(ordered):
                    break
                sector_of[int(ordered[cursor].id)] = plan[index][0] if index < len(plan) else 0.0
                seat_of[int(ordered[cursor].id)] = seat
                cursor += 1
        for healer in ordered[cursor:]:
            sector_of[int(healer.id)] = plan[0][0]
            seat_of[int(healer.id)] = 0

        ring = max(conf.payload.radius + conf.bot.radius + 0.1,
                   p.ring_radius + p.ring_step - p.healer_inset)

        load = {}
        for healer in ordered:
            ba = action.bots[healer.id]
            angle = sector_of[int(healer.id)]
            seat = seat_of[int(healer.id)]

            if healer.id == survivor_id:
                ba.move_action = move_bot(navigate_to(healer.pos, self.home))  # noqa: F405
                ba.turn_action = turn_towards(payload)  # noqa: F405
                ba.special_action = SpecialAction.Healer(fire=False, target=int(healer.id))  # noqa: F405
                continue

            # The station never moves off the payload; seats inside a sector fan out so
            # one splash cannot catch two healers.
            spread = 0.0 if seat == 0 else (18.0 * seat if seat % 2 else -18.0 * seat)
            stand = self._legal_slot(payload, angle + spread, ring, False)
            step = self._separated_step(healer, stand, healers, False)
            ba.move_action = move_bot(step)  # noqa: F405
            here = healer.pos + self._applied(step, conf.bot.speed)

            # Whoever is hurt and reachable from the station, worst first.  No walking.
            patient = None
            for hurt in sorted(
                (b for b in allies if b.health < full - 1e-3 and b.id != healer.id),
                key=lambda b: (b.health, here.dist_sq(b.pos)),
            ):
                if load.get(hurt.id, 0) >= stack:
                    continue
                if here.dist(hurt.pos + hurt.vel) > reach:
                    continue
                if not line_of_sight(here, hurt.pos):  # noqa: F405
                    continue
                patient = hurt
                break

            if patient is None:
                ba.turn_action = turn_towards(  # noqa: F405
                    _inside(payload + Vec2.from_angle_deg(angle) * 6.0))  # noqa: F405
                ba.special_action = SpecialAction.Healer(fire=False, target=int(healer.id))  # noqa: F405
                continue

            load[patient.id] = load.get(patient.id, 0) + 1
            ba.turn_action = turn_towards(patient.pos + patient.vel)  # noqa: F405
            # Asking is free: out of range or out of arc it simply does not land, and
            # there is no heal cooldown to burn.
            ba.special_action = SpecialAction.Healer(fire=True, target=int(patient.id))  # noqa: F405

def get_strategy(team: int):
    """A fresh Brain per call.

    The old module-level singleton cached `_conf`, `_spots` and `_home` for the life of
    the process.  That is fine for one `mm-cli run`, but the tuner plays thousands of
    matches back to back and a cache from match 1 would silently corrupt match 2.

    Parameters come from MM_TUNE_PARAMS_<team> while the tuner is driving, and from the
    dataclass defaults otherwise -- so a submitted bot is unaffected by any of this.
    """
    if SPARRING_MODE and team == 1:
        # The war.  Team 0 is the incumbent `Brain`, team 1 is the `PackBrain`
        # challenger.  Run the series, then submit whichever won: set SPARRING_MODE
        # False, and if the pack won, return `PackBrain(...)` for every team below.
        # `sparring_strategy` is still in this file if you want the dumb bot instead.
        print("[strategy] *** SPARRING MODE: team 1 is the PACK FORMATION challenger. "
              "Set SPARRING_MODE = False before submitting. ***")
        return PackBrain(PackParams.from_env(team)).act

    if SPARRING_MODE:
        print("[strategy] *** SPARRING MODE is ON -- team 0 is the incumbent Brain. "
              "Do not submit like this. ***")

    brain = Brain(Params.from_env(team))
    print(f"[strategy] team {team} reporting in")
    return brain.act


# ═══════════════════════════════════════════════════════════════════════════════
#  TUNER -- everything below this line runs only from the command line.
# ═══════════════════════════════════════════════════════════════════════════════
#
#  Why a cross-entropy search rather than PPO or DQN: the decision rules above are
#  already derived from the engine and are correct.  What is unknown is ~38 scalars.
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
# the friendly logs ran with capture_radius 2.5 and blaster_range 10.
SPEC = [
    ("extractors_wanted",    2.0,   8.0),
    ("miner_lead",           0.0,   6.0),
    ("battle_per_healer",    2.0,   6.0),
    ("max_healers",          2.0,  12.0),
    ("extractor_build_stop", 0.20,  0.80),

    ("convert_lead",        60.0, 700.0),
    ("convert_min_tokens",  50.0, 400.0),

    ("muster_back",          0.04,  0.35),
    ("muster_radius",        2.00,  6.00),
    ("squad_size",           2.0,  12.0),
    ("commit_hold",         20.0, 400.0),

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
    ("garrison_home_radius", 4.0,  20.0),
    ("healers_early",        1.0,   6.0),
    ("raid_radius",          4.0,  16.0),

    ("raid_squad",           0.0,   7.0),
    ("raid_start",           0.02,  0.50),
    ("raid_stop",            0.30,  0.95),
    ("raid_min_battle",      4.0,  18.0),

    ("aim_slack",            0.00,  0.15),
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
# output so you can fix these without guessing.  A friendly-match log ends with a
# commented `# result: {"reason":"payload","tick":6925,"winner":"A"}` line and names
# the winner by letter, so both the letter and the numeric forms are handled.
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
               garrison_base=0, garrison_max=1, raid_squad=0, convert_lead=0)

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

    Tuning 38 parameters at once tells you the package is better; it does not tell you
    which part earned it.  This plays the defaults against a copy with one group
    reverted, so a change that is doing nothing -- or is actively hurting -- shows up
    as a number near zero or below it rather than hiding inside a good aggregate.
    """
    import statistics

    base = Params().to_dict()
    variants = {
        "no muster":        dict(squad_size=0, muster_radius=0.1),
        "8 miners (old)":   dict(extractors_wanted=8, miner_lead=0),
        "no conversion":    dict(convert_lead=0),
        "3 miners (losers)": dict(extractors_wanted=3),
        "5 miners (old)":   dict(extractors_wanted=5),
        "no garrison":      dict(garrison_base=0, garrison_max=0, garrison_per_raider=0.0),
        "garrison from anywhere": dict(garrison_home_radius=40.0),
        "no strike squad":  dict(raid_squad=0),
        "4 anchors (old)":  dict(anchors_wanted=4),
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