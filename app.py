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
from firebase_admin import credentials, firestore, exceptions
from keep_alive import keep_alive

# Load environment variables from .env file
load_dotenv()

# --- Global Firebase Client ---
DB = None 
# ------------------------------

# --- Database File Names (Used for other JSON data) ---
LEVELS_FILE = 'levels.json'
GIVEAWAYS_FILE = 'giveaways.json'
CONFIG_FILE = 'config.json' 
USER_CACHE_FILE = 'user_cache.json'
LICENSE_FILE = 'licenses.json'
# ---------------------------

# --- Configuration and In-Memory Storage ---
LEVELS_DB = {} 
ACTIVE_GIVEWAYS = {} 
GIVEAWAY_MESSAGES = {} 
CONFIG_DB = {} 
USER_CACHE = {} 
LICENSE_DB = {} 
BOT_OWNER_ID = 1356850034993397781
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
        # Step 1: Parse Credentials (Check the credentials format first)
        creds_dict = json.loads(json_creds_string)
        cred = credentials.Certificate(creds_dict)

        # 🔑 CRITICAL FIX: Use the resilient try/except method to check for initialization.
        try:
            # Check if an app is already initialized. If not, this raises a ValueError.
            firebase_admin.get_app() 
            print("INFO: Firebase app already initialized.")
        except ValueError:
            # If it's not initialized, initialize it now.
            firebase_admin.initialize_app(cred)
            print("INFO: Firebase app initialized for the first time.")
        
        # Step 3: Connect to Firestore
        DB = firestore.client()
        print("✅ Successfully connected to Firebase Firestore client.")
    
    except json.JSONDecodeError:
        print("FATAL ERROR: FIREBASE_CREDENTIALS content is not a valid JSON string. Check formatting.")
        DB = None
    except Exception as e:
        # Catch any other critical initialization errors
        print(f"FATAL ERROR: Could not initialize Firebase. Error: {e}")
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
    
def get_license_from_firestore(license_key: str):
    """Retrieves a single license key's data from Firestore."""
    if DB is None:
        print("WARNING: Cannot get license. DB not initialized.")
        return None
    
    try:
        doc_ref = DB.collection('licenses').document(license_key)
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        print(f"ERROR: Failed to retrieve license {license_key} from Firestore: {e}")
        return None
    
def delete_license_from_firestore(license_key: str):
    """Deletes a license key document from Firestore."""
    if DB is None:
        print("WARNING: Cannot delete license. DB not initialized.")
        return False
    
    try:
        # 1. Delete from Firestore
        DB.collection('licenses').document(license_key).delete()
        
        # 2. Also remove from the in-memory cache
        if license_key in LICENSE_DB:
            del LICENSE_DB[license_key]
            
        return True
    except Exception as e:
        print(f"ERROR: Failed to delete license {license_key} from Firestore: {e}")
        return False

# ==============================================================================
# JSON File Persistence Functions 
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


def save_data(data_type):
    """Saves the specified data (config or licenses) to its corresponding file."""
    if data_type == 'config':
        file_path = CONFIG_FILE
        data_to_save = CONFIG_DB
    elif data_type == 'licenses':
        # Assuming you still save licenses locally for backup, though Firestore is primary
        file_path = LICENSE_FILE
        data_to_save = LICENSE_DB
    else:
        print(f"ERROR: Unknown data type '{data_type}' for saving.")
        return

    try:
        # Use 'w' (write) to overwrite the existing file with the updated data
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, indent=4)
        print(f"INFO: Successfully saved {data_type} data to {file_path}")
    except Exception as e:
        print(f"FATAL ERROR: Failed to save {data_type} data. Error: {e}")

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
# Helper Functions
# ==============================================================================

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
    guild_config = CONFIG_DB.get(str(guild_id), {})
    premium_info = guild_config.get('premium', {})
    
    if not premium_info or not premium_info.get('active', False):
        return False, None

    expires_ts = premium_info.get('expires_at')
    
    if expires_ts == "LIFETIME":
        return True, "LIFETIME"
    
    try:
        expires_ts = float(expires_ts) # Ensure it handles floats/ints from JSON
        if expires_ts > time.time():
            return True, expires_ts
        else:
            # License has expired
            return False, expires_ts 
    except (TypeError, ValueError):
        # Invalid expiration format
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

async def get_automod_rule(guild: discord.Guild, rule_name: str) -> discord.AutoModRule | None:
    """Retrieves an existing AutoMod rule by name, if it exists."""
    try:
        rules = await guild.fetch_automod_rules()
        for rule in rules:
            if rule.name == rule_name:
                return rule
        return None
    except discord.Forbidden:
        print(f"ERROR: Bot lacks 'Manage Guild' permission to fetch AutoMod rules in {guild.name}.")
        return None
    except Exception as e:
        print(f"ERROR: Failed to fetch AutoMod rules: {e}")
        return None

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
        await bot.add_cog(AutoModCog(bot))
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
    """Loads licenses after connection."""
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    
    # We moved initialize_firestore() out, so we only run the async license load here.
    await load_licenses_from_firestore()
    
    print('Bot is ready to accept commands.')

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        # We need to check if the interaction has been responded to or deferred
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"You do not have the required permission to use this command: `{error.missing_permissions[0]}`", 
                ephemeral=True
            )
        else:
             # If deferred, use followup
             await interaction.followup.send(f"You do not have the required permission to use this command: `{error.missing_permissions[0]}`", ephemeral=True)
             
    elif isinstance(error, commands.MissingRequiredArgument):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"Missing argument. Usage: `/{interaction.command.name} {interaction.command.usage}`", 
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"Missing argument. Usage: `/{interaction.command.name} {interaction.command.usage}`", ephemeral=True)
            
    # CRITICAL FIX for the Unknown Interaction error in the error handler itself
    elif isinstance(error, app_commands.errors.CommandInvokeError) and isinstance(error.original, discord.errors.NotFound):
        print(f"Error handler avoided 'Unknown interaction' failure. Original command error was: {error.original}")
        # We can't safely respond here because the token is dead. Do nothing.
        
    else:
        print(f"An unexpected error occurred: {error}")
        # Only attempt to send the message if the interaction hasn't been responded to or deferred
        if not interaction.response.is_done():
            await interaction.response.send_message("An unexpected error occurred while executing the command.", ephemeral=True)
        # If it was deferred, the error is likely happening in the followup, so we can't do anything safely.


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
        
        await interaction.response.defer(thinking=True, ephemeral=False)
        
        end_time = time.time() + (duration * 60) # duration in minutes
        
        embed = discord.Embed(
            title=f"🎉 Giveaway: {prize} 🎉",
            description=f"React with 🎉 to enter!\nWinners: **{winner_count}**\nEnds: <t:{int(end_time)}:R> (<t:{int(end_time)}:F>)",
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"Hosted by {interaction.user.display_name}")
        
        await interaction.followup.send(embed=embed)
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

def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == BOT_OWNER_ID # <-- RELIES ON THIS GLOBAL VARIABLE
    return app_commands.check(predicate)

class UtilityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # The custom is_owner check is now correctly applied here
    @app_commands.command(name="eval", description="Executes Python code (Owner only).")
    @is_owner() 
    async def eval_command(self, interaction: discord.Interaction, code: str):
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Use textwrap.indent on the code to make it runnable inside the async function block
        code_block = textwrap.indent(code, '    ') # Use 4 spaces for Python style
        
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
                # The 'f' must be outside the parentheses for a proper f-string
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

        await interaction.followup.send(
            f"**Evaluation Complete**:\n{output}",
            ephemeral=True
        )


    @app_commands.command(name="ping", description="Shows the bot's latency.")
    async def ping_command(self, interaction: discord.Interaction):
        # NOTE: self.bot is correct here
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! Latency is **{latency_ms}ms**.", ephemeral=True)

    @app_commands.command(name="uptime", description="Shows how long the bot has been running.")
    async def uptime_command(self, interaction: discord.Interaction):
        # NOTE: BOT_START_TIME must be defined globally
        uptime_seconds = time.time() - BOT_START_TIME
        uptime_str = format_uptime(uptime_seconds) # format_uptime must be defined globally
        await interaction.response.send_message(f"Bot Uptime: **{uptime_str}**", ephemeral=True)


    # 🔑 CORRECTED: Moved inside the Cog and using app_commands.command
    @app_commands.command(name="set-status", description="Sets the bot's activity status (Owner only).")
    @app_commands.describe(
        activity_type="The type of activity (Playing, Watching, Listening, Competing).",
        status_text="The text for the bot's status."
    )
    @app_commands.choices(
        activity_type=[
            app_commands.Choice(name="Playing", value=0),
            app_commands.Choice(name="Watching", value=3),
            app_commands.Choice(name="Listening", value=2),
            app_commands.Choice(name="Competing", value=5)
        ]
    )
    @is_owner() # 🔑 Using the custom owner check (or app_commands.checks.is_owner() if you prefer built-in)
    async def set_status_command(self, interaction: discord.Interaction, activity_type: int, status_text: str):
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        activity_map = {
            0: discord.ActivityType.playing,
            3: discord.ActivityType.watching,
            2: discord.ActivityType.listening,
            5: discord.ActivityType.competing
        }
        
        activity = discord.Activity(
            type=activity_map.get(activity_type, discord.ActivityType.playing),
            name=status_text
        )
        
        try:
            # Use self.bot.change_presence() or interaction.client.change_presence()
            await self.bot.change_presence(activity=activity)
            await interaction.followup.send(
                f"✅ Bot status updated to **{activity_map[activity_type].name.title()} {status_text}**.", 
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to set status: {e}", ephemeral=True)

class LicenseCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="license_generate", description="Generates a new premium license key (Admin only).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def generate_license_command(self, interaction: discord.Interaction, months: int):
        
        await interaction.response.defer(thinking=True, ephemeral=True) 
        
        global DB 
        if DB is None:
            await interaction.followup.send("❌ **Database not connected**. Cannot generate license.", ephemeral=True)
            return

        license_key = str(uuid.uuid4()).upper().replace('-', '')[:16]
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
        
        success = save_license_to_firestore(license_key, license_data)
        
        if success:
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
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        key = key.upper().strip()

        if DB is None:
            await interaction.followup.send("❌ **Database not connected**. Activation failed.", ephemeral=True)
            return

        license_data = get_license_from_firestore(key)
        
        if not license_data:
            await interaction.followup.send("❌ **Invalid key**. The provided license key was not found.", ephemeral=True)
            return

        # Validation Checks
        if license_data.get('is_used'):
            if license_data.get('used_by_guild') == interaction.guild_id:
                await interaction.followup.send(f"⚠️ This key is already **active on this server**.", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ This key has already been **used** on another server.", ephemeral=True)
            return
        
        expires_at = license_data.get('expires_at', 0)
        if expires_at < time.time():
            await interaction.followup.send("❌ This key has **expired** and cannot be used.", ephemeral=True)
            return
        
        # Process Activation (Update Firestore)
        guild_id_str = str(interaction.guild_id) # 🔑 Use string ID for CONFIG_DB consistency
        user_id = interaction.user.id
        
        license_data['is_used'] = True
        license_data['used_by_guild'] = interaction.guild_id # Save as int/str based on how you prefer it in Firestore
        license_data['used_by_user'] = user_id
        
        success = save_license_to_firestore(key, license_data)
        
        if not success:
            await interaction.followup.send("❌ **Internal Error**: Failed to update the license status in the database. Try again later.", ephemeral=True)
            return
            
        # Update Local Guild Config (CONFIG_DB)
        is_premium, current_expires_ts = is_guild_premium(interaction.guild_id)
        
        if is_premium and current_expires_ts != "LIFETIME":
            start_time = current_expires_ts
            time_str = f"The existing premium status has been **extended**."
        else:
            start_time = time.time()
            time_str = f"Premium is now **activated**."
            
        months = license_data['months']
        new_expires_at = start_time + (30 * 86400 * months)
        
        guild_config = CONFIG_DB.get(guild_id_str, {})
        guild_config['premium'] = {
            'active': True,
            'key': key,
            'activated_by': user_id,
            'expires_at': new_expires_at 
        }
        CONFIG_DB[guild_id_str] = guild_config
        
        save_data('config')
        
        # Success Message
        await interaction.followup.send(
            f"🎉 **Premium Activated!** 🎉\n"
            f"**{time_str}** You have successfully redeemed a **{months}-month** license.\n"
            f"New Expiration: <t:{int(new_expires_at)}:F> (<t:{int(new_expires_at)}:R>)",
            ephemeral=False
        )

    
    @app_commands.command(name="premium_status", description="Shows the current premium status of this server.")
    async def premium_status_command(self, interaction: discord.Interaction):
        
        await interaction.response.defer(thinking=True, ephemeral=False)
        
        is_premium, expires_ts = is_guild_premium(interaction.guild_id)
        
        embed = discord.Embed(title=f"Server Premium Status for {interaction.guild.name}")
        
        if is_premium:
            if expires_ts == "LIFETIME":
                embed.description = "✨ **LIFETIME Premium** ✨"
                embed.color = discord.Color.gold()
                embed.set_footer(text="This server has permanent premium access.")
                
            else:
                expires_at = int(expires_ts)
                timestamp_string = f"<t:{expires_at}:F> (<t:{expires_at}:R>)"
                
                embed.description = "✅ **Premium Active**"
                embed.add_field(name="Expiration Date", value=timestamp_string, inline=False)
                embed.color = discord.Color.green()
                embed.set_footer(text="Premium is currently active.")
                
        else:
            embed.description = "❌ **Standard Access**"
            embed.color = discord.Color.red()
            
            # Check config data directly to provide context on why it's inactive
            guild_config = CONFIG_DB.get(str(interaction.guild_id), {})
            premium_info = guild_config.get('premium', {})
            
            if premium_info and premium_info.get('expires_at') and premium_info.get('expires_at') < time.time():
                 embed.set_footer(text="Premium was active but has expired.")
            else:
                 embed.set_footer(text="To activate premium, use the /license_activate command.")

        await interaction.followup.send(embed=embed)


    @app_commands.command(name="license_remove", description="Permanently deletes a license key from the database (Admin only).")
    @app_commands.checks.has_permissions(administrator=True)
    async def license_remove_command(self, interaction: discord.Interaction, key: str):
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        key = key.upper().strip()
        
        if DB is None:
            await interaction.followup.send("❌ **Database not connected**. Deletion failed.", ephemeral=True)
            return

        license_data = get_license_from_firestore(key)
        if not license_data:
            await interaction.followup.send(f"❌ Key `{key}` was **not found** in the database.", ephemeral=True)
            return

        success = delete_license_from_firestore(key)
        
        if success:
            await interaction.followup.send(f"🗑️ Successfully **deleted** license key: `{key}`.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ **Error**: Failed to delete key `{key}` from the database.", ephemeral=True)


    @app_commands.command(name="subscription_remove", description="Immediately removes premium status from this server (Admin only).")
    @app_commands.checks.has_permissions(administrator=True)
    async def subscription_remove_command(self, interaction: discord.Interaction):
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild_id_str = str(interaction.guild_id) # 🔑 Use string ID for CONFIG_DB consistency
        
        is_premium, _ = is_guild_premium(interaction.guild_id)
        
        if not is_premium:
            await interaction.followup.send("⚠️ This server currently **does not have an active premium subscription** to remove.", ephemeral=True)
            return
            
        # Update CONFIG_DB to remove premium status
        guild_config = CONFIG_DB.get(guild_id_str, {})
        
        guild_config['premium'] = {
            'active': False,
            'expires_at': time.time() - 1 
        }
        CONFIG_DB[guild_id_str] = guild_config
        
        save_data('config')
        
        await interaction.followup.send(
            f"🚫 Premium subscription has been **immediately removed** from this server. Access has reverted to standard.",
            ephemeral=False
        )


 # ==============================================================================
# AutoMod Cog
# ==============================================================================
class AutoModCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.RULE_NAME = "Custom Slur Block List" 
    
    # --- Status Command ---
    @app_commands.command(name="automod_status", description="Shows the status and words in the Custom Slur Block List.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_status_command(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=False)

        # 🔑 Using the top-level helper function
        rule = await get_automod_rule(interaction.guild, self.RULE_NAME)

        if not rule:
            await interaction.followup.send(
                f"ℹ️ The **{self.RULE_NAME}** rule is not set up on this server. Use `/automod_setup` to create it.",
                ephemeral=False
            )
            return

        # AutoMod uses 'presets' (for built-in lists) or 'keywords' (for custom words)
        keywords = rule.trigger.presets or rule.trigger.keywords
        
        embed = discord.Embed(
            title=f"🛡️ AutoMod Rule Status: {self.RULE_NAME}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Status", value="✅ **Active**" if rule.enabled else "⚠️ **Disabled**", inline=True)
        embed.add_field(name="ID", value=f"`{rule.id}`", inline=True)
        
        if keywords:
            word_list = ", ".join(keywords)
            embed.add_field(name="Blocked Words", value=f"```\n{word_list}\n```", inline=False)
        else:
            embed.add_field(name="Blocked Words", value="None configured (Rule active but empty).", inline=False)

        action_desc = "❌ **Rule has no defined action!**"
        for action in rule.actions:
            if action.type == discord.AutoModActionType.block_message:
                action_desc = "🗑️ **Blocks Message**"
                if action.metadata.channel_id:
                     action_desc += f" (Sends alert to <#{action.metadata.channel_id}>)"
                break
        
        embed.set_footer(text=action_desc)

        await interaction.followup.send(embed=embed)


    # --- Setup/Update Command ---
    @app_commands.command(name="automod_setup", description="Sets up or updates the Custom Slur Block List.")
    @app_commands.checks.has_permissions(administrator=True) 
    async def automod_setup_command(self, interaction: discord.Interaction, words: str):
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # 1. Clean and split the word list
        word_list = [w.strip() for w in words.split(',') if w.strip()]
        if not word_list:
            await interaction.followup.send("❌ Please provide a comma-separated list of words to block.", ephemeral=True)
            return

        # 2. Define the action: Block the message and notify the channel where the command was used
        action = discord.AutoModAction(
            type=discord.AutoModActionType.block_message,
            metadata=discord.AutoModActionMetadata(
                custom_message="Your message contains language blocked by server rules.",
                channel_id=interaction.channel_id 
            )
        )

        print(f"DEBUG: Attempting to fetch rule {self.RULE_NAME} for Guild {interaction.guild.id}")

        # 3. Check if the rule exists (to decide between create and edit)
        # 🔑 Using the top-level helper function
        existing_rule = await get_automod_rule(interaction.guild, self.RULE_NAME)

        print(f"DEBUG: Rule fetch complete. Existing rule found: {existing_rule is not None}")

        try:
            if existing_rule:
                # --- EDIT EXISTING RULE ---
                updated_rule = await existing_rule.edit(
                    name=self.RULE_NAME,
                    enabled=True,
                    # Setting the keywords for the custom list
                    trigger_metadata=discord.AutoModTriggerMetadata(keywords=word_list), 
                    actions=[action],
                )
                message = (f"✅ **{self.RULE_NAME}** rule updated successfully! It now blocks **{len(word_list)}** words.")
            else:
                # --- CREATE NEW RULE ---
                new_rule = await interaction.guild.create_automod_rule(
                    name=self.RULE_NAME,
                    event=discord.AutoModEventType.message_send,
                    trigger_type=discord.AutoModTriggerType.keyword,
                    trigger_metadata=discord.AutoModTriggerMetadata(keywords=word_list),
                    actions=[action],
                    enabled=True,
                    exempt_channels=[] 
                )
                message = (f"🎉 **{self.RULE_NAME}** rule created successfully! It blocks **{len(word_list)}** words.")
            
            await interaction.followup.send(message, ephemeral=False)

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ **Permission Error:** I need the **Administrator** or **Manage Guild** permission to create/edit AutoMod rules.",
                ephemeral=True
            )
        except Exception as e:
            print(f"AutoMod Setup Error: {e}")
            await interaction.followup.send(f"❌ An error occurred during AutoMod setup: {e}", ephemeral=True)       
# ==============================================================================
# Bot Run Block
# ==============================================================================

if __name__ == "__main__":
    
    keep_alive()

    # 🚨 CRITICAL CHANGE: Initialize Firestore here to ensure logs are visible 
    # before the Discord connection logs.
    initialize_firestore()

    bot_token = os.environ.get("DISCORD_TOKEN")
    if not bot_token:
        print("ERROR: DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(bot_token)