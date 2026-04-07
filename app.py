from codex_discord.bot import CodexDiscordBot
from codex_discord.core import configure_logging, load_app_config


def main() -> None:
    configure_logging()
    config = load_app_config()
    bot = CodexDiscordBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
