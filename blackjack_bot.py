from dotenv import load_dotenv
import os
import random
import json
import asyncio
import nextcord
from nextcord.ext import commands
from nextcord import Interaction, Embed, AllowedMentions
import logging
from PIL import Image
from io import BytesIO

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

##############################
# CONSTANTS & LOGGING SETUP
##############################
DEFAULT_BALANCE = 1000
DATA_FILE = "balances.json"
logging.basicConfig(level=logging.INFO)

COLOR_PRIMARY   = 0x5865F2
COLOR_SUCCESS   = 0x2ECC71
COLOR_INFO      = 0x3498DB
COLOR_ACCENT    = 0x9B59B6
COLOR_HELP      = 0x00FF00
COLOR_LEADER    = 0xF1C40F

CARD_FOLDER = "cards"
CARD_WIDTH = 70
CARD_SPACING = 10

suit_map = {"♠": "spades", "♥": "hearts", "♦": "diamonds", "♣": "clubs"}
rank_map = {
    "A": "ace", "2": "2", "3": "3", "4": "4", "5": "5",
    "6": "6", "7": "7", "8": "8", "9": "9", "10": "10",
    "J": "jack", "Q": "queen", "K": "king"
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
                super().__init__({str(k): v for k, v in data.items()})
            except json.JSONDecodeError:
                logging.error("JSON decode error, starting fresh.")
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
    return ", ".join([f"{r}{s}" for r, s in hand])

def generate_hand_image(hand, card_folder=CARD_FOLDER, card_width=CARD_WIDTH, spacing=CARD_SPACING):
    images = []
    for rank, suit in hand:
        filename = f"{rank_map[rank]}_of_{suit_map[suit]}.png"
        path = os.path.join(card_folder, filename)
        try:
            img = Image.open(path).convert("RGBA")
        except FileNotFoundError:
            logging.error(f"Missing file: {path}")
            continue
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

##############################
# GAME END LOGIC
##############################
async def process_end_game(ctx: Interaction, game, followup: bool = False):
    """Send two messages so the final image appears above the text:

       1) Dealer's final hand image alone
       2) Results + Updated Balances
    """
    games.pop(ctx.channel_id, None)
    game.dealer_draw()
    lines = game.distribute_pot()
    game.end_game()

    d_val = game.hand_value(game.dealer_hand)
    dealer_img_bytes = generate_hand_image(game.dealer_hand)

    # (1) Dealer final hand image embed
    if dealer_img_bytes:
        embed_image = Embed(
            title="🏁 Blackjack — Game Over",
            description=f"**Dealer's final hand (Value: {d_val}):**",
            color=COLOR_SUCCESS
        )
        file = nextcord.File(dealer_img_bytes, filename="dealer_final.png")
        embed_image.set_image(url="attachment://dealer_final.png")
        if followup:
            await ctx.followup.send(embed=embed_image, file=file)
        else:
            await ctx.response.send_message(embed=embed_image, file=file)
    else:
        embed_image = Embed(
            title="🏁 Blackjack — Game Over",
            description=f"**Dealer's final hand (Value: {d_val}):**\n(No image found)",
            color=COLOR_SUCCESS
        )
        if followup:
            await ctx.followup.send(embed=embed_image)
        else:
            await ctx.response.send_message(embed=embed_image)

    # (2) Results + updated balances
    desc_parts = []
    if lines:
        desc_parts.append("**Results**")
        desc_parts.append("\n".join(lines))

    balance_lines = [f"<@{p.user_id}>: {balances[str(p.user_id)]} chips" for p in game.players]
    if balance_lines:
        desc_parts.append("**Updated Balances**")
        desc_parts.append("\n".join(balance_lines))

    final_desc = "\n".join(desc_parts) if desc_parts else "No results."
    embed_final = Embed(description=final_desc, color=COLOR_SUCCESS)

    if followup:
        await ctx.followup.send(embed=embed_final)
    else:
        await ctx.response.send_message(embed=embed_final)

##############################
# CLASSES & VIEWS
##############################
class PlayerState:
    def __init__(self, user_id: int, bet: int):
        self.user_id = user_id
        self.hand = []
        self.busted = False
        self.stood = False
        self.bet = bet

    def is_done(self):
        return self.busted or self.stood

class BlackjackGame:
    def __init__(self, host_id: int):
        self.host_id = host_id
        self.players = []
        self.dealer_hand = []
        self.deck = self._make_deck()
        random.shuffle(self.deck)
        self.game_active = True
        self.game_over = False
        self.pot = 0
        self.dealt_cards = False
        self.lock = asyncio.Lock()  # concurrency lock

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
            else:
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
# 1) Modal for custom bet
##############################
class BetModal(nextcord.ui.Modal):
    """A simple modal that asks the user to input their bet."""
    def __init__(self, game):
        super().__init__("Join the Game")
        self.game = game
        # Single text input for the bet
        self.bet_input = nextcord.ui.TextInput(
            label="Enter your bet",
            placeholder="Between 10 and 500"
        )
        self.add_item(self.bet_input)

    async def on_submit(self, interaction: Interaction):
        user_id = str(interaction.user.id)
        # parse bet
        try:
            bet = int(self.bet_input.value)
        except ValueError:
            await interaction.response.send_message("Invalid bet, must be a number.", ephemeral=True)
            return
        if bet < 10 or bet > 500:
            await interaction.response.send_message("Bet must be between 10 and 500.", ephemeral=True)
            return
        bal = balances.get(user_id, DEFAULT_BALANCE)
        if bal < bet:
            await interaction.response.send_message("Not enough chips to join!", ephemeral=True)
            return

        # Subtract bet from user
        balances[user_id] = bal - bet
        self.game.add_player(int(user_id), bet)

        # Single public message
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} joined the game with a bet of {bet} chips!",
            ephemeral=False
        )

##############################
# 2) View with "Join Game" + "Deal Cards"
##############################
class JoinDealView(nextcord.ui.View):
    """A view that has both 'Join Game' and 'Deal Cards' buttons."""
    def __init__(self, game, host_id):
        super().__init__(timeout=180)
        self.game = game
        self.host_id = host_id

    @nextcord.ui.button(label="Join Game", style=nextcord.ButtonStyle.primary, custom_id="join_game")
    async def join_game(self, button: nextcord.ui.Button, interaction: Interaction):
        """Open a modal so the user can enter a custom bet."""
        if self.game.dealt_cards:
            await interaction.response.send_message("Cards already dealt, can't join now!", ephemeral=True)
            return

        await interaction.response.send_modal(BetModal(self.game))

    @nextcord.ui.button(label="Deal Cards", style=nextcord.ButtonStyle.success, custom_id="deal_cards")
    async def deal_cards(self, button: nextcord.ui.Button, interaction: Interaction):
        """Host deals the cards once everyone has joined."""
        if interaction.user.id != self.host_id:
            await interaction.response.send_message("Only the host can deal the cards.", ephemeral=True)
            return
        if self.game.dealt_cards:
            await interaction.response.send_message("Cards already dealt!", ephemeral=True)
            return
        if len(self.game.players) == 0:
            await interaction.response.send_message("No players have joined!", ephemeral=True)
            return

        self.game.deal_initial_cards()

        d_val = self.game.hand_value([self.game.dealer_hand[0]])
        dealer_first_img = generate_hand_image([self.game.dealer_hand[0]])
        file = None
        if dealer_first_img:
            file = nextcord.File(dealer_first_img, filename="dealer_first.png")

        embed = Embed(
            title="🃏 Blackjack — Cards Dealt!",
            description=f"**Dealer's first card (Value: {d_val}):**",
            color=COLOR_PRIMARY
        )
        if file:
            embed.set_image(url="attachment://dealer_first.png")

        # disable both buttons
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        await interaction.response.send_message(embed=embed, file=file)

        instructions = "Click **View My Hand** below to see your cards, then choose **Hit** or **Stand**."
        view = HandOptionsView()
        await interaction.followup.send(content=instructions, view=view, ephemeral=False)

##############################
# HAND OPTIONS VIEW
##############################
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

        val = game.hand_value(player.hand)
        img_bytes = generate_hand_image(player.hand)
        if img_bytes is None:
            await interaction.response.send_message("Error generating hand image.", ephemeral=True)
            return

        file = nextcord.File(fp=img_bytes, filename="hand.png")
        private_view = PrivatePlayerView(player.user_id)
        await interaction.response.send_message(
            content=f"🂠 Your current hand value: **{val}**",
            file=file,
            view=private_view,
            ephemeral=True
        )

##############################
# PRIVATE PLAYER VIEW
##############################
class PrivatePlayerView(nextcord.ui.View):
    def __init__(self, player_id: int):
        super().__init__(timeout=180)
        self.player_id = player_id
        self.add_item(HitButton(player_id))
        self.add_item(StandButton(player_id))

class HitButton(nextcord.ui.Button):
    def __init__(self, player_id: int):
        super().__init__(label="🃏 Hit", style=nextcord.ButtonStyle.primary, custom_id="hit")
        self.player_id = player_id

    async def callback(self, interaction: Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return
        async with game.lock:
            player = next((p for p in game.players if p.user_id == self.player_id), None)
            if not player:
                await interaction.response.send_message("You're not in this game.", ephemeral=True)
                return
            if player.is_done():
                await interaction.response.send_message("You already busted or stood.", ephemeral=True)
                return

            game.draw_card_for_player(player)
            drawn_card = player.hand[-1]
            drawn_card_str = f"{drawn_card[0]}{drawn_card[1]}"
            new_val = game.hand_value(player.hand)

            img_bytes = generate_hand_image(player.hand)
            if new_val > 21:
                player.busted = True
                wait_msg = " Waiting for other players..." if len(game.players) > 1 else ""
                if img_bytes:
                    file = nextcord.File(fp=img_bytes, filename="hand.png")
                    await interaction.response.send_message(
                        content=f"❌ You drew **{drawn_card_str}** and busted with **{new_val}**!{wait_msg}",
                        file=file,
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        f"❌ You drew **{drawn_card_str}** and busted with **{new_val}**!{wait_msg}",
                        ephemeral=True
                    )
            else:
                if img_bytes:
                    file = nextcord.File(fp=img_bytes, filename="hand.png")
                    await interaction.response.send_message(
                        content=f"🂠 Your current hand value: **{new_val}**",
                        file=file,
                        view=PrivatePlayerView(player.user_id),
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        content=f"🂠 Your current hand value: **{new_val}**",
                        view=PrivatePlayerView(player.user_id),
                        ephemeral=True
                    )

            if game.all_players_done():
                await process_end_game(interaction, game, followup=True)

class StandButton(nextcord.ui.Button):
    def __init__(self, player_id: int):
        super().__init__(label="✋ Stand", style=nextcord.ButtonStyle.success)
        self.player_id = player_id

    async def callback(self, interaction: Interaction):
        game = games.get(interaction.channel_id)
        if not game:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return
        async with game.lock:
            player = next((p for p in game.players if p.user_id == self.player_id), None)
            if not player:
                await interaction.response.send_message("You're not in this game.", ephemeral=True)
                return
            if player.is_done():
                await interaction.response.send_message("You already busted or stood.", ephemeral=True)
                return

            player.stood = True
            wait_msg = " Waiting for other players..." if len(game.players) > 1 else ""
            # CHANGED: Make stand message public
            await interaction.response.send_message(
                f"<@{player.user_id}> stands.{wait_msg}",
                ephemeral=False
            )

            if game.all_players_done():
                await process_end_game(interaction, game, followup=True)

##############################
# COMMANDS
##############################
@bot.slash_command(description="Pong!")
async def ping(ctx: Interaction):
    await ctx.response.send_message("Pong!", ephemeral=True)

@bot.slash_command(description="Replenish your balance to 100 if you have 0 chips.")
async def blackjack_replenish(ctx: Interaction):
    user_id = str(ctx.user.id)
    if balances.get(user_id, 0) > 0:
        await ctx.response.send_message("You still have chips!", ephemeral=True)
        return
    balances[user_id] = 100
    embed = Embed(
        title="💰 Blackjack — Replenish",
        description="Your balance has been replenished to **100 chips**!",
        color=0x27AE60
    )
    await ctx.response.send_message(embed=embed, ephemeral=True)

@bot.slash_command(description="Start a new game of Blackjack with a bet (min: 10, max: 500).")
async def blackjack_start(ctx: Interaction, bet: int):
    if ctx.channel_id in games:
        await ctx.response.send_message("A game is already in progress!", ephemeral=True)
        return
    if bet < 10 or bet > 500:
        await ctx.response.send_message("Bet must be between 10 and 500!", ephemeral=True)
        return
    user_id = str(ctx.user.id)
    balances.setdefault(user_id, DEFAULT_BALANCE)
    if balances[user_id] <= 0:
        await ctx.response.send_message("You have 0 chips. Use /blackjack_replenish first.", ephemeral=True)
        return
    if bet > balances[user_id]:
        await ctx.response.send_message(f"You only have {balances[user_id]} chips!", ephemeral=True)
        return

    game = BlackjackGame(ctx.user.id)
    games[ctx.channel_id] = game
    balances[user_id] -= bet
    game.add_player(int(user_id), bet)

    desc = (
        f"💰 **Pot:** {game.pot} chips\n"
        f"Players Joined: <@{ctx.user.id}>\n"
        "Click **Join Game** to open a bet modal.\n"
        "Host, click **Deal Cards** when ready."
    )
    embed = Embed(
        title="🃏 Blackjack — New Game Started!",
        description=desc,
        color=COLOR_INFO
    )
    # NEW: The view with "Join Game" & "Deal Cards" 
    view = JoinDealView(game, ctx.user.id)
    await ctx.response.send_message(embed=embed, view=view)

@bot.slash_command(description="Manually end the game.")
async def blackjack_end(ctx: Interaction):
    game = games.pop(ctx.channel_id, None)
    if not game:
        await ctx.response.send_message("No game is active.", ephemeral=True)
        return
    if ctx.user.id != game.host_id and not any(p.user_id == ctx.user.id for p in game.players):
        await ctx.response.send_message("You are not part of this game, so you cannot end it.", ephemeral=True)
        return
    await process_end_game(ctx, game, followup=False)

@bot.slash_command(description="Get help using this bot.")
async def help(ctx: Interaction):
    embed = Embed(
        title="❓ Blackjack — Help",
        description=(
            "Use the commands below to start, join, and play Blackjack.\n"
            "After starting a game with `/blackjack_start <bet>`, other players can click **Join Game** to enter their own bet, "
            "then the host can deal cards. Once cards are dealt, click **View My Hand** to reveal your cards, then choose **Hit** or **Stand**."
        ),
        color=COLOR_HELP
    )
    embed.add_field(name="/ping", value="Check if the bot is online.", inline=False)
    embed.add_field(name="/blackjack_start <bet>", value="Start a new game with a bet (10–500).", inline=False)
    embed.add_field(name="/blackjack_replenish", value="Replenish chips to 100 if you have 0.", inline=False)
    embed.add_field(name="/blackjack_end", value="Manually end the current game (host or participant only).", inline=False)
    embed.add_field(name="/blackjack_leaderboard", value="See top players and their balances.", inline=False)
    await ctx.response.send_message(embed=embed)

@bot.slash_command(description="See the top 15 players and their chip balances.")
async def blackjack_leaderboard(ctx: Interaction):
    if not balances:
        await ctx.response.send_message("No one has a balance yet!", ephemeral=True)
        return

    sorted_bal = sorted(balances.items(), key=lambda x: x[1], reverse=True)[:15]
    embed = Embed(title="🏆 Blackjack — Leaderboard", color=COLOR_LEADER)

    for rank, (uid, bal) in enumerate(sorted_bal, 1):
        member = ctx.guild.get_member(int(uid))
        if member:
            # same style as results embed => <@ID>, else fallback
            name_str = f"<@{uid}>"
        else:
            name_str = f"User {uid}"

        formatted_bal = f"{bal:,}"

        if rank == 1:
            medal = "🥇"
        elif rank == 2:
            medal = "🥈"
        elif rank == 3:
            medal = "🥉"
        else:
            medal = f"#{rank}"

        embed.add_field(
            name=f"{medal} {name_str}",
            value=f"{formatted_bal} chips",
            inline=False
        )

    # allow mention parsing
    await ctx.response.send_message(
        embed=embed,
        allowed_mentions=AllowedMentions(users=True)
    )

##############################
# BOT READY & RUN
##############################
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

bot.run(token)
