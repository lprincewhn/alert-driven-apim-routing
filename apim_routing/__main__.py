"""Run with python3 -m apim_routing --config config.local.json COMMAND."""

import argparse
import getpass
import json
import os
import sys

from .azure import Azure, AzureError
from .config import ConfigError, load
from .deploy import deploy, disable_alerts, enable_alerts, grant_roles, install_policy, smoke
from .render import manifest, render


def parser():
    result = argparse.ArgumentParser(description="Alert-driven degraded routing for existing APIM (Azure Public Cloud)")
    result.add_argument("--config", required=True, help="Secret-free JSON configuration")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Validate configuration offline")
    output = commands.add_parser("render", help="Generate secret-free XML, WDL, alerts and manifest offline")
    output.add_argument("--output-dir", default="rendered")
    deployment = commands.add_parser("deploy", help="Deploy controller, Action Group, disabled alerts and missing named values")
    deployment.add_argument("--webhook-env", default="DINGTALK_WEBHOOK", help="Environment variable containing DingTalk webhook; otherwise getpass")
    commands.add_parser("grant-controller-roles", help="Grant Contributor only on both named value scopes")
    installation = commands.add_parser("install-policy", help="Explicitly replace the existing API policy after saving a local backup")
    installation.add_argument("--confirm", required=True, action="store_true")
    installation.add_argument("--backup", required=True, help="New local JSON backup path (may contain existing policy secrets)")
    check = commands.add_parser("smoke", help="Synthetic controller/notification test; temporary route changes, no model traffic")
    check.add_argument("--confirm-mutations", required=True, action="store_true")
    check.add_argument("--receipt", default="smoke-receipt.json")
    enable = commands.add_parser("enable-alerts", help="Enable only after role, policy and recent smoke checks")
    enable.add_argument("--confirm", required=True, action="store_true")
    enable.add_argument("--smoke-receipt", required=True)
    commands.add_parser("disable-alerts", help="Disable all configured metric and APIM log alerts")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = load(args.config)
        if args.command == "validate":
            result = {"valid": True, **manifest(config)}
        elif args.command == "render":
            result = {"files": render(config, args.output_dir)}
        else:
            azure = Azure(config["subscription_id"])
            if args.command == "deploy":
                webhook = os.environ.get(args.webhook_env)
                if webhook is None:
                    if not sys.stdin.isatty():
                        raise ConfigError("Noninteractive deploy requires the webhook environment variable")
                    webhook = getpass.getpass("DingTalk webhook (hidden): ")
                result = deploy(config, azure, webhook)
            elif args.command == "grant-controller-roles":
                result = grant_roles(config, azure)
            elif args.command == "install-policy":
                result = install_policy(config, azure, args.backup)
            elif args.command == "smoke":
                result = smoke(config, azure, args.receipt)
            elif args.command == "enable-alerts":
                result = enable_alerts(config, azure, args.smoke_receipt)
            else:
                result = disable_alerts(config, azure)
        print(json.dumps(result, indent=2))
        return 0
    except (ConfigError, AzureError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        # Network payloads and paths may contain credentials; never print them.
        print("Error: unexpected local or Azure response shape; details suppressed", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted: verify Azure state and any smoke named-value restoration", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
