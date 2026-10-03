import os
import json
import discord
from discord.ext import commands
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---- Fake web server ----
PORT = int(os.environ.get("PORT", 8000))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_web():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()

threading.Thread(target=run_web, daemon=True).start()

# ---- Load config ----
with open("config.json") as f:
    config = json.load(f)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

bot = commands.Bot(
    command_prefix=config.get("prefix", "/"), 
    intents=discord.Intents.default()
)

@bot.event
async def on_ready():
    print(f"✅ LIVE! Logged in as {bot.user}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

bot.run(BOT_TOKEN)
