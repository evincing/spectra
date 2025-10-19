import discord
from discord.ext import commands, tasks
from discord import app_commands # Required for slash commands
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
import uuid # For generating unique license keys
import firebase_admin
from firebase_admin import credentials, firestore

DB = None # Global Firestore client variable

def initialize_firestore():
    """Initializes the Firebase connection using a secure environment variable."""
    global DB
    
    # Get the JSON string from the environment variable
    json_creds_string = os.environ.get('FIREBASE_CREDENTIALS')
    
    if not json_creds_string:
        print("FATAL ERROR: FIREBASE_CREDENTIALS environment variable not found. Persistence is DISABLED.")
        return # DB remains None here
    
    try:
        # 1. Convert the JSON string back into a Python dictionary/object
        creds_dict = json.loads(json_creds_string) # Fails if JSON is invalid
        
        # 2. Create credentials object from the dictionary
        cred = credentials.Certificate(creds_dict)
        
        # 3. Initialize the Firebase app
        if not firebase_admin._app:
             firebase_admin.initialize_app(cred)
        
        DB = firestore.client()
        print("✅ Successfully initialized Firebase Firestore client.")
    except Exception as e:
        # Fails if the JSON loads fine but Firebase rejects the credentials (e.g., project ID wrong)
        print(f"FATAL ERROR: Could not initialize Firebase. Check FIREBASE_CREDENTIALS format. Error: {e}")
        DB = None # DB is set back to None here

# IMPORTANT: You must have a keep_alive.py file for this to work
from keep_alive import keep_alive 

keep_alive() # Start the keep-alive server

# Load environment variables from .env file
load_dotenv()

# --- Database File Names ---
LEVELS_FILE = 'levels.json'
GIVEAWAYS_FILE = 'giveaways.json'
CONFIG_FILE = 'config.json' 
USER_CACHE_FILE = 'user_cache.json'
LICENSE_FILE = 'licenses.json' # New file for license keys
# ---------------------------

# --- Configuration and In-Memory Storage (will be loaded from file) ---
LEVELS_DB = {} 
ACTIVE_GIVEWAYS = {} 
GIVEAWAY_MESSAGES = {} 
CONFIG_DB = {} 
USER_CACHE = {} 
LICENSE_DB = {} # New storage for licenses
# Note: You should replace this with your actual bot owner ID
# CRITICAL: If you use /eval or /setstatus, this MUST be your Discord User ID
BOT_OWNER_ID = 1356850034993397781 
# Use a lock to ensure thread-safe access to the cache from sync/async code
USER_CACHE_LOCK = threading.Lock() 
# Time the bot started, used for the /uptime command
BOT_START_TIME = time.time() 
# ----------------------------------------------------------------------

# ==============================================================================
# DATABASE PERSISTENCE FUNCTIONS
# ==============================================================================

def load_data():
    """Loads all data (LEVELS_DB, ACTIVE_GIVEWAYS, CONFIG_DB, USER_CACHE, LICENSE_DB) from JSON files."""
    global LEVELS_DB, ACTIVE_GIVEWAYS, CONFIG_DB, USER_CACHE, LICENSE_DB
    
    # Load Levels Data
    if os.path.exists(LEVELS_FILE):
        try:
            with open(LEVELS_FILE, 'r') as f:
                LEVELS_DB = {int(k): v for k, v in json.load(f).items()}
            print(f"Loaded {len(LEVELS_DB)} user levels.")
        except Exception as e:
            print(f"Error loading {LEVELS_FILE}: {e}")
            LEVELS_DB = {}

    # Load Giveaways Data
    if os.path.exists(GIVEAWAYS_FILE):
        try:
            with open(GIVEAWAYS_FILE, 'r') as f:
                ACTIVE_GIVEWAYS = {int(k): v for k, v in json.load(f).items()}
            print(f"Loaded {len(ACTIVE_GIVEWAYS)} active giveaways.")
        except Exception as e:
            print(f"Error loading {GIVEAWAYS_FILE}: {e}")
            ACTIVE_GIVEWAYS = {}

    # Load Config Data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                CONFIG_DB = {int(k): v for k, v in json.load(f).items()}
            print(f"Loaded config data.")
        except Exception as e:
            print(f"Error loading {CONFIG_FILE}: {e}")
            CONFIG_DB = {}

    # Load User Cache Data
    if os.path.exists(USER_CACHE_FILE):
        try:
            with open(USER_CACHE_FILE, 'r') as f:
                USER_CACHE = json.load(f) 
            print(f"Loaded {len(USER_CACHE)} user names from cache.")
        except Exception as e:
            print(f"Error loading {USER_CACHE_FILE}: {e}")
            USER_CACHE = {}

    # Load License Data
    if os.path.exists(LICENSE_FILE):
        try:
            with open(LICENSE_FILE, 'r') as f:
                LICENSE_DB = json.load(f) 
            print(f"Loaded {len(LICENSE_DB)} license keys.")
        except Exception as e:
            print(f"Error loading {LICENSE_FILE}: {e}")
            LICENSE_DB = {}


def save_data(data_type: str):
    """Saves the specified data structure to its corresponding JSON file."""
    if data_type == 'levels':
        data_to_save = LEVELS_DB
        file_name = LEVELS_FILE
    elif data_type == 'giveaways':
        data_to_save = ACTIVE_GIVEWAYS
        file_name = GIVEAWAYS_FILE
    elif data_type == 'config':
        # NOTE: Keys in CONFIG_DB are integers (guild IDs), so convert to str for JSON
        data_to_save = {str(k): v for k, v in CONFIG_DB.items()}
        file_name = CONFIG_FILE
    elif data_type == 'licenses':
        data_to_save = LICENSE_DB
        file_name = LICENSE_FILE
    else:
        return

    try:
        # We save this sync (blocking) as it's not the main bot loop
        with open(file_name, 'w') as f: 
            json.dump(data_to_save, f, indent=4)
    except Exception as e:
        print(f"Error saving {file_name}: {e}")

async def save_user_cache():
    """Saves the USER_CACHE dictionary to a JSON file asynchronously."""
    with USER_CACHE_LOCK: # Ensure thread-safe access
        cache_copy = USER_CACHE.copy()
        
    try:
        # Use aiofiles for non-blocking I/O
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
    if seconds > 0 or not parts: # Include seconds if less than a minute, or if uptime is very short
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
            # Expired, but we'll leave cleanup to the tasks.
            return False, expires_ts 
    except (TypeError, ValueError):
        # Should not happen if data is saved correctly
        return False, None

# ==============================================================================
# Cache Management & Bot Setup
# ==============================================================================

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

# Define Intents (CRITICAL SECTION)
intents = discord.Intents.default()
# These two are privileged and MUST be enabled in the Discord Developer Portal
intents.members = True 
intents.message_content = True 
# Optionally, if using Status/Presence, you should also include:
# intents.presences = True 

bot = commands.Bot(command_prefix='!', intents=intents)

async def setup_hook():
    """Load Cogs, ensure persistence files exist, and then sync commands."""
    
    print("Loading existing data...")
    load_data() 

    # Immediate File Creation for Persistence
    if not os.path.exists(LEVELS_FILE):
        save_data('levels')
        print(f"Created initial empty {LEVELS_FILE}.")
    if not os.path.exists(GIVEAWAYS_FILE):
        save_data('giveaways')
        print(f"Created initial empty {GIVEAWAYS_FILE}.")
    if not os.path.exists(CONFIG_FILE):
        save_data('config')
        print(f"Created initial empty {CONFIG_FILE}.")
    if not os.path.exists(LICENSE_FILE): # New file check
        save_data('licenses')
        print(f"Created initial empty {LICENSE_FILE}.")
    
    # Check for User Cache File
    if not os.path.exists(USER_CACHE_FILE): 
        await save_user_cache() # Use the async version
        print(f"Created initial empty {USER_CACHE_FILE}.")

    print("Loading Cogs...")
    try:
        await bot.add_cog(LevelingCog(bot))
        await bot.add_cog(GiveawayCog(bot))
        await bot.add_cog(UtilityCog(bot)) 
        await bot.add_cog(LicenseCog(bot)) # Add the new cog
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
    """Initializes the bot."""
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
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
        self.last_xp_time = {} # user_id: timestamp

    def get_level_info(self, xp: int):
        """Calculates level info based on XP."""
        # This is a placeholder for your actual leveling formula
        level = int((xp / 100) ** 0.5) 
        xp_required_for_next = ((level + 1) ** 2) * 100
        xp_needed = xp_required_for_next - xp
        return level, xp_required_for_next, xp_needed, xp
    
    # --- LEVELING COMMANDS ---
    @app_commands.command(name="rank", description="Shows your current level and XP.")
    async def rank_command(self, interaction: discord.Interaction, member: discord.Member = None):
        """Shows the level and XP of a user."""
        target = member or interaction.user
        user_id = target.id

        user_data = LEVELS_DB.get(user_id, {'xp': 0, 'level': 0})
        level, xp_required_for_next, xp_needed, xp = self.get_level_info(user_data['xp'])

        await interaction.response.send_message(
            f"**{target.display_name}** is **Level {level}** with **{xp} XP**.\n"
            f"Progress: **{xp_needed} XP** needed for Level {level + 1}."
        )

    @app_commands.command(name="leaderboard", description="Shows the top 10 users by level and XP.")
    async def leaderboard_command(self, interaction: discord.Interaction):
        """Displays the top 10 users from the Levels DB."""
        # Get and sort users by level (desc) then by XP (desc)
        sorted_users = sorted(
            LEVELS_DB.items(), 
            key=lambda item: (item[1]['level'], item[1]['xp']), 
            reverse=True
        )
        
        # Build the leaderboard string
        leaderboard_msg = "🏆 **LEVEL LEADERBOARD** 🏆\n"
        
        for i, (user_id, data) in enumerate(sorted_users[:10]):
            user_name = USER_CACHE.get(str(user_id), f"User ID: {user_id}")
            # Ensure the level key exists before trying to access it
            level_display = data.get('level', 0)
            xp_display = data.get('xp', 0)
            leaderboard_msg += f"{i+1}. **{user_name}** - Level {level_display} ({xp_display} XP)\n"

        await interaction.response.send_message(leaderboard_msg)
    # -------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        user_id = message.author.id
        current_time = time.time()
        cooldown = 5  

        if current_time - self.last_xp_time.get(user_id, 0) >= cooldown:
            self.last_xp_time[user_id] = current_time
            
            # Ensure user is in cache when they send a message
            await update_user_cache(self.bot, user_id) 
            
            xp_gained = random.randint(5, 15)
            
            user_data = LEVELS_DB.get(user_id, {'xp': 0, 'level': 0})
            old_xp = user_data['xp']
            new_xp = old_xp + xp_gained
            
            old_level, _, _, _ = self.get_level_info(old_xp)
            new_level, _, _, _ = self.get_level_info(new_xp)
            
            user_data['xp'] = new_xp
            user_data['level'] = new_level
            LEVELS_DB[user_id] = user_data
            
            save_data('levels') 
            
            if new_level > old_level:
                await message.channel.send(
                    f"🎉 Congratulations, {message.author.mention}! You leveled up to **Level {new_level}**!"
                )
        
# ------------------------------------------------------------------------------
class GiveawayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
    # --- GIVEAWAY CONFIG COMMAND ---
    @app_commands.command(name="set_giveaway_channel", description="Sets the dedicated channel for future giveaway announcements.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_giveaway_channel_command(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Sets the channel where giveaways will be posted."""
        guild_id = interaction.guild_id
        
        # Update CONFIG_DB
        guild_config = CONFIG_DB.get(guild_id, {})
        guild_config['giveaway_channel_id'] = channel.id
        CONFIG_DB[guild_id] = guild_config
        save_data('config')

        await interaction.response.send_message(
            f"✅ Giveaway announcements will now be posted in {channel.mention}.", 
            ephemeral=True
        )

    # --- GIVEAWAY START/END COMMANDS ---
    @app_commands.command(name="start_giveaway", description="Starts a new giveaway.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def start_giveaway_command(self, interaction: discord.Interaction, 
                                     prize: str, 
                                     duration: int = 60): # duration in minutes
        """Starts a giveaway for a specific prize and duration (in minutes)."""
        
        await interaction.response.defer(thinking=True, ephemeral=True)

        guild_config = CONFIG_DB.get(interaction.guild_id, {})
        channel_id = guild_config.get('giveaway_channel_id')
        
        channel = interaction.channel
        
        if channel_id:
            target_channel = interaction.guild.get_channel(channel_id)
            if target_channel:
                channel = target_channel
            else:
                await interaction.followup.send("⚠️ Configured giveaway channel not found. Posting in this channel instead.", ephemeral=True)

        end_time = time.time() + (duration * 60)
        giveaway_id = len(ACTIVE_GIVEWAYS) + 1 # Simple ID generation

        embed = discord.Embed(
            title=f"🎉 GIVEAWAY: {prize} 🎉",
            description=f"React with 🎉 to enter!\nEnds: <t:{int(end_time)}:R>", 
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"Giveaway ID: {giveaway_id} | Hosted by: {interaction.user.display_name}")
        
        try:
            message = await channel.send(embed=embed) # Send to the designated channel
            await message.add_reaction("🎉")

            # Save giveaway data
            ACTIVE_GIVEWAYS[giveaway_id] = {
                'prize': prize,
                'start_time': time.time(),
                'end_time': end_time,
                'channel_id': channel.id,
                'host_id': interaction.user.id,
                'message_id': message.id
            }
            save_data('giveaways')

            await interaction.followup.send(f"✅ Giveaway started for **{prize}** in {channel.mention} (ID: {giveaway_id})!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to start giveaway in {channel.mention}. Check bot permissions. Error: {e}", ephemeral=True)
            return


    @app_commands.command(name="end_giveaway", description="Manually ends an active giveaway and picks a winner.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def end_giveaway_command(self, interaction: discord.Interaction, giveaway_id: int):
        """Ends a giveaway, picks a winner, and announces it."""
        await interaction.response.defer(thinking=True, ephemeral=True)

        if giveaway_id not in ACTIVE_GIVEWAYS:
            await interaction.followup.send(f"❌ Giveaway ID **#{giveaway_id}** is not active or does not exist.", ephemeral=True)
            return

        giveaway_data = ACTIVE_GIVEWAYS.pop(giveaway_id)
        save_data('giveaways')

        prize = giveaway_data['prize']

        try:
            # 1. Fetch channel and message
            channel = interaction.guild.get_channel(giveaway_data['channel_id'])
            if not channel:
                channel = await self.bot.fetch_channel(giveaway_data['channel_id'])
            message = await channel.fetch_message(giveaway_data['message_id'])
        except Exception:
            await interaction.followup.send(f"❌ Could not find the original message or channel for giveaway **#{giveaway_id}**. Giveaway data removed.", ephemeral=True)
            return

        # 2. Get participants
        reaction = discord.utils.get(message.reactions, emoji="🎉")
        winner = None
        participants = []
        
        if reaction:
            # Fetch all users who reacted and filter out bots
            participants = [user async for user in reaction.users() if not user.bot]
            
            if participants:
                winner = random.choice(participants)
                if winner:
                    await update_user_cache(self.bot, winner.id)

        # 3. Announce winner and update embed
        host = interaction.guild.get_member(giveaway_data['host_id']) or f"User ID: {giveaway_data['host_id']}"
        
        if winner:
            announcement = f"The winner of the **{prize}** giveaway is {winner.mention}! Congratulations!"
            new_embed = discord.Embed(
                title=f"🎉 GIVEAWAY ENDED: {prize} 🎉",
                description=f"**Winner:** {winner.mention}\n**Hosted by:** {host.mention if isinstance(host, discord.Member) else host}",
                color=discord.Color.red()
            )
            # Send announcement in the original channel
            await channel.send(announcement)
            await interaction.followup.send(f"✅ Successfully ended giveaway **#{giveaway_id}** and announced the winner!", ephemeral=True)
        else:
            announcement = f"The giveaway for **{prize}** ended with no valid participants."
            new_embed = discord.Embed(
                title=f"🎉 GIVEAWAY ENDED: {prize} (No Winner) 🎉",
                description=f"No valid winner was found.\n**Hosted by:** {host.mention if isinstance(host, discord.Member) else host}",
                color=discord.Color.dark_grey()
            )
            await channel.send(announcement)
            await interaction.followup.send(f"✅ Successfully ended giveaway **#{giveaway_id}**. No winner was announced.", ephemeral=True)

        # 4. Edit the original message to show it has ended
        new_embed.set_footer(text=f"Giveaway ID: {giveaway_id} | Ended by: {interaction.user.display_name}")
        await message.edit(embed=new_embed)


    @app_commands.command(name="reroll_giveaway", description="Rerolls the winner for a specific giveaway ID (Logic TBD).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reroll_giveaway_command(self, interaction: discord.Interaction, giveaway_id: int):
        """Rerolls a giveaway (requires full implementation)."""
        # NOTE: This command is preserved as a placeholder for future implementation
        if giveaway_id in ACTIVE_GIVEWAYS:
            await interaction.response.send_message(f"Rerolling giveaway **#{giveaway_id}**... (Logic TBD)", ephemeral=True)
        else:
            await interaction.response.send_message(f"Giveaway ID **#{giveaway_id}** not found or still active.", ephemeral=True)
            
# ------------------------------------------------------------------------------
class UtilityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
    # --- UTILITY COMMANDS ---
    
    @app_commands.command(name="uptime", description="Returns an embed on how long the bot has been up for.")
    async def uptime_command(self, interaction: discord.Interaction):
        """Displays the bot's current uptime."""
        current_time = time.time()
        uptime_seconds = current_time - BOT_START_TIME
        uptime_formatted = format_uptime(uptime_seconds)
        
        embed = discord.Embed(
            title="Bot Uptime ⏱️",
            description=f"Spectra has been running for: **{uptime_formatted}**",
            color=discord.Color.dark_teal()
        )
        embed.set_footer(text=f"Last restart: {datetime.fromtimestamp(BOT_START_TIME, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        await interaction.response.send_message(embed=embed)


    @app_commands.command(name="status", description="Get the link to the bot's live status page.")
    async def status_command(self, interaction: discord.Interaction):
        """Responds with the bot's live status page URL."""
        status_url = "https://spectra-bot.statuspage.io/"
        await interaction.response.send_message(
            f"🛠️ You can check the live status of the bot here: <{status_url}>"
        )
        
    @app_commands.command(name="say", description="Makes the bot repeat a message in the current or specified channel.")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def say_command(self, interaction: discord.Interaction, text: str, channel: discord.TextChannel = None):
        """Makes the bot repeat a message."""
        target_channel = channel or interaction.channel
        
        await interaction.response.send_message("✅ Message sent.", ephemeral=True)
        await target_channel.send(text)

    @app_commands.command(name="serverinfo", description="Displays detailed information about the current server.")
    async def serverinfo_command(self, interaction: discord.Interaction):
        """Displays detailed information about the server (guild)."""
        guild = interaction.guild
        embed = discord.Embed(
            title=f"Server Information for {guild.name}",
            color=discord.Color.blue()
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            
        embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
        embed.add_field(name="Server ID", value=guild.id, inline=True)
        embed.add_field(name="Members", value=guild.member_count, inline=True)
        embed.add_field(name="Channels", value=len(guild.channels), inline=True)
        embed.add_field(name="Roles", value=len(guild.roles), inline=True)
        embed.add_field(name="Boost Level", value=f"Tier {guild.premium_tier} ({guild.premium_subscription_count} boosts)", inline=True)
        embed.add_field(name="Creation Date", value=f"<t:{int(guild.created_at.timestamp())}:F>", inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Displays detailed information about a user.")
    async def userinfo_command(self, interaction: discord.Interaction, member: discord.Member = None):
        """Displays detailed information about a user."""
        target = member or interaction.user
        
        embed = discord.Embed(
            title=f"User Information for {target.display_name}",
            color=target.color if target.color != discord.Color.default() else discord.Color.green()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        
        # Format roles, excluding @everyone
        roles = [role.name for role in target.roles if role.name != "@everyone"]
        
        embed.add_field(name="Username", value=target.name, inline=True)
        embed.add_field(name="Display Name", value=target.display_name, inline=True)
        embed.add_field(name="User ID", value=target.id, inline=True)
        embed.add_field(name="Account Created", value=f"<t:{int(target.created_at.timestamp())}:R>", inline=True)
        
        # Check if the member is in the current guild
        if isinstance(target, discord.Member):
            embed.add_field(name="Joined Server", value=f"<t:{int(target.joined_at.timestamp())}:R>", inline=True)
            embed.add_field(name=f"Roles ({len(roles)})", value=", ".join(roles) if roles else "None", inline=False)
        else:
            embed.add_field(name="Joined Server", value="Not in this server", inline=True)
        
        await interaction.response.send_message(embed=embed)


    @app_commands.command(name="ping", description="Shows the bot's latency.")
    async def ping_command(self, interaction: discord.Interaction):
        """Responds with the bot's current latency (ping)."""
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! 🏓 Latency: **{latency_ms}ms**")

    @app_commands.command(name="eval", description="Evaluates Python code (Bot Owner only).")
    @app_commands.checks.check(lambda i: i.user.id == BOT_OWNER_ID)
    async def eval_command(self, interaction: discord.Interaction, code: str):
        """Evaluates arbitrary Python code."""
        # io.StringIO and contextlib.redirect_stdout are used to capture print output
        str_obj = io.StringIO()
        
        # Clean the code block input
        code = code.strip('` \n')
        if code.startswith('py'):
            code = code[2:].strip()

        # Define an environment for the executed code
        env = {
            'bot': self.bot,
            'interaction': interaction,
            'channel': interaction.channel,
            'author': interaction.user,
            'guild': interaction.guild,
            'db': LEVELS_DB, # Access to the Levels database
            'save': save_data, # Allow saving data from eval
            '__': {} 
        }

        try:
            # Wrap the code in an async function to allow for `await` calls
            exec_code = 'async def func():\n' + textwrap.indent(code, '    ')
            # Compile and execute the wrapped function definition
            exec(exec_code, env)
            
            # Call the async function and capture standard output (print statements)
            with contextlib.redirect_stdout(str_obj):
                ret = await env['func']()
            
            # If the function returned a value, write it to the output stream
            if ret is not None:
                str_obj.write(str(ret))

        except Exception as e:
            # Need to respond to the interaction first, if not already done
            if not interaction.response.is_done():
                 await interaction.response.send_message(f"**Execution Error:**\n```\n{e}```", ephemeral=True)
            else:
                 await interaction.followup.send(f"**Execution Error:**\n```\n{e}```", ephemeral=True)
            return

        # Get the final output string
        output = str_obj.getvalue()

        # Send the response
        if output:
            if len(output) > 1900: # Discord message limit is 2000 characters
                # Initial response
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"**Output too long. Sending as file.**", 
                        ephemeral=True
                    )
                    # Send output as a file using a follow-up response
                    await interaction.followup.send(
                        file=discord.File(io.BytesIO(output.encode('utf-8')), filename="output.txt"), 
                        ephemeral=True
                    )
                else:
                     await interaction.followup.send(
                        f"**Output too long. Sending as file.**", 
                        file=discord.File(io.BytesIO(output.encode('utf-8')), filename="output.txt"),
                        ephemeral=True
                    )
            else:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"**Evaluation Successful:**\n```python\n{output}```", ephemeral=True)
                else:
                    await interaction.followup.send(f"**Evaluation Successful:**\n```python\n{output}```", ephemeral=True)
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message("✅ Code executed without output.", ephemeral=True)
            else:
                await interaction.followup.send("✅ Code executed without output.", ephemeral=True)


    @eval_command.error
    async def eval_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("❌ This command is restricted to the bot owner.", ephemeral=True)
        else:
            print(f"Eval command error: {error}")
            await interaction.response.send_message(f"An unexpected error occurred in the eval command.", ephemeral=True)

    @app_commands.command(name="clear", description="Deletes a specified number of messages from the channel.")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def clear_command(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        """Deletes messages in the current channel."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Use bulk delete method
        deleted = await interaction.channel.purge(limit=amount)
        
        await interaction.followup.send(
            f"🗑️ Successfully deleted **{len(deleted)}** message(s).", 
            ephemeral=False, 
            delete_after=5 # Self-destruct after 5 seconds
        )
    
    # --- SET STATUS COMMAND ---
    @app_commands.command(name="setstatus", description="Sets the bot's activity and online status (Owner only).")
    @app_commands.checks.check(lambda i: i.user.id == BOT_OWNER_ID)
    @app_commands.describe(
        activity_type="The type of activity (Playing, Listening, Watching, etc.)",
        status_text="The text for the activity (e.g., 'with fire', 'to music')",
        online_status="The bot's overall online status (online, idle, dnd, invisible)"
    )
    @app_commands.choices(activity_type=[
        app_commands.Choice(name="Playing", value=discord.ActivityType.playing.value),
        app_commands.Choice(name="Listening to", value=discord.ActivityType.listening.value),
        app_commands.Choice(name="Watching", value=discord.ActivityType.watching.value),
        app_commands.Choice(name="Competing in", value=discord.ActivityType.competing.value),
    ], online_status=[
        app_commands.Choice(name="Online", value="online"),
        app_commands.Choice(name="Idle", value="idle"),
        app_commands.Choice(name="Do Not Disturb", value="dnd"),
        app_commands.Choice(name="Invisible", value="invisible"),
    ])
    async def setstatus_command(self, interaction: discord.Interaction, 
                                 activity_type: int, 
                                 status_text: str, 
                                 online_status: str):
        
        # Map values back to Discord enums
        activity_map = {
            discord.ActivityType.playing.value: discord.ActivityType.playing,
            discord.ActivityType.listening.value: discord.ActivityType.listening,
            discord.ActivityType.watching.value: discord.ActivityType.watching,
            discord.ActivityType.competing.value: discord.ActivityType.competing,
        }
        
        status_map = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible,
        }

        activity = discord.Activity(type=activity_map[activity_type], name=status_text)
        status = status_map[online_status]

        # Use change_presence to update the bot's status
        await self.bot.change_presence(activity=activity, status=status)
        
        await interaction.response.send_message(
            f"✅ Bot presence updated successfully.\n"
            f"**Status:** `{online_status.capitalize()}`\n"
            f"**Activity:** `{activity_map[activity_type].name.capitalize()} {status_text}`",
            ephemeral=True
        )

    @setstatus_command.error
    async def setstatus_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("❌ This command is restricted to the bot owner.", ephemeral=True)
        else:
            print(f"Setstatus command error: {error}")
            await interaction.response.send_message(f"An unexpected error occurred in the setstatus command.", ephemeral=True)
    
    # -------------------------

# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------
class LicenseCog(commands.Cog):
# ... (all init, unload, and background task methods remain the same)
    
    # --- LICENSE COMMANDS ---
        
    @app_commands.command(name="premium_status", description="Shows if the server has Spectra Premium.")
    async def premium_status_command(self, interaction: discord.Interaction):
        """Displays the server's premium status."""
        
        # 🔑 FIX: Defer the interaction immediately to prevent the 404 timeout
        await interaction.response.defer(thinking=True, ephemeral=False) 
        
        is_premium, expires_ts = is_guild_premium(interaction.guild_id)
        
        embed = discord.Embed(
            title="Spectra Premium Status",
            color=discord.Color.red() if not is_premium else discord.Color.gold()
        )
        
        if is_premium:
            if expires_ts == "LIFETIME":
                expiry_text = "Never (LIFETIME)"
            else:
                expiry_text = f"<t:{expires_ts}:F> (<t:{expires_ts}:R>)"
                
            key = CONFIG_DB.get(interaction.guild_id, {}).get('premium', {}).get('license_key', 'N/A')
            embed.description = "✅ This server currently has **Spectra Premium** enabled!"
            embed.add_field(name="Expires", value=expiry_text, inline=False)
            embed.add_field(name="Active License", value=f"`{key}`", inline=False)
        else:
            embed.description = "❌ This server does **not** have Spectra Premium."
            if expires_ts is not None and expires_ts != "LIFETIME" and int(expires_ts) <= time.time():
                 embed.description += f"\n*(The last premium subscription expired <t:{expires_ts}:R>.)*"
        
        # 🔑 FIX: Use followup.send() after deferring
        await interaction.followup.send(embed=embed)


    @app_commands.command(name="license_generate", description="Generates a premium license key (Bot Owner only).")
# ... (This command remains the same, as it already used defer/followup correctly)
    async def license_generate_command(self, interaction: discord.Interaction, duration: int, reason: str):
        # ... (remains the same)
        if not DB:
            return await interaction.response.send_message("❌ Database not connected. Cannot generate license.", ephemeral=True)
            
        await interaction.response.defer(thinking=True, ephemeral=True)
        # ... (rest of the command)


    @app_commands.command(name="license_delete", description="Deletes a premium license key from the database (Bot Owner only).")
# ... (This command remains the same, as it already used defer/followup correctly)
    async def license_delete_command(self, interaction: discord.Interaction, license_key: str):
        # ... (remains the same)
        if not DB:
            return await interaction.response.send_message("❌ Database not connected. Cannot delete license.", ephemeral=True)
            
        await interaction.response.defer(thinking=True, ephemeral=True)
        # ... (rest of the command)


    @app_commands.command(name="license_status", description="Shows if the provided license is valid and if it expires.")
    @app_commands.describe(license_key="The 32-character license key to check.")
    async def license_status_command(self, interaction: discord.Interaction, license_key: str):
        """Checks the status of a provided license key."""
        
        # 🔑 FIX: Defer the interaction immediately to prevent the 404 timeout
        await interaction.response.defer(thinking=True)
        
        key = license_key.upper().replace('-', '').strip()
        license_info = LICENSE_DB.get(key)
        
        if not license_info:
            embed = discord.Embed(
                title="License Status Check ❌",
                description="The provided license key is **invalid** or does not exist.",
                color=discord.Color.red()
            )
            # 🔑 FIX: Use followup.send() after deferring
            return await interaction.followup.send(embed=embed)

        is_valid = True
        expiry_ts = license_info.get("expiry_timestamp")
        
        # ... (Logic to check validity remains the same) ...

        # Determine general status
        # ... (Logic to determine status and color remains the same) ...
            
        # Build the embed
        # ... (Logic to build embed remains the same) ...

        # 🔑 FIX: Use followup.send() after deferring
        await interaction.followup.send(embed=embed)


    @app_commands.command(name="license_guild", description="Applies a license key to this server to activate premium.")
# ... (This command remains the same, as it already used defer/followup correctly)
    async def license_guild_command(self, interaction: discord.Interaction, license_key: str):
        # ... (remains the same)
        if not DB:
            return await interaction.response.send_message("❌ Database not connected. Cannot apply license.", ephemeral=True)
            
        await interaction.response.defer(thinking=True)
        # ... (rest of the command)

# ------------------------------------------------------------------------------

if __name__ == '__main__':
    TOKEN = os.getenv('DISCORD_BOT_TOKEN') or os.getenv('DISCORD_TOKEN')
    if TOKEN is None:
        print("Error: DISCORD_BOT_TOKEN environment variable not set. Please check your .env file.")
    else:
        bot.run(TOKEN)