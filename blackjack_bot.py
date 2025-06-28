"""
Blackjack Discord Bot  –  Nextcord 2.6+  &  Python 3.10+
=======================================================

• Autosaves player chip balances to JSON (debounced).
• Unique component IDs per player, so every player gets their own Hit/Stand buttons.
• Handles empty deck + modal signature changes in Nextcord v2.
• All original slash commands preserved.

Place 52 PNG card images (named like “ace_of_spades.png”, “10_of_hearts.png”, …)
in a folder called `cards/` alongside this file.

Add DISCORD_TOKEN to a .env file or your shell environment before running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from io import BytesIO
from typing import Dict, List, Tuple

import nextcord
from dotenv import load_dotenv
from nextcord import AllowedMentions, Embed, Interaction
from nextcord.ext import commands
from PIL import Image

# ---------------------------------------------------------------------------#
#  Environment & logging                                                     #
# ---------------------------------------------------------------------------#
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing – add it to .env or environment")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("blackjack")

# ---------------------------------------------------------------------------#
#  Constants                                                                 #
# ---------------------------------------------------------------------------#
DEFAULT_BALANCE = 1_000
DATA_FILE = "balances.json"

COLOR_PRIMARY = 0x5865F2  # blurple
COLOR_SUCCESS = 0x2ECC71
COLOR_INFO = 0x3498DB
COLOR_HELP = 0x00FF00
COLOR_LEADER = 0xF1C40F

CARD_FOLDER = "cards"
CARD_WIDTH = 70
CARD_SPACING = 10

SUIT_MAP = {"♠": "spades", "♥": "hearts", "♦": "diamonds", "♣": "clubs"}
RANK_MAP = {
    "A": "ace",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
    "6": "6",
    "7": "7",
    "8": "8",
    "9": "9",
    "10": "10",
    "J": "jack",
    "Q": "queen",
    "K": "king",
}

Card = Tuple[str, str]  # (rank, suit)


# ---------------------------------------------------------------------------#
#  Balance storage with auto-save                                            #
# ---------------------------------------------------------------------------#
class AutoSaveDict(dict):
    """Dictionary that flushes itself to disk (debounced) on every change."""

    def __init__(self, file: str):
        super().__init__()
        self.file = file
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

        if os.path.exists(file):
            try:
                with open(file, "r", encoding="utf-8") as fp:
                    super().update(json.load(fp))
            except json.JSONDecodeError:
                log.error("Corrupt %s – starting with empty balances", file)

    # ---------------- mutation hooks ---------------- #
    def __setitem__(self, k, v):  # type: ignore[override]
        super().__setitem__(str(k), v)
        self._debounce_save()

    def update(self, *a, **kw):  # type: ignore[override]
        super().update(*a, **kw)
        self._debounce_save()

    # ---------------- save helpers ------------------ #
    def _debounce_save(self, delay: float = 1):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._save_after(delay))

    async def _save_after(self, delay: float):
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                await asyncio.to_thread(self._sync_save)
        except asyncio.CancelledError:
            pass  # a newer write arrived

    def _sync_save(self):
        with open(self.file, "w", encoding="utf-8") as fp:
            json.dump(self, fp, indent=4)


balances: AutoSaveDict[str, int] = AutoSaveDict(DATA_FILE)

# ---------------------------------------------------------------------------#
#  Card-image generator                                                      #
# ---------------------------------------------------------------------------#
def generate_hand_image(hand: List[Card]) -> BytesIO | None:
    """Return BytesIO PNG for the given hand, or None if any asset missing."""
    images: list[Image.Image] = []
    for rank, suit in hand:
        path = os.path.join(CARD_FOLDER, f"{RANK_MAP[rank]}_of_{SUIT_MAP[suit]}.png")
        if not os.path.exists(path):
            log.error("Missing card asset: %s", path)
            return None
        img = Image.open(path).convert("RGBA")
        ratio = img.height / img.width
        img = img.resize((CARD_WIDTH, int(CARD_WIDTH * ratio)), Image.LANCZOS)
        images.append(img)

    total_w = len(images) * CARD_WIDTH + (len(images) - 1) * CARD_SPACING
    max_h = max(i.height for i in images)
    canvas = Image.new("RGBA", (total_w, max_h))
    x = 0
    for img in images:
        canvas.paste(img, (x, 0), img)
        x += CARD_WIDTH + CARD_SPACING

    out = BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    return out


# ---------------------------------------------------------------------------#
#  Core game logic                                                           #
# ---------------------------------------------------------------------------#
class PlayerState:
    def __init__(self, user_id: int, bet: int):
        self.user_id = user_id
        self.bet = bet
        self.hand: list[Card] = []
        self.busted = False
        self.stood = False

    def is_done(self) -> bool:
        return self.busted or self.stood


class BlackjackGame:
    def __init__(self, host_id: int):
        self.host_id = host_id
        self.players: list[PlayerState] = []
        self.dealer_hand: list[Card] = []
        self.deck: list[Card] = self._build_deck()
        random.shuffle(self.deck)
        self.lock = asyncio.Lock()
        self.pot = 0
        self.dealt = False

    # ---------------- static helpers ---------------- #
    @staticmethod
    def _build_deck() -> List[Card]:
        suits = ["♠", "♥", "♦", "♣"]
        ranks = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
        return [(r, s) for s in suits for r in ranks]

    @staticmethod
    def hand_value(hand: List[Card]) -> int:
        total, aces = 0, 0
        for rank, _ in hand:
            if rank.isdigit():
                total += int(rank)
            elif rank in {"J", "Q", "K"}:
                total += 10
            else:  # Ace
                total += 11
                aces += 1
        while total > 21 and aces:
            total -= 10
            aces -= 1
        return total

    # ---------------- player management ------------- #
    def add_player(self, user_id: int, bet: int):
        if any(p.user_id == user_id for p in self.players):
            return
        self.players.append(PlayerState(user_id, bet))
        self.pot += bet

    # ---------------- dealing ----------------------- #
    def deal_initial(self):
        self.dealt = True
        for _ in range(2):
            for p in self.players:
                self._draw(p)
        self.dealer_hand.extend([self.deck.pop(), self.deck.pop()])

    def _draw(self, player: PlayerState):
        if self.deck:
            player.hand.append(self.deck.pop())

    def dealer_play(self):
        while self.hand_value(self.dealer_hand) < 17 and self.deck:
            self.dealer_hand.append(self.deck.pop())

    def everyone_done(self) -> bool:
        return all(p.is_done() for p in self.players)

    # ---------------- settlement -------------------- #
    def settle(self) -> List[str]:
        lines: list[str] = []
        dealer_val = self.hand_value(self.dealer_hand)
        if dealer_val > 21:
            lines.append(f"Dealer busts with {dealer_val}! ❌")

        for p in self.players:
            pv = self.hand_value(p.hand)

            if p.busted:
                lines.append(f"<@{p.user_id}> busted (Value: {pv}) ❌")
                continue

            if dealer_val > 21 or pv > dealer_val:  # win
                win = 2 * p.bet
                balances[str(p.user_id)] = balances.get(str(p.user_id), 0) + win
                lines.append(f"<@{p.user_id}> wins {win} chips (Value: {pv}) 🎉")
            elif pv == dealer_val:  # tie
                balances[str(p.user_id)] = balances.get(str(p.user_id), 0) + p.bet
                lines.append(f"<@{p.user_id}> ties (Value: {pv}) 🤝")
            else:  # loss
                lines.append(f"<@{p.user_id}> loses (Value: {pv}) ❌")

        return lines


# ---------------------------------------------------------------------------#
#  Discord-bot setup                                                         #
# ---------------------------------------------------------------------------#
intents = nextcord.Intents.all()
bot = commands.Bot(intents=intents)
active_games: Dict[int, BlackjackGame] = {}


# ---------------------------------------------------------------------------#
#  End-game helper                                                           #
# ---------------------------------------------------------------------------#
async def conclude(inter: Interaction, game: BlackjackGame, *, followup: bool):
    """Show dealer’s final hand, then results & updated balances."""
    active_games.pop(inter.channel_id, None)
    game.dealer_play()
    dealer_val = game.hand_value(game.dealer_hand)

    # 1️⃣ dealer-hand image
    img_bytes = await asyncio.to_thread(generate_hand_image, game.dealer_hand)
    embed_hand = Embed(
        title="🏁 Blackjack — Game Over",
        description=f"**Dealer's final hand (Value: {dealer_val})**",
        color=COLOR_SUCCESS,
    )
    file = None
    if img_bytes:
        file = nextcord.File(img_bytes, filename="dealer_final.png")
        embed_hand.set_image(url="attachment://dealer_final.png")

    send1 = inter.followup.send if followup else inter.response.send_message
    await send1(embed=embed_hand, file=file)

    # 2️⃣ results & balances
    result_lines = game.settle()
    bal_lines = [
        f"<@{p.user_id}>: {balances.get(str(p.user_id), 0)} chips" for p in game.players
    ]
    desc = "\n".join(["**Results**", *result_lines, "\n**Updated Balances**", *bal_lines])
    await inter.followup.send(embed=Embed(description=desc, color=COLOR_SUCCESS))


# ---------------------------------------------------------------------------#
#  UI: Modals & Views                                                        #
# ---------------------------------------------------------------------------#
class BetModal(nextcord.ui.Modal):
    def __init__(self, game: BlackjackGame):
        super().__init__(title="Join the Game")
        self.game = game
        self.bet_input = nextcord.ui.TextInput(
            label="Enter your bet (10 – 500)", placeholder="e.g. 100"
        )
        self.add_item(self.bet_input)

    async def on_submit(self, inter: Interaction):
        user_id = str(inter.user.id)
        # parse bet
        try:
            bet = int(self.bet_input.value)
        except ValueError:
            return await inter.response.send_message(
                "Bet must be a whole number.", ephemeral=True
            )
        if not 10 <= bet <= 500:
            return await inter.response.send_message(
                "Bet must be between 10 and 500.", ephemeral=True
            )
        bal = balances.get(user_id, DEFAULT_BALANCE)
        if bal < bet:
            return await inter.response.send_message(
                "You don’t have that many chips.", ephemeral=True
            )

        balances[user_id] = bal - bet
        self.game.add_player(int(user_id), bet)
        await inter.response.send_message(
            f"✅ {inter.user.mention} joined with a {bet}-chip bet!", ephemeral=False
        )


class JoinDealView(nextcord.ui.View):
    def __init__(self, game: BlackjackGame, host_id: int):
        super().__init__(timeout=180)
        self.game = game
        self.host_id = host_id

    @nextcord.ui.button(label="Join Game", style=nextcord.ButtonStyle.primary)
    async def join_btn(self, _btn, inter: Interaction):
        if self.game.dealt:
            return await inter.response.send_message(
                "Cards already dealt.", ephemeral=True
            )
        await inter.response.send_modal(BetModal(self.game))

    @nextcord.ui.button(label="Deal Cards", style=nextcord.ButtonStyle.success)
    async def deal_btn(self, button, inter: Interaction):
        if inter.user.id != self.host_id:
            return await inter.response.send_message(
                "Only the host may deal.", ephemeral=True
            )
        if self.game.dealt:
            return await inter.response.send_message(
                "Cards were already dealt.", ephemeral=True
            )
        if not self.game.players:
            return await inter.response.send_message(
                "No players have joined yet!", ephemeral=True
            )

        self.game.deal_initial()

        # disable buttons in original message
        for child in self.children:
            child.disabled = True
        await inter.message.edit(view=self)

        d_val = self.game.hand_value([self.game.dealer_hand[0]])
        first_img = generate_hand_image([self.game.dealer_hand[0]])
        file = None
        embed = Embed(
            title="🃏 Blackjack — Cards Dealt!",
            description=f"**Dealer's first card (Value: {d_val})**",
            color=COLOR_PRIMARY,
        )
        if first_img:
            file = nextcord.File(first_img, filename="dealer_first.png")
            embed.set_image(url="attachment://dealer_first.png")

        await inter.response.send_message(embed=embed, file=file)

        instr = (
            "Click **👁️ View My Hand** below to see your cards, "
            "then choose **🃏 Hit** or **✋ Stand**."
        )
        await inter.followup.send(instr, view=HandOptionsView(), ephemeral=False)


class HandOptionsView(nextcord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @nextcord.ui.button(
        label="👁️ View My Hand", style=nextcord.ButtonStyle.secondary, emoji="👁️"
    )
    async def view_hand(self, _btn, inter: Interaction):
        game = active_games.get(inter.channel_id)
        if not game:
            return await inter.response.send_message("No active game.", ephemeral=True)

        player = next((p for p in game.players if p.user_id == inter.user.id), None)
        if not player:
            return await inter.response.send_message(
                "You are not in this game.", ephemeral=True
            )

        value = game.hand_value(player.hand)
        img_bytes = generate_hand_image(player.hand)
        if img_bytes is None:
            return await inter.response.send_message(
                "Error generating hand image.", ephemeral=True
            )

        file = nextcord.File(img_bytes, filename="my_hand.png")
        await inter.response.send_message(
            content=f"🂠 Your current hand value: **{value}**",
            file=file,
            view=PrivatePlayerView(player.user_id),
            ephemeral=True,
        )


class PrivatePlayerView(nextcord.ui.View):
    """Two buttons that only apply to the requesting player."""

    def __init__(self, player_id: int):
        super().__init__(timeout=180)
        self.player_id = player_id
        self.add_item(HitButton(player_id))
        self.add_item(StandButton(player_id))


class HitButton(nextcord.ui.Button):
    def __init__(self, player_id: int):
        # unique custom_id avoids duplicate-ID error
        super().__init__(
            label="🃏 Hit",
            style=nextcord.ButtonStyle.primary,
            custom_id=f"hit_{player_id}",
        )
        self.player_id = player_id

    async def callback(self, inter: Interaction):
        game = active_games.get(inter.channel_id)
        if not game:
            return await inter.response.send_message("No active game.", ephemeral=True)

        async with game.lock:
            player = next((p for p in game.players if p.user_id == self.player_id), None)
            if not player:
                return await inter.response.send_message(
                    "You aren't in this game.", ephemeral=True
                )
            if player.is_done():
                return await inter.response.send_message(
                    "You already busted or stood.", ephemeral=True
                )

            # empty-deck safety
            if not game.deck:
                await inter.response.send_message(
                    "🛑 The deck is empty – round ends now.", ephemeral=True
                )
                return await conclude(inter, game, followup=True)

            game._draw(player)
            drawn = player.hand[-1]
            new_val = game.hand_value(player.hand)
            img_bytes = generate_hand_image(player.hand)

            if new_val > 21:  # bust
                player.busted = True
                msg = (
                    f"❌ You drew **{drawn[0]}{drawn[1]}** and busted with **{new_val}**! "
                    f"{'Waiting for other players…' if len(game.players) > 1 else ''}"
                )
                if img_bytes:
                    await inter.response.send_message(
                        msg,
                        file=nextcord.File(img_bytes, filename="hand.png"),
                        ephemeral=True,
                    )
                else:
                    await inter.response.send_message(msg, ephemeral=True)
            else:  # still live
                if img_bytes:
                    await inter.response.send_message(
                        content=f"🂠 Your current hand value: **{new_val}**",
                        file=nextcord.File(img_bytes, filename="hand.png"),
                        view=PrivatePlayerView(player.user_id),
                        ephemeral=True,
                    )
                else:
                    await inter.response.send_message(
                        content=f"🂠 Your current hand value: **{new_val}**",
                        view=PrivatePlayerView(player.user_id),
                        ephemeral=True,
                    )

            if game.everyone_done():
                await conclude(inter, game, followup=True)


class StandButton(nextcord.ui.Button):
    def __init__(self, player_id: int):
        super().__init__(
            label="✋ Stand",
            style=nextcord.ButtonStyle.success,
            custom_id=f"stand_{player_id}",
        )
        self.player_id = player_id

    async def callback(self, inter: Interaction):
        game = active_games.get(inter.channel_id)
        if not game:
            return await inter.response.send_message("No active game.", ephemeral=True)

        async with game.lock:
            player = next((p for p in game.players if p.user_id == self.player_id), None)
            if not player:
                return await inter.response.send_message(
                    "You aren't in this game.", ephemeral=True
                )
            if player.is_done():
                return await inter.response.send_message(
                    "You already busted or stood.", ephemeral=True
                )

            player.stood = True
            msg = f"<@{player.user_id}> stands."
            if len(game.players) > 1:
                msg += " Waiting for other players…"
            await inter.response.send_message(msg, ephemeral=False)

            if game.everyone_done():
                await conclude(inter, game, followup=True)


# ---------------------------------------------------------------------------#
#  Slash commands                                                            #
# ---------------------------------------------------------------------------#
@bot.slash_command(description="Pong!")
async def ping(inter: Interaction):
    await inter.response.send_message("Pong!", ephemeral=True)


@bot.slash_command(
    description="Start a new game of Blackjack with your opening bet (10–500)."
)
async def blackjack_start(inter: Interaction, bet: int):
    if inter.channel_id in active_games:
        return await inter.response.send_message(
            "A game is already in progress!", ephemeral=True
        )
    if not 10 <= bet <= 500:
        return await inter.response.send_message(
            "Bet must be between 10 and 500.", ephemeral=True
        )

    user_id = str(inter.user.id)
    balances.setdefault(user_id, DEFAULT_BALANCE)
    if balances[user_id] < bet:
        return await inter.response.send_message(
            f"You only have {balances[user_id]} chips.", ephemeral=True
        )

    game = BlackjackGame(inter.user.id)
    active_games[inter.channel_id] = game
    balances[user_id] -= bet
    game.add_player(int(user_id), bet)

    desc = (
        f"💰 **Pot:** {game.pot} chips\n"
        f"Players Joined: <@{inter.user.id}>\n"
        "Click **Join Game** to enter your own bet.\n"
        "Host: click **Deal Cards** when ready."
    )
    embed = Embed(title="🃏 Blackjack — New Game!", description=desc, color=COLOR_INFO)
    await inter.response.send_message(
        embed=embed, view=JoinDealView(game, inter.user.id)
    )


@bot.slash_command(description="Replenish your balance to 100 chips if you have zero.")
async def blackjack_replenish(inter: Interaction):
    user_id = str(inter.user.id)
    if balances.get(user_id, 0) > 0:
        return await inter.response.send_message(
            "You still have chips!", ephemeral=True
        )
    balances[user_id] = 100
    embed = Embed(
        title="💰 Blackjack — Replenished",
        description="Your balance is now **100** chips.",
        color=COLOR_SUCCESS,
    )
    await inter.response.send_message(embed=embed, ephemeral=True)


@bot.slash_command(description="Manually end the current game.")
async def blackjack_end(inter: Interaction):
    game = active_games.get(inter.channel_id)
    if not game:
        return await inter.response.send_message("No active game.", ephemeral=True)

    if inter.user.id != game.host_id and not any(
        p.user_id == inter.user.id for p in game.players
    ):
        return await inter.response.send_message(
            "You are not part of this game.", ephemeral=True
        )

    await conclude(inter, game, followup=False)


@bot.slash_command(description="See the top 15 players by chip count.")
async def blackjack_leaderboard(inter: Interaction):
    if not balances:
        return await inter.response.send_message("No balances yet!", ephemeral=True)

    top = sorted(balances.items(), key=lambda kv: kv[1], reverse=True)[:15]
    embed = Embed(title="🏆 Blackjack — Leaderboard", color=COLOR_LEADER)
    for rank, (uid, bal) in enumerate(top, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"#{rank}")
        # try member name, then global name
        # --- resolve a friendly name ---
        member = inter.guild.get_member(int(uid)) if inter.guild else None
        if not member:
            try:
                member = await inter.guild.fetch_member(int(uid))  # REST
            except nextcord.NotFound:
                member = None

        if member:
            # prefer server nickname → global display name → username
            name = member.nick or member.global_name or member.name
        else:
            # user left the server (or deleted) – try global lookup
            try:
                user_obj = await bot.fetch_user(int(uid))
                name = user_obj.global_name or user_obj.name
            except Exception:
                name = f"User {uid}"
        embed.add_field(name=f"{medal} {name}", value=f"{bal:,} chips", inline=False)

    await inter.response.send_message(
        embed=embed, allowed_mentions=AllowedMentions(users=True)
    )


@bot.slash_command(description="Show help for all Blackjack commands.")
async def help(inter: Interaction):
    embed = Embed(
        title="❓ Blackjack — Help",
        description=(
            "Use the commands below to start, join and play Blackjack.\n"
            "• `/blackjack_start <bet>` — start a game (bet 10–500)\n"
            "• Others click **Join Game** to wager their own bet\n"
            "• Host clicks **Deal Cards** → each player can view hand & hit/stand"
        ),
        color=COLOR_HELP,
    )
    embed.add_field(name="/ping", value="Check if the bot is alive.", inline=False)
    embed.add_field(
        name="/blackjack_start <bet>",
        value="Start a new game with an initial bet.",
        inline=False,
    )
    embed.add_field(
        name="/blackjack_replenish",
        value="Get 100 chips if you’re broke.",
        inline=False,
    )
    embed.add_field(
        name="/blackjack_end",
        value="Host/player can force-end a game.",
        inline=False,
    )
    embed.add_field(
        name="/blackjack_leaderboard",
        value="Show the top chip holders.",
        inline=False,
    )
    await inter.response.send_message(embed=embed, ephemeral=True)

# ---------------------------------------------------------------------------#
#  Bot ready & run                                                           #
# ---------------------------------------------------------------------------#
@bot.event
async def on_ready():
    log.info("Logged in as %s (ID:%s)", bot.user, bot.user.id)


bot.run(TOKEN)
