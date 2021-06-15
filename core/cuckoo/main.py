# Copyright (C) 2019-2021 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

import os
import click
import logging

from cuckoo.common.storage import cuckoocwd, Paths, InvalidCWDError
from cuckoo.common.log import (
    exit_error, print_info, print_error, print_warning, VERBOSE
)

@click.group(invoke_without_command=True)
@click.option("--cwd", help="Cuckoo Working Directory")
@click.option("--distributed", is_flag=True, help="Start Cuckoo in distributed mode")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging, including for non-Cuckoo modules")
@click.option("-d", "--debug", is_flag=True, help="Enable debug logging")
@click.option("-q", "--quiet", is_flag=True, help="Only log warnings and critical messages")
@click.pass_context
def main(ctx, cwd, distributed, debug, quiet, verbose):
    if not cwd:
        cwd = cuckoocwd.DEFAULT

    ctx.cwd_path = cwd
    if not cuckoocwd.exists(cwd):
        if ctx.invoked_subcommand == "createcwd":
            return

        exit_error(
            f"Cuckoo CWD {cwd} does not yet exist. Run "
            f"'cuckoo createcwd' if this is the first time you are "
            f"running Cuckoo with this CWD path"
        )

    try:
        cuckoocwd.set(cwd)
    except InvalidCWDError as e:
        exit_error(f"Invalid Cuckoo working directory: {e}")

    if verbose:
        ctx.loglevel = VERBOSE
    elif debug:
        ctx.loglevel = logging.DEBUG
    elif quiet:
        ctx.loglevel = logging.WARNING
    else:
        ctx.loglevel = logging.INFO

    if ctx.invoked_subcommand:
        return

    if not os.path.exists(Paths.monitor()):
        exit_error(
            "No monitor and stager binaries are present yet. "
            "Use 'cuckoo getmonitor <zip path>' to unpack and use monitor "
            "and stagers from a Cuckoo monitor zip."
        )

    from cuckoo.common.startup import StartupError
    from cuckoo.common.shutdown import (
        register_shutdown, call_registered_shutdowns
    )

    if distributed:
        from .startup import start_cuckoo_controller as start_cuckoo
    else:
        from .startup import start_cuckoo

    def _stopmsg():
        print("Stopping Cuckoo..")

    register_shutdown(_stopmsg, order=1)

    try:
        start_cuckoo(ctx.loglevel)
    except StartupError as e:
        exit_error(f"Failure during Cuckoo startup: {e}")
    finally:
        call_registered_shutdowns()

@main.command("createcwd")
@click.option("--regen-configs", is_flag=True)
@click.option("--update-directories", is_flag=True)
@click.pass_context
def create_cwd(ctx, update_directories, regen_configs):
    """Create the specified Cuckoo CWD"""
    from cuckoo.common.startup import StartupError
    from cuckoo.common.startup import create_configurations

    cwd_path = ctx.parent.cwd_path
    if os.path.isdir(ctx.parent.cwd_path):
        if not regen_configs and not update_directories:
            exit_error(f"Path {cwd_path} already exists.")

        if not cuckoocwd.is_valid(cwd_path):
            exit_error(
                f"Path {cwd_path} is not a valid Cuckoo CWD. "
                f"Cannot regenerate configurations."
            )

        if regen_configs:
            try:
                create_configurations()
                print_info("Re-created missing configuration files")
                return
            except StartupError as e:
                exit_error(f"Failure during configuration generation: {e}")

        if update_directories:
            try:
                cuckoocwd.update_missing()
                print_info("Created missing directories")
                return
            except InvalidCWDError as e:
                exit_error(f"Failed during directory updating: {e}")

    cuckoocwd.create(cwd_path)
    cuckoocwd.set(cwd_path)
    try:
        create_configurations()
        print_info(f"Created Cuckoo CWD at: {cwd_path}")
    except StartupError as e:
        exit_error(f"Failure during configuration generation: {e}")

@main.command("getmonitor")
@click.argument("zip_path")
def get_monitor(zip_path):
    """Use the monitor and stager binaries from the given
    Cuckoo monitor zip file."""
    from cuckoo.common.guest import unpack_monitor_components
    if not os.path.isfile(zip_path):
        exit_error(f"Zip file does not exist: {zip_path}")

    unpack_monitor_components(zip_path, cuckoocwd.root)

@main.group()
def machine():
    """Add machines to machinery configuration files."""
    pass

@machine.command("add")
@click.argument("machinery")
@click.argument("name")
@click.argument("label")
@click.argument("ip")
@click.argument("platform")
@click.option("--os-version", type=str, help="The version of the platform installed on the machine")
@click.option("--snapshot", type=str, help="A snapshot to use when restoring, other than the default snapshot.")
@click.option("--interface", type=str, help="The network interface that should be used to create network dumps.")
@click.option("--tags", default="", type=str, help="A comma separated list of tags that identify what dependencies/software is installed on the machine.")
def machine_add(machinery, name, label, ip, platform, os_version, snapshot,
                interface, tags):
    """Add a machine to a machinery configuration file."""
    from .startup import add_machine, StartupError
    try:
        add_machine(
            machinery, name=name, label=label, ip=ip, platform=platform,
            os_version=os_version, snapshot=snapshot, interface=interface,
            tags=list(filter(None, [t.strip() for t in tags.split(",")]))
        )
        print_info(f"Added machine {name} to machinery {machinery}")
    except StartupError as e:
        exit_error(f"Failed to add machine. {e}")


def _submit_files(settings, *targets):
    from cuckoo.common import submit
    from cuckoo.common.storage import enumerate_files
    files = []
    for path in targets:
        if not os.path.exists(path):
            yield None, path, "No such file or directory"

        files.extend(enumerate_files(path))

    for path in files:
        try:
            analysis_id = submit.file(
                path, settings, file_name=os.path.basename(path)
            )
            yield analysis_id, path, None
        except submit.SubmissionError as e:
            yield None, path, e

def _submit_urls(settings, *targets):
    from cuckoo.common import submit
    for url in targets:
        try:
            analysis_id = submit.url(url, settings)
            yield analysis_id, url, None
        except submit.SubmissionError as e:
            yield None, url, e


@main.command("submit")
@click.argument("target", nargs=-1)
@click.option("-u", "--url", is_flag=True, help="Submit URL(s) instead of files")
@click.option(
    "--platform", multiple=True,
    help="The platform and optionally the OS version the analysis task must "
         "run on. Specified as platform,osversion or just platform."
)
@click.option("--timeout", type=int, default=120, help="Analysis timeout in seconds")
@click.option("--priority", type=int, default=1, help="The priority of this analysis")
def submission(target, url, platform, timeout, priority):
    """Create a new file analysis"""
    from cuckoo.common import submit
    from cuckoo.common.storage import Paths

    try:
        submit.settings_maker.set_machinesdump_path(Paths.machinestates())
    except submit.SubmissionError as e:
        exit_error(f"Submission failed: {e}")

    try:
        s_helper = submit.settings_maker.new_settings()
        s_helper.set_timeout(timeout)
        s_helper.set_priority(priority)
        s_helper.set_manual(False)

        for p_v in platform:
            # Split platform,version into usable values
            platform_version = p_v.split(",", 1)

            if len(platform_version) == 2:
                s_helper.add_platform(
                    platform=platform_version[0],
                    os_version=platform_version[1]
                )
            else:
                s_helper.add_platform(platform=platform_version[0])

        settings = s_helper.make_settings()
    except submit.SubmissionError as e:
        exit_error(f"Submission failed: {e}")

    if url:
        submitter, kind = _submit_urls, "URL"
    else:
        submitter, kind = _submit_files, "file"

    try:
        for analysis_id, target, error in submitter(settings, *target):
            if error:
                print_error(f"Failed to submit {kind}: {target}. {error}")
            else:
                print_info(f"Submitted {kind}: {analysis_id} -> {target}")
    finally:
        try:
            submit.notify()
        except submit.SubmissionError as e:
            print_warning(e)


@main.group(invoke_without_command=True)
@click.option("-h", "--host", default="localhost", help="Host to bind the development web interface server on")
@click.option("-p", "--port", default=8000, help="Port to bind the development web interface server on")
@click.option("--autoreload", is_flag=True, help="Automatically reload modified Python files")
@click.pass_context
def web(ctx, host, port, autoreload):
    """Start the Cuckoo web interface (development server)"""
    if ctx.invoked_subcommand:
        return

    from cuckoo.web.web.startup import init_web, start_web
    init_web(
        ctx.parent.cwd_path, ctx.parent.loglevel, logfile=Paths.log("web.log")
    )
    start_web(host, port, autoreload=autoreload)

@web.command("djangocommand", context_settings=(dict(ignore_unknown_options=True)))
@click.argument("django_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def webdjangocommand(ctx, django_args):
    """Arguments for this command are passed to Django."""
    from cuckoo.web.web.startup import(
        djangocommands, set_path_settings, init_web
    )

    if "runserver" in django_args:
        init_web(
            ctx.parent.parent.cwd_path, ctx.parent.parent.loglevel,
            logfile=Paths.log("web.log")
        )
    else:
        set_path_settings()

    djangocommands(*django_args)

@main.group(invoke_without_command=True)
@click.option("-h", "--host", default="localhost", help="Host to bind the development web API server on")
@click.option("-p", "--port", default=8090, help="Port to bind the development web API server on")
@click.option("--autoreload", is_flag=True, help="Automatically reload modified Python files")
@click.pass_context
def api(ctx, host, port, autoreload):
    """Start the Cuckoo web API (development server)"""
    if ctx.invoked_subcommand:
        return

    from cuckoo.web.api.startup import init_api, start_api
    init_api(
        ctx.parent.cwd_path, ctx.parent.loglevel, logfile=Paths.log("api.log")
    )
    start_api(host, port, autoreload=autoreload)

@api.command("token")
@click.option("-l", "--list", is_flag=True, help="List all current API tokens and their owners")
@click.option("-c", "--create", type=str, help="Create a new API token for a given owner name")
@click.option("--admin", is_flag=True, help="Grant admin priviles to API token being created")
@click.option("-d", "--delete", type=int, help="Delete the specified token by its token ID")
@click.option("--clear", is_flag=True, help="Delete all API tokens")
def apitoken(list, create, admin, delete, clear):
    """List, create, and delete API tokens."""
    from cuckoo.web.api.startup import load_app
    load_app()
    from cuckoo.web.api import apikey
    if list:
        apikey.print_api_keys()
    elif create:
        try:
            key, identifier = apikey.create_key(create, admin)
            print_info(f"Created key {key} with ID: {identifier}")
        except apikey.APIKeyError as e:
            exit_error(f"API token creation failed: {e}")
    elif delete:
        if apikey.delete_key(delete):
            print_info(f"Deleted key with ID {delete}")
    elif clear:
        if click.confirm("Delete all API tokens?"):
            count = apikey.delete_all()
            print_info(f"Deleted {count} API tokens")
    else:
        with click.Context(apitoken) as ctx:
            print(apitoken.get_help(ctx))

@api.command("djangocommand", context_settings=(dict(ignore_unknown_options=True)))
@click.argument("django_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def apidjangocommand(ctx, django_args):
    """Arguments for this command are passed to Django."""
    from cuckoo.web.api.startup import(
        djangocommands, set_path_settings, init_api
    )

    if "runserver" in django_args:
        init_api(
            ctx.parent.parent.cwd_path, ctx.parent.parent.loglevel,
            logfile=Paths.log("api.log")
        )
    else:
        set_path_settings()

    djangocommands(*django_args)

@main.command()
@click.pass_context
def importmode(ctx):
    """Start the Cuckoo import controller."""
    if ctx.invoked_subcommand:
        return

    from cuckoo.common.startup import StartupError
    from cuckoo.common.shutdown import (
        register_shutdown, call_registered_shutdowns
    )
    from .startup import start_importmode

    def _stopmsg():
        print("Stopping import mode..")

    register_shutdown(_stopmsg, order=1)

    try:
        start_importmode(ctx.parent.loglevel)
    except StartupError as e:
        exit_error(f"Failure during import mode startup: {e}")
    finally:
        call_registered_shutdowns()
