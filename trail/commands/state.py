from __future__ import annotations

import typer

from trail.commands.helpers import call_daemon
from trail.output.rendering import print_output


state_app = typer.Typer(no_args_is_help=True)


@state_app.command("dump")
def state_dump(
    session: str = typer.Option(..., "--session"),
    cw_run: bool = typer.Option(False, "--cw-run", help="只读取货币战争本局档案。"),
) -> None:
    payload = {"session_id": session}
    if cw_run:
        payload["cw_run"] = True
    print_output("state.dump", call_daemon("state.dump", payload, session_id=session))
