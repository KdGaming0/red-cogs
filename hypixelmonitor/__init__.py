from .hypixelmonitor import HypixelMonitor

async def setup(bot):
    await bot.add_cog(HypixelMonitor(bot))