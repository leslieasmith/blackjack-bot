from dotenv import load_dotenv
import os
import random
import json
import asyncio  
import nextcord
from nextcord.ext import commands
from nextcord import Interaction, Embed
import logging  

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

##############################
# CONSTANTS & LOGGING SETUP
##############################
DEFAULT_BALANCE = 1000  #Centralised default balance constant
DATA_FILE = "balances.json"
logging.basicConfig(level=logging.INFO)  

##############################
# AUTO SAVE BALANCES SETUP 
##############################
class AutoSaveDict(dict):
    def __init__(self, file, *args, **kwargs):
        self.file = file
        self._lock = asyncio.Lock()  
        if os.path.exists(file):
            try:
                with open(file, "r") as f:
                    data = json.load(f)
                # Ensure keys are strings.
                super().__init__({str(k): v for k, v in data.items()})
            except json.JSONDecodeError:
                logging.error("JSON decode error in file, starting with an empty dictionary.")
                super().__init__(*args, **kwargs)
                self.save_sync()  
        else:
            super().__init__(*args, **kwargs)
            self.save_sync()

    def __setitem__(self, key, value):
        key = str(key)
        if key in self and self[key] == value:
            return
        super().__setitem__(key, value)
        
        asyncio.create_task(self.async_save())

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        asyncio.create_task(self.async_save())

    def save_sync(self):
        with open(self.file, "w") as f:
            json.dump(self, f, indent=4)

    async def async_save(self):
        async with self._lock:
            await asyncio.to_thread(self.save_sync)


balances = AutoSaveDict(DATA_FILE, {})

##############################
# BOT & DATA SETUP
##############################
intents = nextcord.Intents.all()
bot = commands.Bot(intents=intents)
games = {}  

##############################
# HELPER FUNCTIONS
##############################
def format_hand(hand):
    """Formats a list of card tuples into a user-friendly string."""
    return ", ".join([f"{r}{s}" for r, s in hand])

async def process_end_game(ctx: Interaction, game, followup: bool = False):
    games.pop(ctx.channel_id, None)

    game.dealer_draw()
    lines = game.distribute_pot()
    game.end_game()
    d_val = game.hand_value(game.dealer_hand)
    summary = []
    summary.append(f"**Dealer's final hand**: {format_hand(game.dealer_hand)} (Value: {d_val})\n")
    summary.extend(lines)
    summary.append("\n**Updated Balances**:")
    for p in game.players:
        summary.append(f"<@{p.user_id}>: {balances[str(p.user_id)]} chips")
    message = "\n".join(summary)
    if followup:
        await ctx.followup.send(message, ephemeral=False)
    else:
        await ctx.response.send_message(message, ephemeral=False)

##############################
# INTERACTIVE GAME ACTIONS 
##############################
class HandOptionsView(nextcord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @nextcord.ui.button(label="View My Hand", style=nextcord.ButtonStyle.secondary, custom_id="view_hand")
    async def view_hand(self, button: nextcord.ui.Button, interaction: Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        player = next((p for p in game.players if p.user_id == interaction.user.id), None)
        if not player:
            await interaction.response.send_message("You're not in this game.", ephemeral=True)
            return

        hand_str = format_hand(player.hand)
        hand_val = game.hand_value(player.hand)

        view = PrivatePlayerView()

        await interaction.response.send_message(
            content=f"Your current hand: **{hand_str}** (Value: {hand_val})",
            view=view,
            ephemeral=True
        )

class HitButton(nextcord.ui.Button):
    def __init__(self):
        super().__init__(label="Hit", style=nextcord.ButtonStyle.primary, custom_id="hit")

    async def callback(self, interaction: Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        player = next((p for p in game.players if p.user_id == interaction.user.id), None)
        if not player:
            await interaction.response.send_message("You're not in this game.", ephemeral=True)
            return

        if player.is_done():
            await interaction.response.send_message("You already busted or stood.", ephemeral=True)
            return

        # Draw a card
        game.draw_card_for_player(player)
        new_card = format_hand([player.hand[-1]])
        new_val = game.hand_value(player.hand)

        message = f"You drew **{new_card}** (Hand Value: {new_val})."

        if new_val > 21:
            player.busted = True
            message += f"\n<@{player.user_id}> busts with {new_val}!"
            await interaction.response.send_message(message, ephemeral=True)
        else:
            # Send updated hand + buttons
            hand_str = format_hand(player.hand)
            view = PrivatePlayerView()
            await interaction.response.send_message(
                content=f"Your current hand: **{hand_str}** (Value: {new_val})",
                view=view,
                ephemeral=True
            )

        if game.all_players_done():
            await process_end_game(interaction, game, followup=True)


class StandButton(nextcord.ui.Button):
    def __init__(self):
        super().__init__(label="Stand", style=nextcord.ButtonStyle.success, custom_id="stand")

    async def callback(self, interaction: Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        player = next((p for p in game.players if p.user_id == interaction.user.id), None)
        if not player:
            await interaction.response.send_message("You're not in this game.", ephemeral=True)
            return
        if player.is_done():
            await interaction.response.send_message("You already busted or stood.", ephemeral=True)
            return

        player.stood = True
        await interaction.response.send_message(f"<@{player.user_id}> stands.", ephemeral=True)

        if game.all_players_done():
            await process_end_game(interaction, game, followup=True)

class PrivatePlayerView(nextcord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(HitButton())
        self.add_item(StandButton())

##############################
# PLAYER STATE
##############################
class PlayerState:
    def __init__(self, user_id: int, bet: int):
        self.user_id = user_id
        self.hand = []
        self.busted = False
        self.stood = False
        self.bet = bet

    def is_done(self) -> bool:
        return self.busted or self.stood

##############################
# BLACKJACK GAME LOGIC
##############################
class BlackjackGame:
    def __init__(self, host_id: int):
        self.host_id = host_id      # ID of the game creator
        self.players = []
        self.dealer_hand = []
        self.deck = self._make_deck()
        random.shuffle(self.deck)
        self.game_active = True
        self.game_over = False
        self.pot = 0
        self.dealt_cards = False

    def _make_deck(self):
        suits = ["♠", "♥", "♦", "♣"]
        ranks = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
        return [(r, s) for s in suits for r in ranks]

    def add_player(self, user_id: int, bet: int):
        if any(p.user_id == user_id for p in self.players):
            return
        self.players.append(PlayerState(user_id, bet))
        self.pot += bet

    def deal_initial_cards(self):
        self.dealt_cards = True
        for _ in range(2):
            for p in self.players:
                p.hand.append(self.deck.pop())
        # Dealer gets two cards
        self.dealer_hand.append(self.deck.pop())
        self.dealer_hand.append(self.deck.pop())

    def hand_value(self, hand):
        val = 0
        ace_count = 0
        for r, s in hand:
            if r.isdigit():
                val += int(r)
            elif r in ["J", "Q", "K"]:
                val += 10
            else:  # Ace handling
                val += 11
                ace_count += 1
        while val > 21 and ace_count > 0:
            val -= 10
            ace_count -= 1
        return val

    def draw_card_for_player(self, player: PlayerState):
        if self.deck:
            player.hand.append(self.deck.pop())

    def dealer_draw(self):
        while self.hand_value(self.dealer_hand) < 17 and self.deck:
            self.dealer_hand.append(self.deck.pop())

    def all_players_done(self):
        return all(p.is_done() for p in self.players)

    def end_game(self):
        self.game_active = False
        self.game_over = True

    def distribute_pot(self):
        lines = []
        dealer_val = self.hand_value(self.dealer_hand)
        if dealer_val > 21:
            lines.append(f"Dealer busts with {dealer_val}! ❌")
            for p in self.players:
                if not p.busted:
                    amt = 2 * p.bet
                    balances[str(p.user_id)] += amt
                    lines.append(f"<@{p.user_id}> wins {amt} chips (Value: {self.hand_value(p.hand)}) 🎉")
                else:
                    lines.append(f"<@{p.user_id}> busted (Value: {self.hand_value(p.hand)}) ❌")
        else:
            for p in self.players:
                if p.busted:
                    lines.append(f"<@{p.user_id}> busted (Value: {self.hand_value(p.hand)}) ❌")
                else:
                    pv = self.hand_value(p.hand)
                    if pv > dealer_val:
                        amt = 2 * p.bet
                        balances[str(p.user_id)] += amt
                        lines.append(f"<@{p.user_id}> wins {amt} chips (Value: {pv}) 🎉")
                    elif pv < dealer_val:
                        lines.append(f"<@{p.user_id}> loses (Value: {pv}) ❌")
                    else:
                        tie_amt = p.bet
                        balances[str(p.user_id)] += tie_amt
                        lines.append(f"<@{p.user_id}> ties (Value: {pv}) 🤝")
        return lines

##############################
# COMMANDS
##############################
@bot.slash_command(description="Pong!")
async def ping(ctx: Interaction):
    await ctx.response.send_message("Pong!", ephemeral=True)

@bot.slash_command(description="Replenish your balance to 100 if you have 0 chips.")
async def blackjack_replenish(ctx: Interaction):
    user_id = str(ctx.user.id)
    current_balance = balances.get(user_id, 0)
    if current_balance > 0:
        await ctx.response.send_message(
            f"You still have {current_balance} chips! Replenish is only for those at 0 chips.",
            ephemeral=True
        )
        return
    balances[user_id] = 100
    await ctx.response.send_message("Your balance has been replenished to 100 chips!", ephemeral=True)

@bot.slash_command(description="Start a new game of Blackjack with a bet (min: 10, max: 500).")
async def blackjack_start(ctx: Interaction, bet: int):
    MIN_BET = 10
    MAX_BET = 500
    if ctx.channel_id in games:
        await ctx.response.send_message("A game is already in progress!", ephemeral=True)
        return
    if bet < MIN_BET or bet > MAX_BET:
        await ctx.response.send_message(f"Bets must be between {MIN_BET} and {MAX_BET} chips.", ephemeral=True)
        return
    user_id = str(ctx.user.id)
    balances.setdefault(user_id, DEFAULT_BALANCE)
    if balances[user_id] <= 0:
        await ctx.response.send_message("You have 0 chips. Please run `/blackjack_replenish` to get 100 chips, then try again.", ephemeral=True)
        return
    if bet > balances[user_id]:
        await ctx.response.send_message(f"You only have {balances[user_id]} chips!", ephemeral=True)
        return

    game = BlackjackGame(ctx.user.id)
    games[ctx.channel_id] = game

    balances[user_id] -= bet
    game.add_player(int(user_id), bet)

    await ctx.response.send_message(
        f"**Blackjack game created!**\n**Pot:** {game.pot} chips\nUse `/blackjack_join` to join (before dealing), then `/blackjack_deal` to deal cards.",
        ephemeral=False
    )

@bot.slash_command(description="Join an active Blackjack game before cards are dealt.")
async def blackjack_join(ctx: Interaction, bet: int):
    MIN_BET = 10
    MAX_BET = 500
    game = games.get(ctx.channel_id)
    if not game:
        await ctx.response.send_message("No game is active. Use `/blackjack_start` first.", ephemeral=True)
        return
    if game.dealt_cards:
        await ctx.response.send_message("Cards already dealt, you can't join now!", ephemeral=True)
        return
    if bet < MIN_BET or bet > MAX_BET:
        await ctx.response.send_message(f"Bets must be between {MIN_BET} and {MAX_BET} chips.", ephemeral=True)
        return
    user_id = str(ctx.user.id)
    balances.setdefault(user_id, DEFAULT_BALANCE)
    if balances[user_id] <= 0:
        await ctx.response.send_message("You have 0 chips. Please run `/blackjack_replenish` to get 100 chips, then try again.", ephemeral=True)
        return
    if bet > balances[user_id]:
        await ctx.response.send_message(f"You only have {balances[user_id]} chips!", ephemeral=True)
        return

    balances[user_id] -= bet
    game.add_player(int(user_id), bet)

    await ctx.response.send_message(
        f"<@{ctx.user.id}> joined! **Pot:** {game.pot} chips\n",
        ephemeral=False
    )

@bot.slash_command(description="Deal cards. The dealer's first card is revealed publicly.")
async def blackjack_deal(ctx: Interaction):
    game = games.get(ctx.channel_id)
    if not game:
        await ctx.response.send_message("No active game. Use `/blackjack_start` first.", ephemeral=True)
        return
    if game.dealt_cards:
        await ctx.response.send_message("Cards already dealt!", ephemeral=True)
        return
    if len(game.players) == 0:
        await ctx.response.send_message("No players have joined!", ephemeral=True)
        return

    game.deal_initial_cards()
    dealer_card = f"{game.dealer_hand[0][0]}{game.dealer_hand[0][1]}"
    # DESIGN UPDATE: Use HandOptionsView with a "View My Hand" button.
    view = HandOptionsView()
    await ctx.response.send_message(
        f"**Dealer's first card: {dealer_card}**\nCards have been dealt!\nClick **View My Hand** below to see your cards.",
        view=view,
        ephemeral=False
    )

@bot.slash_command(description="Manually end the game.")
async def blackjack_end(ctx: Interaction):
    game = games.get(ctx.channel_id)
    if not game:
        await ctx.response.send_message("No game is active.", ephemeral=True)
        return
    if ctx.user.id != game.host_id and not any(p.user_id == ctx.user.id for p in game.players):
        await ctx.response.send_message("You are not part of this game, so you cannot end it.", ephemeral=True)
        return

    game = games.pop(ctx.channel_id, None)
    await process_end_game(ctx, game, followup=False)

@bot.slash_command(description="Get help using this bot.")
async def help(ctx: Interaction):
    embed = Embed(
        title="Blackjack Bot Commands",
        description="Use the commands below to start, join, and play Blackjack. After dealing, click **View My Hand** to reveal your cards, then choose **Hit** or **Stand**.",
        color=0x00FF00  # DESIGN UPDATE: Consistent embed color
    )
    embed.add_field(name="/ping", value="Check if the bot is online.", inline=False)
    embed.add_field(name="/blackjack_start <bet>", value="Start a new game with a bet (min: 10, max: 500).", inline=False)
    embed.add_field(name="/blackjack_join <bet>", value="Join an active game before cards are dealt.", inline=False)
    embed.add_field(name="/blackjack_deal", value="Deal cards. The dealer's first card is shown publicly and interactive buttons will appear.", inline=False)
    embed.add_field(name="/blackjack_replenish", value="Replenish your chips to 100 if you have 0.", inline=False)
    embed.add_field(name="/blackjack_end", value="Manually end the current game (host or participant only).", inline=False)
    embed.add_field(name="/blackjack_leaderboard", value="See the top players and their chip balances.", inline=False)
    await ctx.response.send_message(embed=embed)

@bot.slash_command(description="See the top 15 players and their chip balances.")
async def blackjack_leaderboard(ctx: Interaction):
    if not balances:
        await ctx.response.send_message("No one has a balance yet!", ephemeral=True)
        return

    sorted_bal = sorted(balances.items(), key=lambda x: x[1], reverse=True)[:15]
    embed = Embed(title="**Blackjack Leaderboard**")
    for rank, (uid, bal) in enumerate(sorted_bal, 1):
        if rank == 1:
            medal = "🥇"
        elif rank == 2:
            medal = "🥈"
        elif rank == 3:
            medal = "🥉"
        else:
            medal = f"#{rank}"
        try:
            member = ctx.guild.get_member(int(uid))
            if member:
                username = member.display_name
            else:
                user = await bot.fetch_user(int(uid))
                username = user.name
        except Exception:
            username = f"User {uid}"
        formatted_bal = f"{bal:,}"
        embed.add_field(
            name=f"{medal} {username}", 
            value=f"{formatted_bal} chips", 
            inline=False
        )
    await ctx.response.send_message(embed=embed)

##############################
# BOT READY & RUN
##############################
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

bot.run(token)
