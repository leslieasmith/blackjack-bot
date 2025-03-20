from dotenv import load_dotenv
import os
import random
import json
import nextcord
from nextcord.ext import commands
from nextcord import Interaction, Embed

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

##############################
# AUTO SAVE BALANCES SETUP
##############################
DATA_FILE = "balances.json"

class AutoSaveDict(dict):
    def __init__(self, file, *args, **kwargs):
        self.file = file
        if os.path.exists(file):
            with open(file, "r") as f:
                data = json.load(f)
            # Ensure dictionary keys are stored as strings.
            super().__init__({str(k): v for k, v in data.items()})
        else:
            super().__init__(*args, **kwargs)
            self.save()
    def __setitem__(self, key, value):
        key = str(key)  # Convert user ID to string for consistency.
        if key in self and self[key] == value:
            return  # Prevent redundant writes.
        super().__setitem__(key, value)
        self.save()
    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self.save()
    def save(self):
        with open(self.file, "w") as f:
            json.dump(self, f, indent=4)

# Use AutoSaveDict for balances.
balances = AutoSaveDict(DATA_FILE, {})

##############################
# 1) Bot & Data Setup
##############################
intents = nextcord.Intents.default()
bot = commands.Bot(intents=intents)
games = {}  # channel_id -> BlackjackGame

##############################
# 2) Player State
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
# 3) Blackjack Game Logic
##############################
class BlackjackGame:
    def __init__(self):
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
        # Dealer gets two cards.
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
            else:  # Ace
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
# 4) Basic Ping Command
##############################
@bot.slash_command(description="Pong!")
async def ping(ctx: Interaction):
    await ctx.response.send_message("Pong!", ephemeral=True)

##############################
# 5) Start Game Command
##############################
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
    balances.setdefault(user_id, 1000)
    # If user has run out of coins, top up and send a message without starting a game.
    if balances[user_id] <= 0:
        balances[user_id] = 100
        await ctx.response.send_message("You're so bad at Blackjack you have run out of chips! Your balance has been topped up with 100 chips. Please place a new bet to start a game.", ephemeral=True)
        return
    if bet > balances[user_id]:
        await ctx.response.send_message(f"You only have {balances[user_id]} chips!", ephemeral=True)
        return

    game = BlackjackGame()
    games[ctx.channel_id] = game

    balances[user_id] -= bet
    game.add_player(int(user_id), bet)

    await ctx.response.send_message(
        f"**Blackjack game created!**\n**Pot:** {game.pot} chips\n"
        "Use `/blackjack_join` to join (before dealing), then `/blackjack_deal` to deal cards.",
        ephemeral=False
    )

##############################
# 6) Join Game Command
##############################
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
    balances.setdefault(user_id, 1000)
    if bet > balances[user_id]:
        await ctx.response.send_message(f"You only have {balances[user_id]} chips!", ephemeral=True)
        return

    balances[user_id] -= bet
    game.add_player(int(user_id), bet)

    await ctx.response.send_message(
        f"<@{ctx.user.id}> joined! **Pot:** {game.pot} chips\nUse `/blackjack_deal` to deal cards!",
        ephemeral=False
    )

##############################
# 7) Deal Cards Command
##############################
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
    # Get the dealer's first card.
    dealer_card = f"{game.dealer_hand[0][0]}{game.dealer_hand[0][1]}"
    # Public message: Dealer's first card is revealed.
    await ctx.response.send_message(
        f"**Dealer's first card: {dealer_card}**\nCards have been dealt!\n"
        "Please use `/blackjack_myhand` to view your hand privately, and then use `/blackjack_hit` or `/blackjack_stand` to play.",
        ephemeral=False
    )

##############################
# 8) Show My Hand Command
##############################
@bot.slash_command(description="View your current hand privately.")
async def blackjack_myhand(ctx: Interaction):
    """View your current hand (ephemeral)."""
    game = games.get(ctx.channel_id)
    if not game or not game.dealt_cards:
        await ctx.response.send_message("No active or dealt game here.", ephemeral=True)
        return
    player = next((p for p in game.players if p.user_id == ctx.user.id), None)
    if not player:
        await ctx.response.send_message("You're not in this game!", ephemeral=True)
        return
    val = game.hand_value(player.hand)
    await ctx.response.send_message(f"Your current hand: {player.hand} (Value: {val})", ephemeral=True)

##############################
# 9) Hit Command
##############################
@bot.slash_command(description="Take another card.")
async def blackjack_hit(ctx: Interaction):
    game = games.get(ctx.channel_id)
    if not game or not game.dealt_cards or game.game_over:
        await ctx.response.send_message("No active game or cards not dealt yet.", ephemeral=True)
        return
    player = next((p for p in game.players if p.user_id == ctx.user.id), None)
    if not player:
        await ctx.response.send_message("You're not in this game!", ephemeral=True)
        return
    if player.is_done():
        await ctx.response.send_message("You already busted or stood.", ephemeral=True)
        return
    game.draw_card_for_player(player)
    val = game.hand_value(player.hand)
    await ctx.response.send_message(f"You drew {player.hand[-1]} (Value: {val})", ephemeral=True)
    if val > 21:
        player.busted = True
        await ctx.followup.send(f"<@{player.user_id}> **busts** with {val}!", ephemeral=False)
    if game.all_players_done():
        await end_game_followup(ctx)

##############################
# 10) Stand Command
##############################
@bot.slash_command(description="Keep your current hand.")
async def blackjack_stand(ctx: Interaction):
    game = games.get(ctx.channel_id)
    if not game or game.game_over:
        await ctx.response.send_message("No active game or game ended.", ephemeral=True)
        return
    player = next((p for p in game.players if p.user_id == ctx.user.id), None)
    if not player:
        await ctx.response.send_message("You're not in this game!", ephemeral=True)
        return
    if player.is_done():
        await ctx.response.send_message("You already busted or stood.", ephemeral=True)
        return
    await ctx.response.send_message(f"<@{player.user_id}> stands.", ephemeral=False)
    player.stood = True
    if game.all_players_done():
        await end_game_followup(ctx)

##############################
# 11) Manual End Command
##############################
@bot.slash_command(description="Manually end the game.")
async def blackjack_end(ctx: Interaction):
    """Manually end the game."""
    game = games.pop(ctx.channel_id, None)
    if not game:
        await ctx.response.send_message("No game is active.", ephemeral=True)
        return
    game.dealer_draw()
    lines = game.distribute_pot()
    game.end_game()
    d_val = game.hand_value(game.dealer_hand)
    summary = []
    summary.append(f"**Dealer's final hand**: {game.dealer_hand} (Value: {d_val})\n")
    summary.extend(lines)
    summary.append("\n**Updated Balances**:")
    for p in game.players:
        summary.append(f"<@{p.user_id}>: {balances[str(p.user_id)]} chips")
    await ctx.response.send_message("\n".join(summary), ephemeral=False)

##############################
# 12) End Game Followup Helper
##############################
async def end_game_followup(ctx: Interaction):
    game = games.pop(ctx.channel_id, None)
    if not game:
        await ctx.followup.send("No game is active.", ephemeral=True)
        return
    game.dealer_draw()
    lines = game.distribute_pot()
    game.end_game()
    d_val = game.hand_value(game.dealer_hand)
    summary = []
    summary.append(f"**Dealer's final hand**: {game.dealer_hand} (Value: {d_val})\n")
    summary.extend(lines)
    summary.append("\n**Updated Balances**:")
    for p in game.players:
        summary.append(f"<@{p.user_id}>: {balances[str(p.user_id)]} chips")
    await ctx.followup.send("\n".join(summary), ephemeral=False)

##############################
# 13) Help Command
##############################
@bot.slash_command(description="Get help using this bot.")
async def help(ctx: Interaction):
    embed = Embed(title="Blackjack Bot Commands", description="How to play Blackjack using this bot.", color=0x00FF00)
    embed.add_field(name="/ping", value="Check if the bot is online.", inline=False)
    embed.add_field(name="/blackjack_start <bet>", value="Start a new game with a bet (min: 10, max: 500).", inline=False)
    embed.add_field(name="/blackjack_join <bet>", value="Join an active game before the cards are dealt.", inline=False)
    embed.add_field(name="/blackjack_deal", value="Deal cards. The dealer's first card is shown publicly.", inline=False)
    embed.add_field(name="/blackjack_myhand", value="View your current hand privately.", inline=False)
    embed.add_field(name="/blackjack_hit", value="Draw another card.", inline=False)
    embed.add_field(name="/blackjack_stand", value="Keep your current hand.", inline=False)
    embed.add_field(name="/blackjack_end", value="Manually end the current game.", inline=False)
    embed.add_field(name="/blackjack_leaderboard", value="See the top players and their chip balances.", inline=False)
    await ctx.response.send_message(embed=embed)

##############################
# 14) Leaderboard Command
##############################
@bot.slash_command(description="See the top players and their chip balances.")
async def blackjack_leaderboard(ctx: Interaction):
    if not balances:
        await ctx.response.send_message("No one has a balance yet!", ephemeral=True)
        return
    sorted_bal = sorted(balances.items(), key=lambda x: x[1], reverse=True)
    lines = ["**Blackjack Leaderboard**"]
    for rank, (uid, bal) in enumerate(sorted_bal, 1):
        lines.append(f"**{rank}.** <@{uid}> - {bal} chips")
    await ctx.response.send_message("\n".join(lines), ephemeral=False)

##############################
# 15) Bot Ready & Run
##############################
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

bot.run(token)
