import discord
from discord.ext import commands, tasks
from discord import app_commands
import os 
from dotenv import load_dotenv
import json
import time
import random
import io
import contextlib
import textwrap 
import aiofiles 
import threading
from datetime import datetime, timedelta, timezone
import uuid
import firebase_admin
from firebase_admin import credentials, firestore
from keep_alive import keep_alive

# Load environment variables from .env file
load_dotenv()

# --- Global Firebase Client ---
DB = None # Global Firestore client reference
# ------------------------------

# --- Database File Names (Used for other JSON data) ---
LEVELS_FILE = 'levels.json'
GIVEAWAYS_FILE = 'giveaways.json'
CONFIG_FILE = 'config.json' 
USER_CACHE_FILE = 'user_cache.json'
# ---------------------------

# --- Configuration and In-Memory Storage ---
LEVELS_DB = {} 
ACTIVE_GIVEWAYS = {} 
GIVEAWAY_MESSAGES = {} 
CONFIG_DB = {} 
USER_CACHE = {} 
LICENSE_DB = {} # Storage for licenses, loaded/saved via Firestore
BOT_OWNER_ID = 1356850034993397781 # REPLACE THIS WITH YOUR ACTUAL USER ID
USER_CACHE_LOCK = threading.Lock() 
BOT_START_TIME = time.time() 
# ----------------------------------------------------------------------

# ==============================================================================
# FIREBASE PERSISTENCE FUNCTIONS
# ==============================================================================

def initialize_firestore():
    """Initializes the Firebase connection using a secure environment variable."""
    print("--- Starting Firebase Initialization Check ---")
    global DB
    
    json_creds_string = os.environ.get('FIREBASE_CREDENTIALS')
    
    if not json_creds_string:
        print("FATAL ERROR: FIREBASE_CREDENTIALS environment variable not found. Persistence is DISABLED.")
        return

    try:
        creds_dict = json.loads(json_creds_string)
        cred = credentials.Certificate(creds_dict)
        
        if not firebase_admin._app:
             firebase_admin.initialize_app(cred)
        
        DB = firestore.client()
        print("✅ Successfully initialized Firebase Firestore client.")
    except Exception as e:
        print(f"FATAL ERROR: Could not initialize Firebase. Check FIREBASE_CREDENTIALS format. Error: {e}")
        DB = None
    print("--- Firebase Initialization Check Complete ---")

async def load_licenses_from_firestore():
    """Loads all licenses from Firestore into the in-memory LICENSE_DB."""
    global LICENSE_DB
    if DB is None:
        print("WARNING: Cannot load licenses from Firestore. DB not initialized.")
        return

    try:
        licenses_ref = DB.collection('licenses')
        docs = licenses_ref.stream()
        
        count = 0
        for doc in docs:
            LICENSE_DB[doc.id] = doc.to_dict()
            count += 1
            
        print(f"Loaded {count} license keys from Firestore.")
    except Exception as e:
        print(f"ERROR: Failed to load licenses from Firestore: {e}")

def save_license_to_firestore(license_key: str, license_data: dict):
    """Saves a single license key's data to Firestore."""
    if DB is None:
        print("WARNING: Cannot save license. DB not initialized.")
        return False
    
    try:
        licenses_ref = DB.collection('licenses')
        licenses_ref.document(license_key).set(license_data)
        return True
    except Exception as e:
        print(f"ERROR: Failed to save license {license_key} to Firestore: {e}")
        return False

# ==============================================================================
# JSON File Persistence Functions (for other data)
# ==============================================================================

def load_data():
    """Loads all data (LEVELS_DB, ACTIVE_GIVEWAYS, CONFIG_DB, USER_CACHE) from JSON files."""
    global LEVELS_DB, ACTIVE_GIVEWAYS, CONFIG_DB, USER_CACHE
    
    if os.path.exists(LEVELS_FILE):
        try:
            with open(LEVELS_FILE, 'r') as f:
                LEVELS_DB = {int(k): v for k, v in json.load(f).items()}
            print(f"Loaded {len(LEVELS_DB)} user levels.")
        except Exception as e:
            print(f"Error loading {LEVELS_FILE}: {e}")
            LEVELS_DB = {}

    if os.path.exists(GIVEAWAYS_FILE):
        try:
            with open(GIVEAWAYS_FILE, 'r') as f:
                ACTIVE_GIVEWAYS = {int(k): v for k, v in json.load(f).items()}
            print(f"Loaded {len(ACTIVE_GIVEWAYS)} active giveaways.")
        except Exception as e:
            print(f"Error loading {GIVEAWAYS_FILE}: {e}")
            ACTIVE_GIVEWAYS = {}

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                CONFIG_DB = {int(k): v for k, v in json.load(f).items()}
            print(f"Loaded config data.")
        except Exception as e:
            print(f"Error loading {CONFIG_FILE}: {e}")
            CONFIG_DB = {}

    if os.path.exists(USER_CACHE_FILE):
        try:
            with open(USER_CACHE_FILE, 'r') as f:
                USER_CACHE = json.load(f) 
            print(f"Loaded {len(USER_CACHE)} user names from cache.")
        except Exception as e:
            print(f"Error loading {USER_CACHE_FILE}: {e}")
            USER_CACHE = {}


def save_data(data_type: str):
    """Saves the specified data structure to its corresponding JSON file."""
    if data_type == 'levels':
        data_to_save = LEVELS_DB
        file_name = LEVELS_FILE
    elif data_type == 'giveaways':
        data_to_save = ACTIVE_GIVEWAYS
        file_name = GIVEAWAYS_FILE
    elif data_type == 'config':
        data_to_save = {str(k): v for k, v in CONFIG_DB.items()}
        file_name = CONFIG_FILE
    else:
        return

    try:
        with open(file_name, 'w') as f: 
            json.dump(data_to_save, f, indent=4)
    except Exception as e:
        print(f"Error saving {file_name}: {e}")

async def save_user_cache():
    """Saves the USER_CACHE dictionary to a JSON file asynchronously."""
    with USER_CACHE_LOCK:
        cache_copy = USER_CACHE.copy()
        
    try:
        async with aiofiles.open(USER_CACHE_FILE, 'w') as f:
            await f.write(json.dumps(cache_copy, indent=4)) 
    except Exception as e:
        print(f"Error saving user cache: {e}")

# ==============================================================================
# Helper Functions (omitted for brevity, assume presence)
# ==============================================================================

# NOTE: The helper functions (format_uptime, is_guild_premium, update_user_cache)
# are assumed to be present as defined in the previous response's context.

def format_uptime(seconds):
    """Converts seconds into a human-readable string (e.g., '1 day, 2 hours, 30 minutes')."""
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0 or not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
        
    return ", ".join(parts)


def is_guild_premium(guild_id: int):
    """Checks if a guild has active, non-expired premium status."""
    guild_config = CONFIG_DB.get(guild_id, {})
    premium_info = guild_config.get('premium', {})
    
    if not premium_info or not premium_info.get('active', False):
        return False, None

    expires_ts = premium_info.get('expires_at')
    
    if expires_ts == "LIFETIME":
        return True, "LIFETIME"
    
    try:
        expires_ts = int(expires_ts)
        if expires_ts > time.time():
            return True, expires_ts
        else:
            return False, expires_ts 
    except (TypeError, ValueError):
        return False, None

async def update_user_cache(bot, user_id: int):
    """Fetches a user and updates the in-memory and file cache."""
    global USER_CACHE
    user_id_str = str(user_id)
    
    if user_id_str in USER_CACHE:
        return
        
    try:
        user = bot.get_user(user_id) 
        if user is None:
            user = await bot.fetch_user(user_id) 
            
        username = user.global_name if user.global_name else user.name
        
        with USER_CACHE_LOCK:
            USER_CACHE[user_id_str] = username
        
        await save_user_cache() 
        
    except discord.NotFound:
        with USER_CACHE_LOCK:
            USER_CACHE[user_id_str] = f"Unknown User ({user_id_str})"
        await save_user_cache() 
        print(f"Could not fetch user {user_id}: User Not Found.")
    except Exception as e:
        with USER_CACHE_LOCK:
            USER_CACHE[user_id_str] = f"Unknown User ({user_id_str})"
        await save_user_cache() 
        print(f"Could not fetch user {user_id}: {e}")

# ==============================================================================
# Bot Setup
# ==============================================================================

intents = discord.Intents.default()
intents.members = True 
intents.message_content = True 

bot = commands.Bot(command_prefix='!', intents=intents)

async def setup_hook():
    """Load Cogs, ensure persistence files exist, and then sync commands."""
    
    print("Loading existing data from JSON files...")
    load_data() 

    # Immediate File Creation for Persistence Checks
    if not os.path.exists(LEVELS_FILE):
        save_data('levels')
        print(f"Created initial empty {LEVELS_FILE}.")
    if not os.path.exists(GIVEAWAYS_FILE):
        save_data('giveaways')
        print(f"Created initial empty {GIVEAWAYS_FILE}.")
    if not os.path.exists(CONFIG_FILE):
        save_data('config')
        print(f"Created initial empty {CONFIG_FILE}.")
    
    if not os.path.exists(USER_CACHE_FILE): 
        await save_user_cache()
        print(f"Created initial empty {USER_CACHE_FILE}.")

    print("Loading Cogs...")
    try:
        await bot.add_cog(LevelingCog(bot))
        await bot.add_cog(GiveawayCog(bot))
        await bot.add_cog(UtilityCog(bot)) 
        await bot.add_cog(LicenseCog(bot))
        print("Cogs Loaded successfully.")
    except Exception as e:
        print(f"Failed to load a Cog: {e}")
    
    try:
        synced = await bot.tree.sync()
        print(f"Successfully synced {len(synced)} command(s) to Discord.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

bot.setup_hook = setup_hook

@bot.event
async def on_ready():
    """Initializes the bot and loads data."""
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    
    initialize_firestore()
    await load_licenses_from_firestore()
    
    print('Bot is ready to accept commands.')

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await interaction.response.send_message(
            f"You do not have the required permission to use this command: `{error.missing_permissions[0]}`", 
            ephemeral=True
        )
    elif isinstance(error, commands.MissingRequiredArgument):
        await interaction.response.send_message(
            f"Missing argument. Usage: `/{interaction.command.name} {interaction.command.usage}`", 
            ephemeral=True
        )
    else:
        print(f"An unexpected error occurred: {error}")
        await interaction.response.send_message("An unexpected error occurred while executing the command.", ephemeral=True)

# ==============================================================================
# Cogs
# ==============================================================================

class LevelingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        user_id = str(message.author.id)
        if user_id not in LEVELS_DB:
            LEVELS_DB[user_id] = {'xp': 0, 'level': 0}
        
        LEVELS_DB[user_id]['xp'] += random.randint(15, 25)
        
        required_xp = (LEVELS_DB[user_id]['level'] + 1) * 100
        if LEVELS_DB[user_id]['xp'] >= required_xp:
            LEVELS_DB[user_id]['level'] += 1
            LEVELS_DB[user_id]['xp'] = 0
            # await message.channel.send(f"🎉 Congrats {message.author.mention}, you reached level {LEVELS_DB[user_id]['level']}!")

        save_data('levels')

    @app_commands.command(name="rank", description="Shows a user's current level and XP.")
    async def rank_command(self, interaction: discord.Interaction, user: discord.Member = None):
        user = user or interaction.user
        user_id_str = str(user.id)
        
        data = LEVELS_DB.get(user_id_str, {'xp': 0, 'level': 0})
        
        embed = discord.Embed(
            title=f"Level Rank for {user.display_name}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Level", value=data['level'], inline=True)
        embed.add_field(name="XP", value=data['xp'], inline=True)
        
        await interaction.response.send_message(embed=embed)


class GiveawayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    @app_commands.command(name="giveaway_start", description="Starts a new giveaway.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_start(self, interaction: discord.Interaction, prize: str, duration: int, winner_count: int):
        
        end_time = time.time() + (duration * 60) # duration in minutes
        end_dt = datetime.fromtimestamp(end_time, tz=timezone.utc)
        
        embed = discord.Embed(
            title=f"🎉 Giveaway: {prize} 🎉",
            description=f"React with 🎉 to enter!\nWinners: **{winner_count}**\nEnds: <t:{int(end_time)}:R> (<t:{int(end_time)}:F>)",
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"Hosted by {interaction.user.display_name}")
        
        await interaction.response.send_message(embed=embed)
        giveaway_message = await interaction.original_response()
        await giveaway_message.add_reaction("🎉")

        ACTIVE_GIVEWAYS[giveaway_message.id] = {
            'channel_id': interaction.channel_id,
            'end_time': end_time,
            'prize': prize,
            'winner_count': winner_count,
            'host_id': interaction.user.id
        }
        save_data('giveaways')

    @tasks.loop(minutes=1)
    async def check_giveaways(self):
        
        current_time = time.time()
        expired_giveaways = [mid for mid, data in ACTIVE_GIVEWAYS.items() if data['end_time'] <= current_time]
        
        for message_id in expired_giveaways:
            data = ACTIVE_GIVEWAYS.pop(message_id)
            save_data('giveaways')
            
            channel = self.bot.get_channel(data['channel_id'])
            if not channel:
                continue

            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                continue

            users = set()
            reaction = discord.utils.get(message.reactions, emoji='🎉')
            if reaction:
                async for user in reaction.users():
                    if not user.bot:
                        users.add(user)
            
            participants = list(users)
            
            if not participants:
                final_message = "😢 Giveaway ended! No one entered the giveaway."
            else:
                num_winners = min(data['winner_count'], len(participants))
                winners = random.sample(participants, num_winners)
                winner_mentions = ", ".join([w.mention for w in winners])
                
                final_message = (
                    f"🎉 **GIVEAWAY ENDED!** 🎉\n"
                    f"Prize: **{data['prize']}**\n"
                    f"Winners ({num_winners}): {winner_mentions}!"
                )
            
            await channel.send(final_message, reference=message)


class UtilityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def is_owner():
        """A simple check to confirm the user is the bot owner."""
        async def predicate(interaction: discord.Interaction) -> bool:
            return interaction.user.id == BOT_OWNER_ID
        return app_commands.check(predicate)

    @app_commands.command(name="ping", description="Shows the bot's latency.")
    async def ping_command(self, interaction: discord.Interaction):
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! Latency is **{latency_ms}ms**.", ephemeral=True)

    @app_commands.command(name="uptime", description="Shows how long the bot has been running.")
    async def uptime_command(self, interaction: discord.Interaction):
        uptime_seconds = time.time() - BOT_START_TIME
        uptime_str = format_uptime(uptime_seconds)
        await interaction.response.send_message(f"Bot Uptime: **{uptime_str}**", ephemeral=True)

    @app_commands.command(name="eval", description="Executes Python code (Owner only).")
    @is_owner()
    async def eval_command(self, interaction: discord.Interaction, code: str):
        
        code_block = textwrap.indent(code, '  ')
        
        env = {
            'bot': self.bot,
            'interaction': interaction,
            'channel': interaction.channel,
            'author': interaction.user,
            'guild': interaction.guild,
            'commands': commands,
            'discord': discord,
            'DB': DB,
        }
        
        stdout = io.StringIO()
        
        try:
            with contextlib.redirect_stdout(stdout):
                exec(
                    f'async def func():\n{code_block}', 
                    env
                )
                result = await env['func']()
                output = stdout.getvalue()
        
        except Exception as e:
            output = f'```py\n{e.__class__.__name__}: {e}```'
        else:
            if result is not None:
                output += f'```py\n{result}```'
            elif output:
                output = f'```py\n{output}```'
            else:
                output = '```py\nExecuted successfully with no output.```'

        await interaction.response.send_message(
            f"**Evaluation Complete**:\n{output}",
            ephemeral=True
        )


class LicenseCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="license_generate", description="Generates a new premium license key (Admin only).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def generate_license_command(self, interaction: discord.Interaction, months: int):
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        global DB 
        if DB is None:
            await interaction.followup.send("❌ **Database not connected**. Cannot generate license. Check the bot console logs for details.", ephemeral=True)
            return

        # 1. Generate unique key
        license_key = str(uuid.uuid4()).upper().replace('-', '')[:16]
        
        # 2. Calculate expiration timestamp
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30*months)).timestamp()
        
        license_data = {
            'months': months,
            'created_by': interaction.user.id,
            'created_at': time.time(),
            'expires_at': expires_at,
            'is_used': False,
            'used_by_guild': None,
            'used_by_user': None
        }
        
        # 3. Save to Firestore 
        success = save_license_to_firestore(license_key, license_data)
        
        if success:
            # 4. Also update in-memory cache for immediate use
            LICENSE_DB[license_key] = license_data
            
            await interaction.followup.send(
                f"✅ License Key Generated for **{months} months**:\n"
                f"```\n{license_key}```\n"
                f"Expires: <t:{int(expires_at)}:F>", 
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Failed to save license to the database. Check logs.", ephemeral=True)
    
    @app_commands.command(name="license_activate", description="Activates a premium license key for this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def activate_license_command(self, interaction: discord.Interaction, key: str):
         await interaction.response.send_message(f"Activation logic for key `{key}` is pending implementation. Check `license_generate` to confirm the database connection is working.", ephemeral=True)

# ==============================================================================
# Bot Run Block
# ==============================================================================

if __name__ == "__main__":
    
    keep_alive() # Start the web server in a separate thread

    bot_token = os.environ.get("DISCORD_TOKEN")
    if not bot_token:
        print("ERROR: DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(bot_token)