"""
Functionality for the whole app, easily importable via one name
"""

import io
import logging
import os
import shutil
import subprocess  # nosec
import sys
import time
from logging.handlers import RotatingFileHandler

from pickley import decode, pickley_program_path, python_interpreter


LOG = logging.getLogger(__name__)
PICKLEY = "pickley"

HOME = os.path.expanduser("~")
DRYRUN = False
PYTHON = python_interpreter()
WRAPPER_MARK = "# Wrapper generated by https://pypi.org/project/pickley/"
SETTINGS = None  # type: pickley.settings.Settings

LATEST_CHANNEL = "latest"
VENV_PACKAGER = "venv"
DEFAULT_DELIVERY = "symlink"


class State:
    """Helps track state without using globals"""

    quiet = False
    output = True
    testing = False
    logging = False
    audit_handler = None
    debug_handler = None


def resolved_path(path, base=None):
    """
    :param str path: Path to resolve
    :param str|None base: Base path to use to resolve relative paths (default: current working dir)
    :return str: Absolute path
    """
    if not path:
        return path
    path = os.path.expanduser(path)
    if base and not os.path.isabs(path):
        return os.path.join(base, path)
    return os.path.abspath(path)


PICKLEY_PROGRAM_PATH = resolved_path(pickley_program_path())
sys.argv[0] = PICKLEY_PROGRAM_PATH


def short(path, base=None):
    """
    :param path: Path to represent in its short form
    :param str|list|None base: Base folder(s) to relativise paths to
    :return str: Short form, using '~' if applicable
    """
    if not path:
        return path
    path = str(path).replace(SETTINGS.meta.path + "/", "")
    if base:
        if not isinstance(base, list):
            base = [base]
        for b in base:
            if b:
                path = path.replace(b + "/", "")
    path = path.replace(HOME, "~")
    return path


def debug(message, *args, **kwargs):
    if not State.quiet and State.logging:
        LOG.debug(message, *args, **kwargs)
    if State.testing:
        print(str(message) % args)


def info(message, *args, **kwargs):
    output = kwargs.pop("output", State.output)
    if State.logging:
        LOG.info(message, *args, **kwargs)
    if (not State.quiet and output) or State.testing:
        print(str(message) % args)


def warning(message, *args, **kwargs):
    if State.logging:
        LOG.warning(message, *args, **kwargs)
    if State.output or State.testing:
        print("WARNING: %s" % (str(message) % args))


def error(message, *args, **kwargs):
    if State.logging:
        LOG.error(message, *args, **kwargs)
    if State.output or State.testing:
        print("ERROR: %s" % (str(message) % args))


def abort(*args, **kwargs):
    """
    :param args: Args passed through for error reporting
    :param kwargs: Args passed through for error reporting
    :return: kwargs["return_value"] (default: -1) to signify failure to non-fatal callers
    """
    code = kwargs.pop("code", 1)
    fatal = kwargs.pop("fatal", True)
    quiet = kwargs.pop("quiet", False)
    return_value = kwargs.pop("return_value", -1)
    if not quiet and args:
        error(*args, **kwargs)
    if fatal:
        sys.exit(code)
    return return_value


def relaunch():
    """
    Rerun with same args, to pick up freshly bootstrapped installation
    """
    State.output = False
    run_program(*sys.argv, stdout=sys.stdout, stderr=sys.stderr)
    if not DRYRUN:
        sys.exit(0)


def installed_names():
    """Yield names of currently installed packages"""
    result = []
    if os.path.isdir(SETTINGS.meta.path):
        for fname in os.listdir(SETTINGS.meta.path):
            fpath = os.path.join(SETTINGS.meta.path, fname)
            if os.path.isdir(fpath):
                if os.path.exists(os.path.join(fpath, ".current.json")):
                    result.append(fname)
    return result


def setup_audit_log():
    """Log to <meta>/audit.log"""
    if DRYRUN or State.audit_handler:
        return
    path = SETTINGS.meta.full_path("audit.log")
    ensure_folder(path)
    State.audit_handler = RotatingFileHandler(path, maxBytes=500 * 1024, backupCount=1)
    State.audit_handler.setLevel(logging.DEBUG)
    State.audit_handler.setFormatter(logging.Formatter("%(asctime)s [%(process)s] %(levelname)s - %(message)s"))
    logging.root.addHandler(State.audit_handler)
    State.logging = True
    info(":: %s", represented_args(sys.argv), output=False)


def setup_debug_log():
    """Log to stderr"""
    # Log to console with --debug or --dryrun
    if State.debug_handler:
        return
    State.output = False
    State.debug_handler = logging.StreamHandler()
    State.debug_handler.setLevel(logging.DEBUG)
    State.debug_handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    logging.root.addHandler(State.debug_handler)
    logging.root.setLevel(logging.DEBUG)
    State.logging = True


def get_lines(path, max_size=8192, fatal=True, quiet=False):
    """
    :param str path: Path of text file to return lines from
    :param int max_size: Return contents only for files smaller than 'max_size' bytes
    :param bool fatal: Abort execution on failure if True
    :param bool quiet: Don't log if True
    :return list|None: Lines from file contents
    """
    if not path or not os.path.isfile(path) or os.path.getsize(path) > max_size:
        # Intended for small text files, pretend no contents for binaries
        return None

    try:
        with open(path, "rt") as fh:
            return fh.readlines()

    except Exception as e:
        return abort("Can't read %s: %s", short(path), e, fatal=fatal, quiet=quiet, return_value=None)


def virtualenv_path():
    """
    :return str: Path to our own virtualenv.py
    """
    import virtualenv
    path = virtualenv.__file__
    if path and path.endswith(".pyc"):
        path = path[:-1]
    return path


def is_universal(wheels_folder, package_name, version):
    """
    :param str wheels_folder: Path to folder where wheels reside
    :param str package_name: Pypi package name
    :param str version: Specific version of 'package_name' to examine
    :return bool: True if wheel exists and is universal
    """
    if os.path.isdir(wheels_folder):
        prefix = "%s-%s-" % (package_name, version)
        for fname in os.listdir(wheels_folder):
            if fname.startswith(prefix) and fname.endswith(".whl"):
                return "py2.py3-none" in fname


def added_env_paths(env_vars, env=None):
    """
    :param dict env_vars: Env vars to customize
    :param dict env: Original env vars
    """
    if not env_vars:
        return None
    if not env:
        env = dict(os.environ)
    result = dict(env)
    for env_var, paths in env_vars.items():
        current = env.get(env_var, "")
        current = [x for x in current.split(":") if x]
        added = 0
        for path in paths.split(":"):
            if os.path.isdir(path) and path not in current:
                added += 1
                current.append(path)
        if added:
            result[env_var] = ":".join(current)
    return result


def file_younger(path, age):
    """
    :param str path: Path to file
    :param int age: How many seconds to consider the file too old
    :return bool: True if file exists and is younger than 'age' seconds
    """
    try:
        return time.time() - os.path.getmtime(path) < age

    except OSError:
        return False


def check_pid(pid):
    """Check For the existence of a unix pid"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def touch(path):
    """
    :param str path: Path to file to touch
    """
    return write_contents(path, "")


def write_contents(path, contents, verbose=False, fatal=True):
    """
    :param str path: Path to file
    :param str contents: Contents to write
    :param bool verbose: Don't log if False (dryrun being always logged)
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    if not path:
        return 0

    if DRYRUN:
        action = "write %s bytes to" % len(contents) if contents else "touch"
        debug("Would %s %s", action, short(path))
        return 1

    ensure_folder(path, fatal=fatal)
    if verbose and contents:
        debug("Writing %s bytes to %s", len(contents), short(path))

    try:
        with open(path, "w") as fh:
            if contents:
                fh.write(contents)
            else:
                os.utime(path, None)
        return 1

    except Exception as e:
        return abort("Can't write to %s: %s", short(path), e, fatal=fatal)


def parent_folder(path, base=None):
    """
    :param str path: Path to file or folder
    :param str|None base: Base folder to use for relative paths (default: current working dir)
    :return str: Absolute path of parent folder of 'path'
    """
    return path and os.path.dirname(resolved_path(path, base=base))


def first_line(path):
    """
    :param str path: Path to file
    :return str|None: First line of file, if any
    """
    try:
        with io.open(path, "rt", errors="ignore") as fh:
            return fh.readline().strip()
    except (IOError, TypeError):
        return None


def flatten(result, value, separator=None, unique=True):
    """
    :param list result: Flattened values
    :param value: Possibly nested arguments (sequence of lists, nested lists)
    :param str|None separator: Split values with 'separator' if specified
    :param bool unique: If True, return unique values only
    """
    if not value:
        # Convenience: allow to filter out --foo None easily
        if value is None and not unique and result and result[-1].startswith("-"):
            result.pop(-1)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            flatten(result, item, separator=separator, unique=unique)
        return
    if separator is not None and hasattr(value, "split") and separator in value:
        flatten(result, value.split(separator), separator=separator, unique=unique)
        return
    if not unique or value not in result:
        result.append(value)


def flattened(value, separator=None, unique=True):
    """
    :param value: Possibly nested arguments (sequence of lists, nested lists)
    :param str|None separator: Split values with 'separator' if specified
    :param bool unique: If True, return unique values only
    :return list: 'value' flattened out (leaves from all involved lists/tuples)
    """
    result = []
    flatten(result, value, separator=separator, unique=unique)
    return result


def relocate_venv_file(path, source, destination, fatal=True, quiet=False):
    """
    :param str path: Path of file to relocate (change mentions of 'source' to 'destination')
    :param str source: Where venv used to be
    :param str destination: Where venv is moved to
    :param bool fatal: Abort execution on failure if True
    :param bool quiet: Don't log if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    content = None
    content = get_lines(path, fatal=fatal, quiet=quiet)
    if not content:
        return 0

    modified = False
    lines = []
    for line in content:
        if source in line:
            line = line.replace(source, destination)
            modified = True
        lines.append(line)

    if not modified:
        return 0

    return write_contents(path, "".join(lines), fatal=fatal)


def ensure_folder(path, folder=False, fatal=True):
    """
    :param str path: Path to file or folder
    :param bool folder: If True, 'path' refers to a folder (file otherwise)
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    if not path:
        return 0

    if folder:
        folder = resolved_path(path)
    else:
        folder = parent_folder(path)
    if os.path.isdir(folder):
        return 0

    if DRYRUN:
        debug("Would create %s", short(folder))
        return 1

    try:
        os.makedirs(folder)
        return 1

    except Exception as e:
        return abort("Can't create folder %s: %s", short(folder), e, fatal=fatal)


def copy_file(source, destination, fatal=True):
    """
    Copy source -> destination

    :param str source: Source file or folder
    :param str destination: Destination file or folder
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    if not source or not destination or source == destination:
        return 0

    if DRYRUN:
        debug("Would copy %s -> %s", short(source), short(destination))
        return 1

    if not os.path.exists(source):
        return abort("%s does not exist, can't copy to %s", short(source), short(destination), fatal=fatal)

    ensure_folder(destination, fatal=fatal)
    delete_file(destination, fatal=fatal)
    try:
        if os.path.isdir(source):
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy(source, destination)

        shutil.copystat(source, destination)  # Make sure last modification time is preserved
        return 1

    except Exception as e:
        return abort("Can't copy %s -> %s: %s", short(source), short(destination), e, fatal=fatal)


def find_venvs(folder, seen=None):
    """
    :param str folder: Folder to scan for venvs
    :param dict|None seen: Allows to not get stuck on circular symlinks
    """
    if folder and os.path.isdir(folder):
        if seen is None:
            folder = os.path.realpath(folder)
            seen = set()

        if folder not in seen:
            seen.add(folder)
            files = os.listdir(folder)
            if "bin" in files:
                bin_folder = os.path.join(folder, "bin")
                if is_executable(os.path.join(bin_folder, "python")):
                    yield bin_folder
                    return
            for name in files:
                fname = os.path.join(folder, name)
                for path in find_venvs(fname, seen=seen):
                    yield path


def move_file(source, destination, fatal=True):
    """
    Move source -> destination

    :param str source: Source file or folder
    :param str destination: Destination file or folder
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    if not source or not destination or source == destination:
        return 0

    if DRYRUN:
        debug("Would move %s -> %s", short(source), short(destination))
        return 1

    if not os.path.exists(source):
        return abort("%s does not exist, can't move to %s", short(source), short(destination), fatal=fatal)

    relocated = 0
    for bin_folder in find_venvs(source):
        debug("Relocating venv %s -> %s", short(source), short(destination))
        for name in os.listdir(bin_folder):
            fpath = os.path.join(bin_folder, name)
            relocated += relocate_venv_file(fpath, source, destination, fatal=fatal)

    ensure_folder(destination, fatal=fatal)
    delete_file(destination, fatal=fatal)
    try:
        shutil.move(source, destination)
        return 1

    except Exception as e:
        return abort("Can't move %s -> %s: %s", short(source), short(destination), e, fatal=fatal)


def delete_file(path, fatal=True):
    """
    :param str|None path: Path to file or folder to delete
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    islink = path and os.path.islink(path)
    if not islink and (not path or not os.path.exists(path)):
        return 0

    if DRYRUN:
        debug("Would delete %s", short(path))
        return 1

    debug("Deleting %s", short(path))
    try:
        if islink or os.path.isfile(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)
        return 1

    except Exception as e:
        return abort("Can't delete %s: %s", short(path), e, fatal=fatal)


def make_executable(path, fatal=True):
    """
    :param str path: chmod file with 'path' as executable
    :param bool fatal: Abort execution on failure if True
    :return int: 1 if effectively done, 0 if no-op, -1 on failure
    """
    if is_executable(path):
        return 0

    if DRYRUN:
        debug("Would make %s executable", short(path))
        return 1

    if not os.path.exists(path):
        return abort("%s does not exist, can't make it executable", short(path), fatal=fatal)

    try:
        os.chmod(path, 0o755)  # nosec
        return 1

    except Exception as e:
        return abort("Can't chmod %s: %s", short(path), e, fatal=fatal)


def is_executable(path):
    """
    :param str path: Path to file
    :return bool: True if file exists and is executable
    """
    return path and os.path.isfile(path) and os.access(path, os.X_OK)


def which(program):
    """
    :param str program: Program name to find via env var PATH
    :return str|None: Full path to program, if one exists and is executable
    """
    if not program:
        return None
    if os.path.isabs(program):
        return program if is_executable(program) else None
    for p in os.environ.get("PATH", "").split(":"):
        fp = os.path.join(p, program)
        if is_executable(fp):
            return fp
    return None


def run_program(program, *args, **kwargs):
    """Run 'program' with 'args'"""
    args = flattened(args, unique=False)
    full_path = which(program)

    fatal = kwargs.pop("fatal", True)
    base = kwargs.pop("base", None)
    logger = kwargs.pop("logger", debug)
    dryrun = kwargs.pop("dryrun", fatal and DRYRUN)
    message = "Would run" if dryrun else "Running"
    message = "%s: %s %s" % (message, short(full_path or program, base=base), represented_args(args, base=base))
    logger(message)

    if dryrun:
        return message

    if not full_path:
        return abort("%s is not installed", short(program, base=base), fatal=fatal, return_value=None)

    stdout = kwargs.pop("stdout", subprocess.PIPE)
    stderr = kwargs.pop("stderr", subprocess.PIPE)
    args = [full_path] + args
    try:
        p = subprocess.Popen(args, stdout=stdout, stderr=stderr, env=added_env_paths(kwargs.pop("path_env", None)))  # nosec
        output, error = p.communicate()
        output = decode(output)
        error = decode(error)
        if output:
            output = output.strip()
        if error:
            error = error.strip()

        if p.returncode:
            info = ": %s\n%s" % (error, output) if output or error else ""
            return abort("%s exited with code %s%s", short(program, base=base), p.returncode, info, fatal=fatal, return_value=None)

        return output

    except Exception as e:
        return abort("%s failed: %s", short(program, base=base), e, exc_info=e, fatal=fatal, return_value=None)


def quoted(text):
    """
    :param str text: Text to optionally quote
    :return str: Quoted if 'text' contains spaces
    """
    if text and " " in text:
        sep = "'" if '"' in text else '"'
        return "%s%s%s" % (sep, text, sep)
    return text


def represented_args(args, base=None, separator=" ", shorten=True):
    """
    :param list|tuple args: Arguments to represent
    :param str|None base: Base folder to relativise paths to
    :param str separator: Separator to use
    :param bool shorten: If True, shorten involved paths
    :return str: Quoted as needed textual representation
    """
    result = []
    for text in args:
        if shorten:
            text = short(text, base=base)
        result.append(quoted(text))
    return separator.join(result)


def to_int(text, default=None):
    """
    :param str|int|float text: Value to convert
    :param int|float|None default: Default to use if 'text' can't be parsed
    :return float:
    """
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


class FolderBase(object):
    """
    This class allows to more easily deal with folders
    """

    def __init__(self, path, name=None):
        """
        :param str path: Path to folder
        :param str|None name: Name of this folder (defaults to basename of 'path')
        """
        self.path = resolved_path(path)
        self.name = name or os.path.basename(path)

    def relative_path(self, path):
        """
        :param str path: Path to relativize
        :return str: 'path' relative to self.path
        """
        return os.path.relpath(path, self.path)

    def relativize(self, component):
        return component if not component or not component.startswith("/") else component[1:]

    def full_path(self, *relative):
        """
        :param list(str) *relative: Relative components
        :return str: Full path based on self.path
        """
        relative = [self.relativize(c) for c in relative]
        return os.path.join(self.path, *relative)

    def __repr__(self):
        return "%s: %s" % (self.name, short(self.path))
