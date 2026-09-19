from . import *


def get_strategy(team: int) -> Strategy:
    """
    Main entry point.

    Team B sees a mirrored world, so the same strategy works for
    both teams.
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
        bot for bot in friends
        if bot.class_ == BotClass.Battle
    ]

    healers = [
        bot for bot in friends
        if bot.class_ == BotClass.Healer
    ]

    extractors = [
        bot for bot in friends
        if bot.class_ == BotClass.Extractor
    ]

    enemy_battles = [
        bot for bot in enemies
        if bot.class_ == BotClass.Battle
    ]

    enemy_healers = [
        bot for bot in enemies
        if bot.class_ == BotClass.Healer
    ]

    enemy_extractors = [
        bot for bot in enemies
        if bot.class_ == BotClass.Extractor
    ]

    # =========================================================
    # MEMORY
    # =========================================================

    if memory is None:
        memory = {
            "last_capture": None,
            "payload_momentum": 0.0,
        }

    last_capture = memory["last_capture"]

    if last_capture is not None:
        delta = state.capture - last_capture

        memory["payload_momentum"] = (
            memory["payload_momentum"] * 0.90
            + delta
        )

    memory["last_capture"] = state.capture

    payload_momentum = memory["payload_momentum"]

    payload_losing = payload_momentum < -0.0001

    # =========================================================
    # BASIC HELPERS
    # =========================================================

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

    # =========================================================
    # STRATEGIC AREAS
    # =========================================================

    base_enemies = [
        enemy
        for enemy in enemies
        if enemy.pos.dist(
            state.deposit_me.pos
        ) <= 10.0
    ]

    payload_enemies = [
        enemy
        for enemy in enemies
        if enemy.pos.dist(payload) <= 10.0
    ]

    enemy_base_enemies = [
        enemy
        for enemy in enemies
        if enemy.pos.dist(
            state.deposit_other.pos
        ) <= 10.0
    ]

    enemy_base_extractors = [
        enemy
        for enemy in enemy_extractors
        if enemy.pos.dist(
            state.deposit_other.pos
        ) <= 10.0
    ]

    enemy_base_healers = [
        enemy
        for enemy in enemy_healers
        if enemy.pos.dist(
            state.deposit_other.pos
        ) <= 10.0
    ]

    enemy_base_battles = [
        enemy
        for enemy in enemy_battles
        if enemy.pos.dist(
            state.deposit_other.pos
        ) <= 10.0
    ]

    enemy_at_base = nearest(
        state.deposit_me.pos,
        base_enemies,
    )

    enemy_at_payload = nearest(
        payload,
        payload_enemies,
    )

    base_threat = (
        enemy_at_base is not None
    )

    payload_contested = (
        enemy_at_payload is not None
    )

    # =========================================================
    # ADAPTIVE ARMY STATE
    # =========================================================

    wounded_battles = [
        bot
        for bot in battles
        if bot.health < conf.bot.health
    ]

    damage_ratio = (
        len(wounded_battles)
        / max(1, len(battles))
    )

    enemy_combat_pressure = (
        len(enemy_battles)
        + 0.75 * len(enemy_healers)
    )

    our_combat_power = (
        len(battles)
        + 0.75 * len(healers)
    )

    combat_deficit = (
        enemy_combat_pressure
        > our_combat_power + 2
    )

    # =========================================================
    # ADAPTIVE EXTRACTOR TARGET
    # =========================================================
    #
    # Economy grows as the army becomes established.
    #
    # Early:
    #       3-4
    #
    # Mid:
    #       5-6
    #
    # Strong army:
    #       7
    #
    # Very large army / late game:
    #       8
    #
    # BUT:
    # If we are under heavy attack, combat takes priority.
    # =========================================================

    if state.tick < 1000:
        desired_extractors = 3

    elif state.tick < 2200:
        desired_extractors = 4

    elif len(battles) >= 8:
        desired_extractors = 6

    else:
        desired_extractors = 5

    if len(battles) >= 13:
        desired_extractors = max(
            desired_extractors,
            7,
        )

    if len(battles) >= 18:
        desired_extractors = max(
            desired_extractors,
            8,
        )

    # Do not spend production on economy while the army
    # is significantly behind.
    if combat_deficit and len(extractors) >= 3:
        desired_extractors = min(
            desired_extractors,
            len(extractors),
        )

    # If we have almost no economy, rebuild it immediately.
    if len(extractors) <= 1:
        desired_extractors = max(
            desired_extractors,
            3,
        )

    # =========================================================
    # ADAPTIVE HEALER TARGET
    # =========================================================

    if len(battles) < 6:
        desired_healers = 1

    elif len(battles) < 10:
        desired_healers = 2

    else:
        desired_healers = 3

    if len(battles) >= 13:
        desired_healers = 4

    if len(battles) >= 18:
        desired_healers = 5

    if damage_ratio >= 0.25:
        desired_healers = min(
            desired_healers + 1,
            5,
        )

    # =========================================================
    # EXTRACTOR POSITIONS
    # =========================================================

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
        sorted(
            extractors,
            key=lambda unit: unit.id,
        )
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

    # =========================================================
    # FORMATION
    # =========================================================
    #
    # The army approaches like:
    #
    #             B1
    #          B2    B3
    #       B4    B5    B6
    #    B7    B8    B9    B10
    #
    # This prevents one giant cluster.
    # =========================================================

    def get_formation_position(
        target,
        index,
        total,
    ):

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

        side = Vec2(
            -direction.y,
            direction.x,
        )

        # Staggered rows.
        rows = (
            1,
            2,
            3,
            4,
            3,
            2,
        )

        current_index = index
        row = 0

        for row_size in rows:

            if current_index < row_size:

                lateral_offset = (
                    current_index
                    - (row_size - 1) / 2.0
                ) * 1.8

                depth_offset = (
                    row * 1.4
                )

                candidate = (
                    target
                    - direction * depth_offset
                    + side * lateral_offset
                )

                if disc_free(
                    candidate,
                    conf.bot.radius,
                ):
                    return candidate

                return target

            current_index -= row_size
            row += 1

        return target

    # =========================================================
    # ROLE ALLOCATION
    # =========================================================

    # Minimum home defense.
    if base_threat:
        guard_count = 3 if len(battles) >= 10 else 2
    else:
        guard_count = 1

    # If payload is in danger, increase payload force.
    if payload_losing or payload_contested:
        payload_count = min(
            8,
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

    # =========================================================
    # PAYLOAD SQUAD
    # =========================================================

    payload_squad = sorted(
        battles,
        key=lambda bot:
            bot.pos.dist_sq(payload),
    )[:payload_count]

    available = [
        bot
        for bot in battles
        if bot not in payload_squad
    ]

    # =========================================================
    # HOME DEFENDERS
    # =========================================================

    defenders = sorted(
        available,
        key=lambda bot:
            bot.pos.dist_sq(
                state.deposit_me.pos
            ),
    )[:guard_count]

    # =========================================================
    # RESOURCE RAID
    # =========================================================
    #
    # Only send raiders when:
    #
    # 1. We have enough fighters
    # 2. Payload isn't losing
    # 3. Enemy has extractors
    #
    # Two fighters maximum.
    # =========================================================

    remaining_for_raid = [
        bot
        for bot in battles
        if bot not in payload_squad
        and bot not in defenders
    ]

    raid_allowed = (
        len(battles) >= 8
        and len(enemy_base_extractors) > 0
        and not payload_losing
        and not (
            payload_contested
            and len(battles) < 12
        )
    )

    if raid_allowed:
        raid_count = min(
            2,
            len(remaining_for_raid),
        )
    else:
        raid_count = 0

    raiders = sorted(
        remaining_for_raid,
        key=lambda bot:
            bot.pos.dist_sq(
                state.deposit_other.pos
            ),
    )[:raid_count]

    # =========================================================
    # TARGET SELECTION
    # =========================================================

    def choose_payload_target(bot):

        nearby = [
            enemy
            for enemy in enemies
            if bot.pos.dist(enemy.pos) <= 10.0
        ]

        # 1. HEALER FIRST
        nearby_healers = [
            enemy
            for enemy in nearby
            if enemy.class_ == BotClass.Healer
        ]

        if nearby_healers:
            return nearest(
                bot.pos,
                nearby_healers,
            )

        # 2. BATTLE
        nearby_battles = [
            enemy
            for enemy in nearby
            if enemy.class_ == BotClass.Battle
        ]

        if nearby_battles:
            return nearest(
                bot.pos,
                nearby_battles,
            )

        # 3. EXTRACTOR
        nearby_extractors = [
            enemy
            for enemy in nearby
            if enemy.class_ == BotClass.Extractor
        ]

        if nearby_extractors:
            return nearest(
                bot.pos,
                nearby_extractors,
            )

        # Don't chase distant enemies.
        return None

    def choose_defender_target(bot):

        close = [
            enemy
            for enemy in enemies
            if enemy.pos.dist(
                state.deposit_me.pos
            ) <= 10.0
        ]

        if not close:
            return None

        # Healer first if one is attacking our base.
        close_healers = [
            enemy
            for enemy in close
            if enemy.class_ == BotClass.Healer
        ]

        if close_healers:
            return nearest(
                bot.pos,
                close_healers,
            )

        return nearest(
            bot.pos,
            close,
        )

    def choose_raider_target(bot):

        # =====================================================
        # ABSOLUTE PRIORITY:
        # ENEMY RESOURCE EXTRACTOR
        # =====================================================

        resource_extractors = [
            enemy
            for enemy in enemy_extractors
            if enemy.pos.dist(
                state.deposit_other.pos
            ) <= 10.0
        ]

        if resource_extractors:
            return nearest(
                bot.pos,
                resource_extractors,
            )

        # =====================================================
        # SECOND:
        # ENEMY HEALER
        # =====================================================

        resource_healers = [
            enemy
            for enemy in enemy_healers
            if enemy.pos.dist(
                state.deposit_other.pos
            ) <= 10.0
        ]

        if resource_healers:
            return nearest(
                bot.pos,
                resource_healers,
            )

        # =====================================================
        # THIRD:
        # ENEMY BATTLE
        # =====================================================

        resource_battles = [
            enemy
            for enemy in enemy_battles
            if enemy.pos.dist(
                state.deposit_other.pos
            ) <= 10.0
        ]

        if resource_battles:
            return nearest(
                bot.pos,
                resource_battles,
            )

        return None

    # =========================================================
    # BATTLE BOT ACTIONS
    # =========================================================

    payload_bots = sorted(
        payload_squad,
        key=lambda bot: bot.id,
    )

    for index, bot in enumerate(
        payload_bots
    ):

        bot_action = action.bots[bot.id]

        target = choose_payload_target(
            bot
        )

        formation_position = (
            get_formation_position(
                payload,
                index,
                len(payload_bots),
            )
        )

        # -----------------------------------------------------
        # LOW HEALTH
        # -----------------------------------------------------

        close_enemy = nearest(
            bot.pos,
            enemies,
        )

        should_retreat = (
            bot.health <= 3.0
            and close_enemy is not None
            and bot.pos.dist(
                close_enemy.pos
            ) <= 6.0
        )

        if should_retreat:

            away = (
                bot.pos
                - close_enemy.pos
            ).normalize_or_zero()

            retreat = (
                bot.pos
                + away * 2.5
            )

            if disc_free(
                retreat,
                conf.bot.radius,
            ):
                bot_action.move_action = (
                    move_to(
                        bot,
                        retreat,
                    )
                )

            if target is not None:
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

            continue

        # -----------------------------------------------------
        # NORMAL FORMATION
        # -----------------------------------------------------

        bot_action.move_action = (
            move_to(
                bot,
                formation_position,
            )
        )

        if target is not None:
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

        else:
            bot_action.turn_action = (
                turn_towards(payload)
            )

    # =========================================================
    # HOME DEFENDERS
    # =========================================================

    for bot in defenders:

        bot_action = action.bots[bot.id]

        target = choose_defender_target(
            bot
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

            bot_action.move_action = (
                move_to(
                    bot,
                    state.deposit_me.pos,
                )
            )

            bot_action.turn_action = (
                turn_towards(
                    state.deposit_me.pos
                )
            )

    # =========================================================
    # RESOURCE RAIDERS
    # =========================================================

    for index, bot in enumerate(
        raiders
    ):

        bot_action = action.bots[bot.id]

        target = choose_raider_target(
            bot
        )

        if target is not None:

            # Go directly toward extractor.
            if (
                target.class_
                == BotClass.Extractor
            ):

                destination = target.pos

            else:

                # Stay around enemy economy.
                offsets = (
                    Vec2(0.0, 2.5),
                    Vec2(0.0, -2.5),
                )

                destination = (
                    state.deposit_other.pos
                    + offsets[
                        index % 2
                    ]
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

            # Move toward enemy economy.
            offsets = (
                Vec2(0.0, 2.5),
                Vec2(0.0, -2.5),
            )

            destination = (
                state.deposit_other.pos
                + offsets[index % 2]
            )

            bot_action.move_action = (
                move_to(
                    bot,
                    destination,
                )
            )

            bot_action.turn_action = (
                turn_towards(
                    state.deposit_other.pos
                )
            )

    # =========================================================
    # RESERVE / EXTRA BATTLES
    # =========================================================

    assigned = (
        payload_squad
        + defenders
        + raiders
    )

    reserves = [
        bot
        for bot in battles
        if bot not in assigned
    ]

    for index, bot in enumerate(
        sorted(
            reserves,
            key=lambda unit: unit.id,
        )
    ):

        bot_action = action.bots[bot.id]

        target = choose_payload_target(
            bot
        )

        destination = (
            get_formation_position(
                payload,
                index,
                max(1, len(reserves)),
            )
        )

        bot_action.move_action = (
            move_to(
                bot,
                destination,
            )
        )

        if target is not None:
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

    # =========================================================
    # HEALERS
    # =========================================================

    ordered_healers = sorted(
        healers,
        key=lambda bot: bot.id,
    )

    # Keep healers behind the battle line.
    healer_offsets = (
        Vec2(-3.0, 2.0),
        Vec2(-3.0, -2.0),
        Vec2(-4.0, 0.0),
        Vec2(-5.0, 2.5),
        Vec2(-5.0, -2.5),
    )

    frontline = payload_bots

    for index, healer in enumerate(
        ordered_healers
    ):

        healer_action = (
            action.bots[healer.id]
        )

        # First healer can support the raid,
        # but ONLY when the raid is safe to perform.
        if (
            index == 0
            and len(raiders) > 0
        ):

            assigned_battles = raiders

            healer_anchor = (
                state.deposit_other.pos
                + Vec2(
                    -2.5,
                    0.0,
                )
            )

        else:

            assigned_battles = [
                bot
                for j, bot in enumerate(
                    frontline
                )
                if (
                    j % max(
                        1,
                        len(ordered_healers),
                    )
                    == index
                )
            ]

            healer_anchor = (
                payload
                + healer_offsets[
                    index
                    % len(healer_offsets)
                ]
            )

        wounded = [
            bot
            for bot in assigned_battles
            if bot.health < conf.bot.health
        ]

        patient = min(
            wounded,
            key=lambda bot: (
                bot.health,
                healer.pos.dist_sq(
                    bot.pos
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

            healer_action.turn_action = (
                turn_towards(payload)
            )

    # =========================================================
    # ADAPTIVE PRODUCTION
    # =========================================================

    #
    # Priority:
    #
    # 1. Emergency economy
    # 2. Combat deficit
    # 3. Battle core
    # 4. Healers
    # 5. Economy expansion
    # 6. Battles
    #

    if len(extractors) == 0:

        next_bot = BotClass.Extractor

    elif (
        len(extractors) < 2
        and len(battles) < 5
    ):

        next_bot = BotClass.Extractor

    elif combat_deficit:

        # Enemy has a significant combat advantage.
        next_bot = BotClass.Battle

    elif len(battles) < 8:

        # Build the initial combat force.
        next_bot = BotClass.Battle

    elif len(healers) < desired_healers:

        # Keep the frontline alive.
        next_bot = BotClass.Healer

    elif len(extractors) < desired_extractors:

        # Economy expands after combat core exists.
        next_bot = BotClass.Extractor

    else:

        # Default late-game production.
        next_bot = BotClass.Battle

    action.fabricator_next = int(
        next_bot
    )

    # =========================================================
    # ADAPTIVE RUSH
    # =========================================================

    tokens = state.fabricator_me.tokens

    can_rush = (
        state.tick
        < conf.max_ticks
        - conf.endgame_ticks
        and tokens
        >= conf.fabricator.rush_cost
        and len(friends)
        < BOTS_MAX
    )

    action.rush_order = can_rush

    return action