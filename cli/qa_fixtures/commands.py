"""Flask CLI commands for waitlist CTA QA fixtures."""

from __future__ import annotations

import os
import sys

import click
from flask.cli import with_appcontext

from cli.qa_fixtures.registry import SCENARIO_REGISTRY, all_scenario_keys, fixture_emails_for_keys
from cli.qa_fixtures.safety import (
    KNOWN_FIXTURE_PASSWORD,
    is_production_environment,
    production_confirmation_prompt,
    require_production_ack,
)
from cli.qa_fixtures.scenarios import reset_fixture_users, seed_all_fixture_scenarios, seed_scenario
from models import db


def _resolve_scenario_keys(scenario):
    keys = all_scenario_keys()
    if scenario:
        if scenario not in keys:
            raise click.ClickException(
                f"Unknown scenario {scenario!r}. Choose from: {', '.join(keys)}"
            )
        return [scenario]
    return keys


def _format_expected_cta(expected_cta) -> str:
    if expected_cta is None:
        return "(no CTA expected)"
    if isinstance(expected_cta, list):
        parts = []
        for item in expected_cta:
            parts.append(
                "feature_interest={feature_interest}\n"
                "source={source}\n"
                "cta_context={cta_context}".format(**item)
            )
        return "\n---\n".join(parts)
    return (
        "feature_interest={feature_interest}\n"
        "source={source}\n"
        "cta_context={cta_context}".format(**expected_cta)
    )


def _print_results_table(rows):
    click.echo(
        "\nScenario           Email                                          "
        "URL                          Expected CTA metadata"
    )
    click.echo("─" * 110)
    for row in rows:
        cta_block = _format_expected_cta(SCENARIO_REGISTRY[row["scenario"]]["expected_cta"])
        first_line = (
            f"{row['scenario']:<18} {row['email']:<46} "
            f"{row['test_path']:<28} {cta_block.splitlines()[0]}"
        )
        click.echo(first_line)
        indent = " " * 95
        for line in cta_block.splitlines()[1:]:
            click.echo(f"{indent}{line}")


@click.command("seed-waitlist-cta-test-data")
@click.option("--reset", is_flag=True, help="Delete fixture users before seeding.")
@click.option("--scenario", default=None, help="Seed only one scenario key.")
@click.option(
    "--allow-production",
    is_flag=True,
    help="Acknowledge running in a production-classified environment.",
)
@click.option("--yes", is_flag=True, help="Skip interactive production confirmation (non-TTY).")
@with_appcontext
def seed_waitlist_cta_test_data(reset, scenario, allow_production, yes):
    """Create or refresh waitlist CTA QA fixture users."""
    keys = _resolve_scenario_keys(scenario)
    require_production_ack(allow_production=allow_production, yes=yes)
    if is_production_environment() and sys.stdin.isatty() and not yes:
        production_confirmation_prompt(fixture_emails_for_keys(keys))

    if reset:
        deleted = reset_fixture_users(keys)
        click.echo(f"Reset removed {deleted} fixture user(s).")

    try:
        if scenario:
            reset_fixture_users([scenario], commit=False)
            result = seed_scenario(scenario)
            db.session.commit()
            results = [result]
        elif keys == all_scenario_keys():
            results = seed_all_fixture_scenarios()
        else:
            results = []
            for key in keys:
                reset_fixture_users([key], commit=False)
                results.append(seed_scenario(key))
            db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    _print_results_table(results)
    base_url = (os.getenv("PUBLIC_BASE_URL") or "http://127.0.0.1:5000").rstrip("/")
    click.echo(f"\nLogin URL: {base_url}/login")
    click.echo(f"Password for all fixtures: {KNOWN_FIXTURE_PASSWORD}")


@click.command("reset-waitlist-cta-test-data")
@click.option("--scenario", default=None, help="Reset only one scenario key.")
@click.option(
    "--allow-production",
    is_flag=True,
    help="Acknowledge running in a production-classified environment.",
)
@click.option("--yes", is_flag=True, help="Skip interactive production confirmation (non-TTY).")
@with_appcontext
def reset_waitlist_cta_test_data_cmd(scenario, allow_production, yes):
    """Delete waitlist CTA QA fixture users and related data."""
    keys = _resolve_scenario_keys(scenario)
    require_production_ack(allow_production=allow_production, yes=yes)
    if is_production_environment() and sys.stdin.isatty() and not yes:
        production_confirmation_prompt(fixture_emails_for_keys(keys))

    deleted = reset_fixture_users(keys)
    click.echo(f"Removed {deleted} fixture user(s).")


def register_cli(app):
    app.cli.add_command(seed_waitlist_cta_test_data)
    app.cli.add_command(reset_waitlist_cta_test_data_cmd)
