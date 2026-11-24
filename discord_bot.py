import os
import discord
from discord import app_commands
from discord.ext import commands
import requests

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CONVERTER_URL = os.environ.get("CONVERTER_URL", "https://convertisseur-de-lien-facebook-de-jean-b6b7d.containers.snapdeploy.app").rstrip("/")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Connecté : {bot.user} | {len(synced)} commande(s) synchronisée(s)")
    except Exception as e:
        print("Erreur synchronisation :", e)

class FacebookFallbackView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=600)
        self.add_item(discord.ui.Button(label="Ouvrir sur Facebook", emoji="🌐", style=discord.ButtonStyle.link, url=url))

@bot.tree.command(name="facebook", description="Convertit un lien Facebook /share/ en lien direct")
@app_commands.describe(lien="Le lien Facebook /share/ à convertir")
async def facebook(interaction: discord.Interaction, lien: str):
    await interaction.response.defer(ephemeral=True)
    try:
        response = requests.post(f"{CONVERTER_URL}/api/resolve", json={"url": lien.strip()}, headers={"Accept": "application/json"}, timeout=20)
        try:
            data = response.json()
        except Exception:
            data = {}

        if response.ok and data.get("final_url"):
            final_url = data["final_url"]
            titre = "⚡ **Lien trouvé dans le cache**" if data.get("cached") is True else "✅ **Lien Facebook converti**"
            await interaction.followup.send(f"{titre}\n\n{final_url}", ephemeral=True)
            return

        if data.get("blocked") or data.get("unresolved") or response.status_code in (422, 502):
            await interaction.followup.send(
                "⚠️ **Facebook n’a pas fourni le lien final automatiquement.**\n\n"
                "1. Ouvre Facebook avec le bouton ci-dessous.\n"
                "2. **Tu dois être connecté à ton compte Facebook dans le navigateur.**\n"
                "3. Attends que la bonne page soit affichée.\n"
                "4. Copie l’adresse dans la barre du navigateur.\n"
                "5. Utilise ensuite la commande **/facebook-enregistrer** pour mémoriser le résultat.",
                view=FacebookFallbackView(lien.strip()), ephemeral=True)
            return

        await interaction.followup.send("❌ " + data.get("error", "Impossible de convertir ce lien."), ephemeral=True)
    except requests.RequestException:
        await interaction.followup.send("❌ Le convertisseur est momentanément indisponible.", ephemeral=True)

@bot.tree.command(name="facebook-enregistrer", description="Enregistre manuellement un lien Facebook dans le cache")
@app_commands.describe(lien_partage="Le lien Facebook /share/ d’origine", lien_final="Le lien final obtenu dans le navigateur")
async def facebook_enregistrer(interaction: discord.Interaction, lien_partage: str, lien_final: str):
    await interaction.response.defer(ephemeral=True)
    try:
        response = requests.post(f"{CONVERTER_URL}/api/cache-add", json={"original_url": lien_partage.strip(), "final_url": lien_final.strip()}, headers={"Accept": "application/json"}, timeout=20)
        try:
            data = response.json()
        except Exception:
            data = {}

        if response.ok:
            await interaction.followup.send(
                "✅ **Lien enregistré dans le cache.**\n\n"
                f"{data.get('final_url', lien_final.strip())}\n\n"
                "La prochaine conversion du même lien /share/ sera automatique.",
                ephemeral=True)
            return

        await interaction.followup.send("❌ " + data.get("error", "Impossible d’enregistrer ce lien."), ephemeral=True)
    except requests.RequestException:
        await interaction.followup.send("❌ Le convertisseur est momentanément indisponible.", ephemeral=True)

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN absente")

bot.run(DISCORD_TOKEN)
