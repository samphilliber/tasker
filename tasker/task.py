#   Copyright 2014 Nathan C. Keim
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
import six
from functools import reduce
import os, sys
import inspect, contextlib, functools
from collections import OrderedDict
from warnings import warn
import json

from path import Path

from .base import DirBase, AttrDict
from .storage import FileBase
from .progress import Progress, DEFAULT_STATUS_FILE, DEFAULT_STATUS_DIR
from . import debug
from .debug import tasker_traceback

if six.PY2:
    from exceptions import IOError

class LockException(IOError):
    pass


def _listify(arg):
    if isinstance(arg, (list, tuple)):
        return arg
    else:
        return [arg,]

def _sanitize_filename(name):
    ret = "".join([c for c in name
                    if c.isalpha() or c.isdigit() or c in ' _']).rstrip()
    if not len(ret):
        raise ValueError('"%s" contains no valid characters from which to form a '
                         'filename.' % name)
    return ret

def _uniq(l):
    """Removes duplicates from a list, preserving order."""
    r = []
    for item in l:
        if item not in r: r.append(item)
    return r

def _nestmap(fcn, data_part):
    """Works like map() but preserves nested structures of dicts, lists, and tuples."""
    if isinstance(data_part, dict):
        try:
            return {k: _nestmap(fcn, data_part[k]) for k in data_part}
        except:  # Hide ourselves, and the dict comprehension
            if debug.EDIT_TRACEBACKS:
                typ, val, tb = sys.exc_info()
                six.reraise(typ, val, tb.tb_next.tb_next)
            else:
                raise
    elif isinstance(data_part, (list, tuple)):
        return [_nestmap(fcn, v) for v in data_part]
    else:
        try:
            return fcn(data_part)
        except:  # Remove ourselves from the traceback
            if debug.EDIT_TRACEBACKS:
                typ, val, tb = sys.exc_info()
                six.reraise(typ, val, tb.tb_next)
            else:
                raise

class TaskUnit(object):
    """Represents a single task within a Tasker instance.
    Ordinarily one uses the computes() and stores() methods of
    the Tasker instance to define a task, instead of instantiating
    a TaskUnit directly.

    DETAILS:

    Parameters:
    'func' is a function which turns 'ins' into 'outs'.
    'ins' is some data structure (preferably list or dict) populated
        with filenames, FileBase instances, or other TaskUnit instances.
    'outs' is a filename or FileBase instance, or a list thereof.
    'tasker' is a parent Tasker instance, which presently serves to
        set the working directory for this task.

    When 'func' is called, it takes two arguments:
        - The first is its own TaskUnit instance.
        - The second is structured like 'ins', but with FileBase instances
        replaced with those files' contents, and with each TaskUnit instance
        replaced with a list (or single value) corresponding to that
        TaskUnit's outputs.

    'func' returns a list (or single value) which corresponds to the elements of
    'outs'. Elements not corresponding to a FileBase instance are ignored; the
    contents of those files must be writen with code inside 'func',
    and read by a separate function.

    In general, any filename can be either relative to the task's working directory.
    or absolute. 'func' will always be executed in the task's working directory.

    When a user asks for the outputs of 'func', they are loaded from disk and
    returned as a dict, which is indexed just like in the argument to 'func'.

    Tips for writing func(tsk, ins):
        'tsk.progress' is a statusboard.Progress instance that makes
            it easy for your task to report its status. Its start()
            and _finish() methods will be called automatically.
    """
    def __init__(self, func, ins, outs, tasker):
        self.func = func
        self.__name__ = func.__name__
        self.__doc__ = func.__doc__
        self.tasker = tasker
        self.p = self.tasker.p # This directory will always be my working dir
        self._outs_as_given = outs
        self.outs = _listify(outs)
        self.output_files = list(map(self._get_filename, self.outs))
        self._ins_as_given = ins
        self.ins = _listify(ins)
        self.input_files, self.input_tasks = self._flatten_dependencies()

        self._running = False # Prevent recursion
        self._syncing = False # Prevent recursion

    # Deal with arbitrary user specification of task inputs/outputs
    def _get_filename(self, fileobj):
        """Returns absolute path to a file, from a string or FileBase."""
        if isinstance(fileobj, FileBase): 
            fn = Path(fileobj.filename)
        else:
            fn = Path(fileobj)
        if fn.isabs():
            return fn
        else:
            return (self.p / fn).normpath().abspath()

    def _prepare_data(self, data_part):
        """Resolves 'data_part' into its run-time representation.

        Tasks are resolved into their outputs. FileBase instances become their contents. 
        """
        if six.PY2:
            unicode_var = unicode
        else:
            unicode_var = str
        if isinstance(data_part, (str, unicode_var, Path)): # Literal filename
            return self._get_filename(data_part)
        elif isinstance(data_part, FileBase): # Formatted file
            return data_part.read()
        elif isinstance(data_part, TaskUnit): # Another task
            # Continued by that task, so that its working dir, etc. are used
            try:
                return data_part.load()
            except:  # Remove ourselves from the traceback
                if debug.EDIT_TRACEBACKS:
                    typ, val, tb = sys.exc_info()
                    six.reraise(typ, val, tb.tb_next)
                else:
                    raise
        else:
            raise ValueError('%r does not specify a valid input source' % data_part)

    def _flatten_dependencies(self):
        """Returns a list of files and a list of tasks.
        Files are returned as absolute path objects."""
        deps = self._flatten_dependencies_recurse(self.ins)
        files, tasks = set(), set()
        for v in deps:
            if isinstance(v, TaskUnit):
                tasks.add(v)
            else:
                files.add(v)  # Could be a filename or a FileBase instance
        # Make file paths relative to my working dir.
        return list(map(self._get_filename, files)), list(tasks)

    def _flatten_dependencies_recurse(self, in_part):
        """Walks the 'self.ins' data structure, collecting objects."""
        flatten = lambda seq: reduce((lambda a,b: a+b), seq, [])
        if isinstance(in_part, (str, FileBase)):
            return [in_part,]
        elif isinstance(in_part, TaskUnit):
            return [in_part,] + list(in_part.output_files)
        elif isinstance(in_part, dict):
            return flatten([self._flatten_dependencies_recurse(v) \
                    for v in in_part.values()])
        elif isinstance(in_part, (list, tuple)):
            return flatten([self._flatten_dependencies_recurse(v) for v in in_part])
        else:
            raise ValueError('Part of input specification could not be handled: %s' \
                    % repr(in_part))

    def __enter__(self):
        """Context manager. Handles locking, inputs, and progress 
        before and after running a task. Yields the task's inputs.
        """
        self._lockfile = self.tasker._lockfile(self.__name__)
        if self._running:
            raise LockException('Attempting to run task "%s" in "%s" when '
                                'already in progress.' % \
                                (self.__name__, self.tasker.p))
        if self.tasker.is_working(task=self.__name__):  # Suspenders and a belt
            raise LockException('%s says task "%s" is already running there.' % \
                                (self._lockfile, self.__name__))
        self._old_dir = os.getcwd()
        try:
            self._running = True
            os.chdir(self.p)
            self.progress = Progress(persistent_info={
                'task': self.__name__, 'pid': os.getpid(), })
            self._lockfile.dirname().makedirs_p()
            self._lockfile.touch() # Establish lock
            self.progress.working()
            try:
                ins = _nestmap(self._prepare_data, self._ins_as_given)
            except:
                # Remove ourselves from the call stack
                if debug.EDIT_TRACEBACKS:
                    typ, val, tb = sys.exc_info()
                    six.reraise(typ, val, tb.tb_next)
                else:
                    raise
            return ins  # Task function will run with this input
        except:
            # We still need to clean up
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, typ, val, tb):
        """Context manager counterpart to __enter__"""
        if tb is not None:
            self.progress.update({'status': 'ERROR'})
        else:
            self.progress._finish()  # Change status to "done"
        self._running = False
        if self._lockfile.exists(): self._lockfile.unlink()
        os.chdir(self._old_dir)

    def __repr__(self):
        return 'TaskUnit: ' + self.func.__name__

    def __hash__(self):
        return id(self) # So we can put these in sets

    def _output_mtime(self):
        """Newest modification time of output files.

        None if no outputs defined; -1 if any are missing.
        """
        if not len(self.output_files):
            return None
        else:
            try:
                return max([of.mtime for of in self.output_files])
            except OSError:
                return -1  # Missing file

    def _walk_up(self, run=False, force=False):
        """Go up the dependency tree, looking for out-of-date tasks,
        including this one.

        run : if true, run() tasks that are out of date.
        force : if true, run *this* task whether it is out of date or not.

        Notes on returned dictionary keys:
            all_current : not (Does/will this task need to be run()?)
            done : Can the output be produced trivially?
                (For tasks that don't store their output, are all deps up to date?)
        """
        if self._syncing:
            raise RuntimeError('Cyclic dependency: "%s" somehow depends on '
                'itself.' % self.__name__)
        try:
            self._syncing = True
            up_results = [it._walk_up(run=run) for it in self.input_tasks]
        finally:
            self._syncing = False

        result = dict(
            all_current=all(ur['all_current'] for ur in up_results),
                # Note that all([]) == True
            needed_tasks=reduce(lambda a,b: a+b,
                                [ur['needed_tasks'] for ur in up_results], []),
            visited_tasks=reduce(lambda a,b: a+b,
                                [ur['visited_tasks'] for ur in up_results], [self,]),
            )

        input_mtimes = [-1] + [ur['mtime'] for ur in up_results]
        missing_files = []
        for inf in self.input_files:
            try:
                input_mtimes.append(inf.mtime)
            except OSError:
                missing_files.append(inf)
        output_mtime = self._output_mtime()

        # Run task if missing outputs, stale outputs, or an upstream task has been re-run.
        # Note that missing *inputs* do not trigger a run, which would presumably fail.
        # This is to prevent a scenario in which the user deletes an obscure input file,
        # asks for a downstream value, thus inadvertently wipes the entire chain of stored values,
        # and has no way to recompute anything.
        if force or output_mtime == -1 or \
                    (output_mtime is not None and output_mtime < max(input_mtimes)) or \
                    not result['all_current']:
            result['all_current'] = False
            result['needed_tasks'].append(self)
            result['done'] = False
            if run:
                for missing_file in missing_files:
                    warn('Attempting to run task "%s", but required file "%s" is '
                         'missing. Failure is likely.' % (self.__name__, missing_file))
                self.run()
                output_mtime = self._output_mtime()
                if output_mtime is not None and output_mtime < max(input_mtimes):
                    raise RuntimeError('Task "%s" failed to update its output files.'
                                       % self.__name__)
        elif output_mtime is None and (  # This task does not store its outputs, and
            missing_files  # It reads files that are missing, or
            or not all(ur['done'] for ur in up_results)):  # It depends on tasks that are not computed
                result['done'] = False
        else:
            result['done'] = True

        if output_mtime is None:
            result['mtime'] = max(input_mtimes)  # No outputs
        else:
            result['mtime'] = output_mtime
        return result  # needed_tasks, visited_tasks, missing_files, all_current, mtime

    # Public interface
    def __call__(self):
        """Update outputs if necessary and read from disk. 
        
        See load() for details of return value."""
        self.sync()
        return self.load()

    def load(self):
        """Return representation of task output on disk.

        Structure is the same as in the task definition --- a single
        value or a sequence of values.

        Where a FileBase instance was used (i.e. a recognized file format like JSON()),
        the contents of the file are returned. Otherwise, the path to the file is
        returned.
        """
        # We return a dict in which the values are referred to by various names,
        # the wame way we pass input data to self.func() itself.
        return _nestmap(self._prepare_data, self._outs_as_given)

    def run(self):
        """Execute task (always) and write output files in recognized formats.

        Does not update dependencies.

        Temporarily changes to task's working directory.

        Status information is written to "taskerstatus.json" in this
        directory. If this file already indicates a "working" status,
        raises a LockException.
        """
        with tasker_traceback(self.__name__, self.tasker.p), \
                self as ins:
            try:
                outdata = self.func(self, ins)
            except:
                # Hide run() in the call stack
                if debug.EDIT_TRACEBACKS:
                    typ, val, tb = sys.exc_info()
                    six.reraise(typ, val, tb.tb_next)
                else:
                    raise

            if len(self.outs):
                if len(self.outs) == 1:
                    outdata = [outdata,]
                elif len(outdata) != len(self.outs):
                    raise RuntimeError('Expected %i output values '
                        'but got %i.' % (len(self.outs), len(self.outdata)))
                for of, od in zip(self.outs, outdata):
                    if isinstance(of, FileBase): of.save(od)

    def force(self):
        """Re-run task and return outputs."""
        self._walk_up(run=True, force=True)
        return self.load()

    def sync(self):
        """Update dependencies, and this task, as needed."""
        self._walk_up(run=True)

    def is_current(self):
        """True if this task's output is readily available.

        If the task does store its output, this implies that the output
        has been computed and stored, and that no dependency is newer.
        (Missing upstream files do *not* make this False.)

        If the task does not store its output, this implies that all
        its dependencies are available and current.
        """
        return self._walk_up()['done']

    def report(self):
        """List tasks that would have to be run to update outputs."""
        return _uniq(self._walk_up()['needed_tasks'])

    def clear(self):
        """Delete this task's output files and directories."""
        for f in self.output_files:
            if f.isdir(): f.rmtree()
            elif f.isfile(): f.unlink()


class TaskUnitNoStore(TaskUnit):
    """For task functions that don't store results to disk.
    """

    def __init__(self, func, ins, tasker):
        super(TaskUnitNoStore, self).__init__(func, ins, [], tasker)

    def run(self):
        """Does nothing, as there is nothing on disk to update."""
        pass

    def __call__(self):
        """Run task and return its output, after updating dependencies.

        Temporarily changes to task's working directory.

        Status information is written to "taskerstatus.json" in this
        directory. If this file already indicates a "working" status,
        raises a LockException.
        """
        self.sync()
        try:
            with tasker_traceback(self.__name__, self.tasker.p), \
                    self as ins:
                return self.func(self, ins)
        except:
            # Hide __call__() in the call stack
            if debug.EDIT_TRACEBACKS:
                typ, val, tb = sys.exc_info()
                six.reraise(typ, val, tb.tb_next)
            else:
                raise

    load = __call__
    force = __call__

    def clear(self):
        raise NotImplementedError("This task has no stored "
                    "outputs to clear. You must call clear() for each "
                    "storing task that depends on it.")


class Tasker(DirBase):
    """Object to set up tasks within a single directory.
    
    Initialize with the directory name."""
    def __init__(self, dirname='.'):
        super(Tasker, self).__init__(dirname)
        self.tasks = OrderedDict()
        self.conf = AttrDict()

    def create_task(self, ins, outs):
        """Return a decorator that turns the function into a task
        with registered inputs and outputs.

        This is more explicit (and inelegant) than stores().
        """
        # If FileBase instances in 'ins' and 'outs' do not refer to absolute paths,
        # they need to be made relative to our working dir.
        def rectify_filepath(iospec):
            if isinstance(iospec, FileBase):
                iospec.set_parentdir(self.p)
            return iospec
        def mktask(func): 
            t = TaskUnit(func, _nestmap(rectify_filepath, ins), 
                    _nestmap(rectify_filepath, outs), self)
            self.tasks[t.__name__] = t
            setattr(self, t.__name__, t)
            return t
        return mktask

    def stores(self, *outputs):
        """Create a task that stores outputs in the specified files.

        Arguments ('outputs') should match the sequence (or single value)
        returned by the task function.

        'stores()' returns a decorator that turns the function into a task
        with the specified outputs, and inputs given by that function's
        keyword arguments (and default values).
        """
        if len(outputs) == 1:
            outputs = outputs[0] # No sequences at all.

        def rectify_filepath(iospec):
            if isinstance(iospec, FileBase):
                iospec.set_parentdir(self.p)
            return iospec

        def mktask(func):
            args, _, _, defaults = inspect.getargspec(func)
            if defaults is None: defaults = {}
            if not args:
                raise RuntimeError('Task function must take at least one argument: '
                                   'the task instance itself.')
            elif len(args) - 1 != len(defaults):
                raise RuntimeError('All task function args (except first) should '
                                   'have default values,')
            ins = dict(zip(args[1:], defaults))
            @functools.wraps(func)
            def task_func_with_kw(tsk, ins):
                assert isinstance(tsk, TaskUnit)
                assert isinstance(ins, dict)
                try:
                    return func(tsk, **ins)
                except:
                    # Hide this wrapper function in the call stack
                    if debug.EDIT_TRACEBACKS:
                        typ, val, tb = sys.exc_info()
                        six.reraise(typ, val, tb.tb_next)
                    else:
                        raise

            if outputs:
                t = TaskUnit(task_func_with_kw, _nestmap(rectify_filepath, ins),
                             _nestmap(rectify_filepath, outputs), self)
            else:
                t = TaskUnitNoStore(task_func_with_kw,
                                    _nestmap(rectify_filepath, ins), self)
            self.tasks[t.__name__] = t
            setattr(self, t.__name__, t)
            return t
        return mktask

    def computes(self, func):
        """Decorator that creates a task without stored outputs."""
        return self.stores()(func)

    __call__ = computes

    def which(self, filename):
        """Return the TaskUnit instance that is responsible for 'filename'.

        'filename' can be relative to this instance's working dir, or absolute."""
        fnp = Path.filename
        if fnp.isabs: fnp_abs = fnp
        else: fnp_abs = (self.p / fnp).abspath()
        for t in self.tasks:
            if fnp_abs in t.output_files:
                return t

    def clear(self):
        """Remove all output files of all tasks."""
        for t in self.tasks.values():
            if not isinstance(t, TaskUnitNoStore):
                t.clear()

    def is_working(self, task=None):
        """Check "taskerstatus.json" to see if any task is running.

        task : Check whether task with this name is running (optional).
        """
        try:
            sf = open(self.p / DEFAULT_STATUS_FILE, 'r')
            sfinfo = json.load(sf)
            if sfinfo['status'] == 'working':
                if task is not None:
                    return self._lockfile(task).exists()
                else:
                    return True
        except (IOError, ValueError, KeyError):
            return False

    def _lockfile(self, taskname):
        """Returns path instance for task-specific lockfile"""
        return (self.p / DEFAULT_STATUS_DIR / _sanitize_filename(taskname))

    def unlock(self):
        """Remove all lock and status files, to release working lock."""
        sfn = self.p / DEFAULT_STATUS_FILE
        if sfn.exists():
            sfn.unlink()
        lfd = self.p / DEFAULT_STATUS_DIR
        if lfd.exists() and lfd.isdir():
            assert not os.path.samefile(lfd, self.p)  # Basic safety check
            lfd.rmtree()
    def menu(self):
        """List tasks, statuses, and descriptions.
        
        Tasks are displayed in the order added."""
        six.print_('Tasks for %s:' % self.p)
        if not len(self.tasks):
            six.print_( 'No tasks defined.')
            return
        tasknames = self.tasks.keys()
        donestrings = ['+' if t.is_current() else '' for t in self.tasks.values()]
        doclines = [t.__doc__.split('\n')[0].strip() if t.__doc__ else ''\
                for t in self.tasks.values()]
        maxname = max(map(len, tasknames))
        width = 80  # Width of display
        six.print_('{0:^5} {1:<{maxname}}  {2:<{descwidth}}'.format('Done?', 'Name', 'Description',
                maxname=maxname, descwidth=width - 8 - maxname))
        six.print_('-' * width)
        for done, name, desc in zip(donestrings, tasknames, doclines):
            six.print_('{0:^5} {1:<{maxname}}  {2:<{descwidth}}'.format(done, name, desc,
                    maxname=maxname, descwidth=width - 8 - maxname))
