import os
import time
import threading
from datetime import datetime
import psutil

from openquake.engine import logs, no_distribute
from openquake.engine.db import models
from django.db import connection

MB = 1024 * 1024  # 1 megabyte


# I did not make any attempt to make this class thread-safe,
# since it is intended to be used in single-threaded programs, as
# in the engine
class PerformanceMonitor(object):
    """
    Measure the resident memory occupied by a list of processes during
    the execution of a block of code. Should be used as a context manager,
    as follows::

     with PerformanceMonitor([os.getpid()]) as mm:
         do_something()
     maxmemory, = mm.mem_peaks

    At the end of the block the PerformanceMonitor object will have the
    following 5 public attributes:

    .start_time: when the monitor started (a datetime object)
    .duration: time elapsed between start and stop (in seconds)
    .exc: None unless an exception happened inside the block of code
    .mem: a tuple of lists with the memory measures (in megabytes)
    .mem_peaks: a tuple with the maximum memory occupations (in megabytes)

    The memory tuples have the same length as the number of processes.
    The behaviour of the PerformanceMonitor can be customized by subclassing it
    and by overriding the methods on_exit() and on_running().
    The on_exit() method is called at end and it is used to display
    or store the results of the analysis; the on_running() method is
    called while the analysis is running and can be used to display
    or store the partial results. It is also possible to specify the .tic
    attribute (the interval of time between measures, 1 second by default)
    to perform a finer grained analysis.
    """

    def __init__(self, pids, tic=1.0):
        self._procs = [psutil.Process(pid) for pid in pids]
        self.tic = tic  # measure the memory at every tic
        self._monitor = None  # monitor thread polling for memory occupation
        self._running = False  # associated to the monitor thread
        self._start_time = None  # seconds from the epoch
        self.start_time = None  # datetime object
        self.duration = None  # seconds
        self.exc = None  # exception
        self.rss_measures = dict((proc, []) for proc in self._procs)
        self.poll_memory()

    @property
    def mem(self):
        "A tuple of memory measurements, a list of integers (MB) for process"
        return tuple(self.rss_measures[proc] for proc in self._procs)

    @property
    def mem_peaks(self):
        "A tuple of peak memory measurements, an integer (MB) for process"
        return tuple(map(max, self.mem))

    def start(self):
        "Start the monitor thread"
        self._running = True
        self._start_time = time.time()
        self.start_time = datetime.fromtimestamp(self._start_time)
        self._monitor = threading.Thread(None, self._run)
        self._monitor.start()

    def stop(self):
        "Stop the monitor thread and call on_exit"
        self._running = False
        self._monitor.join()
        self.duration = time.time() - self._start_time
        self.on_exit()

    def __enter__(self):
        "Call .start"
        self.exc = None
        self.start()
        return self

    def __exit__(self, etype, exc, tb):
        "Call .stop"
        self.exc = exc
        self.stop()

    def _run(self):
        """
        Poll the /proc/<pid> file every .tic seconds and stores
        the memory information in self.rss_measures for each process
        """
        while self._running:
            self.poll_memory()
            self.on_running()
            time.sleep(self.tic)

    def poll_memory(self):
        """
        Poll the memory occupation for each process and update
        the dictionary self.rss_measures
        """
        for proc in list(self._procs):
            try:
                rss = proc.get_memory_info().rss // MB
            except psutil.AccessDenied:
                # no access to information about this process
                # don't not try to check it anymore
                self._procs.remove(proc)
            else:
                self.rss_measures[proc].append(rss)  # in mbytes

    def on_exit(self):
        "Save the results: to be overridden in subclasses"
        print 'start_time =', self.start_time
        print 'duration =', self.duration
        print 'mem_peaks =', self.mem_peaks
        print 'exc =', self.exc

    def on_running(self):
        "Save the partial results: to be overridden in subclasses"
        print 'Mem peaks:', self.mem_peaks


class EnginePerformanceMonitor(PerformanceMonitor):
    """
    PerformanceMonitor specialized for the engine. It takes in input a
    string, a job_id, and a celery task; the on_exit method
    saves in the uiapi.performance table the relevant info.
    """
    def __init__(self, operation, job_id, task=None, tic=0.1):
        self.job_id = job_id
        if task:
            self.task = task.__name__
            self.task_id = task.request.id
        else:
            self.task = None
            self.task_id = None
        self.operation = operation
        py_pid = os.getpid()
        pg_pid = connection.cursor().connection.get_backend_pid()
        try:
            psutil.Process(pg_pid)
        except psutil.error.NoSuchProcess:  # the db is on a different machine
            pids = [py_pid]
        else:
            pids = [py_pid, pg_pid]
        super(EnginePerformanceMonitor, self).__init__(pids, tic)

    @property
    def mem(self):
        """
        Returns the pair

          (python-memory-measures, postgres-memory-measures)

        If the database is on a different machine postgres-memory-measures
        is None.
        """
        if len(self._procs) == 1:  # pg progress not available
            return (self.rss_measures[self._procs[0]], [None])
        else:
            return super(EnginePerformanceMonitor, self).mem

    def on_exit(self):
        """
        Save the peak memory consumption on the uiapi.performance table.
        """
        pymemory, pgmemory = self.mem_peaks
        if self.exc is None:  # save only valid calculations
            pf = models.Performance(
                oq_job_id=self.job_id,
                task_id=self.task_id,
                task=self.task,
                operation=self.operation,
                start_time=self.start_time,
                duration=self.duration,
                pymemory=pymemory,
                pgmemory=pgmemory)
            pf.save()

    def on_running(self):
        """
        Log memory consumption as the computation goes on; it only works
        when the environment variable OQ_NO_DISTRIBUTE is set, since it
        is intended for debugging purposes.
        """
        if no_distribute():
            logs.LOG.warn('PyMem: %d mb, PgMem: %d mb' % self.mem_peaks)
