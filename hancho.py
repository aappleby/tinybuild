#!/usr/bin/python3

"""Hancho v0.1.0 @ 2024-03-25 - A simple, pleasant build system."""

import argparse
import asyncio
import builtins
import inspect
import io
import json
import os
import re
import subprocess
import sys
import traceback
import time
import types
import random
from os import path
import glob

# If we were launched directly, a reference to this module is already in
# sys.modules[__name__]. Stash another reference in sys.modules["hancho"] so
# that build.hancho and descendants don't try to load a second copy of Hancho.
sys.modules["hancho"] = sys.modules[__name__]

# The maximum number of recursion levels we will do to expand a macro.
# Tests currently require MAX_EXPAND_DEPTH >= 6
MAX_EXPAND_DEPTH = 20

# Matches "{expression}" macros
macro_regex = re.compile("^{[^}]*}$")

# Matches macros inside a template string.
template_regex = re.compile("{[^}]*}")

def log_line(message):
    app.log += message
    if not Config.quiet:
        sys.stdout.write(message)
        sys.stdout.flush()


def log(message, *args, sameline=False, **kwargs):
    """Simple logger that can do same-line log messages like Ninja."""
    if not sys.stdout.isatty():
        sameline = False

    if sameline:
        kwargs.setdefault("end", "")

    output = io.StringIO()
    print(message, *args, file=output, **kwargs)
    output = output.getvalue()

    if not output:
        return

    if sameline:
        output = output[: os.get_terminal_size().columns - 1]
        output = "\r" + output + "\x1B[K"
        log_line(output)
    else:
        if app.line_dirty:
            log_line("\n")
        log_line(output)

    app.line_dirty = sameline


def _flatten(variant):
    if isinstance(variant, list):
        return [x for element in variant for x in _flatten(element)]
    return [variant]


def _abs_path(raw_path, strict=False):
    if isinstance(raw_path, list):
        return [_abs_path(p, strict) for p in raw_path]
    result = path.abspath(raw_path)
    if strict and not path.exists(result):
        raise FileNotFoundError(raw_path)
    return result


def _rel_path(path1, path2):
    if isinstance(path1, list):
        return [_rel_path(p, path2) for p in path1]
    # Generating relative paths in the presence of symlinks doesn't work with either
    # Path.relative_to or os.path.relpath - the former balks at generating ".." in paths, the
    # latter does generate them but "path/with/symlink/../foo" doesn't behave like you think it
    # should. What we really want is to just remove redundant cwd stuff off the beginning of the
    # path, which we can do with simple string manipulation.
    return path1.removeprefix(path2 + "/") if path1 != path2 else ""


def _join_path(*args):
    """Returns all possible concatenated paths from the given paths (or arrays of paths)."""
    match len(args):
        case 0:
            return ""
        case 1:
            return list(args)
        case 2:
            args0 = _flatten(args[0])
            args1 = _flatten(args[1])
            result = [path.join(arg0, arg1) for arg0 in args0 for arg1 in args1]
            return result[0] if len(result) == 1 else result
        case _:
            return _join_path(args[0], _join_path(*args[1:]))

def _color(red=None, green=None, blue=None):
    """Converts RGB color to ANSI format string."""
    # Color strings don't work in Windows console, so don't emit them.
    if not Config.use_color or os.name == "nt":
        return ""
    if red is None:
        return "\x1B[0m"
    return f"\x1B[38;2;{red};{green};{blue}m"


def _run_cmd(cmd):
    """Runs a console command synchronously and returns its stdout with whitespace stripped."""
    return subprocess.check_output(cmd, shell=True, text=True).strip()


def _swap_ext(name, new_ext):
    """Replaces file extensions on either a single filename or a list of filenames."""
    if isinstance(name, list):
        return [_swap_ext(n, new_ext) for n in name]
    return path.splitext(name)[0] + new_ext


def _mtime(filename):
    """Gets the file's mtime and tracks how many times we've called mtime()"""
    app.mtime_calls += 1
    return os.stat(filename).st_mtime_ns


def _maybe_as_number(text):
    """Tries to convert a string to an int, then a float, then gives up. Used for ingesting
    unrecognized flag values."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


async def _await_variant(variant):
    """Recursively replaces every awaitable in the variant with its awaited value."""
    match variant:
        case asyncio.CancelledError():
            raise variant
        case Task():
            # If the task hasn't been queued yet, queue it now before we await it.
            if variant.promise is None:
                app.queue_pending_tasks()

            # We don't recurse through subtasks because they should await themselves.
            if inspect.isawaitable(variant.promise):
                promise = await variant.promise
                variant.promise = await _await_variant(promise)
        case Config():
            await _await_variant(variant.__dict__)
        case dict():
            for key in variant:
                variant[key] = await _await_variant(variant[key])
        case list():
            for index, value in enumerate(variant):
                variant[index] = await _await_variant(value)
        case _ if inspect.isawaitable(variant):
            variant = await variant
    return variant

####################################################################################################

def _dump_object(o):
    return f"{type(o).__name__} @ {hex(id(o))}"

def _dump_config(config):
    class Encoder(json.JSONEncoder):
        def default(self, o):
            return _dump_object(o)
    return json.dumps(config.__dict__, indent=2, cls=Encoder)

def _dump_task(task):
    class Encoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, Config):
                return o.__dict__
            return _dump_object(o)
    return json.dumps(task.__dict__, indent=2, cls=Encoder)

####################################################################################################

class Config:
    """A Config object is just a 'bag of fields'."""

    def __init__(self, *args, **kwargs):
        self.update(*args, kwargs)

    def __repr__(self):
        return f"{_dump_object(self)} = {_dump_config(self)}"

    def update(self, *args, **kwargs):
        for arg in args:
            self.merge(arg)
        self.merge(kwargs)
        return self

    def merge(self, _dict):
        for key, val in _dict.items():
            setattr(self, key, val)

    def items(self):
        return self.__dict__.items()

    # required to support "self.__dict__.update(config)"
    def keys(self):
        return self.__dict__.keys()

    # required to use config as mapping in eval()
    def __getitem__(self, key):
        return getattr(self, key)

    def expand(self, variant):
        return expand(self, variant)

    ####################
    # All static methods and fields are available to use in any template string.

    # fmt: off
    config   = lambda self,            *args, **kwargs : Config(*args, kwargs)
    extend   = lambda self,            *args, **kwargs : type(self)(self, *args, kwargs)
    repo     = lambda self, repo_path, *args, **kwargs : repo(self, repo_path, *args, **kwargs)
    repo2    = lambda self, repo_path, *args, **kwargs : repo2(self, repo_path, *args, **kwargs)
    #command  = lambda self, command,   *args, **kwargs : Command(command, self, *args, **kwargs)

    #command2  = lambda self, command,   *args, **kwargs : Command(command, self, *args, **kwargs)
    def command2(self, command, *args, **kwargs):
        result = Command(command, self, *args, **kwargs)
        #if hasattr(result, "base_path"):
        #    delattr(result, "base_path")
        #if hasattr(result, "base_name"):
        #    delattr(result, "base_name")
        return result

    task     = lambda self,            *args, **kwargs : Task(self, *args, kwargs)
    module   = lambda self, file_name, *args, **kwargs : load(self, file_name, False, *args, kwargs)
    include  = lambda self, file_name, *args, **kwargs : load(self, file_name, True, *args, kwargs)

    reset    = lambda self : app.reset()
    build    = lambda self : app.build()
    get_log  = lambda self : app.log

    abs_path  = staticmethod(_abs_path)
    rel_path  = staticmethod(_rel_path)
    join_path = staticmethod(_join_path)
    color     = staticmethod(_color)
    glob      = staticmethod(glob.glob)
    len       = staticmethod(len)
    run_cmd   = staticmethod(_run_cmd)
    swap_ext  = staticmethod(_swap_ext)
    flatten   = staticmethod(_flatten)
    print     = staticmethod(print)
    basename  = staticmethod(path.basename)

    root_path = os.getcwd()
    root_name = "build.hancho"

    repo_path = os.getcwd()
    repo_name = ""

    depformat = 'gcc'
    job_count = 1
    ext_build = False

    jobs      = os.cpu_count()
    verbose   = False
    quiet     = False
    dry_run   = False
    debug     = False
    force     = False
    shuffle   = False
    trace     = False
    use_color = True

    abs_command_path  = "{abs_path(join_path(base_path,   command_path))}"
    abs_source_path   = "{abs_path(join_path(base_path,   source_path))}"
    abs_build_path    = "{abs_path(join_path(base_path,   build_path))}"

    abs_command_files = "{flatten(join_path(abs_command_path, command_files))}"
    abs_source_files  = "{flatten(join_path(abs_source_path,  source_files))}"
    abs_build_files   = "{flatten(join_path(abs_build_path,   build_files))}"
    abs_build_deps    = "{flatten(join_path(abs_build_path,   build_deps))}"

    rel_source_path   = "{rel_path(abs_source_path,   abs_command_path)}"
    rel_build_path    = "{rel_path(abs_build_path,    abs_command_path)}"

    rel_command_files = "{rel_path(abs_command_files, abs_command_path)}"
    rel_source_files  = "{rel_path(abs_source_files,  abs_command_path)}"
    rel_build_files   = "{rel_path(abs_build_files,   abs_command_path)}"
    rel_build_deps    = "{rel_path(abs_build_deps,    abs_command_path)}"

    default_command_path = "{base_path}"
    default_source_path  = "{base_path}"
    default_build_path   = "{root_path}/{build_dir}/{build_tag}/{repo_name}/{rel_path(abs_source_path, repo_path)}"

    command_path = "{default_command_path}"
    source_path  = "{default_source_path}"
    build_path   = "{default_build_path}"
    # fmt: on

#----------------------------------------

class Command(Config):
    """A Command is a Config that we can call like a function."""

    def __init__(self, command, *args, **kwargs):
        super().__init__(*args, **kwargs, command = command)

    def __call__(self, source_files = None, build_files = None, **kwargs):
        if isinstance(build_files, Config):
            log("You've got a config in your build_files")
            assert False

        new_config = Config(self)
        if source_files is not None:
            new_config.source_files = source_files
        if build_files is not None:
            new_config.build_files = build_files
        new_config.update(kwargs)
        return Task(**new_config)

#----------------------------------------

def repo(config, _repo_path, *args, **kwargs):
    repo_path = _abs_path(_join_path(config.base_path, _repo_path))
    repo_name = path.basename(repo_path)

    repo = Repo(
        config,
        *args,
        **kwargs,
        repo_path = repo_path,
        repo_name = repo_name,
        base_path = repo_path,
        base_name = "",
    )
    return repo

def repo2(config, _file_path, _file_name, *args, **kwargs):
    _file_path = config.expand(_file_path)
    _file_name = config.expand(_file_name)

    repo_path = _abs_path(_join_path(config.base_path, _file_path))
    repo_name = path.basename(repo_path)

    _file_pathname = _abs_path(_join_path(repo_path, _file_name))

    return load(
        config,
        _file_pathname,
        is_include=False,
        *args,
        **kwargs,
        repo_path = repo_path,
        repo_name = repo_name,
    )

def load(config, _file_name, is_include = False, *args, **kwargs):
    _file_name = config.expand(_file_name)
    file_path, file_name = path.split(_abs_path(_join_path(config.base_path, _file_name)))

    mod_config = Module(config, *args, **kwargs)

    if is_include:
        mod_config.base_path = config.base_path
        mod_config.base_name = config.base_name
    else:
        mod_config.base_path = file_path
        mod_config.base_name = file_name

    module = app.load_module(file_path, file_name, mod_config, is_include = True)

    # Module loaded, copy all its public stuff into this config
    for key, val in module.__dict__.items():
        if key.startswith("_") or key == "hancho":
            continue
        setattr(mod_config, key, val)
    return mod_config

class Repo(Config):
    pass

class Include(Config):
    pass

class Module(Config):
    pass

####################################################################################################
# The template expansion / macro evaluation code requires some explanation.
#
# We do not necessarily know in advance how the users will nest strings, templates, callbacks,
# etcetera. So, when we need to produce a flat list of files from whatever was passed to
# source_files, we need to do a bunch of dynamic-dispatch-type stuff to ensure that we can always
# turn that thing into a flat list of files.
#
# We also need to ensure that if anything in this process throws an exception (or if an exception
# was passed into a rule due to a previous rule failing) that we always propagate the exception up
# to Task.run_async, where it will be handled and propagated to other Tasks.
#
# The result of this is that the functions here are mutually recursive in a way that can lead to
# confusing callstacks, but that should handle every possible case of stuff inside other stuff.
#
# The depth checks are to prevent recursive runaway - the MAX_EXPAND_DEPTH limit is arbitrary but
# should suffice.


def expand(config, variant, fail_ok=False):
    """Expands all templates anywhere inside 'variant'."""
    match variant:
        case Config():
            return variant
        case dict():
            return variant
        case BaseException():
            raise variant
        case Task():
            return expand(config, variant.promise, fail_ok)
        case list():
            return [expand(config, s) for s in variant]
        case str() if macro_regex.search(variant):
            return eval_macro(config, variant, fail_ok)
        case str() if template_regex.search(variant):
            return expand_template(config, variant, fail_ok)
        case int() | bool() | float() | str() | staticmethod():
            return variant
        case _ if inspect.isfunction(variant):
            return variant
        case _:
            message = f"{_color(255, 0, 0)}Don't know how to expand {type(variant).__name__} ='{variant}'{_color()}"
            log(message)
            raise ValueError(message)


def expand_template(config, template, fail_ok=False):
    """Replaces all macros in template with their stringified values."""
    if config.trace:
        log(("┃" * app.expand_depth) + f"┏ Expand '{template}'")

    result = ""
    try:
        app.expand_depth += 1
        old_template = template
        while span := template_regex.search(template):
            result += template[0 : span.start()]
            try:
                macro = template[span.start() : span.end()]
                variant = eval_macro(config, macro, fail_ok)
                result += " ".join([str(s) for s in _flatten(variant)])
            except BaseException as err:
                log(err)
                log(f"{_color(255, 255, 0)}Expanding template '{old_template}' failed! - {err}{_color()}")
                raise err
            template = template[span.end() :]
        result += template
    finally:
        app.expand_depth -= 1

    if config.trace:
        log(("┃" * app.expand_depth) + f"┗ '{result}'")
    return result

class Expander:
    """JIT template expansion for use in eval()."""

    def __init__(self, config):
        self.config = config

    def __getitem__(self, key):
        return self.get(key)

    def __getattr__(self, key):
        return self.get(key)

    def get(self, key):
        expanded = expand(self.config, getattr(self.config, key))
        return Expander(expanded) if isinstance(expanded, Config) else expanded


def eval_macro(config, macro, fail_ok=False):
    """Evaluates the contents of a "{macro}" string."""
    if app.expand_depth > MAX_EXPAND_DEPTH:
        raise RecursionError(f"Expanding '{macro}' failed to terminate")
    if config.trace:
        log(("┃" * app.expand_depth) + f"┏ Eval '{macro}'")
    app.expand_depth += 1
    # pylint: disable=eval-used
    result = ""
    try:
        # We must pass the JIT expanded config to eval() otherwise we'll try and join unexpanded
        # paths and stuff, which will break.
        if not isinstance(config, Expander):
            config = Expander(config)
        result = eval(macro[1:-1], {}, config)
    except BaseException as err:
        if not fail_ok:
            log(err)
            log(f"{_color(255, 255, 0)}Expanding macro '{macro}' failed! - {err}{_color()}")
            raise err
    finally:
        app.expand_depth -= 1

    if config.trace:
        log(("┃" * app.expand_depth) + f"┗ {result}")
    return result

####################################################################################################

class Task:
    """Calling a Rule creates a Task."""

    # pylint: disable=too-many-instance-attributes
    # pylint: disable=attribute-defined-outside-init

    def __init__(self, *args, **kwargs):

        import pprint
        #pprint.pprint(args)
        #pprint.pprint(kwargs)

        defaults = Config(
            desc          = "{source_files} -> {build_files}",

            root_path     = Config.root_path,
            repo_path     = Config.root_path,
            base_path     = app.topdir(),

            command       = None,
            command_path  = "{default_command_path}",
            command_files = [],

            source_path   = "{default_source_path}",
            source_files  = [],

            build_tag     = "",
            build_dir     = "build",
            build_path    = "{default_build_path}",
            build_files   = [],
            build_deps    = [],

            other_files   = [],
        )

        # Note - We can't set promise = asyncio.create_task() here, as we're not guaranteed to be
        # in an event loop yet

        self.config = Config(defaults, *args, kwargs)
        self.action = Config()
        self.reason = None
        self.promise = None

        if self.config.command is None:
            raise ValueError(f"Task has no command - {self}")

        app.tasks_total += 1
        app.pending_tasks.append(self)

    def __repr__(self):
        return f"{_dump_object(self)} = {_dump_task(self)}"

    async def run_async(self):
        """Entry point for async task stuff, handles exceptions generated during task execution."""
        try:
            # Await everything awaitable in this task's rule.
            await _await_variant(self.config)

            # Everything awaited, task_init runs synchronously.
            self.task_init()

            # Check if we need a rebuild
            self.reason = self.needs_rerun(self.config.force)

            if self.config.debug:
                log(self)

            # Run the commands if we need to.
            if self.reason:
                result = await self.run_commands()
                app.tasks_pass += 1
            else:
                log(
                    f"{_color(128,196,255)}[{self.action.task_index}/{app.tasks_total}]{_color()} {self.action.desc}",
                    sameline=not self.config.verbose,
                )
                if self.config.verbose or self.config.debug:
                    log(f"{_color(128,128,128)}Files {self.action.build_files} are up to date{_color()}")
                result = self.action.abs_build_files
                app.tasks_skip += 1

            return result

        # If this task failed, we print the error and propagate a cancellation to downstream tasks.
        except BaseException as err:
            log(_color(255, 128, 128))
            log(err)
            if not self.config.quiet:
                traceback.print_exception(*sys.exc_info())
            log(_color())
            app.tasks_fail += 1
            return asyncio.CancelledError()

        # If any of this tasks's dependencies were cancelled, we propagate the cancellation to
        # downstream tasks.
        except asyncio.CancelledError as cancel:
            app.tasks_cancel += 1
            return cancel

    def task_init(self):
        """All the setup steps needed before we run a task."""

        # Expand all the critical fields

        app.task_counter += 1

        config = self.config
        action = self.action

        action.desc          = config.expand(config.desc)
        action.command       = _flatten(config.expand(config.command))
        action.depformat     = config.expand(config.depformat)
        action.job_count     = config.expand(config.job_count)
        action.ext_build     = config.expand(config.ext_build)
        action.task_index    = app.task_counter

        # FIXME we can probably ditch some of these, we really only need the abs ones

        action.base_path     = config.base_path
        action.command_path  = config.expand(config.command_path)
        action.source_path   = config.expand(config.source_path)
        action.build_path    = config.expand(config.build_path)

        action.command_files = _flatten(config.expand(config.command_files))
        action.source_files  = _flatten(config.expand(config.source_files))
        action.build_files   = _flatten(config.expand(config.build_files))
        action.build_deps    = _flatten(config.expand(config.build_deps))

        action.abs_command_path  = _abs_path(_join_path(action.base_path, action.command_path), strict=True)
        action.abs_source_path   = _abs_path(_join_path(action.base_path, action.source_path), strict=True)
        action.abs_build_path    = _abs_path(_join_path(action.base_path, action.build_path))

        action.abs_command_files = _abs_path(_flatten(_join_path(action.abs_command_path, action.command_files)), strict=True)
        action.abs_source_files  = _abs_path(_flatten(_join_path(action.abs_source_path, action.source_files)), strict=True)
        action.abs_build_files   = _abs_path(_flatten(_join_path(action.abs_build_path, action.build_files)))
        action.abs_build_deps    = _abs_path(_flatten(_join_path(action.abs_build_path, action.build_deps)))

        if not str(action.abs_build_path).startswith(str(Config.root_path)):
            raise ValueError(f"Path error, build_path {action.abs_build_path} is not under root_path {Config.root_path}")

        # Check for duplicate task outputs
        for abs_file in action.abs_build_files:
            if abs_file in app.all_build_files:
                raise NameError(f"Multiple rules build {abs_file}!")
            app.all_build_files.add(abs_file)

        # Make sure our output directories exist
        if not config.dry_run:
            for abs_file in action.abs_build_files:
                os.makedirs(path.dirname(abs_file), exist_ok=True)


    def needs_rerun(self, force=False):
        """Checks if a task needs to be re-run, and returns a non-empty reason if so."""
        # Pylint really doesn't like this function, lol.
        # pylint: disable=too-many-return-statements
        # pylint: disable=too-many-branches

        if force:
            return f"Files {self.action.abs_build_files} forced to rebuild"
        if not self.action.abs_source_files:
            return "Always rebuild a target with no inputs"
        if not self.action.abs_build_files:
            return "Always rebuild a target with no outputs"

        # Check if any of our output files are missing.
        for abs_file in self.action.abs_build_files:
            if not path.exists(abs_file):
                return f"Rebuilding because {abs_file} is missing"

        # Check if any of our input files are newer than the output files.
        min_out = min(_mtime(f) for f in self.action.abs_build_files)

        for abs_file in self.action.abs_source_files:
            if _mtime(abs_file) >= min_out:
                return f"Rebuilding because {abs_file} has changed"

        for abs_file in self.action.abs_command_files:
            if _mtime(abs_file) >= min_out:
                return f"Rebuilding because {abs_file} has changed"

        for mod in app.loaded_modules:
            if _mtime(mod.__file__) >= min_out:
                return f"Rebuilding because {mod.__file__} has changed"

        # Check all dependencies in the depfile, if present.
        for abs_depfile in self.action.abs_build_deps:
            if not path.exists(abs_depfile):
                continue
            if self.config.debug:
                log(f"Found depfile {abs_depfile}")
            with open(abs_depfile, encoding="utf-8") as depfile:
                deplines = None
                if self.action.depformat == "msvc":
                    # MSVC /sourceDependencies json depfile
                    deplines = json.load(depfile)["Data"]["Includes"]
                elif self.action.depformat == "gcc":
                    # GCC .d depfile
                    deplines = depfile.read().split()
                    deplines = [d for d in deplines[1:] if d != "\\"]
                else:
                    raise ValueError(f"Invalid depformat {depformat}")

                # The contents of the depfile are RELATIVE TO THE WORKING DIRECTORY
                deplines = [path.join(self.action.abs_command_path, d) for d in deplines]
                for abs_file in deplines:
                    if _mtime(abs_file) >= min_out:
                        return f"Rebuilding because {abs_file} has changed"

        # All checks passed; we don't need to rebuild this output.
        # Empty string = no reason to rebuild
        return ""

    async def run_commands(self):
        """Grabs a lock on the jobs needed to run this task's commands, then runs all of them."""

        result = []
        try:
            # Wait for enough jobs to free up to run this task.
            await app.acquire_jobs(self.action.job_count)

            # Print the "[1/N] Compiling foo.cpp -> foo.o" status line and debug information
            log(
                f"{_color(128,255,196)}[{self.action.task_index}/{app.tasks_total}]{_color()} {self.action.desc}",
                sameline=not self.config.verbose,
            )

            if self.config.verbose or self.config.debug:
                log(f"{_color(128,128,128)}Reason: {self.reason}{_color()}")

            for exp_command in self.action.command:
                if self.config.verbose or self.config.debug:
                    rel_command_path = _rel_path(self.action.abs_command_path, Config.root_path)
                    log(f"{_color(128,128,255)}{rel_command_path}$ {_color()}", end="")
                    log("(DRY RUN) " if self.config.dry_run else "", end="")
                    log(exp_command)
                result = await self.run_command(exp_command)
        finally:
            await app.release_jobs(self.action.job_count)

        return result

    async def run_command(self, command):
        """Runs a single command, either by calling it or running it in a subprocess."""

        # Early exit if this is just a dry run
        if self.action.dry_run:
            return self.action.abs_build_files

        # Custom commands just get called and then early-out'ed.
        if callable(command):
            result = command(self)
            if inspect.isawaitable(result):
                result = await result
            return result

        # Non-string non-callable commands are not valid
        if not isinstance(command, str):
            raise ValueError(f"Don't know what to do with {command}")

        # Create the subprocess via asyncio and then await the result.
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self.action.abs_command_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        (stdout_data, stderr_data) = await proc.communicate()

        self.stdout = stdout_data.decode()
        self.stderr = stderr_data.decode()
        self.returncode = proc.returncode

        # Print command output if needed
        if (self.stdout or self.stderr) and not self.config.quiet:
            if self.stderr:
                log("-----stderr-----")
                log(self.stderr, end="")
            if self.stdout:
                log("-----stdout-----")
                log(self.stdout, end="")

        # Task complete, check the task return code
        if self.returncode:
            raise ValueError(
                f"Command '{command}' exited with return code {self.returncode}"
            )

        # Task passed, return the output file list
        return self.action.abs_build_files

####################################################################################################

class App:
    """The application state. Mostly here so that the linter will stop complaining about my use of
    global variables. :D"""

    # pylint: disable=too-many-instance-attributes
    def __init__(self):
        self.repos = []
        self.loaded_modules = []
        self.all_build_files = set()

        self.tasks_total = 0
        self.tasks_pass = 0
        self.tasks_fail = 0
        self.tasks_skip = 0
        self.tasks_cancel = 0
        self.task_counter = 0

        self.mtime_calls = 0
        self.line_dirty = False
        self.expand_depth = 0

        self.pending_tasks = []
        self.queued_tasks = []
        self.jobs_available = os.cpu_count()
        self.jobs_lock = asyncio.Condition()
        self.log = ""
        self.dirstack = [os.getcwd()]

    ########################################

    def pushdir(self, path):
        path = _abs_path(path, strict=True)
        self.dirstack.append(path)
        os.chdir(path)

    def popdir(self):
        self.dirstack.pop()
        os.chdir(self.dirstack[-1])

    def topdir(self):
        return self.dirstack[-1]

    ########################################

    def reset(self):
        self.__init__()

    ########################################

    def main(self):
        result = -1
        try:
            self.parse_args()
            self.pushdir(Config.root_path)
            self.load_hanchos()
            result = self.build()
        finally:
            self.popdir()
        return result

    ########################################

    def parse_args(self):
        # pylint: disable=line-too-long
        # fmt: off
        parser = argparse.ArgumentParser()
        parser.add_argument("root_name",               default="build.hancho", type=str, nargs="?", help="The name of the .hancho file(s) to build")
        parser.add_argument("-C", "--chdir",           default=".", dest="root_path", type=str,     help="Change directory before starting the build")
        parser.add_argument("-j", "--jobs",            default=os.cpu_count(), type=int,            help="Run N jobs in parallel (default = cpu_count)")
        parser.add_argument("-v", "--verbose",         default=False, action="store_true",          help="Print verbose build info")
        parser.add_argument("-q", "--quiet",           default=False, action="store_true",          help="Mute all output")
        parser.add_argument("-n", "--dry_run",         default=False, action="store_true",          help="Do not run commands")
        parser.add_argument("-d", "--debug",           default=False, action="store_true",          help="Print debugging information")
        parser.add_argument("-f", "--force",           default=False, action="store_true",          help="Force rebuild of everything")
        parser.add_argument("-s", "--shuffle",         default=False, action="store_true",          help="Shuffle task order to shake out dependency issues")
        parser.add_argument("-e", "--trace",           default=False, action="store_true",          help="Trace template & macro expansion")
        # fmt: on

        # Parse the command line

        (flags, unrecognized) = parser.parse_known_args()
        flags = flags.__dict__

        root_path = _abs_path(flags["root_path"])
        root_name = flags["root_name"]

        flags["root_path"] = root_path
        flags["root_name"] = root_name
        flags["repo_path"] = root_path
        flags["repo_name"] = ""

        # Unrecognized command line parameters also become config fields if they are flag-like
        unrecognized_flags = {}
        for span in unrecognized:
            if match := re.match(r"-+([^=\s]+)(?:=(\S+))?", span):
                key = match.group(1)
                val = match.group(2)
                val = _maybe_as_number(val) if val is not None else True
                unrecognized_flags[key] = val

        for key, val in flags.items():
            setattr(Config, key, val)

        for key, val in unrecognized_flags.items():
            setattr(Config, key, val)

    ########################################

    def load_hanchos(self):
        time_a = time.perf_counter()

        if Config.debug:
            c = Config()
            for key, val in Config.__dict__.items():
                if not key.startswith("_"):
                    setattr(c, key, val)
            log(f"global config = {c}")

        root_config = Config(
            base_path    = Config.root_path,
            base_name    = Config.root_name,
        )

        self.load_module(Config.root_path, Config.root_name, root_config, is_include = False)
        time_b = time.perf_counter()

        if Config.debug or Config.verbose:
            log(f"Loading .hancho files took {time_b-time_a:.3f} seconds")

    ########################################

    def build(self):
        """Run tasks until we're done with all of them."""
        result = -1
        try:
            # For some reason "result = asyncio.run(self.async_main())" might be breaking actions
            # in Github, so I'm using get_event_loop().run_until_complete().
            # Seems to fix the issue.
            result = asyncio.get_event_loop().run_until_complete(self.async_run_tasks())
        except BaseException as err:
            log(err)
        return result

    ########################################

    def queue_pending_tasks(self):
        """Creates an asyncio.Task for each task in the pending list and clears the pending list."""

        if self.pending_tasks:
            if Config.shuffle:
                log(f"Shufflin' {len(self.pending_tasks)} tasks")
                random.shuffle(self.pending_tasks)

            for task in self.pending_tasks:
                task.promise = asyncio.create_task(task.run_async())
                self.queued_tasks.append(task)
            self.pending_tasks = []

    ########################################

    async def async_run_tasks(self):
        # Run all tasks in the queue until we run out.

        self.jobs_available = Config.jobs

        # Tasks can create other tasks, and we don't want to block waiting on a whole batch of
        # tasks to complete before queueing up more. Instead, we just keep queuing up any pending
        # tasks after awaiting each one. Because we're awaiting tasks in the order they were
        # created, this will effectively walk through all tasks in dependency order.

        time_a = time.perf_counter()

        self.queue_pending_tasks()
        while self.queued_tasks:
            task = self.queued_tasks.pop(0)
            if inspect.isawaitable(task.promise):
                await task.promise
            self.queue_pending_tasks()

        time_b = time.perf_counter()

        if Config.debug or Config.verbose:
            log(f"Running tasks took {time_b-time_a:.3f} seconds")

        # Done, print status info if needed
        if Config.debug:
            log(f"tasks total:     {self.tasks_total}")
            log(f"tasks passed:    {self.tasks_pass}")
            log(f"tasks failed:    {self.tasks_fail}")
            log(f"tasks skipped:   {self.tasks_skip}")
            log(f"tasks cancelled: {self.tasks_cancel}")
            log(f"mtime calls:     {self.mtime_calls}")

        if self.tasks_fail:
            log(f"hancho: {_color(255, 128, 128)}BUILD FAILED{_color()}")
        elif self.tasks_pass:
            log(f"hancho: {_color(128, 255, 128)}BUILD PASSED{_color()}")
        else:
            log(f"hancho: {_color(128, 128, 255)}BUILD CLEAN{_color()}")

        return -1 if self.tasks_fail else 0

    ########################################

    def load_module(self, file_path, file_name, config, is_include = False):
        """Loads a Hancho module ***while chdir'd into its directory***"""

        file_path = config.expand(file_path)
        file_name = config.expand(file_name)

        # We must chdir()s into the .hancho file directory before running it so that
        # glob() can resolve files relative to the .hancho file itself. We are _not_ in an async
        # context here so there should be no other threads trying to change cwd.
        app.pushdir(file_path)

        file_pathname = _join_path(file_path, file_name)

        if config.debug or config.verbose:
            log(_color(128,255,128) + f"Loading module {file_pathname}" + _color())

        with open(file_pathname, encoding="utf-8") as file:
            source = file.read()
            code = compile(source, file_name, "exec", dont_inherit=True)

        mod_name = path.splitext(file_name)[0]
        module = type(sys)(mod_name)
        module.__file__ = file_pathname
        module.__builtins__ = builtins
        module.hancho = config

        self.loaded_modules.append(module)

        try:
            # Why Pylint thinks this is not callable is a mystery.
            # pylint: disable=not-callable
            types.FunctionType(code, module.__dict__)()
        finally:
            app.popdir()
        return module

    ########################################

    async def acquire_jobs(self, count):
        """Waits until 'count' jobs are available and then removes them from the job pool."""

        if count > Config.jobs:
            raise ValueError(f"Nedd {count} jobs, but pool is {Config.jobs}.")

        await self.jobs_lock.acquire()
        await self.jobs_lock.wait_for(lambda: self.jobs_available >= count)
        self.jobs_available -= count
        self.jobs_lock.release()

    ########################################
    # NOTE: The notify_all here is required because we don't know in advance which tasks will
    # be capable of running after we return jobs to the pool. HOWEVER, this also creates an
    # O(N^2) slowdown when we have a very large number of pending tasks (>1000) due to the
    # "Thundering Herd" problem - all tasks will wake up, only a few will acquire jobs, the
    # rest will go back to sleep again, this will repeat for every call to release_jobs().

    async def release_jobs(self, count):
        """Returns 'count' jobs back to the job pool."""

        await self.jobs_lock.acquire()
        self.jobs_available += count
        self.jobs_lock.notify_all()
        self.jobs_lock.release()

####################################################################################################
# Always create an App() object so we can use it for bookkeeping even if we loaded Hancho as a
# module instead of running it directly.

app = App()

if __name__ == "__main__":
    sys.exit(app.main())
