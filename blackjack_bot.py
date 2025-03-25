from dotenv import load_dotenv
import os
import random
import json
import asyncio  
import nextcord
from nextcord.ext import commands
from nextcord import Interaction, Embed
import logging
from PIL import Image
from io import BytesIO

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

##############################
# CONSTANTS & LOGGING SETUP
##############################
DEFAULT_BALANCE = 1000  # Centralised default balance constant
DATA_FILE = "balances.json"
logging.basicConfig(level=logging.INFO)

# Define color constants
COLOR_PRIMARY   = 0x5865F2  # Blurple - for deal messages
COLOR_SUCCESS   = 0x2ECC71  # Green - for game over
COLOR_INFO      = 0x3498DB  # Info blue - for new game start
COLOR_ACCENT    = 0x9B59B6  # Purple - for join messages
COLOR_HELP      = 0x00FF00  # Green - for help embed
COLOR_LEADER    = 0xF1C40F  # Gold - for leaderboard

# Folder and image parameters
CARD_FOLDER = "cards"
CARD_WIDTH = 120  # Adjusted for clarity
CARD_SPACING = 10

# Mapping for card image filenames:
suit_map = {"♠": "spades", "♥": "hearts", "♦": "diamonds", "♣": "clubs"}
rank_map = {
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
    "K": "king"
}

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
    """Formats a list of card tuples into a user-friendly string for text display."""
    return ", ".join([f"{r}{s}" for r, s in hand])

def generate_hand_image(hand, card_folder=CARD_FOLDER, card_width=CARD_WIDTH, spacing=CARD_SPACING):
    """
    Generates a composite image of the cards in 'hand' (a list of (rank, suit) tuples).
    Returns a BytesIO object containing the PNG image.
    """
    images = []
    for rank, suit in hand:
        filename = f"{rank_map[rank]}_of_{suit_map[suit]}.png"
        path = os.path.join(card_folder, filename)
        try:
            img = Image.open(path).convert("RGBA")
        except FileNotFoundError:
            logging.error(f"Missing file: {path}")
            continue
        # Resize card using LANCZOS filter for high quality
        aspect_ratio = img.height / img.width
        new_height = int(card_width * aspect_ratio)
        img = img.resize((card_width, new_height), Image.LANCZOS)
        images.append(img)
    if not images:
        return None
    total_width = len(images) * card_width + (len(images) - 1) * spacing
    max_height = max(img.height for img in images)
    final_image = Image.new("RGBA", (total_width, max_height), (0, 0, 0, 0))
    x_offset = 0
    for img in images:
        final_image.paste(img, (x_offset, 0), img)
        x_offset += card_width + spacing
    output = BytesIO()
    final_image.save(output, format="PNG")
    output.seek(0)
    return output

async def process_end_game(ctx: Interaction, game, followup: bool = False):
    games.pop(ctx.channel_id, None)
    game.dealer_draw()
    lines = game.distribute_pot()
    game.end_game()
    d_val = game.hand_value(game.dealer_hand)
    # Generate dealer final hand image
    dealer_img_bytes = generate_hand_image(game.dealer_hand)
    embed = Embed(
        title="🏁 Blackjack — Game Over",
        color=COLOR_SUCCESS,
        description="**Dealer's final hand:**"
    )
    if dealer_img_bytes is not None:
        embed.set_image(url="attachment://dealer_final.png")
    embed.add_field(name="🎯 Results", value="\n".join(lines), inline=False)
    embed.add_field(name="💰 Updated Balances", value="\n".join(
        f"<@{p.user_id}>: {balances[str(p.user_id)]} chips" for p in game.players
    ), inline=False)
    if followup:
        if dealer_img_bytes:
            await ctx.followup.send(embed=embed, file=nextcord.File(dealer_img_bytes, filename="dealer_final.png"))
        else:
            await ctx.followup.send(embed=embed)
    else:
        if dealer_img_bytes:
            await ctx.response.send_message(embed=embed, file=nextcord.File(dealer_img_bytes, filename="dealer_final.png"))
        else:
            await ctx.response.send_message(embed=embed)

##############################
# INTERACTIVE GAME ACTIONS 
##############################

# JoinDealView provides Join and Deal buttons; it updates the embed live with joined players and disables buttons after dealing.
class JoinDealView(nextcord.ui.View):
    def __init__(self, game, host_id):
        super().__init__(timeout=180)
        self.game = game
        self.host_id = host_id
        self.joined_players = [f"<@{host_id}>"]  # Host is the first player

    @nextcord.ui.button(label="Join Game", style=nextcord.ButtonStyle.primary, custom_id="join_game")
    async def join(self, button: nextcord.ui.Button, interaction: Interaction):
        user_id = str(interaction.user.id)
        if any(p.user_id == interaction.user.id for p in self.game.players):
            await interaction.response.send_message("You have already joined!", ephemeral=True)
            return
        # For simplicity, set a default bet of 100 chips
        bet = 100
        if balances.get(user_id, DEFAULT_BALANCE) < bet:
            await interaction.response.send_message("Not enough chips to join.", ephemeral=True)
            return
        balances[user_id] -= bet
        self.game.add_player(int(user_id), bet)
        self.joined_players.append(f"<@{user_id}>")
        # Update embed description with current pot and joined players
        new_description = (
            f"💰 **Pot:** {self.game.pot} chips\n"
            "Players Joined: " + ", ".join(self.joined_players) + "\n"
            "Host, click **Deal Cards** when ready."
        )
        embed = interaction.message.embeds[0]
        embed.description = new_description
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message("You have joined the game!", ephemeral=True)

    @nextcord.ui.button(label="Deal Cards", style=nextcord.ButtonStyle.success, custom_id="deal_cards")
    async def deal(self, button: nextcord.ui.Button, interaction: Interaction):
        if interaction.user.id != self.host_id:
            await interaction.response.send_message("Only the host can deal the cards.", ephemeral=True)
            return
        # Disable both buttons
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        # Call the blackjack_deal logic (simulate a slash command call)
        await blackjack_deal(interaction)

# When players view their hand, they see a simplified message and the card image.
class HandOptionsView(nextcord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @nextcord.ui.button(label="👁️ View My Hand", style=nextcord.ButtonStyle.secondary, custom_id="view_my_hand")
    async def view_hand(self, button: nextcord.ui.Button, interaction: Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return
        player = next((p for p in game.players if p.user_id == interaction.user.id), None)
        if not player:
            await interaction.response.send_message("You're not in this game.", ephemeral=True)
            return
        hand_val = game.hand_value(player.hand)
        img_bytes = generate_hand_image(player.hand)
        if img_bytes is None:
            await interaction.response.send_message("Error generating hand image.", ephemeral=True)
            return
        file = nextcord.File(fp=img_bytes, filename="hand.png")
        view = PrivatePlayerView()
        # Simplified message text
        await interaction.response.send_message(
            content=f"🂠 Your current hand value: **{hand_val}**",
            file=file,
            view=view,
            ephemeral=True
        )

class HitButton(nextcord.ui.Button):
    def __init__(self):
        super().__init__(label="🃏 Hit", style=nextcord.ButtonStyle.primary, custom_id="hit")

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
        new_val = game.hand_value(player.hand)
        if new_val > 21:
            player.busted = True
            await interaction.response.send_message(
                f"❌ You drew a card and busted with a hand value of {new_val}!",
                ephemeral=True
            )
        else:
            img_bytes = generate_hand_image(player.hand)
            if img_bytes is None:
                await interaction.response.send_message("Error generating hand image.", ephemeral=True)
                return
            file = nextcord.File(fp=img_bytes, filename="hand.png")
            view = PrivatePlayerView()
            await interaction.response.send_message(
                content=f"🂠 Your current hand value: **{new_val}**",
                file=file,
                view=view,
                ephemeral=True
            )
        if game.all_players_done():
            await process_end_game(interaction, game, followup=True)

class StandButton(nextcord.ui.Button):
    def __init__(self):
        super().__init__(label="✋ Stand", style=nextcord.ButtonStyle.success, custom_id="stand")

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
    embed = Embed(
        description="💰 Your balance has been replenished to **100 chips**!",
        color=0x27AE60
    )
    await ctx.response.send_message(embed=embed, ephemeral=True)

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
    view = JoinDealView(game, ctx.user.id)
    initial_description = (
        f"💰 **Pot:** {game.pot} chips\n"
        f"Players Joined: <@{ctx.user.id}>\n"
        "Click **Join Game** to participate.\n"
        "Click **Deal Cards** when ready to play."
    )
    embed = Embed(
        title="🃏 New Blackjack Game Started!",
        description=initial_description,
        color=COLOR_INFO
    )
    msg = await ctx.response.send_message(embed=embed, view=view)
    # Optionally, store msg in game (e.g., game.message = msg) for live updates

@bot.slash_command(description="Deal cards. The dealer's first card is revealed publicly as an image.")
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
    # Generate image for dealer's first card
    dealer_first_img = generate_hand_image([game.dealer_hand[0]])
    file = None
    if dealer_first_img:
        file = nextcord.File(fp=dealer_first_img, filename="dealer_first.png")
    view = HandOptionsView()
    # Updated embed: Show dealer's first card image first, then instructions
    embed = Embed(
        title="🃏 Blackjack — Cards Dealt!",
        description="**Dealer's first card:**",
        color=COLOR_PRIMARY
    )
    if file:
        embed.set_image(url="attachment://dealer_first.png")
    embed.add_field(name="\u200b", value="Click **View My Hand** below to see your cards, then choose **Hit** or **Stand**.", inline=False)
    await ctx.response.send_message(embed=embed, view=view, file=file)

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
        description=(
            "Use the commands below to start, join, and play Blackjack.\n"
            "After dealing, click **View My Hand** to reveal your cards, then choose **Hit** or **Stand**."
        ),
        color=COLOR_HELP
    )
    embed.add_field(name="/ping", value="Check if the bot is online.", inline=False)
    embed.add_field(name="/blackjack_start <bet>", value="Start a new game with a bet (min: 10, max: 500).", inline=False)
    embed.add_field(name="/blackjack_join <bet>", value="Join an active game before cards are dealt.", inline=False)
    embed.add_field(name="/blackjack_deal", value="Deal cards. The dealer's first card is shown publicly as an image and interactive buttons will appear.", inline=False)
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
    embed = Embed(title="🏆 Blackjack Leaderboard", color=COLOR_LEADER)
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
