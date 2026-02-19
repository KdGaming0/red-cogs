from .hypixelupdatechecker import HypixelUpdateChecker

async def setup(bot):
    await bot.add_cog(HypixelUpdateChecker(bot))