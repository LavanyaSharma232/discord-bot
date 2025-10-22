#
# ===================================================================================
#  The Universal Bot: bot.py (MULTI-PROJECT FINAL VERSION)
# ===================================================================================
#
import discord
from discord.ext import commands
from discord import app_commands
import os
import requests
import sqlite3
from flask import Flask, request, abort
from threading import Thread
import hashlib
import hmac
from dotenv import load_dotenv
import psycopg2 # New library for PostgreSQL
import secrets  # New library for generating secure secrets
import re

# --- CONFIGURATION ---
# The only secrets we need from the environment are the Discord token and the database URL.
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')

# --- DATABASE SETUP ---
def get_db_connection():
    """Establishes a connection to the database."""
    # When deployed on Railway, DATABASE_URL will exist.
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    # For local testing, we can fall back to a simple SQLite file.
    else:
        print("WARNING: DATABASE_URL not found. Falling back to local scores.db for testing.")
        return sqlite3.connect("local_scores.db", check_same_thread=False)

# --- DISCORD BOT SETUP ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    """This function runs when the bot successfully connects to Discord."""
    print(f'Bot is online and logged in as {bot.user}')
    try:
        # Sync the slash commands with Discord.
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

# --- DATA HELPER FUNCTIONS (Now repo-aware) ---
def update_score(repo_id, username, points_to_add):
    """Updates the score for a user for a specific repository."""
    conn = get_db_connection()
    cur = conn.cursor()
    # Find existing score for this user in this specific repo
    cur.execute("SELECT points FROM scores WHERE repo_id = %s AND github_username = %s", (repo_id, username))
    result = cur.fetchone()
    if result:
        new_points = result[0] + points_to_add
        cur.execute("UPDATE scores SET points = %s WHERE repo_id = %s AND github_username = %s", (new_points, repo_id, username))
    else:
        cur.execute("INSERT INTO scores (repo_id, github_username, points) VALUES (%s, %s, %s)", (repo_id, username, points_to_add))
    conn.commit()
    cur.close()
    conn.close()
    print(f"Updated score for {username} in repo_id {repo_id}. Added {points_to_add} points.")

def get_points_from_pr_labels(repo_name, issue_number):
    """Fetches issue labels from GitHub API and returns points."""
    url = f"https://api.github.com/repos/{repo_name}/issues/{issue_number}"
    response = requests.get(url)
    if response.status_code != 200:
        print(f"Error fetching issue #{issue_number} from {repo_name}. Status: {response.status_code}")
        return 0
    data = response.json()
    labels = {label['name'].lower() for label in data.get('labels', [])}
    if 'hard' in labels: return 20
    elif 'medium' in labels: return 10
    elif 'easy' in labels: return 5
    return 0

# --- NEW ADMIN SLASH COMMANDS ---
@bot.tree.command(name="register", description="Register or update your GitHub repository for scoring.")
@app_commands.describe(repo_name="Your repository in 'Username/RepoName' format.", channel="The channel for leaderboard announcements.")
@app_commands.checks.has_permissions(administrator=True)
async def register(interaction: discord.Interaction, repo_name: str, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True) # Acknowledge privately
    guild_id = interaction.guild.id
    admin_user_id = interaction.user.id
    webhook_secret = secrets.token_hex(16)
    
    conn = get_db_connection()
    cur = conn.cursor()
    # "Upsert" logic: Update if the server is already registered, otherwise insert a new record.
    cur.execute(
        """
        INSERT INTO repositories (guild_id, repo_name, webhook_secret, channel_id, admin_user_id) 
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (guild_id) 
        DO UPDATE SET repo_name = EXCLUDED.repo_name, webhook_secret = EXCLUDED.webhook_secret, channel_id = EXCLUDED.channel_id, admin_user_id = EXCLUDED.admin_user_id
        RETURNING id;
        """,
        (guild_id, repo_name, webhook_secret, channel.id, admin_user_id)
    )
    repo_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    # Railway automatically provides its public domain as an environment variable.
    base_url = os.getenv('RAILWAY_PUBLIC_DOMAIN', 'discord-bot-production-89dc.up.railway.app') 
    payload_url = f"https://{base_url}/github-webhook/{repo_id}"

    embed = discord.Embed(title="✅ Repository Registered Successfully!", color=discord.Color.green())
    embed.description = "Please CREATE or UPDATE the webhook in your GitHub repository's settings with these values."
    embed.add_field(name="Payload URL", value=f"```{payload_url}```", inline=False)
    embed.add_field(name="Webhook Secret", value=f"```{webhook_secret}```", inline=False)
    embed.add_field(name="Content Type", value="`application/json`", inline=False)
    embed.set_footer(text="This information is private and unique to your server.")
    await interaction.followup.send(embed=embed, ephemeral=True)

@register.error
async def register_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("Sorry, you must be a server administrator to run this command.", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Displays the leaderboard for this server's repository.")
async def leaderboard(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Select scores by joining through the repositories table to find the one for this Discord server.
    cur.execute("SELECT s.github_username, s.points FROM scores s JOIN repositories r ON s.repo_id = r.id WHERE r.guild_id = %s ORDER BY s.points DESC LIMIT 10", (guild_id,))
    results = cur.fetchall()
    cur.close()
    conn.close()

    if not results:
        await interaction.response.send_message("The leaderboard is currently empty for the registered repository.")
        return

    embed = discord.Embed(title="🏆 Open Source Event Leaderboard 🏆", color=discord.Color.gold())
    description = ""
    for rank, (username, points) in enumerate(results, 1):
        description += f"**{rank}.** {username} - `{points} points`\n"
    embed.description = description
    await interaction.response.send_message(embed=embed)

# --- FLASK WEB SERVER (Now Dynamic) ---
app = Flask(__name__)

@app.route('/github-webhook/<int:repo_id>', methods=['POST'])
def github_webhook(repo_id):
    conn = get_db_connection()
    cur = conn.cursor()
    # Fetch the specific configuration for the repo that received the webhook
    cur.execute("SELECT webhook_secret, channel_id, repo_name FROM repositories WHERE id = %s", (repo_id,))
    repo_config = cur.fetchone()
    cur.close()
    conn.close()

    if not repo_config:
        return ('Repository not found', 404)
    
    repo_secret, repo_channel_id, repo_name = repo_config

    # Validate the webhook using the secret fetched from the database
    signature = request.headers.get('X-Hub-Signature-256')
    if not signature or not signature.startswith('sha256='):
        abort(400)
    hash_object = hmac.new(repo_secret.encode('utf-8'), msg=request.data, digestmod=hashlib.sha256)
    expected_signature = 'sha256=' + hash_object.hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        abort(403)

    # Process the PR, now using the dynamic repo_name and channel_id
    data = request.json
    if data.get('action') == 'closed' and data['pull_request']['merged']:
        pr = data['pull_request']
        username = pr['user']['login']
        pr_number = pr['number']
        pr_title = pr['title']
        pr_url = pr['html_url']
        pr_body = pr.get('body') or ""

        issue_number_match = re.search(r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s#(\d+)", pr_body, re.IGNORECASE)
        if not issue_number_match:
            print(f"PR #{pr_number} for repo {repo_name} has no linked issue. No points awarded.")
            return ('', 204)

        issue_number = int(issue_number_match.group(1))
        points = get_points_from_pr_labels(repo_name, issue_number)

        if points > 0:
            update_score(repo_id, username, points)
            channel = bot.get_channel(repo_channel_id)
            if channel:
                embed = discord.Embed(title="🎉 New Contribution Merged! 🎉", description=f"**[{pr_title}]({pr_url})**", color=discord.Color.green())
                embed.add_field(name="Contributor", value=f"**{username}**", inline=True)
                embed.add_field(name="Points Awarded", value=f"**{points}**", inline=True)
                bot.loop.create_task(channel.send(embed=embed))
    return ('', 204)

# --- RUN EVERYTHING ---
def run_bot():
    # We run the bot in a separate thread so it doesn't block the web server.
    bot.run(DISCORD_TOKEN)

# Start the bot in a separate thread when the script is executed.
bot_thread = Thread(target=run_bot)
bot_thread.daemon = True
bot_thread.start()






