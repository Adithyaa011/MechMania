"""MechMania 32 -- "Deliverables" competition bot.

The whole fleet runs on one observation about `step_payload`: the payload only moves
for a side that has at least one bot inside `capture_radius` **while the other side has
none**.  Bodies past the first do not make it move faster.  So the match is decided by a
fight over a 2.5-unit circle, and every tick answers two questions -- do we have bodies
in that circle, and are the enemy's bodies in it dying?

One tick, in order (`Brain._act`):

  1. economy      -- what the fabricator builds next, and whether we pay to rush it
  2. roles        -- miners, payload bodies, healers, home defenders
  3. movement     -- a destination and a facing for every bot
  4. fire control -- shots allocated so two bots never waste a volley on one target

Engine facts the code leans on, all re-read from `get_config()` rather than hardcoded:

  * a blast makes its victim invulnerable from the tick of the hit, so a second shot at
    the same bot on the same tick is thrown away.  `_fire_control` never allows it.
  * the blaster fires along the bot's *facing*, after that tick's move and turn, so
    aiming means predicting both.
  * walls, the payload and the deposits stop a shot; allies do not, and splash only ever
    hurts the enemy fleet -- there is no friendly fire here.
  * nothing separates two overlapping bots and splash is measured to the hull, so the
    fleet fans out into rings instead of stacking.
  * no bot is built in the last `endgame_ticks`, and an empty fleet in that window is an
    instant loss.
"""

from . import *

import math
import random

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
#  It prints a loud warning into the gamelog on every handshake while it is on.
# ═══════════════════════════════════════════════════════════════════════════════
SPARRING_MODE = True

# ───────────────────────────────── tuning ──────────────────────────────────────
# Everything here is a preference; every *rule* comes from `get_config()`.

EXTRACTORS_WANTED = 6  # the deposit holds 8 slots, shared with the enemy
BATTLE_PER_HEALER = 3  # one healer exactly cancels one battle bot's sustained damage
MAX_HEALERS = 8
EXTRACTOR_BUILD_STOP = 0.55  # fraction of the match after which a new miner cannot pay off

ANCHORS_WANTED = 4  # bodies kept inside the capture circle at all times
ANCHOR_RADIUS = 2.05  # inside `capture_radius`, outside the payload hull
# Degrees off the rear axis.  Deliberately not 0: a bot parked dead behind the payload is
# in cover, but the payload blocks its own shots too, and a stalemate at nil-nil is a draw.
ANCHOR_ANGLES = (70.0, -70.0, 110.0, -110.0, 45.0, -45.0, 135.0, -135.0, 20.0, -20.0)
LINE_RADIUS = 4.60  # the shooting line: well inside blaster range of the circle
LINE_SPACING = 1.30  # gap between the line's rings when it gets crowded
LINE_PER_RING = 10
LINE_ARC = 110.0  # how wide the line fans out across the side it is screening
THREAT_RANGE = 14.0  # enemies this close to the payload are what the line lines up against
RALLY_BACK = 0.20  # how far back down the path (in capture units) a beaten line regroups

AIM_SLACK = 0.04  # shaved off the target hull so a marginal shot is not taken
WOUNDED_PULLBACK = 4.0  # health at or below which a body drifts back to the healers
MINER_FLEE_HEALTH = 4.0  # a miner this hurt stops mining and runs home
RAID_RADIUS = 9.0  # an enemy this close to our deposit counts as a raid
CHEAP_BUDGET = 1200  # ticks left in the compute bank below which we cut corners


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _sorted_by(bots: List[BotState], key) -> List[BotState]:
    return sorted(bots, key=key)


def _inside(p: Vec2) -> Vec2:
    """Anything we hand to `navigate_to` has to be somewhere on the map."""
    edge = 0.6
    return Vec2(_clamp(p.x, edge, MAP_SIZE - edge), _clamp(p.y, edge, MAP_SIZE - edge))


class Brain:
    """One match's worth of state.  `act` is the strategy the engine calls each tick."""

    def __init__(self) -> None:
        self._conf: Optional[GameConfig] = None
        self._spots: Optional[List[Vec2]] = None
        self._home: Optional[Vec2] = None
        self._announced = False

    # ────────────────────────────── plumbing ───────────────────────────────

    @property
    def conf(self) -> GameConfig:
        if self._conf is None:
            self._conf = get_config()
        return self._conf

    @property
    def home(self) -> Vec2:
        """Our own spawn corner -- where `spawn_bot` puts every new body."""
        if self._home is None:
            r = self.conf.bot.radius
            self._home = Vec2(r + 0.1, float(MAP_SIZE) - r - 0.1)
        return self._home

    @property
    def enemy_home(self) -> Vec2:
        """Their spawn corner: `spawn_bot` mirrors our own, so it is ours rotated."""
        return Vec2(float(MAP_SIZE) - self.home.x, float(MAP_SIZE) - self.home.y)

    def act(self, state: GameState) -> FleetAction:
        """Never let a bug cost the match: a raised exception would kill the process."""
        try:
            return self._act(state)
        except Exception as exc:  # noqa: BLE001 -- last line of defence
            print(f"[strategy] tick {state.tick}: {exc!r}")
            return self._fallback(state)

    def _fallback(self, state: GameState) -> FleetAction:
        """Contest the payload and keep building.  Correct, if not clever."""
        action = FleetAction.new()
        try:
            payload = state.payload_pos()
            for bot in state.fleet_me:
                ba = action.bots[bot.id]
                ba.move_action = move_bot(navigate_to(bot.pos, payload))
                ba.turn_action = turn_towards(payload)
            action.fabricator_next = int(BotClass.Battle)
            action.rush_order = (
                state.fabricator_me.tokens >= self.conf.fabricator.rush_cost
                and not state.fleet_me.is_full()
            )
        except Exception:  # noqa: BLE001
            pass
        return action

    # ─────────────────────────────── the tick ──────────────────────────────

    def _act(self, state: GameState) -> FleetAction:
        conf = self.conf
        action = FleetAction.new()

        if not self._announced:
            self._announced = True
            print(
                f"[strategy] online: {conf.max_ticks} ticks, endgame at "
                f"{conf.max_ticks - conf.endgame_ticks}"
            )

        tick = state.tick
        endgame = tick >= conf.max_ticks - conf.endgame_ticks
        cheap = get_budget().remaining < CHEAP_BUDGET

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

        # 2 ─ roles ──────────────────────────────────────────────────────────
        miners = [b for b in allies if b.class_ == BotClass.Extractor]
        healers = [b for b in allies if b.class_ == BotClass.Healer]
        battle = [b for b in allies if b.class_ == BotClass.Battle]

        deposit = state.deposit_me.pos
        raiders = [e for e in enemies if e.pos.dist(deposit) <= RAID_RADIUS]

        # Fresh bodies still standing in our own half answer a raid on the deposit; they
        # are the ones already closest to it, so nothing walks backwards for this.
        defenders: List[BotState] = []
        if raiders and not endgame:
            wanted = min(len(raiders) + 1, max(1, len(battle) // 3))
            near_home = _sorted_by(
                [b for b in battle if b.pos.dist(deposit) < b.pos.dist(payload)],
                key=lambda b: b.pos.dist(deposit),
            )
            defenders = near_home[:wanted]
        defender_ids = {b.id for b in defenders}

        # In the endgame nothing can be built, tokens buy nothing, and an empty fleet
        # loses on the spot -- so the miners stop mining and become bodies on the point.
        bodies = [b for b in battle if b.id not in defender_ids]
        if endgame:
            bodies = bodies + miners
            working_miners: List[BotState] = []
        else:
            working_miners = miners

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
        self._orders_miners(state, action, working_miners, enemies, survivor_id)
        aims = self._orders_bodies(
            state, action, bodies, enemies_pred, payload, endgame, survivor_id, cheap
        )
        aims.update(self._orders_defenders(state, action, defenders, raiders))
        self._orders_healers(state, action, healers, allies, enemies, payload, survivor_id)

        # 4 ─ fire control ───────────────────────────────────────────────────
        self._fire_control(state, action, aims, enemies_pred)

        return action

    # ───────────────────────────── the fabricator ──────────────────────────

    def _next_class(self, state: GameState, endgame: bool) -> BotClass:
        """What the next body should be: economy first, then a battle line, then the
        healers that keep it standing."""
        conf = self.conf

        miners = healers = battle = 0
        for bot in state.fleet_me:
            cls = bot.class_
            if cls == BotClass.Extractor:
                miners += 1
            elif cls == BotClass.Healer:
                healers += 1
            else:
                battle += 1

        # A miner earns `extract_rate` a tick and needs a few hundred ticks just to walk
        # to the deposit, so it is only worth buying while there is match left to pay for.
        mining_window = state.tick < conf.max_ticks * EXTRACTOR_BUILD_STOP
        wanted_miners = min(EXTRACTORS_WANTED, int(conf.deposit.extractor_cap))
        if not endgame and mining_window and miners < wanted_miners:
            return BotClass.Extractor

        if battle >= 1 and healers < min(MAX_HEALERS, max(1, battle // BATTLE_PER_HEALER)):
            return BotClass.Healer

        return BotClass.Battle

    # ──────────────────────────────── miners ───────────────────────────────

    def _mine_spots(self, state: GameState) -> List[Vec2]:
        """Places a bot can stand, see our deposit from, and mine it.

        Worked out once -- the deposit never moves and neither do the walls.  Close rings
        first: the nearer the ring, the wider the aiming error the extraction ray forgives.
        """
        if self._spots is not None:
            return self._spots

        conf = self.conf
        deposit = state.deposit_me.pos
        reach = conf.bot.base_extract_range * 0.85
        spots: List[Vec2] = []

        for radius in (1.5, 2.1, 2.7, 3.4, 4.0):
            if radius > reach:
                break
            for step in range(24):
                p = deposit + Vec2.from_angle_deg(step * 15.0) * radius
                if not (0.5 < p.x < MAP_SIZE - 0.5 and 0.5 < p.y < MAP_SIZE - 0.5):
                    continue
                if any(p.dist_sq(q) < 0.49 for q in spots):
                    continue  # splash is measured to the hull: do not park bots on top of each other
                if not point_free(p):
                    continue
                if not line_of_sight(p, deposit):
                    continue
                spots.append(p)
            if len(spots) >= int(conf.deposit.extractor_cap):
                break

        if not spots:  # nothing legal found: stand off the hull and let collision sort it
            spots = [deposit + Vec2(0.0, conf.deposit.radius + conf.bot.radius + 0.1)]

        self._spots = spots
        return spots

    def _orders_miners(
        self,
        state: GameState,
        action: FleetAction,
        miners: List[BotState],
        enemies: List[BotState],
        survivor_id: int,
    ) -> None:
        if not miners:
            return

        conf = self.conf
        deposit = state.deposit_me.pos
        spots = self._mine_spots(state)

        # Stable by id, so nobody swaps spots with a neighbour every tick.
        for index, bot in enumerate(sorted(miners, key=lambda b: b.id)):
            ba = action.bots[bot.id]
            # Free to ask every tick: out of range or out of sight it simply does not land.
            ba.special_action = SpecialAction.Extractor(mine=True)
            ba.turn_action = turn_towards(deposit)

            if bot.id == survivor_id:
                ba.move_action = move_bot(navigate_to(bot.pos, self.home))
                continue

            threat = self._nearest(bot.pos, enemies)
            if (
                bot.health <= MINER_FLEE_HEALTH
                and threat is not None
                and bot.pos.dist(threat.pos) <= conf.bot.blaster_range + 1.0
            ):
                # The body is worth a rush order; the tokens it would have mined are not.
                ba.move_action = move_bot(navigate_to(bot.pos, self.home))
                continue

            spot = spots[index % len(spots)]
            if bot.pos.dist(spot) > 0.35:
                ba.move_action = move_bot(navigate_to(bot.pos, spot))
            # else: hold still -- a stationary miner keeps its ray on the deposit.

    # ───────────────────────── bodies on the payload ───────────────────────

    def _front_angle(self, payload: Vec2, enemies: List[BotState]) -> float:
        """The heading from the payload towards wherever the enemy is coming from.

        Their standing bodies if any are close, otherwise their spawn corner -- every bot
        they build appears there, so with the point clear that heading is the lane their
        reinforcements have to walk up.  Screening it is what stops the last stretch of a
        push from stalling on a body that pops out next to their own goal.
        """
        close = [e for e in enemies if e.pos.dist(payload) <= THREAT_RANGE]
        reference = self._front(payload, close) if close else self.enemy_home
        delta = reference - payload
        if delta.norm_sq() < 1e-8:
            return 0.0
        return delta.angle_deg()

    def _hold_slots(
        self, state: GameState, payload: Vec2, count: int, front: float, cheap: bool
    ) -> List[Vec2]:
        """`count` standing places: anchors on the point, then a firing line behind it.

        The first `ANCHORS_WANTED` sit inside the capture circle -- that is the whole game,
        since one body in there is all it takes to stop an enemy push.  They sit on the far
        side of the payload from the enemy, but off its flanks rather than dead behind it:
        behind is cover, but the payload stops their own shots as well, and two fleets in
        mutual cover is a nil-nil draw.

        Everyone else forms a line at `LINE_RADIUS` across the side the enemy is coming
        from, so the reinforcement walking at the point is shot before it arrives.  The
        blaster reaches four times further than the capture circle is wide, so the line
        covers the whole circle without standing in the scrum.
        """
        rear = front + 180.0
        out: List[Vec2] = []

        anchors = min(count, ANCHORS_WANTED)
        for k in range(anchors):
            angle = ANCHOR_ANGLES[k % len(ANCHOR_ANGLES)]
            out.append(self._legal_slot(payload, rear + angle, ANCHOR_RADIUS, cheap))

        span = LINE_ARC * 2.0 / max(1, LINE_PER_RING - 1)
        for k in range(count - anchors):
            ring, seat = divmod(k, LINE_PER_RING)
            side = 1.0 if seat % 2 == 0 else -1.0
            angle = front + ((seat + 1) // 2) * span * side
            out.append(self._legal_slot(payload, angle, LINE_RADIUS + ring * LINE_SPACING, cheap))
        return out

    def _legal_slot(self, payload: Vec2, angle: float, radius: float, cheap: bool) -> Vec2:
        p = _inside(payload + Vec2.from_angle_deg(angle) * radius)
        if cheap or point_free(p):
            return p
        for nudge in (18.0, -18.0, 36.0, -36.0, 60.0, -60.0):
            q = _inside(payload + Vec2.from_angle_deg(angle + nudge) * radius)
            if point_free(q):
                return q
        return payload

    def _orders_bodies(
        self,
        state: GameState,
        action: FleetAction,
        bodies: List[BotState],
        enemies_pred: List,
        payload: Vec2,
        endgame: bool,
        survivor_id: int,
        cheap: bool,
    ) -> dict:
        """Everyone whose job is the point.  Returns each shooter's aim, for step 4."""
        aims: dict = {}
        if not bodies:
            return aims

        conf = self.conf
        speed = conf.bot.speed
        capture = conf.payload.capture_radius

        holders = _sorted_by(bodies, key=lambda b: (b.pos.dist_sq(payload), b.id))
        front = self._front_angle(payload, [e for e, _ in enemies_pred])
        slots = self._hold_slots(state, payload, len(holders), front, cheap)

        inside = sum(1 for b in holders if b.pos.dist(payload) <= capture)
        contested = any(ep.dist(payload) <= capture + 1.0 for _, ep in enemies_pred)

        # A reinforcement that would arrive alone into a losing fight waits a little way
        # back down the path instead of handing the enemy a free kill.
        near_us = sum(1 for b in holders if b.pos.dist(payload) <= capture + 2.0)
        near_them = sum(1 for _, ep in enemies_pred if ep.dist(payload) <= capture + 2.0)
        outnumbered = near_them > near_us + 1
        rally = payload_pos(_clamp(state.capture - RALLY_BACK, -1.0, 1.0))

        for index, bot in enumerate(holders):
            ba = action.bots[bot.id]
            anchor = index < ANCHORS_WANTED

            if bot.id == survivor_id:
                target = self.home
            elif outnumbered and not endgame and not anchor:
                # Feeding bodies into a losing scrum one at a time is how a fleet gets
                # wiped.  The anchors keep the circle honest; everyone else regroups a
                # path segment back, out of blaster reach, and comes back as a block.
                target = rally
            elif (
                not endgame
                and bot.health <= WOUNDED_PULLBACK
                and inside >= 3
                and not anchor
            ):
                # Hurt, and not the last body holding the circle: drift back to the
                # healers.  Healing is a trickle, so it only pays if we stay alive for it.
                target = rally
            else:
                target = slots[index]

            step = navigate_to(bot.pos, target) if bot.pos.dist(target) > 0.22 else Vec2(0.0, 0.0)
            ba.move_action = move_bot(step)
            here = bot.pos + self._applied(step, speed)

            if bot.class_ != BotClass.Battle:
                # Miners pressed into service in the endgame: bodies, not shooters.
                ba.turn_action = turn_towards(payload)
                ba.special_action = SpecialAction.Extractor(mine=True)
                continue

            mark = self._pick_target(
                here, enemies_pred, payload, cheap, bot.next_fire_tick <= state.tick, state.tick
            )
            ba.special_action = SpecialAction.Battle(fire=False)  # step 4 may flip this
            if mark is None:
                # Nothing in reach: face up the path, where the next one comes from.
                ahead = payload_pos(_clamp(state.capture + 0.05, -1.0, 1.0))
                ba.turn_action = turn_towards(ahead if contested else payload)
                continue

            _, mark_pos = mark
            ba.turn_action = turn_towards(mark_pos)
            aims[int(bot.id)] = (bot, here, self._after_turn(bot, here, mark_pos))

        return aims

    def _orders_defenders(
        self,
        state: GameState,
        action: FleetAction,
        defenders: List[BotState],
        raiders: List[BotState],
    ) -> dict:
        """Battle bots answering a raid on our deposit rather than walking past it."""
        aims: dict = {}
        if not defenders or not raiders:
            return aims

        speed = self.conf.bot.speed
        for bot in defenders:
            ba = action.bots[bot.id]
            mark = self._nearest(bot.pos, raiders)
            if mark is None:
                continue
            mark_pos = mark.pos + mark.vel
            # Close to a range where the shot is forgiving, then stand and shoot.
            step = (
                navigate_to(bot.pos, mark_pos)
                if bot.pos.dist(mark_pos) > 4.0
                else Vec2(0.0, 0.0)
            )
            ba.move_action = move_bot(step)
            here = bot.pos + self._applied(step, speed)
            ba.turn_action = turn_towards(mark_pos)
            ba.special_action = SpecialAction.Battle(fire=False)
            aims[int(bot.id)] = (bot, here, self._after_turn(bot, here, mark_pos))
        return aims

    # ──────────────────────────────── healers ──────────────────────────────

    def _orders_healers(
        self,
        state: GameState,
        action: FleetAction,
        healers: List[BotState],
        allies: List[BotState],
        enemies: List[BotState],
        payload: Vec2,
        survivor_id: int,
    ) -> None:
        if not healers:
            return

        conf = self.conf
        full = conf.bot.health
        reach = conf.bot.base_heal_range
        stack = max(1, int(conf.bot.heal_stack_cap))  # a fourth healer on one bot is wasted

        wounded = _sorted_by(
            [b for b in allies if b.health < full - 1e-3 and b.class_ != BotClass.Healer],
            key=lambda b: (b.health, b.pos.dist_sq(payload)),
        )
        if not wounded:  # keep the healers themselves standing
            wounded = _sorted_by(
                [b for b in allies if b.health < full - 1e-3],
                key=lambda b: (b.health, b.pos.dist_sq(payload)),
            )

        load: dict = {}
        for healer in sorted(healers, key=lambda b: b.id):
            ba = action.bots[healer.id]

            if healer.id == survivor_id:
                ba.move_action = move_bot(navigate_to(healer.pos, self.home))
                ba.turn_action = turn_towards(payload)
                ba.special_action = SpecialAction.Healer(fire=False, target=int(healer.id))
                continue

            patient = None
            for hurt in wounded:
                if hurt.id == healer.id:  # a healer cannot heal itself
                    continue
                if load.get(hurt.id, 0) >= stack:
                    continue
                if healer.pos.dist(hurt.pos) > reach * 3.0:
                    continue
                patient = hurt
                break

            if patient is None:
                # Nobody to mend: sit just behind the line, inside the capture circle so
                # the body still counts against an enemy push.
                back = (payload - self._front(payload, enemies)).normalize_or_zero()
                anchor = _inside(payload + back * 2.0) if back.norm_sq() > 1e-8 else payload
                step = (
                    navigate_to(healer.pos, anchor)
                    if healer.pos.dist(anchor) > 0.3
                    else Vec2(0.0, 0.0)
                )
                ba.move_action = move_bot(step)
                ba.turn_action = turn_towards(payload)
                ba.special_action = SpecialAction.Healer(fire=False, target=int(healer.id))
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
                away = Vec2(0.0, 1.0)
            stand = _inside(patient_pos + away * (reach * 0.55))

            step = (
                navigate_to(healer.pos, stand) if healer.pos.dist(stand) > 0.3 else Vec2(0.0, 0.0)
            )
            ba.move_action = move_bot(step)
            ba.turn_action = turn_towards(patient_pos)
            # Asking always is free: an out-of-range or out-of-arc channel simply does not
            # land, and there is no heal cooldown to burn.
            ba.special_action = SpecialAction.Healer(fire=True, target=int(patient.id))

    # ────────────────────────────── fire control ───────────────────────────

    def _fire_control(
        self,
        state: GameState,
        action: FleetAction,
        aims: dict,
        enemies_pred: List,
    ) -> None:
        """Decide who actually pulls a trigger this tick.

        A blast makes its victim invulnerable from that same tick, so a second shot into
        the same bot on the same tick is thrown away entirely.  Shots are allocated
        against a claim set, closest (most reliable) shooter first; a bot whose target is
        already spoken for holds its cooldown for next tick instead of wasting it.
        """
        if not aims or not enemies_pred:
            return

        conf = self.conf
        tick = state.tick
        splash = conf.bot.base_blaster_splash_radius + conf.bot.radius
        deposits = (state.deposit_me.pos, state.deposit_other.pos)
        payload = state.payload_pos()

        shots = []
        for bot_id, (bot, here, angle) in aims.items():
            if bot.next_fire_tick > tick:
                continue  # blaster still cooling
            hit = self._ray_hit(here, angle, enemies_pred, payload, deposits)
            if hit is None:
                continue
            _, enemy_pos, gap = hit
            shots.append((gap, bot_id, enemy_pos))

        shots.sort(key=lambda s: s[0])  # the closest shot is the one most likely to land

        claimed = set()
        for _, bot_id, enemy_pos in shots:
            # Everything the blast would actually damage: whatever the ray stops on plus
            # anything else inside the splash, minus bots already invulnerable and bots
            # already claimed by an earlier shooter this tick.
            victims = [
                int(e.id)
                for e, ep in enemies_pred
                if ep.dist(enemy_pos) <= splash
                and e.invulnerable_until_tick <= tick
                and int(e.id) not in claimed
            ]
            if not victims:
                continue
            claimed.update(victims)
            action.bots[bot_id].special_action = SpecialAction.Battle(fire=True)

    def _ray_hit(self, origin: Vec2, angle: float, enemies_pred: List, payload: Vec2, deposits):
        """The enemy a shot from `origin` along `angle` would stop on, or `None`.

        Mirrors `step_blasters`: the ray runs out to `blaster_range`, allies are
        transparent, and walls, the payload and the deposits stop it short.
        """
        conf = self.conf
        reach = conf.bot.blaster_range
        hull = conf.bot.radius - AIM_SLACK

        best = None
        best_gap = reach + 1.0
        for enemy, pos in enemies_pred:
            delta = pos - origin
            gap = delta.norm()
            if gap > reach or gap < 1e-4 or gap >= best_gap:
                continue
            error = abs(diff_degrees(delta.angle_deg(), angle))
            if error >= 90.0:
                continue
            if gap * math.sin(math.radians(error)) > hull:
                continue  # the ray would slide past the hull
            if self._disc_blocks(origin, pos, payload, conf.payload.radius):
                continue
            if any(self._disc_blocks(origin, pos, d, conf.deposit.radius) for d in deposits):
                continue
            if not line_of_sight(origin, pos):
                continue
            best, best_gap = (enemy, pos, gap), gap
        return best

    @staticmethod
    def _disc_blocks(origin: Vec2, target: Vec2, centre: Vec2, radius: float) -> bool:
        """Whether a solid disc sits on the segment between the two points."""
        if radius <= 0.0:
            return False
        if (centre - origin).dot(target - origin) <= 0.0:
            return False  # behind the shooter
        if origin.dist(centre) - radius >= origin.dist(target):
            return False  # past the target
        return point_seg_dist(centre, origin, target) < radius

    # ──────────────────────────────── helpers ──────────────────────────────

    def _pick_target(
        self,
        here: Vec2,
        enemies_pred: List,
        payload: Vec2,
        cheap: bool,
        ready: bool,
        tick: int,
    ):
        """Who this bot points at.  Finishing a wounded body beats chipping a fresh one,
        and a body standing on the point is worth more dead than one out in the field.

        A bot with a loaded blaster also skips anything still inside its invulnerability
        window: that blast would be absorbed for nothing and cost a full cooldown."""
        conf = self.conf
        reach = conf.bot.blaster_range
        capture = conf.payload.capture_radius

        ranked = []
        for enemy, pos in enemies_pred:
            gap = here.dist(pos)
            if gap > reach + 6.0:
                continue
            shielded = 1 if (ready and enemy.invulnerable_until_tick > tick) else 0
            on_point = 0 if pos.dist(payload) <= capture + 1.0 else 1
            ranked.append(
                ((0 if gap <= reach else 1, shielded, on_point, enemy.health, gap), enemy, pos)
            )
        if not ranked:
            return None
        ranked.sort(key=lambda r: r[0])

        if cheap:
            return ranked[0][1], ranked[0][2]
        for _, enemy, pos in ranked[:4]:
            if line_of_sight(here, pos):
                return enemy, pos
        return ranked[0][1], ranked[0][2]

    def _after_turn(self, bot: BotState, here: Vec2, target: Vec2) -> float:
        """The facing the engine will give this bot once this tick's turn is applied.

        `eval_tick` moves first and turns second, so the aim is taken from where the bot
        will be standing, not where it is now -- and the turn is capped at `turn_speed`.
        """
        limit = self.conf.bot.turn_speed
        wanted = (target - here).angle_deg()
        return normalize_degrees(
            bot.angle + _clamp(diff_degrees(wanted, bot.angle), -limit, limit)
        )

    @staticmethod
    def _applied(direction: Vec2, speed: float) -> Vec2:
        """What `MoveAction::sanitize` does to our order, so we can predict our own step."""
        length = direction.norm()
        if length > 1.0:
            direction = direction / length
        return direction * speed

    @staticmethod
    def _nearest(point: Vec2, bots: List[BotState]) -> Optional[BotState]:
        best = None
        best_gap = 0.0
        for bot in bots:
            gap = point.dist_sq(bot.pos)
            if best is None or gap < best_gap:
                best, best_gap = bot, gap
        return best

    @staticmethod
    def _front(payload: Vec2, enemies: List[BotState]) -> Vec2:
        """Where the pressure is coming from -- the enemy centre of mass."""
        if not enemies:
            return payload + Vec2(1.0, 0.0)
        total = Vec2(0.0, 0.0)
        for enemy in enemies:
            total = total + enemy.pos
        return total / float(len(enemies))


# ───────────────────────── the sparring partner ────────────────────────────────
#  Only ever used locally, as team 1, while `SPARRING_MODE` is on.  It is meant to be
#  a plausible mid-table opponent, not a good one: it mines a little, walks at the
#  payload, chases whoever is nearest, and shoots on the crude "in range and visible"
#  test most first-day bots use.  The randomness matters -- a deterministic opponent
#  gets beaten the same way every match and hides the holes in a real strategy.


def sparring_strategy(state: GameState) -> FleetAction:
    conf = get_config()
    action = FleetAction.new()
    payload = state.payload_pos()
    fleet = list(state.fleet_me)
    enemies = list(state.fleet_other)

    miners = sum(1 for b in fleet if b.class_ == BotClass.Extractor)
    if miners < random.choice((2, 3, 4)):
        action.fabricator_next = int(BotClass.Extractor)
    elif random.random() < 0.2:
        action.fabricator_next = int(BotClass.Healer)
    else:
        action.fabricator_next = int(BotClass.Battle)

    in_endgame = state.tick >= conf.max_ticks - conf.endgame_ticks
    action.rush_order = (
        not in_endgame
        and state.fabricator_me.tokens >= conf.fabricator.rush_cost
        and random.random() < 0.8
    )

    deposit = state.deposit_me.pos
    mining_spot = deposit + Vec2(0.0, conf.deposit.radius + conf.bot.radius + 1.0)

    for bot in fleet:
        ba = action.bots[bot.id]

        if bot.class_ == BotClass.Extractor:
            ba.move_action = move_bot(navigate_to(bot.pos, mining_spot))
            ba.turn_action = turn_towards(deposit)
            ba.special_action = SpecialAction.Extractor(mine=True)
            continue

        mark = None
        for enemy in enemies:
            if mark is None or bot.pos.dist_sq(enemy.pos) < bot.pos.dist_sq(mark.pos):
                mark = enemy

        if bot.class_ == BotClass.Healer:
            hurt = None
            for ally in fleet:
                if ally.id == bot.id or ally.health >= conf.bot.health:
                    continue
                if hurt is None or ally.health < hurt.health:
                    hurt = ally
            if hurt is None:
                ba.move_action = move_bot(navigate_to(bot.pos, payload))
                ba.turn_action = turn_towards(payload)
                ba.special_action = SpecialAction.Healer(fire=False, target=int(bot.id))
            else:
                ba.move_action = move_bot(navigate_to(bot.pos, hurt.pos))
                ba.turn_action = turn_towards(hurt.pos)
                ba.special_action = SpecialAction.Healer(fire=True, target=int(hurt.id))
            continue

        # Battle: half the fleet sits on the point, the rest chases, and a wandering
        # offset keeps them from filing into a single stack.
        if mark is None or bot.id % 2 == 0:
            goal = payload + Vec2.from_angle_deg(random.uniform(0.0, 360.0)) * 1.8
        else:
            goal = mark.pos

        ba.move_action = move_bot(navigate_to(bot.pos, _inside(goal)))
        if mark is None:
            ba.turn_action = turn_towards(payload)
            ba.special_action = SpecialAction.Battle(fire=False)
            continue

        ba.turn_action = turn_towards(mark.pos)
        can_shoot = (
            bot.pos.dist(mark.pos) <= conf.bot.blaster_range
            and line_of_sight(bot.pos, mark.pos)
        )
        ba.special_action = SpecialAction.Battle(fire=can_shoot)

    return action


_BRAIN = Brain()


def get_strategy(team: int) -> Strategy:
    """In a real match both sides run `Brain.act`: the engine mirrors the world for the
    top-right team, so there is nothing for a side to specialise in.

    While `SPARRING_MODE` is on, team 1 runs the sparring partner instead so that a
    local `mm-cli run` is a real test rather than a clone fighting itself.
    """
    if SPARRING_MODE and team == 1:
        print("[strategy] *** SPARRING MODE: team 1 is the practice dummy. "
              "Set SPARRING_MODE = False before submitting. ***")
        return sparring_strategy

    if SPARRING_MODE:
        print("[strategy] *** SPARRING MODE is ON -- do not submit like this. ***")
    print(f"[strategy] team {team} reporting in")
    return _BRAIN.act