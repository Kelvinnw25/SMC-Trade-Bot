from v2.app import Orchestrator


def main():
    bot = Orchestrator.create()
    bot.run_forever()


if __name__ == "__main__":
    main()