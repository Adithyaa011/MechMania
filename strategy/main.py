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


def get_strategy(team: int) -> Strategy:
    """
    Both teams use the same strategy.

    The engine mirrors the world for the top-right team,
    so the strategy can always think of itself as being
    on the bottom-left side.
    """

    if team == 0:
        print("Hello! I am team A (bottom left)")
    else:
        print("Hello! I am team B (top right)")

    return improved_strategy


def get_closest_enemy(bot, state):
    """
    Return the BotState of the enemy closest to this bot.

    Returns None if there are no enemies.
    """

    closest_enemy = None
    closest_distance = float("inf")

    for enemy in state.fleet_other:

        distance = bot.pos.dist_sq(enemy.pos)

        if distance < closest_distance:
            closest_distance = distance
            closest_enemy = enemy

    return closest_enemy


def get_enemy_near_position(position, state):
    """
    Find the enemy closest to a strategic position.

    This is used for:
        - our deposit
        - the payload
    """

    closest_enemy = None
    closest_distance = float("inf")

    for enemy in state.fleet_other:

        distance = position.dist_sq(enemy.pos)

        if distance < closest_distance:
            closest_distance = distance
            closest_enemy = enemy

    return closest_enemy


def choose_target(bot, state):
    """
    Choose an enemy based on tactical priority.

    Priority:
        1. Enemy threatening our deposit
        2. Enemy threatening the payload
        3. Enemy closest to our bot
    """

    deposit_position = state.deposit_me.pos
    payload_position = state.payload_pos()

    # ---------------------------------------------------------
    # Priority 1: Protect our deposit
    # ---------------------------------------------------------

    deposit_enemy = get_enemy_near_position(
        deposit_position,
        state
    )

    if deposit_enemy is not None:

        deposit_distance = deposit_position.dist(
            deposit_enemy.pos
        )

        # Enemy is close enough to threaten the extractor.
        if deposit_distance <= 10.0:
            return deposit_enemy

    # ---------------------------------------------------------
    # Priority 2: Contest the payload
    # ---------------------------------------------------------

    payload_enemy = get_enemy_near_position(
        payload_position,
        state
    )

    if payload_enemy is not None:

        payload_distance = payload_position.dist(
            payload_enemy.pos
        )

        if payload_distance <= 10.0:
            return payload_enemy

    # ---------------------------------------------------------
    # Priority 3: Attack closest enemy
    # ---------------------------------------------------------

    return get_closest_enemy(
        bot,
        state
    )


def move_towards(bot, target):
    """
    Navigate the bot toward a target while accounting for walls.
    """

    if target is None:
        return None

    return move_bot(
        navigate_to(
            bot.pos,
            target
        )
    )


def get_battle_action(bot, enemy, conf):
    """
    Decide whether the battle bot should fire.

    The bot fires only when:
        - enemy is within range
        - line of sight is clear
    """

    if enemy is None:
        return SpecialAction.Battle(
            fire=False
        )

    distance = bot.pos.dist(
        enemy.pos
    )

    can_fire = (
        distance <= conf.bot.blaster_range
        and line_of_sight(
            bot.pos,
            enemy.pos
        )
    )

    return SpecialAction.Battle(
        fire=can_fire
    )


def improved_strategy(state: GameState) -> FleetAction:

    # ---------------------------------------------------------
    # Get fixed match configuration
    # ---------------------------------------------------------

    conf = get_config()

    # Create an empty action for the fleet.
    action = FleetAction.new()

    # Current payload position.
    payload = state.payload_pos()

    # ---------------------------------------------------------
    # Decide what the fabricator should build
    # ---------------------------------------------------------

    # Default:
    # Build Battle bots.
    next_bot = BotClass.Battle

    # Bot ID 0 is our extractor.
    #
    # If it is missing, rebuild an Extractor before anything
    # else.
    if not state.fleet_me.get(0):
        next_bot = BotClass.Extractor

    # ---------------------------------------------------------
    # Calculate extractor mining position
    # ---------------------------------------------------------

    mining_spot = (
        state.deposit_me.pos
        + Vec2(
            0.0,
            conf.deposit.radius + conf.bot.radius
        )
    )

    # Only one bot should initially be assigned to the payload.
    payload_bot_assigned = False

    # ---------------------------------------------------------
    # Process every living bot
    # ---------------------------------------------------------

    for bot in state.fleet_me:

        bot_action = action.bots[bot.id]

        # =====================================================
        # ROLE 1: EXTRACTOR
        # =====================================================

        if bot.class_ == BotClass.Extractor:

            # Move toward our deposit.
            bot_action.move_action = move_bot(
                navigate_to(
                    bot.pos,
                    mining_spot
                )
            )

            # Face the deposit.
            bot_action.turn_action = turn_towards(
                state.deposit_me.pos
            )

            # Mine.
            bot_action.special_action = (
                SpecialAction.Extractor(
                    mine=True
                )
            )

            continue

        # =====================================================
        # ROLE 2: PAYLOAD RUNNER
        # =====================================================

        if not payload_bot_assigned:

            enemy = choose_target(
                bot,
                state
            )

            # If an enemy is immediately nearby,
            # fight before blindly walking toward payload.
            if enemy is not None:

                enemy_distance = bot.pos.dist(
                    enemy.pos
                )

                if (
                    enemy_distance
                    <= conf.bot.blaster_range * 1.5
                ):

                    bot_action.move_action = (
                        move_towards(
                            bot,
                            enemy.pos
                        )
                    )

                    bot_action.turn_action = (
                        turn_towards(
                            enemy.pos
                        )
                    )

                    bot_action.special_action = (
                        get_battle_action(
                            bot,
                            enemy,
                            conf
                        )
                    )

                    payload_bot_assigned = True

                    continue

            # No immediate threat:
            # move toward the payload.
            bot_action.move_action = (
                move_towards(
                    bot,
                    payload
                )
            )

            payload_bot_assigned = True

            continue

        # =====================================================
        # ROLE 3: COMBAT BOT
        # =====================================================

        enemy = choose_target(
            bot,
            state
        )

        # -----------------------------------------------------
        # No enemy exists
        # -----------------------------------------------------

        if enemy is None:

            # Instead of doing nothing,
            # help with the payload.
            bot_action.move_action = (
                move_towards(
                    bot,
                    payload
                )
            )

            continue

        # -----------------------------------------------------
        # Move toward target
        # -----------------------------------------------------

        bot_action.move_action = (
            move_towards(
                bot,
                enemy.pos
            )
        )

        # -----------------------------------------------------
        # Aim toward target
        # -----------------------------------------------------

        bot_action.turn_action = (
            turn_towards(
                enemy.pos
            )
        )

        # -----------------------------------------------------
        # Fire if possible
        # -----------------------------------------------------

        bot_action.special_action = (
            get_battle_action(
                bot,
                enemy,
                conf
            )
        )

    # ---------------------------------------------------------
    # Fabricator
    # ---------------------------------------------------------

    action.fabricator_next = int(
        next_bot
    )

    # ---------------------------------------------------------
    # Rush order
    # ---------------------------------------------------------

    in_endgame = (
        state.tick
        >= conf.max_ticks - conf.endgame_ticks
    )

    action.rush_order = (
        not in_endgame
        and
        state.fabricator_me.tokens
        >= conf.fabricator.rush_cost
    )

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