from . import *


def get_strategy(team: int) -> Strategy:
    """
    Same mirrored strategy for both teams.
    The engine mirrors the world for team B, so the strategy
    can always think of itself as the bottom-left team.
    """

    if team == 0:
        print("Hello! I am team A (bottom left)")
    else:
        print("Hello! I am team B (top right)")

    memory = {
        "last_capture": None,
        "payload_momentum": 0.0,
    }

    def strategy(state: GameState) -> FleetAction:
        return team_a_strategy(state, memory)

    return strategy


def team_a_strategy(state: GameState, memory=None) -> FleetAction:

    conf = get_config()
    action = FleetAction.new()

    payload = state.payload_pos()

    friends = list(state.fleet_me)
    enemies = list(state.fleet_other)

    battles = [
        b for b in friends
        if b.class_ == BotClass.Battle
    ]

    healers = [
        h for h in friends
        if h.class_ == BotClass.Healer
    ]

    extractors = [
        e for e in friends
        if e.class_ == BotClass.Extractor
    ]

    # ---------------------------------------------------------
    # MEMORY / PAYLOAD MOMENTUM
    # ---------------------------------------------------------

    if memory is None:
        memory = {
            "last_capture": None,
            "payload_momentum": 0.0,
        }

    last_capture = memory["last_capture"]

    if last_capture is not None:
        delta = state.capture - last_capture

        memory["payload_momentum"] = (
            memory["payload_momentum"] * 0.9
            + delta
        )

    memory["last_capture"] = state.capture

    losing_payload = (
        memory["payload_momentum"] < -0.0001
    )

    # ---------------------------------------------------------
    # HELPERS
    # ---------------------------------------------------------

    def nearest(position, bots):
        return min(
            bots,
            key=lambda bot: position.dist_sq(bot.pos),
            default=None,
        )

    def move_to(bot, position):
        if position is None:
            return None

        return move_bot(
            navigate_to(
                bot.pos,
                position,
            )
        )

    def can_fire(bot, target):
        if target is None:
            return False

        return (
            bot.pos.dist(target.pos)
            <= conf.bot.blaster_range
            and line_of_sight(
                bot.pos,
                target.pos,
            )
        )

    def battle_action(bot, target):
        return SpecialAction.Battle(
            fire=can_fire(bot, target)
        )

    # ---------------------------------------------------------
    # ENEMY GROUPS
    # ---------------------------------------------------------

    enemy_battles = [
        e for e in enemies
        if e.class_ == BotClass.Battle
    ]

    enemy_healers = [
        e for e in enemies
        if e.class_ == BotClass.Healer
    ]

    enemy_extractors = [
        e for e in enemies
        if e.class_ == BotClass.Extractor
    ]

    # ---------------------------------------------------------
    # OUR EXTRACTOR FORMATION
    # ---------------------------------------------------------

    extractor_slots = (
        (0.0, 0.90),
        (0.70, 0.57),
        (0.88, -0.20),
        (0.39, -0.81),
        (-0.39, -0.81),
        (-0.88, -0.20),
        (-0.70, 0.57),
    )

    for slot, bot in enumerate(
        sorted(extractors, key=lambda b: b.id)
    ):

        dx, dy = extractor_slots[
            slot % len(extractor_slots)
        ]

        mining_position = (
            state.deposit_me.pos
            + Vec2(dx, dy)
        )

        if not disc_free(
            mining_position,
            conf.bot.radius,
        ):
            mining_position = (
                state.deposit_me.pos
                + Vec2(
                    0.0,
                    conf.deposit.radius
                    + conf.bot.radius,
                )
            )

        bot_action = action.bots[bot.id]

        bot_action.move_action = move_to(
            bot,
            mining_position,
        )

        bot_action.turn_action = turn_towards(
            state.deposit_me.pos
        )

        bot_action.special_action = (
            SpecialAction.Extractor(
                mine=True
            )
        )

    # ---------------------------------------------------------
    # STRATEGIC AREAS
    # ---------------------------------------------------------

    enemies_at_base = [
        e for e in enemies
        if e.pos.dist(
            state.deposit_me.pos
        ) <= 10.0
    ]

    enemies_at_payload = [
        e for e in enemies
        if e.pos.dist(payload) <= 10.0
    ]

    enemy_at_base = nearest(
        state.deposit_me.pos,
        enemies_at_base,
    )

    enemy_at_payload = nearest(
        payload,
        enemies_at_payload,
    )

    base_threat = (
        enemy_at_base is not None
    )

    payload_contested = (
        enemy_at_payload is not None
    )

    # ---------------------------------------------------------
    # ENEMY RESOURCE AREA
    # ---------------------------------------------------------
    #
    # IMPORTANT:
    # Raiders only care about extractors near the
    # enemy deposit. They won't randomly chase an
    # extractor somewhere else on the map.
    # ---------------------------------------------------------

    enemy_resource_extractors = [
        e for e in enemy_extractors
        if e.pos.dist(
            state.deposit_other.pos
        ) <= 10.0
    ]

    enemy_resource_healers = [
        e for e in enemy_healers
        if e.pos.dist(
            state.deposit_other.pos
        ) <= 10.0
    ]

    enemy_resource_battles = [
        e for e in enemy_battles
        if e.pos.dist(
            state.deposit_other.pos
        ) <= 10.0
    ]

    # ---------------------------------------------------------
    # ROLE ASSIGNMENT
    # ---------------------------------------------------------

    # Keep some fighters at home.
    if base_threat:
        guard_count = (
            3 if len(battles) >= 10
            else 2
        )
    else:
        guard_count = (
            1 if len(battles) >= 5
            else 0
        )

    # Don't send the whole army away from the payload.
    if losing_payload or payload_contested:
        payload_count = min(
            7,
            max(
                0,
                len(battles) - guard_count,
            ),
        )
    else:
        payload_count = min(
            6,
            max(
                0,
                len(battles) - guard_count,
            ),
        )

    # Closest fighters to payload become the main squad.
    payload_squad = sorted(
        battles,
        key=lambda b: b.pos.dist_sq(payload),
    )[:payload_count]

    remaining = [
        b for b in battles
        if b not in payload_squad
    ]

    # ---------------------------------------------------------
    # HOME DEFENDERS
    # ---------------------------------------------------------

    defenders = sorted(
        remaining,
        key=lambda b:
            b.pos.dist_sq(
                state.deposit_me.pos
            ),
    )[:guard_count]

    # ---------------------------------------------------------
    # RESOURCE RAIDERS
    # ---------------------------------------------------------
    #
    # We specifically send fighters to the enemy
    # extractor zone.
    #
    # Priority:
    #       ENEMY EXTRACTOR
    #             ↓
    #       ENEMY HEALER
    #             ↓
    #       ENEMY BATTLE
    #
    # This happens unless our payload is in danger.
    # ---------------------------------------------------------

    raid_pool = [
        b for b in battles
        if b not in payload_squad
        and b not in defenders
    ]

    if (
        len(battles) >= 7
        and not losing_payload
        and not payload_contested
        and len(enemy_resource_extractors) > 0
    ):
        raid_count = min(
            2,
            len(raid_pool),
        )
    else:
        raid_count = 0

    raiders = sorted(
        raid_pool,
        key=lambda b:
            b.pos.dist_sq(
                state.deposit_other.pos
            ),
    )[:raid_count]

    # ---------------------------------------------------------
    # STAGGERED WEDGE FORMATION
    # ---------------------------------------------------------

    def formation_position(
        index,
        total,
        target,
    ):
        """
        Formation:

                    B1
                 B2    B3
              B4   B5   B6
           B7   B8   B9   B10
        """

        if total <= 0:
            return target

        direction = (
            target - payload
        ).normalize_or_zero()

        if (
            direction.x == 0
            and direction.y == 0
        ):
            direction = Vec2(
                1.0,
                0.0,
            )

        # Perpendicular vector.
        side = Vec2(
            -direction.y,
            direction.x,
        )

        # Number of fighters in each row.
        rows = [
            1,
            2,
            3,
            4,
            3,
            2,
        ]

        current = index
        row_number = 0

        for row_size in rows:

            if current < row_size:

                lateral = (
                    current
                    - (row_size - 1) / 2.0
                ) * 1.7

                depth = (
                    row_number * 1.35
                )

                candidate = (
                    target
                    - direction * depth
                    + side * lateral
                )

                if disc_free(
                    candidate,
                    conf.bot.radius,
                ):
                    return candidate

                fallback = (
                    payload
                    + side * lateral
                )

                if disc_free(
                    fallback,
                    conf.bot.radius,
                ):
                    return fallback

                return payload

            current -= row_size
            row_number += 1

        return target

    # ---------------------------------------------------------
    # TARGET SELECTION
    # ---------------------------------------------------------

    def choose_combat_target(
        bot,
        role,
    ):
        """
        Different roles have different priorities.
        """

        # =====================================================
        # DEFENDER
        # =====================================================

        if role == "defender":

            close_enemies = [
                e for e in enemies
                if e.pos.dist(
                    state.deposit_me.pos
                ) <= 9.0
            ]

            if close_enemies:

                close_healers = [
                    e for e in close_enemies
                    if e.class_ == BotClass.Healer
                ]

                if close_healers:
                    return nearest(
                        bot.pos,
                        close_healers,
                    )

                return nearest(
                    bot.pos,
                    close_enemies,
                )

            # If nobody is attacking home,
            # don't run across the map.
            return None

        # =====================================================
        # RESOURCE RAIDER
        # =====================================================

        if role == "raider":

            # FIRST PRIORITY:
            # Destroy enemy extractors.
            local_extractors = [
                e for e in enemy_extractors
                if e.pos.dist(
                    state.deposit_other.pos
                ) <= 10.0
            ]

            if local_extractors:
                return nearest(
                    bot.pos,
                    local_extractors,
                )

            # SECOND PRIORITY:
            # Kill healer protecting resource area.
            local_healers = [
                e for e in enemy_healers
                if e.pos.dist(
                    state.deposit_other.pos
                ) <= 10.0
            ]

            if local_healers:
                return nearest(
                    bot.pos,
                    local_healers,
                )

            # THIRD PRIORITY:
            # Fight battles at their base.
            local_battles = [
                e for e in enemy_battles
                if e.pos.dist(
                    state.deposit_other.pos
                ) <= 10.0
            ]

            if local_battles:
                return nearest(
                    bot.pos,
                    local_battles,
                )

            return None

        # =====================================================
        # PAYLOAD FIGHTER
        # =====================================================

        nearby = [
            e for e in enemies
            if bot.pos.dist(e.pos) <= 11.0
        ]

        # FIRST:
        # Enemy healer.
        nearby_healers = [
            e for e in nearby
            if e.class_ == BotClass.Healer
        ]

        if nearby_healers:
            return nearest(
                bot.pos,
                nearby_healers,
            )

        # SECOND:
        # Enemy battle.
        nearby_battles = [
            e for e in nearby
            if e.class_ == BotClass.Battle
        ]

        if nearby_battles:
            return nearest(
                bot.pos,
                nearby_battles,
            )

        # THIRD:
        # Extractor.
        nearby_extractors = [
            e for e in nearby
            if e.class_ == BotClass.Extractor
        ]

        if nearby_extractors:
            return nearest(
                bot.pos,
                nearby_extractors,
            )

        # Only chase an enemy around the payload.
        if enemies_at_payload:
            return nearest(
                bot.pos,
                enemies_at_payload,
            )

        return None

    # ---------------------------------------------------------
    # PAYLOAD BATTLE FORMATION
    # ---------------------------------------------------------

    ordered_payload = sorted(
        payload_squad,
        key=lambda b: b.id,
    )

    for index, bot in enumerate(
        ordered_payload
    ):

        bot_action = action.bots[bot.id]

        target = choose_combat_target(
            bot,
            "payload",
        )

        # Every fighter gets its own position.
        destination = formation_position(
            index,
            len(ordered_payload),
            payload,
        )

        # Keep the wedge even while shooting.
        bot_action.move_action = move_to(
            bot,
            destination,
        )

        if target is not None:
            bot_action.turn_action = (
                turn_towards(target.pos)
            )

            bot_action.special_action = (
                battle_action(
                    bot,
                    target,
                )
            )
        else:
            bot_action.turn_action = (
                turn_towards(payload)
            )

    # ---------------------------------------------------------
    # HOME DEFENDERS
    # ---------------------------------------------------------

    for bot in defenders:

        bot_action = action.bots[bot.id]

        target = choose_combat_target(
            bot,
            "defender",
        )

        if target is not None:

            if bot.pos.dist(
                target.pos
            ) <= 8.0:

                destination = target.pos

            else:

                destination = (
                    state.deposit_me.pos
                )

            bot_action.move_action = (
                move_to(
                    bot,
                    destination,
                )
            )

            bot_action.turn_action = (
                turn_towards(target.pos)
            )

            bot_action.special_action = (
                battle_action(
                    bot,
                    target,
                )
            )

        else:

            bot_action.move_action = (
                move_to(
                    bot,
                    state.deposit_me.pos,
                )
            )

    # ---------------------------------------------------------
    # RESOURCE RAID
    # ---------------------------------------------------------

    for index, bot in enumerate(
        raiders
    ):

        bot_action = action.bots[bot.id]

        target = choose_combat_target(
            bot,
            "raider",
        )

        if target is not None:

            if (
                target.class_
                == BotClass.Extractor
            ):
                # Go EXACTLY to the enemy extractor.
                destination = target.pos

            else:
                # Once extractor is dead,
                # stay around their resource area.
                destination = (
                    state.deposit_other.pos
                )

            bot_action.move_action = (
                move_to(
                    bot,
                    destination,
                )
            )

            bot_action.turn_action = (
                turn_towards(
                    target.pos
                )
            )

            bot_action.special_action = (
                battle_action(
                    bot,
                    target,
                )
            )

        else:

            # Approach enemy resource area
            # from different directions.
            raid_offsets = (
                Vec2(0.0, 2.0),
                Vec2(0.0, -2.0),
            )

            destination = (
                state.deposit_other.pos
                + raid_offsets[
                    index
                    % len(raid_offsets)
                ]
            )

            if not disc_free(
                destination,
                conf.bot.radius,
            ):
                destination = (
                    state.deposit_other.pos
                )

            bot_action.move_action = (
                move_to(
                    bot,
                    destination,
                )
            )

    # ---------------------------------------------------------
    # RESERVE BATTLE BOTS
    # ---------------------------------------------------------

    assigned = set(
        payload_squad
        + defenders
        + raiders
    )

    reserves = [
        b for b in battles
        if b not in assigned
    ]

    for index, bot in enumerate(
        sorted(
            reserves,
            key=lambda b: b.id,
        )
    ):

        bot_action = action.bots[bot.id]

        target = choose_combat_target(
            bot,
            "payload",
        )

        if target is not None:

            bot_action.move_action = (
                move_to(
                    bot,
                    target.pos,
                )
            )

            bot_action.turn_action = (
                turn_towards(
                    target.pos
                )
            )

            bot_action.special_action = (
                battle_action(
                    bot,
                    target,
                )
            )

        else:

            destination = (
                formation_position(
                    index,
                    max(
                        1,
                        len(reserves),
                    ),
                    payload,
                )
            )

            bot_action.move_action = (
                move_to(
                    bot,
                    destination,
                )
            )

    # ---------------------------------------------------------
    # LOW-HEALTH RETREAT
    # ---------------------------------------------------------

    for bot in battles:

        # Raiders should continue their mission.
        if bot in raiders:
            continue

        if bot.health > 3.0:
            continue

        close_enemy = nearest(
            bot.pos,
            enemies,
        )

        if (
            close_enemy is not None
            and bot.pos.dist(
                close_enemy.pos
            ) <= 5.0
            and bot not in defenders
        ):

            away = (
                bot.pos
                - close_enemy.pos
            ).normalize_or_zero()

            retreat_position = (
                bot.pos
                + away * 2.5
            )

            if disc_free(
                retreat_position,
                conf.bot.radius,
            ):

                bot_action = (
                    action.bots[bot.id]
                )

                bot_action.move_action = (
                    move_to(
                        bot,
                        retreat_position,
                    )
                )

                bot_action.turn_action = (
                    turn_towards(
                        close_enemy.pos
                    )
                )

                bot_action.special_action = (
                    battle_action(
                        bot,
                        close_enemy,
                    )
                )

    # ---------------------------------------------------------
    # HEALERS
    # ---------------------------------------------------------

    ordered_healers = sorted(
        healers,
        key=lambda h: h.id,
    )

    # Healers stay behind the army.
    healer_offsets = (
        Vec2(-2.8, 1.8),
        Vec2(-2.8, -1.8),
        Vec2(-4.0, 0.0),
        Vec2(-4.5, 2.5),
        Vec2(-4.5, -2.5),
    )

    frontline = (
        ordered_payload
        if ordered_payload
        else battles
    )

    for index, healer in enumerate(
        ordered_healers
    ):

        healer_action = (
            action.bots[healer.id]
        )

        # First healer supports resource raid.
        if (
            raiders
            and index == 0
        ):

            assigned = raiders

            healer_anchor = (
                state.deposit_other.pos
                + Vec2(
                    0.0,
                    -2.5,
                )
            )

        else:

            assigned = [
                bot
                for j, bot in enumerate(
                    frontline
                )
                if j
                % max(
                    1,
                    len(ordered_healers),
                ) == index
            ]

            healer_anchor = (
                payload
                + healer_offsets[
                    index
                    % len(healer_offsets)
                ]
            )

        # Find wounded unit.
        wounded = [
            bot
            for bot in assigned
            if bot.health
            < conf.bot.health
        ]

        patient = min(
            wounded,
            key=lambda b: (
                b.health,
                healer.pos.dist_sq(
                    b.pos
                ),
            ),
            default=None,
        )

        if patient is not None:

            healer_action.move_action = (
                move_to(
                    healer,
                    patient.pos,
                )
            )

            healer_action.turn_action = (
                turn_towards(
                    patient.pos
                )
            )

            can_heal = (
                healer.pos.dist(
                    patient.pos
                )
                <= conf.bot.base_heal_range
                and line_of_sight(
                    healer.pos,
                    patient.pos,
                )
            )

            healer_action.special_action = (
                SpecialAction.Healer(
                    fire=can_heal,
                    target=patient.id,
                )
            )

        else:

            if not disc_free(
                healer_anchor,
                conf.bot.radius,
            ):
                healer_anchor = payload

            healer_action.move_action = (
                move_to(
                    healer,
                    healer_anchor,
                )
            )

    # ---------------------------------------------------------
    # PRODUCTION
    # ---------------------------------------------------------

    wounded_count = sum(
        bot.health < conf.bot.health
        for bot in battles
    )

    damage_ratio = (
        wounded_count
        / max(
            1,
            len(battles),
        )
    )

    # Don't over-invest in economy.
    desired_extractors = 6

    # Normal healer count.
    desired_healers = 3

    # Increase healers when army is large and damaged.
    if (
        len(battles) >= 12
        and damage_ratio >= 0.25
    ):

        desired_healers = 5

    elif (
        len(battles) >= 9
        and damage_ratio >= 0.18
    ):

        desired_healers = 4

    # ---------------------------------------------------------
    # PRODUCTION PRIORITY
    # ---------------------------------------------------------
    #
    # Early:
    #
    # 3-4 Extractors
    #       ↓
    # 8 Battles
    #       ↓
    # 3 Healers
    #       ↓
    # Mostly Battles
    #
    # Later:
    # Top up extractors to 6.
    # ---------------------------------------------------------

    if len(extractors) == 0:

        next_bot = BotClass.Extractor

    elif (
        len(extractors) < 4
        and len(battles) < 6
    ):

        next_bot = BotClass.Extractor

    elif len(battles) < 8:

        next_bot = BotClass.Battle

    elif len(healers) < desired_healers:

        next_bot = BotClass.Healer

    elif (
        len(extractors) < desired_extractors
        and len(battles) >= 10
    ):

        next_bot = BotClass.Extractor

    else:

        next_bot = BotClass.Battle

    action.fabricator_next = int(
        next_bot
    )

    # ---------------------------------------------------------
    # RUSH PRODUCTION
    # ---------------------------------------------------------

    action.rush_order = (
        state.tick
        < conf.max_ticks
        - conf.endgame_ticks
        and
        state.fabricator_me.tokens
        >= conf.fabricator.rush_cost
        and
        len(friends)
        < BOTS_MAX
    )

    return action