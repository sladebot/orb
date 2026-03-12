from __future__ import annotations

from . import config as config_cli
from . import auth as auth_cli


def _prompt_choice(prompt: str, options: list[tuple[str, str]]) -> str:
    print(prompt)
    for key, label in options:
        print(f"  {key}. {label}")
    return input("Select an option: ").strip().lower()


async def _configure_openai() -> None:
    choice = _prompt_choice(
        "\nOpenAI setup",
        [
            ("1", "Use browser OAuth"),
            ("2", "Paste API key"),
            ("3", "Back"),
        ],
    )
    if choice == "1":
        await auth_cli.auth_openai()
        return
    if choice == "2":
        key = input("OpenAI API key: ").strip()
        if not key:
            print("No OpenAI API key provided.")
            return
        auth_cli._save_credentials("openai", {"api_key": key})
        print(f"OpenAI key stored at {auth_cli.CREDS_PATH}")


def _configure_local_models() -> None:
    enabled = config_cli.local_models_enabled()
    label = "enabled" if enabled else "disabled"
    choice = _prompt_choice(
        f"\nLocal models are currently {label}.",
        [
            ("1", "Enable local models"),
            ("2", "Disable local models"),
            ("3", "Back"),
        ],
    )
    if choice == "1":
        config_cli.set_value("local_models", "true")
        print("local_models = true")
    elif choice == "2":
        config_cli.set_value("local_models", "false")
        print("local_models = false")


async def run_onboarding() -> None:
    print("Orb onboarding\n")
    while True:
        choice = _prompt_choice(
            "\nWhat do you want to configure?",
            [
                ("1", "Anthropic auth"),
                ("2", "OpenAI auth"),
                ("3", "Local models"),
                ("4", "Show current auth and config"),
                ("5", "Finish"),
            ],
        )
        if choice == "1":
            await auth_cli.auth_anthropic()
        elif choice == "2":
            await _configure_openai()
        elif choice == "3":
            _configure_local_models()
        elif choice == "4":
            print()
            await auth_cli.auth_status()
            print()
            config_cli.show_config()
        elif choice in {"5", "q", "quit", "exit"}:
            print("Onboarding complete.")
            return
        else:
            print("Invalid choice.")
