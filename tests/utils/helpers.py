# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010-2012, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

"""
Helper functions for our unit and smoke tests.
"""


import collections
import functools
import logging
import mock as mock_module
import numpy
import os
import random
import redis
import shutil
import string
import subprocess
import sys
import tempfile
import textwrap
import time
import shapely

from django.core import exceptions

from openquake.engine.calculators.hazard.general import store_gmpe_map
from openquake.engine.calculators.hazard.general import store_source_model
from openquake.engine.db import models
from openquake.engine.engine import JobContext
from openquake.engine import engine
from openquake.engine import engine2
from openquake.engine import logs
from openquake.engine.input.logictree import LogicTreeProcessor
from openquake.engine.utils import config, get_calculator_class

CD = os.path.dirname(__file__)  # current directory

RUNNER = os.path.abspath(os.path.join(CD, '../../bin/openquake'))

DATA_DIR = os.path.abspath(os.path.join(CD, '../data'))

OUTPUT_DIR = os.path.abspath(os.path.join(CD, '../data/output'))

WAIT_TIME_STEP_FOR_TASK_SECS = 0.5
MAX_WAIT_LOOPS = 10


#: Wraps mock.patch() to make mocksignature=True by default.
patch = functools.partial(mock_module.patch, mocksignature=True)


def default_user():
    """Return the default user to be used for test setups."""
    return models.OqUser.objects.get(user_name="openquake")


def delete_profile(job):
    """Disassociate the job's profile and delete it."""
    [j2p] = models.Job2profile.objects.extra(
        where=["oq_job_id=%s"], params=[job.id])
    jp = j2p.oq_job_profile
    j2p.delete()
    jp.delete()


def insert_inputs(job, inputs):
    """Insert the input records for the given data and job."""
    for imt, imp in inputs:
        iobj = models.Input(path=imp, input_type=imt, owner=job.owner,
                            size=random.randint(1024, 16 * 1024))
        iobj.save()
        i2j = models.Input2job(input=iobj, oq_job=job)
        i2j.save()


def _patched_mocksignature(func, mock=None, skipfirst=False):
    """
    Fixes arguments order and support of staticmethods in mock.mocksignature.
    """
    static = False
    if isinstance(func, staticmethod):
        static = True
        func = func.__func__

    if mock is None:
        mock = mock_module.Mock()
    signature, func = mock_module._getsignature(func, skipfirst)

    checker = eval("lambda %s: None" % signature)
    mock_module._copy_func_details(func, checker)

    def funcopy(*args, **kwargs):
        checker(*args, **kwargs)
        return mock(*args, **kwargs)

    if not hasattr(mock_module, '_setup_func'):
        # compatibility with mock < 0.8
        funcopy.mock = mock
    else:
        mock_module._setup_func(funcopy, mock)
    if static:
        funcopy = staticmethod(funcopy)
    return funcopy
mock_module.mocksignature = _patched_mocksignature


def get_data_path(file_name):
    return os.path.join(DATA_DIR, file_name)


def get_output_path(file_name):
    return os.path.join(OUTPUT_DIR, file_name)


def demo_file(file_name):
    """
    Take a file name and return the full path to the file in the demos
    directory.
    """
    return os.path.join(
        os.path.dirname(__file__), "../../demos", file_name)


def testdata_path(file_name):
    """
    Take a file name and return the full path to the file in the
    tests/data/demos directory
    """
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "../data/demos", file_name))


def job_from_file(config_file_path):
    """
    Create a JobContext instance from the given configuration file.

    The results are configured to go to XML files.  *No* database record will
    be stored for the job.  This allows running test on jobs without requiring
    a database.
    """

    job = engine._job_from_file(config_file_path, 'xml')
    cleanup_loggers()

    return job


def create_job(params, **kwargs):
    job_id = kwargs.pop('job_id', 0)

    return JobContext(params, job_id, **kwargs)


def run_hazard_job(cfg, exports=None):
    """
    Given the path to job config file, run the job and assert that it was
    successful. If this assertion passes, return the completed job.

    :param str cfg:
        Path to a job config file.
    :param list exports:
        A list of export format types. Currently only 'xml' is supported.
    :returns:
        The completed :class:`~openquake.engine.db.models.OqJob`.
    """
    if exports is None:
        exports = []

    job = get_hazard_job(cfg)
    job.is_running = True
    job.save()

    models.JobStats.objects.create(oq_job=job)

    calc_mode = job.hazard_calculation.calculation_mode
    calc = get_calculator_class('hazard', calc_mode)(job)
    completed_job = engine2._do_run_calc(job, exports, calc, 'hazard')
    job.is_running = False
    job.save()

    return completed_job


def run_hazard_job_sp(config_file, params=None, check_output=False,
                      silence=False, force_inputs=True):
    """
    Given a path to a config file, run an openquake hazard job as a separate
    process using `subprocess`.

    :param str config_file:
        Path to the calculation config file.
    :param list params:
        List of additional command line params to bin/openquake. Optional.
    :param bool check_output:
        If `True`, use :func:`subprocess.check_output` instead of
        :func:`subprocess.check_call`.
    :param bool silence:
        If `True`, silence all stdout and stderr messages.
    :param bool force_inputs:
        Defaults to `True`. If `True`, run openquake with the `--force-inputs`
        option.

    :returns:
        With the default input, return the return code of the subprocess.

        If ``check_output`` is set to True, return the output of the subprocess
        call to bin/openquake as a `str`. See
        http://docs.python.org/library/subprocess.html#subprocess.check_output
        for more details.
    :raises:
        If the return code of the subprocess call is not 0, a
        :exception:`subprocess.CalledProcessError` is raised.
    """
    args = [RUNNER, "--run-hazard=%s" % config_file]
    if force_inputs:
        args.append('--force-inputs')

    if params is not None:
        args.extend(params)

    devnull = None
    if silence:
        devnull = open(os.devnull, 'wb')

    try:
        if check_output:
            return subprocess.check_output(args, stdout=devnull,
                                           stderr=devnull)
        else:
            return subprocess.check_call(args, stdout=devnull, stderr=devnull)
    finally:
        if devnull is not None:
            devnull.close()


def run_risk_job_sp(config_file, hazard_id, params=None, silence=False,
                    force_inputs=True):
    """
    Given a path to a config file, run an openquake risk job as a separate
    process using `subprocess`. See `run_hazard_job_sp` for the signature

    :param hazard_id:
      ID of the hazard output used by the risk calculation
    """

    args = [RUNNER, "--run-risk=%s" % config_file,
            "--log-level=debug",
            "--hazard-output-id=%d" % hazard_id]
    if force_inputs:
        args.append('--force-inputs')

    if params is not None:
        args.extend(params)

    devnull = None
    if silence:
        devnull = open(os.devnull, 'wb')

    print 'Running:', ' '.join(args)  # this is useful for debugging
    try:
        return subprocess.check_call(args, stderr=devnull, stdout=devnull)
    finally:
        if devnull is not None:
            devnull.close()


def store_hazard_logic_trees(a_job):
    """Helper function to store the source model and GMPE logic trees in the
    KVS so that it can be read by the Java code.

    :param a_job:
        :class:`openquake.engine.engine.JobContext` instance.
    """
    lt_proc = LogicTreeProcessor(
        a_job['BASE_PATH'],
        a_job['SOURCE_MODEL_LOGIC_TREE_FILE_PATH'],
        a_job['GMPE_LOGIC_TREE_FILE_PATH'])

    src_model_seed = a_job['SOURCE_MODEL_LT_RANDOM_SEED']
    gmpe_seed = a_job['GMPE_LT_RANDOM_SEED']

    src_model_rnd = random.Random()
    src_model_rnd.seed(src_model_seed)
    gmpe_rnd = random.Random()
    gmpe_rnd.seed(gmpe_seed)

    store_source_model(a_job.job_id, src_model_rnd.getrandbits(32),
                       a_job.params, lt_proc)
    store_gmpe_map(a_job.job_id, gmpe_rnd.getrandbits(32), lt_proc)


def timeit(method):
    """Decorator for timing methods"""

    def _timed(*args, **kw):
        """Wrapped function for timed methods"""
        timestart = time.time()
        result = method(*args, **kw)
        timeend = time.time()

        print '%r (%r, %r) %2.2f sec' % (
            method.__name__, args, kw, timeend - timestart)
        return result
    try:
        import nose
        return nose.tools.make_decorator(method)(_timed)
    except ImportError:
        pass
    return _timed


def skipit(method):
    """Decorator for skipping tests"""
    try:
        import nose
        from nose.plugins.skip import SkipTest
    except ImportError:

        def skip_me(*_args, **_kw):
            """The skipped method"""
            print "Can't raise nose SkipTest error, silently skipping %r" % (
                method.__name__)
        return skip_me

    def skipme(*_args, **_kw):
        """The skipped method"""
        print "Raising a nose SkipTest error"
        raise SkipTest("skipping method %r" % method.__name__)

    return nose.tools.make_decorator(method)(skipme)


def assertDeepAlmostEqual(test_case, expected, actual, *args, **kwargs):
    """
    Assert that two complex structures have almost equal contents.

    Compares lists, dicts and tuples recursively. Checks numeric values
    using test_case's :py:meth:`unittest.TestCase.assertAlmostEqual` and
    checks all other values with :py:meth:`unittest.TestCase.assertEqual`.
    Accepts additional positional and keyword arguments and pass those
    intact to assertAlmostEqual() (that's how you specify comparison
    precision).

    :param test_case: TestCase object on which we can call all of the basic
        'assert' methods.
    :type test_case: :py:class:`unittest.TestCase` object
    """
    is_root = not '__trace' in kwargs
    trace = kwargs.pop('__trace', 'ROOT')
    try:
        if isinstance(expected, (int, float, long, complex)):
            test_case.assertAlmostEqual(expected, actual, *args, **kwargs)
        elif isinstance(expected, (list, tuple, numpy.ndarray)):
            test_case.assertEqual(len(expected), len(actual))
            for index in xrange(len(expected)):
                v1, v2 = expected[index], actual[index]
                assertDeepAlmostEqual(test_case, v1, v2,
                                      __trace=repr(index), *args, **kwargs)
        elif isinstance(expected, dict):
            test_case.assertEqual(set(expected), set(actual))
            for key in expected:
                assertDeepAlmostEqual(test_case, expected[key], actual[key],
                                      __trace=repr(key), *args, **kwargs)
        else:
            test_case.assertEqual(expected, actual)
    except AssertionError as exc:
        exc.__dict__.setdefault('traces', []).append(trace)
        if is_root:
            trace = ' -> '.join(reversed(exc.traces))
            exc = AssertionError("%s\nTRACE: %s" % (exc.message, trace))
        raise exc


def assertModelAlmostEqual(test_case, expected, actual):
    """
    Assert that two Django models are equal. For values which are numbers,
    we use :py:meth:`unittest.TestCase.assertAlmostEqual` for number
    comparisons with a reasonable precision tolerance.

    If the `expected` input value contains nested models, this function
    will recurse through them and check for equality.

    :param test_case: TestCase object on which we can call all of the basic
        'assert' methods.
    :type test_case: :py:class:`unittest.TestCase` object
    :type expected: dict
    :type actual: dict
    """

    from django.contrib.gis.db import models as gis_models

    test_case.assertEqual(type(expected), type(actual))

    def getattr_or_none(model, field):
        try:
            return getattr(model, field.name)
        except exceptions.ObjectDoesNotExist:
            return None

    for field in expected._meta.fields:
        if field.name == 'last_update':
            continue

        exp_val = getattr_or_none(expected, field)
        act_val = getattr_or_none(actual, field)

        # If it's a number, use assertAlmostEqual to compare
        # the values with a reasonable tolerance.
        if isinstance(exp_val, (int, float, long, complex)):
            test_case.assertAlmostEqual(exp_val, act_val)
        elif isinstance(exp_val, gis_models.Model):
            # make a recursive call in case there are nested models
            assertModelAlmostEqual(test_case, exp_val, act_val)
        else:
            test_case.assertEqual(exp_val, act_val)


def wait_for_celery_tasks(celery_results,
                          max_wait_loops=MAX_WAIT_LOOPS,
                          wait_time=WAIT_TIME_STEP_FOR_TASK_SECS):
    """celery_results is a list of celery task result objects.
    This function waits until all tasks have finished.
    """

    # if a celery task has not yet finished, wait for a second
    # then check again
    counter = 0
    while (False in [result.ready() for result in celery_results]):
        counter += 1

        if counter > max_wait_loops:
            raise RuntimeError("wait too long for celery worker threads")

        time.sleep(wait_time)

# preserve stdout/stderr (note: we want the nose-manipulated stdout/stderr,
# otherwise we could just use __stdout__/__stderr__)
STDOUT = sys.stdout
STDERR = sys.stderr


def cleanup_loggers():
    root = logging.getLogger()

    for h in list(root.handlers):
        if (isinstance(h, logging.FileHandler) or
            isinstance(h, logging.StreamHandler) or
                isinstance(h, logs.AMQPHandler)):
            root.removeHandler(h)

    # restore the damage created by redirect_stdouts_to_logger; this is only
    # necessary because tests perform multiple log initializations, sometimes
    # for AMQP, sometimes for console
    sys.stdout = STDOUT
    sys.stderr = STDERR


class TestStore(object):
    """Simple key value store, to be used in tests only."""

    _conn = None

    @classmethod
    def kvs(cls):
        TestStore.open()
        return TestStore._conn

    @classmethod
    def open(cls):
        """Initialize the test store."""
        if TestStore._conn is not None:
            return
        TestStore._conn = redis.Redis(db=int(config.get("kvs", "test_db")))

    @classmethod
    def close(cls):
        """Close the test store."""
        TestStore._conn.flushdb()
        TestStore._conn = None

    @classmethod
    def nextkey(cls):
        """Generate an unused key

        :return: The test store key generated.
        :rtype: integer
        """
        TestStore.open()
        return TestStore._conn.incr('the-key', amount=1)

    @classmethod
    def add(cls, obj):
        """Add a datum to the store and return the key chosen.

        :param obj: The datum to be added to the store.
        :returns: The identifier of the datum added.
        :rtype: integer
        """
        TestStore.open()
        return TestStore.put(TestStore.nextkey(), obj)

    @classmethod
    def put(cls, key, obj):
        """Append the datum to the kvs list identified the given `key`.

        :param key: The key for the datum to be added to the store.
        :param obj: The datum to be added to the store.
        :returns: The `key` given.
        """
        TestStore.open()
        if isinstance(obj, list) or isinstance(obj, tuple):
            for elem in obj:
                TestStore._conn.rpush(key, elem)
        else:
            TestStore._conn.rpush(key, obj)
        return key

    @classmethod
    def remove(cls, oid):
        """Remove the datum with given identifier from the store.

        :param oid: The identifier associated with the datum to be removed.
        """
        TestStore.open()
        TestStore._conn.delete(oid)

    @classmethod
    def lookup(cls, oid):
        """Return the datum associated with `oid` or `None`.

        :param oid: The identifier of the datum sought.
        """
        TestStore.open()
        num_of_words = TestStore._conn.llen(oid)
        if num_of_words > 1:
            return TestStore._conn.lrange(oid, 0, num_of_words + 1)
        else:
            return TestStore._conn.lindex(oid, 0)

    @classmethod
    def set(cls, key, obj):
        """Asssociate a single datum with the given `key`.

        :param key: The key for the datum to be added to the store.
        :param obj: The datum to be added to the store.
        :returns: The `key` given.
        """
        TestStore.open()
        TestStore._conn.set(key, obj)

    @classmethod
    def get(cls, key):
        """Return the datum associated with the given `key` or `None`.

        :param key: The key of the datum sought.
        :returns: The datum associated with the given `key` or `None`.
        """
        TestStore.open()
        return TestStore._conn.get(key)


def touch(content=None, dir=None, prefix="tmp", suffix="tmp"):
    """Create temporary file with the given content.

    Please note: the temporary file must be deleted bu the caller.

    :param string content: the content to write to the temporary file.
    :param string dir: directory where the file should be created
    :param string prefix: file name prefix
    :param string suffix: file name suffix
    :returns: a string with the path to the temporary file
    """
    if dir is not None:
        if not os.path.exists(dir):
            os.makedirs(dir)
    fh, path = tempfile.mkstemp(dir=dir, prefix=prefix, suffix=suffix)
    if content:
        fh = os.fdopen(fh, "w")
        fh.write(content)
        fh.close()
    return path


class DbTestCase(object):
    """Class which contains various db-related testing helpers."""

    IMLS = [0.005, 0.007, 0.0098, 0.0137, 0.0192, 0.0269, 0.0376, 0.0527,
            0.0738, 0.103, 0.145, 0.203, 0.284, 0.397, 0.556, 0.778]

    @classmethod
    def teardown_inputs(cls, inputs, filesystem_only):
        if filesystem_only:
            return
        [input.delete() for input in inputs]

    @classmethod
    def setup_job_profile(cls, job, force_inputs, save2db=True):
        """Create a profile for the given job.

        :param job: The :class:`openquake.engine.db.models.OqJob`
            instance to use.
        :param bool force_inputs: If `True` the model input files will be
            parsed and the resulting content written to the database no matter
            what.
        :param bool save2db: If `False` the job profile instance will be
            returned but not saved to the database. Otherwise it is saved to
            the database and returned then.
        :returns: a :class:`openquake.engine.db.models.OqJobProfile` instance
        """
        oqjp = models.OqJobProfile()
        oqjp.owner = job.owner
        oqjp.force_inputs = force_inputs
        oqjp.calc_mode = "classical"
        oqjp.job_type = ['hazard']
        oqjp.region_grid_spacing = 0.01
        oqjp.min_magnitude = 5.0
        oqjp.investigation_time = 50.0
        oqjp.component = "gmroti50"
        oqjp.imt = "pga"
        oqjp.truncation_type = "twosided"
        oqjp.truncation_level = 3
        oqjp.reference_vs30_value = 760
        oqjp.imls = cls.IMLS
        oqjp.poes = [0.01, 0.10]
        oqjp.realizations = 1
        oqjp.width_of_mfd_bin = 0.1
        oqjp.treat_grid_source_as = 'pointsources'
        oqjp.treat_area_source_as = 'pointsources'
        oqjp.subduction_rupture_floating_type = 'Along strike and down dip'
        oqjp.subduction_rupture_aspect_ratio = 1.5
        oqjp.subduction_fault_surface_discretization = 0.5
        oqjp.subduction_fault_rupture_offset = 10.0
        oqjp.subduction_fault_magnitude_scaling_sigma = 0.0
        oqjp.subduction_fault_magnitude_scaling_relationship = \
            'W&C 1994 Mag-Length Rel.'
        oqjp.standard_deviation_type = 'total'
        oqjp.sadigh_site_type = 'Rock'
        oqjp.rupture_floating_type = 'Along strike and down dip'
        oqjp.rupture_aspect_ratio = 1.5
        oqjp.reference_depth_to_2pt5km_per_sec_param = 5.0
        oqjp.quantile_levels = [0.25, 0.50]
        oqjp.maximum_distance = 200
        oqjp.include_subductive_fault = True
        oqjp.include_subduction_fault_source = True
        oqjp.include_grid_sources = True
        oqjp.include_fault_source = True
        oqjp.include_area_sources = True
        oqjp.fault_surface_discretization = 1.0
        oqjp.fault_rupture_offset = 5.0
        oqjp.fault_magnitude_scaling_sigma = 0.0
        oqjp.fault_magnitude_scaling_relationship = 'W&C 1994 Mag-Length Rel.'
        oqjp.compute_mean_hazard_curve = True
        oqjp.area_source_magnitude_scaling_relationship = \
            'W&C 1994 Mag-Length Rel.'
        oqjp.area_source_discretization = 0.1
        oqjp.treat_area_source_as = 'pointsources'
        oqjp.subduction_rupture_floating_type = 'downdip'
        oqjp.rupture_floating_type = 'downdip'
        oqjp.sadigh_site_type = 'rock'
        oqjp.region = (
            "POLYGON((-81.3 37.2, -80.63 38.04, -80.02 37.49, -81.3 37.2))")
        oqjp.source_model_lt_random_seed = 23
        oqjp.gmpe_lt_random_seed = 5
        if save2db:
            oqjp.save()
        return oqjp

    @classmethod
    def setup_classic_job(cls, create_job_path=True, inputs=None,
                          force_inputs=False, omit_profile=False,
                          user_name="openquake"):
        """Create a classic job with associated upload and inputs.

        :param bool create_job_path: if set the path for the job will be
            created and captured in the job record
        :param list inputs: a list of 2-tuples where the first and the second
            element are the input type and path respectively
        :param bool force_inputs: If `True` the model input files will be
            parsed and the resulting content written to the database no matter
            what.
        :param bool omit_profile: If `True` no job profile will be created.
        :param str user_name: The name of the user that is running the job.
        :returns: a :py:class:`db.models.OqJob` instance
        """
        job = engine.prepare_job(user_name)
        if not omit_profile:
            oqjp = cls.setup_job_profile(job, force_inputs)
            models.Job2profile(oq_job=job, oq_job_profile=oqjp).save()

        # Insert input model files
        if inputs:
            insert_inputs(job, inputs)

        if create_job_path:
            job.path = os.path.join(tempfile.mkdtemp(), str(job.id))
            job.save()

            os.mkdir(job.path)
            os.chmod(job.path, 0777)

        return job

    @classmethod
    def teardown_job(cls, job, filesystem_only=True):
        """
        Tear down the file system (and potentially db) artefacts for the
        given job.

        :param job: a :py:class:`db.models.OqJob` instance
        :param bool filesystem_only: if set the oq_job/oq_param/
            input database records will be left intact. This saves time and the
            test db will be dropped/recreated prior to the next db test suite
            run anyway.
        """
        DbTestCase.teardown_inputs(models.inputs4job(job.id),
                                   filesystem_only=filesystem_only)
        if filesystem_only:
            return

        job.delete()
        try:
            oqjp = models.profile4job(job.id)
            oqjp.delete()
        except ValueError:
            # no job profile for this job
            pass

    def generate_output_path(self, job, output_type="hazard_map"):
        """Return a random output path for the given job."""
        path = touch(
            dir=os.path.join(job.path, "computed_output"), suffix=".xml",
            prefix="hzrd." if output_type == "hazard_map" else "loss.")
        return path

    def teardown_output(self, output, teardown_job=True, filesystem_only=True):
        """
        Tear down the file system (and potentially db) artefacts for the
        given output.

        :param output: the :py:class:`db.models.Output` instance
            in question
        :param bool teardown_job: the associated job and its related artefacts
            shall be torn down as well.
        :param bool filesystem_only: if set the various database records will
            be left intact. This saves time and the test db will be
            dropped/recreated prior to the next db test suite run anyway.
        """
        job = output.oq_job
        if not filesystem_only:
            output.delete()
        if teardown_job:
            self.teardown_job(job, filesystem_only=filesystem_only)


class ConfigTestCase(object):
    """Class which contains various configuration- and environment-related
    testing helpers."""

    def setup_config(self):
        self.orig_env = os.environ.copy()
        os.environ.clear()
        # Move the local configuration file out of the way if it exists.
        # Otherwise the tests that follow will break.
        local_path = "%s/openquake.cfg" % os.path.abspath(os.getcwd())
        if os.path.isfile(local_path):
            shutil.move(local_path, "%s.test_bakk" % local_path)

    def teardown_config(self):
        os.environ.clear()
        os.environ.update(self.orig_env)
        # Move the local configuration file back into place if it was stashed
        # away.
        local_path = "%s/openquake.cfg" % os.path.abspath(os.getcwd())
        if os.path.isfile("%s.test_bakk" % local_path):
            shutil.move("%s.test_bakk" % local_path, local_path)
        config.Config().cfg.clear()
        config.Config()._load_from_file()

    def prepare_config(self, section, data=None):
        """Set up a configuration with the given `max_mem` value."""
        if data is not None:
            data = '\n'.join(["%s=%s" % item for item in data.iteritems()])
            content = """
                [%s]
                %s""" % (section, data)
        else:
            content = ""
        site_path = touch(content=textwrap.dedent(content))
        os.environ["OQ_SITE_CFG_PATH"] = site_path
        config.Config().cfg.clear()
        config.Config()._load_from_file()


class RedisTestCase(object):
    """Redis-related utilities for testing."""

    def connect(self, *args, **kwargs):
        host = config.get("kvs", "host")
        port = config.get("kvs", "port")
        port = int(port) if port else 6379
        stats_db = config.get("kvs", "stats_db")
        stats_db = int(stats_db) if stats_db else 15
        args = {"host": host, "port": port, "db": stats_db}
        return redis.Redis(**args)


def random_string(length=16):
    """Generate a random string of the given length."""
    result = ""
    while len(result) < length:
        result += random.choice(string.letters + string.digits)
    return result


def prepare_cli_output(raw_output, discard_header=True):
    """Given a huge string of output from a `subprocess.check_output` call,
    split on newlines, strip, and discard empty lines.

    If ``discard_header`` is `True`, drop the first row in the output.

    Returns a `list` of strings, 1 for each row in the CLI output.
    """
    lines = raw_output.split('\n')
    # strip and drop empty lines
    lines = [x.strip() for x in lines if len(x.strip()) > 0]

    if discard_header:
        lines.pop(0)

    return lines


def deep_eq(a, b, decimal=7, exclude=None):
    """Deep compare two objects for equality by traversing __dict__ and
    __slots__.

    Caution: This function will exhaust generators.

    :param decimal:
        Desired precision (digits after the decimal point) for numerical
        comparisons.

    :param exclude: a list of attributes that will be excluded when
    traversing objects

    :returns:
        Return `True` or `False` (to indicate if objects are equal) and a `str`
        message. If the two objects are equal, the message is empty. If the two
        objects are not equal, the message indicates which part of the
        comparison failed.
    """
    exclude = exclude or []

    try:
        _deep_eq(a, b, decimal=decimal, exclude=exclude)
    except AssertionError, err:
        return False, err.message
    return True, ''


def _deep_eq(a, b, decimal, exclude=None):
    """Do the actual deep comparison. If the two items up for comparison are
    not equal, a :exception:`AssertionError` is raised (to
    :function:`deep_eq`).
    """

    exclude = exclude or []

    def _test_dict(a, b):
        """Compare `dict` types recursively."""
        assert len(a) == len(b), (
            "Dicts %(a)s and %(b)s do not have the same length."
            " Actual lengths: %(len_a)s and %(len_b)s") % dict(
                a=a, b=b, len_a=len(a), len_b=len(b))

        for key in a:
            if not key in exclude:
                _deep_eq(a[key], b[key], decimal)

    def _test_seq(a, b):
        """Compare `list` or `tuple` types recursively."""
        assert len(a) == len(b), (
            "Sequences %(a)s and %(b)s do not have the same length."
            " Actual lengths: %(len_a)s and %(len_b)s") % \
            dict(a=a, b=b, len_a=len(a), len_b=len(b))

        for i, item in enumerate(a):
            _deep_eq(item, b[i], decimal)

    # lists or tuples
    if isinstance(a, (list, tuple)):
        _test_seq(a, b)
    # dicts
    elif isinstance(a, dict):
        _test_dict(a, b)
    # objects with a __dict__
    elif hasattr(a, '__dict__'):
        assert a.__class__ == b.__class__, (
            "%s and %s are different classes") % (a.__class__, b.__class__)
        _test_dict(a.__dict__, b.__dict__)
    # iterables (not strings)
    elif isinstance(a, collections.Iterable) and not isinstance(a, str):
        # If there's a generator or another type of iterable, treat it as a
        # `list`. NOTE: Generators will be exhausted if you do this.
        _test_seq(list(a), list(b))
    # objects with __slots__
    elif hasattr(a, '__slots__'):
        assert a.__class__ == b.__class__, (
            "%s and %s are different classes") % (a.__class__, b.__class__)
        assert a.__slots__ == b.__slots__, (
            "slots %s and %s are not the same") % (a.__slots__, b.__slots__)
        for slot in a.__slots__:
            if not slot in exclude:
                _deep_eq(getattr(a, slot), getattr(b, slot), decimal)
    else:
        # Objects must be primitives

        # Are they numbers?
        if isinstance(a, (int, long, float, complex)):
            numpy.testing.assert_almost_equal(a, b, decimal=decimal)
        else:
            assert a == b, "%s != %s" % (a, b)


def get_hazard_job(cfg, username=None):
    """
    Given a path to a config file, create a
    :class:`openquake.engine.db.models.OqJob` object for a hazard calculation.
    """
    username = username if username is not None else default_user().user_name

    job = engine2.prepare_job(username)
    params, files = engine2.parse_config(open(cfg, 'r'), force_inputs=True)
    haz_calc = engine2.create_hazard_calculation(
        job.owner, params, files.values())
    haz_calc = models.HazardCalculation.objects.get(id=haz_calc.id)
    job.hazard_calculation = haz_calc
    job.save()
    return job


def get_risk_job(risk_cfg, hazard_cfg, output_type="curve", username=None):
    """
    Takes in input the paths to a risk job config file and a hazard job config
    file.

    Creates fake hazard outputs suitable to be used by a risk
    calculation and then creates a :class:`openquake.engine.db.models.OqJob`
    object for a risk calculation. It also returns the input files
    referenced by the risk config file.

    :param output_type: gmf, gmf_scenario, or curve
    """
    username = username if username is not None else default_user().user_name

    hazard_job = get_hazard_job(hazard_cfg, username)
    hc = hazard_job.hazard_calculation

    rlz = models.LtRealization.objects.create(
        hazard_calculation=hazard_job.hazard_calculation,
        ordinal=1, seed=1, weight=None,
        sm_lt_path="test_sm", gsim_lt_path="test_gsim",
        is_complete=False, total_items=1, completed_items=1)
    if output_type == "curve":
        hazard_output = models.HazardCurve.objects.create(
            lt_realization=rlz,
            output=models.Output.objects.create_output(
                hazard_job, "Test Hazard output", "hazard_curve"),
            investigation_time=hc.investigation_time,
            imt="PGA", imls=[0.1, 0.2, 0.3])

        for point in ["POINT(-1.01 1.01)", "POINT(0.9 1.01)",
                      "POINT(0.01 0.01)", "POINT(0.9 0.9)"]:
            models.HazardCurveData.objects.create(
                hazard_curve=hazard_output,
                poes=[0.1, 0.2, 0.3],
                location="%s" % point)

    elif output_type == "gmf_scenario":
        output = models.Output.objects.create_output(
            hazard_job, "Test Hazard output", "gmf_scenario")
        for point in ["POINT(15.48 38.0900001)", "POINT(15.565 38.17)",
                      "POINT(15.481 38.25)"]:
            hazard_output = models.GmfScenario.objects.create(
                output=output,
                imt="PGA",
                location=point,
                gmvs=[0.1, 0.2, 0.3])

    else:
        rupture_ids = get_rupture_ids(hazard_job, hc, rlz, 3)
        hazard_output = models.GmfCollection.objects.create(
            output=models.Output.objects.create_output(
                hazard_job, "Test Hazard output", "gmf"),
            lt_realization=rlz,
            complete_logic_tree_gmf=False)

        gmf_set = models.GmfSet.objects.create(
            gmf_collection=hazard_output,
            investigation_time=hc.investigation_time,
            ses_ordinal=1,
            complete_logic_tree_gmf=False)

        for point in ["POINT(15.310 38.225)", "POINT(15.71 37.225)",
                      "POINT(15.48 38.091)", "POINT(15.565 38.17)",
                      "POINT(15.481 38.25)"]:
            models.Gmf.objects.create(
                gmf_set=gmf_set,
                imt="PGA", gmvs=[0.1, 0.2, 0.3],
                rupture_ids=rupture_ids,
                result_grp_ordinal=1,
                location=point)

    hazard_job.status = "complete"
    hazard_job.save()
    job = engine2.prepare_job(username)
    params, files = engine2.parse_config(
        open(risk_cfg, 'r'), force_inputs=True)

    params.update(dict(hazard_output_id=hazard_output.output.id))

    risk_calc = engine2.create_risk_calculation(
        job.owner, params, files.values())
    risk_calc = models.RiskCalculation.objects.get(id=risk_calc.id)
    job.risk_calculation = risk_calc
    job.save()
    return job, files


def get_rupture_ids(job, hc, lt_realization, num):
    """
    :returns: a list of IDs of newly created ruptures associated with
    `job` and an instance of
    :class:`openquake.engine.db.models.HazardCalculation`. It also
    creates a father :class:`openquake.engine.db.models.SES`. Each
    rupture has a magnitude ranging from 0 to 10, no geographic
    information and result_grp_ordinal set to 1.

    :param lt_realization: an instance of
    :class:`openquake.engine.db.models.LtRealization` to be associated
    with the newly created SES object

    :param int num: the number of ruptures to create
    """
    ses = models.SES.objects.create(
        ses_collection=models.SESCollection.objects.create(
            output=models.Output.objects.create_output(
                job, "Test SES Collection", "ses"),
            lt_realization=lt_realization),
        investigation_time=hc.investigation_time,
        ordinal=1,
        complete_logic_tree_ses=False)

    return [
        models.SESRupture.objects.create(
            ses=ses,
            magnitude=i * 10. / float(num),
            strike=0,
            dip=0,
            rake=0,
            tectonic_region_type="test region type",
            is_from_fault_source=False,
            lons=[], lats=[], depths=[],
            result_grp_ordinal=1,
            rupture_ordinal=0).id
        for i in range(num)]


def random_location_generator(min_x=-180, max_x=180, min_y=-90, max_y=90):
    return shapely.geometry.Point(
        (min_x + random.random() * (max_x - min_x),
         min_y + random.random() * (max_y - min_y)))
