"""
See https://github.com/zsimic/pickley
"""

import logging
import logging.config
import os
import sys
from logging.handlers import RotatingFileHandler

import click

from pickley import CurrentFolder, PingLock, PingLockException, short, system
from pickley.package import DELIVERERS, PACKAGERS, VenvPackager
from pickley.settings import meta_folder, SETTINGS


def setup_audit_log():
    """Log to <base>/audit.log"""
    if system.DRYRUN or system.AUDIT_HANDLER:
        return
    path = SETTINGS.meta.full_path("audit.log")
    system.ensure_folder(path)
    system.AUDIT_HANDLER = RotatingFileHandler(path, maxBytes=500 * 1024, backupCount=1)
    system.AUDIT_HANDLER.setLevel(logging.DEBUG)
    system.AUDIT_HANDLER.setFormatter(logging.Formatter("%(asctime)s [%(process)s] %(levelname)s - %(message)s"))
    logging.root.addHandler(system.AUDIT_HANDLER)
    system.info(":: %s", system.represented_args(sys.argv), output=False)


def setup_debug_log():
    """Log to stderr"""
    if system.DEBUG_HANDLER:
        return
    system.OUTPUT = False
    system.DEBUG_HANDLER = logging.StreamHandler()
    system.DEBUG_HANDLER.setLevel(logging.DEBUG)
    system.DEBUG_HANDLER.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    logging.root.addHandler(system.DEBUG_HANDLER)
    logging.root.setLevel(logging.DEBUG)


def relaunch():
    """
    Rerun with same args, to pick up freshly bootstrapped installation
    """
    system.OUTPUT = False
    system.run_program(*sys.argv, stdout=sys.stdout, stderr=sys.stderr)
    if not system.DRYRUN:
        sys.exit(0)


def bootstrap(testing=False):
    """
    Bootstrap pickley: re-install it as venv if need be

    Packaged as a pex, pickley is easy to distribute,
    however there are some edge cases where running pip from a pex-packaged CLI doesn't work very well
    So, first thing we do is re-package ourselves as a venv on the target machine
    """
    if not testing and (system.QUIET or getattr(sys, "real_prefix", None)):
        # Don't bootstrap in quiet mode, or if we're running from a venv already
        return

    p = VenvPackager(system.PICKLEY)
    p.refresh_current()
    if p.current.packager == p.implementation_name:
        # We're already packaged correctly, no need to bootstrap
        return

    try:
        # Re-install ourselves with correct packager
        p.internal_install(bootstrap=True)
        relaunch()

    except PingLockException:
        return


@click.group(context_settings=dict(help_option_names=["-h", "--help"], max_content_width=140), epilog=__doc__)
@click.version_option()
@click.option("--debug", is_flag=True, help="Show debug logs")
@click.option("--quiet", "-q", is_flag=True, help="Quiet mode, do not output anything")
@click.option("--dryrun", "-n", is_flag=True, help="Perform a dryrun")
@click.option("--base", "-b", metavar="PATH", help="Base installation folder to use (default: folder containing pickley)")
@click.option("--python", metavar="PATH", help="Python interpreter to use")
@click.option("--delivery", "-d", type=click.Choice(DELIVERERS.names()), help="Delivery method to use")
@click.option("--packager", "-p", type=click.Choice(PACKAGERS.names()), help="Packager to use")
def main(debug, quiet, dryrun, base, python, delivery, packager):
    """
    Package manager for python CLIs
    """
    if dryrun:
        debug = True
    if debug:
        quiet = False
    system.DRYRUN = bool(dryrun)
    system.QUIET = bool(quiet)

    if base:
        base = system.resolved_path(base)
        if not os.path.exists(base):
            system.abort("Can't use %s as base: folder does not exist", short(base))
        SETTINGS.set_base(base)

    SETTINGS.set_cli_config(python=python, delivery=delivery, packager=packager)
    SETTINGS.add(system.config_paths(system.TESTING))

    # Disable logging.config, as pip tries to get smart and configure all logging...
    logging.config.dictConfig = lambda x: None
    logging.getLogger("pip").setLevel(logging.WARNING)

    logging.root.setLevel(logging.INFO if quiet else logging.DEBUG)

    if debug:
        # Log to console with --debug or --dryrun
        setup_debug_log()


@main.command()
@click.option("--verbose", "-v", is_flag=True, help="Show more information")
@click.argument("packages", nargs=-1, required=False)
def check(verbose, packages):
    """
    Check whether specified packages need an upgrade
    """
    setup_audit_log()
    code = 0
    packages = SETTINGS.resolved_packages(packages) or SETTINGS.current_names()
    if not packages:
        system.info("No packages installed")

    else:
        for name in packages:
            p = PACKAGERS.resolved(name)
            p.refresh_current()
            p.refresh_desired()
            if not p.desired.valid:
                system.error(p.desired.representation(verbose))
                code = 1
            elif not p.current.version or not p.current.valid:
                system.info(p.desired.representation(verbose, note="is not installed"))
                code = 1
            elif p.current.version != p.desired.version:
                system.info(p.current.representation(verbose, note="can be upgraded to %s" % p.desired.version))
                code = 1
            else:
                system.info(p.current.representation(verbose, note="is installed"))

    sys.exit(code)


@main.command()
@click.option("--verbose", "-v", is_flag=True, help="Show more information")
def list(verbose):
    """
    List installed packages
    """
    setup_audit_log()
    packages = SETTINGS.current_names()
    if not packages:
        system.info("No packages installed")

    else:
        for name in packages:
            p = PACKAGERS.resolved(name)
            p.refresh_current()
            system.info(p.current.representation(verbose))


@main.command()
@click.option("--force", "-f", is_flag=True, help="Force installation, even if already installed")
@click.argument("packages", nargs=-1, required=True)
def install(force, packages):
    """
    Install a package from pypi
    """
    setup_audit_log()
    bootstrap()

    packages = SETTINGS.resolved_packages(packages)
    for name in packages:
        p = PACKAGERS.resolved(name)
        p.install(force=force)


@main.command()
@click.option("--dist", "-d", default="./dist", show_default=True, help="Folder where to produce package")
@click.option("--build", "-b", default="./build", show_default=True, help="Folder to use as build cache")
@click.argument("folder", required=True)
def package(dist, build, folder):
    """
    Package a project from source checkout
    """
    dist = system.resolved_path(dist)
    build = system.resolved_path(build)
    folder = system.resolved_path(folder)

    SETTINGS.meta = meta_folder(build)
    setup_audit_log()
    bootstrap()

    if not os.path.isdir(folder):
        system.abort("Folder %s does not exist", short(folder))

    setup_py = os.path.join(folder, "setup.py")
    if not os.path.exists(setup_py):
        system.abort("No setup.py in %s", short(folder))

    with CurrentFolder(folder):
        # Some setup.py's assume their working folder is the folder where they're in
        name = system.run_program(sys.executable, setup_py, "--name", fatal=False)
        if not name:
            system.abort("Could not determine package name from %s", short(setup_py))

    p = PACKAGERS.resolved(name)
    p.dist_folder = system.resolved_path(dist)
    p.build_folder = system.resolved_path(build)
    p.source_folder = system.resolved_path(folder)
    r = p.package()
    system.info("Packaged %s successfully, produced: %s", short(folder), system.represented_args(r, base=folder))


@main.command()
@click.option("--diagnostics", "-d", is_flag=True, help="Show diagnostics info")
def settings(diagnostics):
    """
    Show settings
    """
    setup_audit_log()
    if diagnostics:
        system.info("python interpreter: %s", short(system.PYTHON))
        system.info("sys.executable    : %s", short(sys.executable))
        system.info("sys.prefix        : %s", short(getattr(sys, "prefix", None)))
        system.info("sys.real_prefix   : %s", short(getattr(sys, "real_prefix", None)))
        system.info("meta              : %s" % short(SETTINGS.meta.path))
        system.info("")

    system.info(SETTINGS.represented())


@main.command(name="auto-upgrade")
@click.argument("package", required=True)
def auto_upgrade(package):
    """
    Auto-upgrade a package
    """
    p = PACKAGERS.resolved(package)
    p.refresh_current()
    if not p.current.valid:
        system.abort("%s is not currently installed", package)

    ping = PingLock(SETTINGS.meta.full_path(package), seconds=SETTINGS.version_check_delay)
    if ping.is_young():
        # We checked for auto-upgrade recently, no need to check again yet
        system.debug("Skipping auto-upgrade, checked recently")
        sys.exit(0)
    ping.touch()

    setup_audit_log()
    try:
        p.internal_install()

    except PingLockException:
        system.debug("Skipping auto-upgrade, %s is currently being installed by another process" % package)
        sys.exit(0)
