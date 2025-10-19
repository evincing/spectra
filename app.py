import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta, timezone
import time
import uuid
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore

# ==============================================================================
# 1. FIREBASE & GLOBAL DATA INITIALIZATION
# ==============================================================================

# NOTE: Since we are using an external database, the old CONFIG_FILE/LICENSE_FILE 
# globals are no longer needed. We only need the in-memory cache.

BOT_OWNER_ID = 123456789012345678  # REPLACE WITH YOUR ACTUAL BOT OWNER ID
DB = None  # Global Firestore client variable
CONFIG_DB = {}  # In-memory cache for guild configs (premium status lives here)
LICENSE_DB = {}  # In-memory cache for license keys


# --- FIREBASE PERSISTENCE FUNCTIONS ---

def initialize_firestore():
    """Initializes the Firebase connection using a secure environment variable."""
    global DB
    
    # Get the JSON string from the environment variable
    json_creds_string = os.environ.get('FIREBASE_CREDENTIALS')
    
    if not json_creds_string:
        print("FATAL ERROR: FIREBASE_CREDENTIALS environment variable not found. Persistence is DISABLED.")
        return

    try:
        # Convert the JSON string back into a Python dictionary/object
        creds_dict = json.loads(json_creds_string)
        
        # Create credentials object from the dictionary
        cred = credentials.Certificate(creds_dict)
        
        # Initialize the Firebase app
        if not firebase_admin._app:
             firebase_admin.initialize_app(cred)
        
        DB = firestore.client()
        print("✅ Successfully initialized Firebase Firestore client.")
    except Exception as e:
        print(f"FATAL ERROR: Could not initialize Firebase. Check FIREBASE_CREDENTIALS format. Error: {e}")
        DB = None

# --- LOAD FUNCTIONS ---

async def load_licenses_from_firestore():
    """Loads all license documents from the 'licenses' collection and updates the local cache."""
    global LICENSE_DB
    if not DB: return

    try:
        licenses_ref = DB.collection('licenses')
        docs = licenses_ref.stream()
        
        LICENSE_DB = {}
        for doc in docs:
            LICENSE_DB[doc.id] = doc.to_dict()
            
        print(f"Loaded {len(LICENSE_DB)} licenses from Firestore.")
    except Exception as e:
        print(f"Error loading licenses from Firestore: {e}")

async def load_configs_from_firestore():
    """Loads all guild configs into the global CONFIG_DB cache."""
    global CONFIG_DB
    if not DB: return

    try:
        docs = DB.collection('configs').stream()
        # Convert guild_id from string (Firestore document ID) back to int (Python key)
        CONFIG_DB = {int(doc.id): doc.to_dict() for doc in docs}
        print(f"Loaded {len(CONFIG_DB)} guild configs from Firestore.")
    except Exception as e:
        print(f"Error loading configs from Firestore: {e}")
        
# --- SAVE FUNCTIONS ---

def save_license_to_firestore(license_key: str, license_data: dict):
    """Saves or updates a single license document."""
    if not DB: return

    try:
        license_ref = DB.collection('licenses').document(license_key)
        license_ref.set(license_data)
        # Update local cache after successful write
        LICENSE_DB[license_key] = license_data
    except Exception as e:
        print(f"Error saving license {license_key} to Firestore: {e}")

def save_config_to_firestore(guild_id: int, guild_data: dict):
    """Saves or updates a single guild config document."""
    if not DB: return

    try:
        # Use the string version of the guild_id as the document ID
        guild_id_str = str(guild_id)
        config_ref = DB.collection('configs').document(guild_id_str)
        config_ref.set(guild_data)
        # Update local cache after successful write
        CONFIG_DB[guild_id] = guild_data
    except Exception as e:
        print(f"Error saving config for guild {guild_id}: {e}")
        
        
# --- UTILITY FUNCTION (Uses the CONFIG_DB cache) ---
def is_guild_premium(guild_id):
    """Checks if a guild is premium and returns the status and expiry timestamp."""
    config = CONFIG_DB.get(guild_id, {})
    premium_info = config.get('premium', {})
    
    is_active = premium_info.get('active', False)
    expires_ts = premium_info.get('expires_at', None) # None if never activated or record is old

    # Check for expired but active-marked status (should be caught by the loop, but safety check)
    if is_active and expires_ts != "LIFETIME" and expires_ts is not None and int(expires_ts) <= time.time():
        return False, expires_ts # Expired, return False
        
    return is_active, expires_ts


# ==============================================================================
# 2. BOT SETUP AND COGS
# ==============================================================================

class SpectraBot(commands.Bot):
    def __init__(self):
        # Set required intents
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True # Needed for guild owner checks if necessary
        
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        
        # --- CRITICAL: Initialize DB and Load Data on Startup ---
        initialize_firestore()
        await load_licenses_from_firestore()
        await load_configs_from_firestore()
        # --------------------------------------------------------
        
        # Add cogs after data is loaded
        await self.add_cog(LicenseCog(self))
        
        # Sync application commands
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} application commands.")
        except Exception as e:
            print(f"Failed to sync commands: {e}")

# ------------------------------------------------------------------------------
class LicenseCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_licenses.start()
        
    def cog_unload(self):
        """Cancel the loop when the cog is unloaded."""
        self.check_licenses.cancel()

    # --- BACKGROUND TASK ---
    @tasks.loop(minutes=10)
    async def check_licenses(self):
        """Automatically checks for and removes expired premium statuses."""
        if not DB: return # Skip if DB is not connected
        
        current_time = time.time()
        
        # Iterate over a copy of the keys to safely modify the CONFIG_DB
        for guild_id, config in list(CONFIG_DB.items()): 
            premium_info = config.get('premium', {})
            expires_ts = premium_info.get('expires_at')
            
            # Check if active, not lifetime, and expired
            if premium_info.get('active', False) and expires_ts != "LIFETIME":
                try:
                    expires_ts = int(expires_ts)
                    if current_time >= expires_ts:
                        # 1. Update the local cache
                        CONFIG_DB[guild_id]['premium']['active'] = False
                        CONFIG_DB[guild_id]['premium']['inactivated_at'] = int(current_time)
                        CONFIG_DB[guild_id]['premium']['removal_reason'] = "Expired automatically."
                        
                        # 2. Persist the change to Firestore
                        save_config_to_firestore(guild_id, CONFIG_DB[guild_id])
                        
                        # 3. Try to inform the guild owner
                        try:
                            guild = self.bot.get_guild(guild_id)
                            if guild and guild.owner:
                                await guild.owner.send(f"⚠️ **Spectra Premium Status Removed**\nThe premium license for your server **{guild.name}** has **automatically expired**.")
                        except Exception:
                            pass
                            
                        print(f"Cleaned up expired premium for guild {guild_id}.")
                except (TypeError, ValueError):
                    print(f"Error checking premium for guild {guild_id}: Invalid timestamp.")

    @check_licenses.before_loop
    async def before_check_licenses(self):
        await self.bot.wait_until_ready()
        
    # --- LICENSE COMMANDS ---
        
    @app_commands.command(name="premium_status", description="Shows if the server has Spectra Premium.")
    async def premium_status_command(self, interaction: discord.Interaction):
        """Displays the server's premium status."""
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
        
        await interaction.response.send_message(embed=embed)


    @app_commands.command(name="license_generate", description="Generates a premium license key (Bot Owner only).")
    @app_commands.checks.check(lambda i: i.user.id == BOT_OWNER_ID)
    @app_commands.describe(
        duration="Duration in days for the license to last. Set to 0 for lifetime.",
        reason="A brief description for why the license was created (e.g., 'Test key', 'Giveaway winner')."
    )
    async def license_generate_command(self, interaction: discord.Interaction, duration: int, reason: str):
        """Generates a license key with a specified expiration."""
        if not DB:
            return await interaction.response.send_message("❌ Database not connected. Cannot generate license.", ephemeral=True)
            
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        key = str(uuid.uuid4()).upper().replace('-', '') # Generates a unique 32-char key
        
        if duration <= 0:
            expires_at = "LIFETIME"
            expiry_timestamp = "N/A"
        else:
            expiry_date = datetime.now(timezone.utc) + timedelta(days=duration)
            expires_at = expiry_date.strftime("%Y-%m-%d %H:%M:%S UTC")
            expiry_timestamp = int(expiry_date.timestamp())

        # Store the license data
        license_data = {
            "created_by": interaction.user.id,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "expires_at": expires_at,
            "expiry_timestamp": expiry_timestamp, 
            "reason": reason,
            "is_used": False, 
            "guild_id": None
        }
        
        # --- PERSISTENCE: Save new license to Firestore ---
        save_license_to_firestore(key, license_data)
        # --------------------------------------------------
        
        embed = discord.Embed(
            title="🔑 Premium License Key Generated",
            description=f"**Key:** `{key}`",
            color=discord.Color.gold()
        )
        embed.add_field(name="Duration", value=f"{duration} day(s)", inline=True)
        embed.add_field(name="Expires", value=expires_at, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)


    @app_commands.command(name="license_delete", description="Deletes a premium license key from the database (Bot Owner only).")
    @app_commands.checks.check(lambda i: i.user.id == BOT_OWNER_ID)
    @app_commands.describe(license_key="The 32-character license key to delete.")
    async def license_delete_command(self, interaction: discord.Interaction, license_key: str):
        """Deletes a license key."""
        if not DB:
            return await interaction.response.send_message("❌ Database not connected. Cannot delete license.", ephemeral=True)
            
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        key = license_key.upper().replace('-', '').strip()
        
        if key in LICENSE_DB:
            # Check if it's currently used by a guild
            if LICENSE_DB[key]['is_used']:
                guild_id = LICENSE_DB[key]['guild_id']
                
                # Check if it's the active premium key for that guild
                guild_config = CONFIG_DB.get(guild_id, {})
                premium_info = guild_config.get('premium', {})
                if premium_info.get('active', False) and premium_info.get('license_key') == key:
                    
                    # 1. Update local cache
                    CONFIG_DB[guild_id]['premium']['active'] = False
                    CONFIG_DB[guild_id]['premium']['removal_reason'] = "License deleted by owner."
                    
                    # 2. PERSISTENCE: Save updated config to Firestore
                    save_config_to_firestore(guild_id, CONFIG_DB[guild_id])
                    
                    # Try to notify the guild owner
                    try:
                        guild = self.bot.get_guild(guild_id)
                        if guild and guild.owner:
                            await guild.owner.send(f"⚠️ **Spectra Premium Status Removed**\nYour server's premium key (`{key}`) was manually **deleted by the bot owner**.")
                    except Exception:
                        pass 

                await interaction.followup.send(f"⚠️ License key `{key}` deleted. Premium status was removed from Guild ID `{guild_id}` if it was active.", ephemeral=True)
            else:
                 await interaction.followup.send(f"✅ License key `{key}` deleted successfully.", ephemeral=True)

            # --- PERSISTENCE: Delete from Firestore and local cache ---
            DB.collection('licenses').document(key).delete()
            del LICENSE_DB[key]
            # -----------------------------------------------------------
        else:
            await interaction.followup.send(f"❌ License key `{key}` not found in the database.", ephemeral=True)


    @app_commands.command(name="license_status", description="Shows if the provided license is valid and if it expires.")
    @app_commands.describe(license_key="The 32-character license key to check.")
    async def license_status_command(self, interaction: discord.Interaction, license_key: str):
        """Checks the status of a provided license key."""
        await interaction.response.defer(thinking=True)
        
        key = license_key.upper().replace('-', '').strip()
        license_info = LICENSE_DB.get(key)
        
        if not license_info:
            embed = discord.Embed(
                title="License Status Check ❌",
                description="The provided license key is **invalid** or does not exist.",
                color=discord.Color.red()
            )
            return await interaction.followup.send(embed=embed)

        is_valid = True
        expiry_ts = license_info.get("expiry_timestamp")
        
        if expiry_ts != "N/A" and int(expiry_ts) < time.time():
            is_valid = False
            expiry_display = f"Expired: <t:{expiry_ts}:F>"
        elif expiry_ts == "N/A":
            expiry_display = "LIFETIME (Never expires)"
        else:
            expiry_display = f"Expires: <t:{expiry_ts}:F> (<t:{expiry_ts}:R>)"

        # Determine general status
        if license_info.get("is_used") and is_valid:
            status = "✅ Active & Bound to a Guild"
            color = discord.Color.green()
        elif license_info.get("is_used") and not is_valid:
            status = "❌ Expired & Used"
            color = discord.Color.dark_red()
        elif not is_valid:
            status = "❌ Expired"
            color = discord.Color.dark_red()
        else:
            status = "⚠️ Unused & Valid"
            color = discord.Color.orange()
            
        # Build the embed
        embed = discord.Embed(
            title="License Status Check ℹ️",
            color=color
        )
        embed.add_field(name="Status", value=status, inline=False)
        embed.add_field(name="Key", value=f"`{key}`", inline=False)
        embed.add_field(name="Expiration", value=expiry_display, inline=True)
        embed.add_field(name="Used", value="Yes" if license_info.get("is_used") else "No", inline=True)
        embed.add_field(name="Bound Guild ID", value=license_info.get("guild_id") or "N/A", inline=True)

        await interaction.followup.send(embed=embed)


    @app_commands.command(name="license_guild", description="Applies a license key to this server to activate premium.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(license_key="The license key to apply to this server.")
    async def license_guild_command(self, interaction: discord.Interaction, license_key: str):
        """Applies a valid license key to the current guild."""
        if not DB:
            return await interaction.response.send_message("❌ Database not connected. Cannot apply license.", ephemeral=True)
            
        await interaction.response.defer(thinking=True)
        guild_id = interaction.guild_id
        
        key = license_key.upper().replace('-', '').strip()
        license_info = LICENSE_DB.get(key)
        
        # --- Validation Checks ---
        if not license_info:
            return await interaction.followup.send("❌ Invalid key. The provided license key was not found.", ephemeral=True)
        
        is_premium, expires_ts = is_guild_premium(guild_id)
        if is_premium:
             return await interaction.followup.send(f"❌ This server already has an active premium subscription expiring {'LIFETIME' if expires_ts == 'LIFETIME' else f'<t:{expires_ts}:R>'}. Wait for it to expire or contact the bot owner.", ephemeral=True)
        
        if license_info.get('expiry_timestamp') != "N/A" and int(license_info.get('expiry_timestamp', 0)) <= time.time():
            return await interaction.followup.send("❌ This key has **expired** and cannot be used.", ephemeral=True)
            
        if license_info.get('is_used') and license_info.get('guild_id') != guild_id:
            return await interaction.followup.send(f"❌ This key is already in use by another server (Guild ID: `{license_info.get('guild_id')}`).", ephemeral=True)
        
        # --- Application ---
        
        # 1. Update LICENSE_DB (mark key as used and bind to this guild)
        license_info['is_used'] = True
        license_info['guild_id'] = guild_id
        # PERSISTENCE: Save updated license to Firestore
        save_license_to_firestore(key, license_info) 
        
        # 2. Update CONFIG_DB (activate premium for this guild)
        guild_config = CONFIG_DB.get(guild_id, {})
        guild_config['premium'] = {
            'active': True,
            'license_key': key,
            'activated_by': interaction.user.id,
            'activated_at': int(time.time()),
            'expires_at': license_info['expiry_timestamp'] # "N/A" or timestamp
        }
        # PERSISTENCE: Save updated config to Firestore
        save_config_to_firestore(guild_id, guild_config)
        
        # 3. Success Message
        expiry_display = "LIFETIME" if license_info['expires_at'] == "LIFETIME" else f"Expires <t:{license_info['expiry_timestamp']}:R>"
        
        embed = discord.Embed(
            title="✅ Premium Activated Successfully!",
            description=f"**{interaction.guild.name}** now has Spectra Premium!",
            color=discord.Color.green()
        )
        embed.add_field(name="Key Used", value=f"`{key}`", inline=False)
        embed.add_field(name="Premium Status", value=expiry_display, inline=True)
        embed.add_field(name="Activated By", value=interaction.user.mention, inline=True)

        await interaction.followup.send(embed=embed)


# ==============================================================================
# 3. BOT EXECUTION
# ==============================================================================

if __name__ == "__main__":
    bot_token = os.environ.get("DISCORD_TOKEN") # Ensure you have DISCORD_TOKEN set on Render
    if not bot_token:
        print("ERROR: DISCORD_TOKEN environment variable not set.")
    else:
        bot = SpectraBot()
        bot.run(bot_token)