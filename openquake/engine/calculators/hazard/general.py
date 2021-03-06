# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010-2013, GEM Foundation.
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

"""Common code for the hazard calculators."""

import math
import os
import random
import re
import StringIO

import openquake.hazardlib
import openquake.hazardlib.site
import numpy

from django.db import transaction, connections
from django.db.models import Sum
from django.core.exceptions import ObjectDoesNotExist

from openquake.hazardlib import geo as hazardlib_geo
from openquake.hazardlib import correlation
from openquake.nrmllib import parsers as nrml_parsers
from shapely import geometry

from openquake.engine import engine2
from openquake.engine import kvs
from openquake.engine import writer
from openquake.engine.export import core as export_core
from openquake.engine.export import hazard as hazard_export
from openquake.engine.calculators import base
from openquake.engine.db import models
from openquake.engine.input import logictree
from openquake.engine.input import source
from openquake.engine.job.validation import MAX_SINT_32
from openquake.engine.job.validation import MIN_SINT_32
from openquake.engine import logs
from openquake.engine.utils import config
from openquake.engine.utils import stats


QUANTILE_PARAM_NAME = "QUANTILE_LEVELS"
POES_PARAM_NAME = "POES"
# Dilation in decimal degrees (http://en.wikipedia.org/wiki/Decimal_degrees)
# 1e-5 represents the approximate distance of one meter at the equator.
DILATION_ONE_METER = 1e-5


def store_source_model(job_id, seed, params, calc):
    """Generate source model from the source model logic tree and store it in
    the KVS.

    :param int job_id: numeric ID of the job
    :param int seed: seed for random logic tree sampling
    :param dict params: the config parameters as (dict)
    :param calc: logic tree processor
    :type calc: :class:`openquake.engine.input.logictree.LogicTreeProcessor`
        instance
    """
    logs.LOG.info("Storing source model from job config")
    key = kvs.tokens.source_model_key(job_id)
    mfd_bin_width = float(params.get('WIDTH_OF_MFD_BIN'))
    calc.sample_and_save_source_model_logictree(
        kvs.get_client(), key, seed, mfd_bin_width)


def store_gmpe_map(job_id, seed, calc):
    """Generate a hash map of GMPEs (keyed by Tectonic Region Type) and store
    it in the KVS.

    :param int job_id: numeric ID of the job
    :param int seed: seed for random logic tree sampling
    :param calc: logic tree processor
    :type calc: :class:`openquake.engine.input.logictree.LogicTreeProcessor`
        instance
    """
    logs.LOG.info("Storing GMPE map from job config")
    key = kvs.tokens.gmpe_key(job_id)
    calc.sample_and_save_gmpe_logictree(kvs.get_client(), key, seed)


@transaction.commit_on_success(using='job_init')
def store_site_model(input_mdl, site_model_source):
    """Invoke site model parser and save the site-specified parameter data to
    the database.

    :param input_mdl:
        The `uiapi.input` record which the new `hzrdi.site_model` records
        reference. This `input` record acts as a container for the site model
        data.
    :param site_model_source:
        Filename or file-like object containing the site model XML data.
    :returns:
        `list` of :class:`openquake.engine.db.models.SiteModel` objects. These
        represent to newly-inserted `hzrdi.site_model` records.
    """
    parser = nrml_parsers.SiteModelParser(site_model_source)

    sm_data = []

    inserter = writer.BulkInserter(models.SiteModel)

    for node in parser.parse():
        sm = dict()
        # sm = models.SiteModel()
        sm['vs30'] = node.vs30
        sm['vs30_type'] = node.vs30_type
        sm['z1pt0'] = node.z1pt0
        sm['z2pt5'] = node.z2pt5
        sm['location'] = node.wkt
        sm['input_id'] = input_mdl.id
        # sm.save()
        # sm_data.append(sm)
        inserter.add_entry(**sm)

    inserter.flush()

    return sm_data


def validate_site_model(sm_nodes, mesh):
    """Given the geometry for a site model and the geometry of interest for the
    calculation (``mesh``, make sure the geometry of interest lies completely
    inside of the convex hull formed by the site model locations.

    If a point of interest lies directly on top of a vertex or edge of the site
    model area (a polygon), it is considered "inside"

    :param sm_nodes:
        Sequence of :class:`~openquake.engine.db.models.SiteModel` objects.
    :param mesh:
        A :class:`openquake.hazardlib.geo.mesh.Mesh` which represents the
        calculation points of interest.

    :raises:
        :exc:`RuntimeError` if the area of interest (given as a mesh) is not
        entirely contained by the site model.
    """
    sm_mp = geometry.MultiPoint(
        [(n.location.x, n.location.y) for n in sm_nodes]
    )

    sm_ch = sm_mp.convex_hull
    # Enlarging the area if the site model nodes
    # create a straight line with zero area.
    if sm_ch.area == 0:
        sm_ch = sm_ch.buffer(DILATION_ONE_METER)

    sm_poly = hazardlib_geo.Polygon(
        [hazardlib_geo.Point(*x) for x in sm_ch.exterior.coords]
    )

    # "Intersects" is the correct operation (not "contains"), since we're just
    # checking a collection of points (mesh). "Contains" would tell us if the
    # points are inside the polygon, but would return `False` if a point was
    # directly on top of a polygon edge or vertex. We want these points to be
    # included.
    intersects = sm_poly.intersects(mesh)

    if not intersects.all():
        raise RuntimeError(
            ['Sites of interest are outside of the site model coverage area.'
             ' This configuration is invalid.']
        )


def get_site_model(hc_id):
    """Get the site model :class:`~openquake.engine.db.models.Input` record
    for the given job id.

    :param int hc_id:
        The id of a :class:`~openquake.engine.db.models.HazardCalculation`.

    :returns:
        The site model :class:`~openquake.engine.db.models.Input` record for
        this job.
    :raises:
        :exc:`RuntimeError` if the job has more than 1 site model.
    """
    site_model = models.inputs4hcalc(hc_id, input_type='site_model')

    if len(site_model) == 0:
        return None
    elif len(site_model) > 1:
        # Multiple site models for 1 job are not allowed.
        raise RuntimeError("Only 1 site model per job is allowed, found %s."
                           % len(site_model))

    # There's only one site model.
    return site_model[0]


def get_closest_site_model_data(input_model, point):
    """Get the closest available site model data from the database for a given
    site model :class:`~openquake.engine.db.models.Input` and
    :class:`openquake.hazardlib.geo.point.Point`.

    :param input_model:
        :class:`openquake.engine.db.models.Input` with `input_type` of
        'site_model'.
    :param site:
        :class:`openquake.hazardlib.geo.point.Point` instance.

    :returns:
        The closest :class:`openquake.engine.db.models.SiteModel` for the given
        ``input_model`` and ``point`` of interest.

        This function uses the PostGIS `ST_Distance_Sphere
        <http://postgis.refractions.net/docs/ST_Distance_Sphere.html>`_
        function to calculate distance.

        If there is no site model data, return `None`.
    """
    query = """
    SELECT
        hzrdi.site_model.*,
        min(ST_Distance_Sphere(location, %s))
            AS min_distance
    FROM hzrdi.site_model
    WHERE input_id = %s
    GROUP BY id
    ORDER BY min_distance
    LIMIT 1;"""

    raw_query_set = models.SiteModel.objects.raw(
        query, ['SRID=4326; %s' % point.wkt2d, input_model.id]
    )

    site_model_data = list(raw_query_set)

    assert len(site_model_data) <= 1, (
        "This query should return at most 1 record.")

    if len(site_model_data) == 1:
        return site_model_data[0]
    else:
        return None


def store_site_data(hc_id, site_model_inp, mesh):
    """
    Given a ``mesh`` of points (calculation points of interest) and a
    site model (``site_model_inp``), get the closest site model data
    for each points and store the mesh point location plus the site parameters
    as a single record in the `htemp.site_data` table.

    NOTE: This should only be necessary for calculations which specify a site
    model. Otherwise, the same 4 reference site parameters are used for all
    sites.

    :param int hc_id:
        ID of a :class:`~openquake.engine.db.models.HazardCalculation`.
    :param site_model_inp:
        An :class:`~openquake.engine.db.models.Input` with an
        `input_type` == 'site_model'. This tells us which site model dataset to
        query.
    :param mesh:
        Calculation points of interest, as a
        :class:`openquake.hazardlib.geo.mesh.Mesh`.
    :returns:
        The :class:`openquake.engine.db.models.SiteData` object that was
        created to store computation points of interest with associated site
        parameters.
    """
    lons = []
    lats = []
    vs30s = []
    vs30_types = []
    z1pt0s = []
    z2pt5s = []

    for pt in mesh:
        smd = get_closest_site_model_data(site_model_inp, pt)

        lons.append(pt.longitude)
        lats.append(pt.latitude)

        vs30s.append(smd.vs30)
        vs30_types.append(smd.vs30_type)
        z1pt0s.append(smd.z1pt0)
        z2pt5s.append(smd.z2pt5)

    site_data = models.SiteData(hazard_calculation_id=hc_id)
    site_data.lons = numpy.array(lons)
    site_data.lats = numpy.array(lats)
    site_data.vs30s = numpy.array(vs30s)
    # We convert from strings to booleans here because this is what a hazardlib
    # SiteCollection expects for the vs30 type. If we do the conversion here,
    # we only do it once and we can directly consume the data on the worker
    # side without having to convert inside each task.
    site_data.vs30_measured = numpy.array(vs30_types) == 'measured'
    site_data.z1pt0s = numpy.array(z1pt0s)
    site_data.z2pt5s = numpy.array(z2pt5s)
    site_data.save()

    return site_data


def gen_sources(src_ids, apply_uncertainties, rupture_mesh_spacing,
                width_of_mfd_bin, area_source_discretization):
    """
    Hazardlib source objects generator for a given set of sources.

    Performs lazy loading, converting and processing of sources.

    :param src_ids:
        A list of IDs for :class:`openquake.engine.db.models.ParsedSource`
        records.
    :param apply_uncertainties:
        A function to be called on each generated source. See
        :meth:`openquake.engine.input.logictree.LogicTreeProcessor.\
parse_source_model_logictree_path`

    For information about the other parameters, see
    :func:`openquake.engine.input.source.nrml_to_hazardlib`.
    """
    for src_id in src_ids:
        parsed_source = models.ParsedSource.objects.get(id=src_id)

        hazardlib_source = source.nrml_to_hazardlib(
            parsed_source.nrml, rupture_mesh_spacing, width_of_mfd_bin,
            area_source_discretization)

        apply_uncertainties(hazardlib_source)
        yield hazardlib_source


def im_dict_to_hazardlib(im_dict):
    """
    Given the dict of intensity measure types and levels, convert them to a
    dict with the same values, except create :mod:`mhlib.imt` objects for the
    new keys.

    :returns:
        A dict of intensity measure level lists, keyed by an IMT object. See
        :mod:`openquake.hazardlib.imt` for more information.
    """
    # TODO: file a bug about  SA periods in hazardlib imts.
    # Why are values of 0.0 not allowed? Technically SA(0.0) means PGA, but
    # there must be a reason why we can't do this.
    hazardlib_im = {}

    for imt, imls in im_dict.items():
        hazardlib_imt = imt_to_hazardlib(imt)
        hazardlib_im[hazardlib_imt] = imls

    return hazardlib_im


def imt_to_hazardlib(imt):
    """Covert an IMT string to an hazardlib object.

    :param str imt:
        Given the IMT string (defined in the job config file), convert it to
        equivlent hazardlib object. See :mod:`openquake.hazardlib.imt`.
    """
    if 'SA' in imt:
        match = re.match(r'^SA\(([^)]+?)\)$', imt)
        period = float(match.group(1))
        return openquake.hazardlib.imt.SA(period, models.DEFAULT_SA_DAMPING)
    else:
        imt_class = getattr(openquake.hazardlib.imt, imt)
        return imt_class()


def update_realization(lt_rlz_id, num_items):
    """
    Call this function when a task is complete to update realization counters
    with the ``num_items`` completed.

    If the `completed_items` becomes equal to the `total_items` for the
    realization, the realization will be marked as complete.

    .. note::
        Because this function performs a SELECT FOR UPDATE query, it is
        expected that this should be called in the context of a transaction, to
        avoid race conditions.

    :param int lt_rlz_id:
        ID of the :class:`openquake.engine.db.models.LtRealization` we want
        to update.
    :param int num_items:
        The number of items by which we want to increment the realization's
        `completed_items` counter.
    """
    ltr_query = """
    SELECT * FROM hzrdr.lt_realization
    WHERE id = %s
    FOR UPDATE
    """

    [lt_rlz] = models.LtRealization.objects.raw(
        ltr_query, [lt_rlz_id])

    lt_rlz.completed_items += num_items
    if lt_rlz.completed_items == lt_rlz.total_items:
        lt_rlz.is_complete = True

    lt_rlz.save()


def get_correl_model(hc):
    """
    Helper function for constructing the appropriate correlation model.

    :param hc:
        A :class:`openquake.engine.db.models.HazardCalculation` instance.

    :returns:
        A correlation object. See :mod:`openquake.hazardlib.correlation` for
        more info.
    """
    correl_model_cls = getattr(
        correlation,
        '%sCorrelationModel' % hc.ground_motion_correlation_model,
        None)
    if correl_model_cls is None:
        # There's no correlation model for this calculation.
        return None

    return correl_model_cls(**hc.ground_motion_correlation_params)


class BaseHazardCalculatorNext(base.CalculatorNext):
    """
    Abstract base class for hazard calculators. Contains a bunch of common
    functionality, like initialization procedures.
    """

    def __init__(self, *args, **kwargs):
        super(BaseHazardCalculatorNext, self).__init__(*args, **kwargs)

        self.progress.update(in_queue=0)

    @property
    def computation_mesh(self):
        """
        :class:`openquake.hazardlib.geo.mesh.Mesh` representing the points
        of interest for the calculation.
        """
        return self.hc.points_to_compute()

    @property
    def hc(self):
        """
        A shorter and more convenient way of accessing the
        :class:`~openquake.engine.db.models.HazardCalculation`.
        """
        return self.job.hazard_calculation

    def block_size(self):
        """
        For hazard calculators, the number of work items per task
        is specified in the configuration file.
        """
        return int(config.get('hazard', 'block_size'))

    def concurrent_tasks(self):
        """
        For hazard calculators, the number of tasks to be in queue
        at any given time is specified in the configuration file.
        """
        return int(config.get('hazard', 'concurrent_tasks'))

    def finalize_hazard_curves(self):
        """
        Create the final output records for hazard curves. This is done by
        copying the temporary results from `htemp.hazard_curve_progress` to
        `hzrdr.hazard_curve` (for metadata) and `hzrdr.hazard_curve_data` (for
        the actual curve PoE values). Foreign keys are made from
        `hzrdr.hazard_curve` to `hzrdr.lt_realization` (realization information
        is need to export the full hazard curve results).
        """
        im = self.hc.intensity_measure_types_and_levels
        points = self.computation_mesh

        # prepare site locations for the stored function call
        lons = '{%s}' % ', '.join(str(v) for v in points.lons)
        lats = '{%s}' % ', '.join(str(v) for v in points.lats)

        realizations = models.LtRealization.objects.filter(
            hazard_calculation=self.hc.id)

        for rlz in realizations:
            # create a new `HazardCurve` 'container' record for each
            # realization for each intensity measure type
            for imt, imls in im.items():
                hc_im_type, sa_period, sa_damping = models.parse_imt(imt)

                hco = models.Output(
                    owner=self.hc.owner,
                    oq_job=self.job,
                    display_name="hc-rlz-%s" % rlz.id,
                    output_type='hazard_curve',
                )
                hco.save()

                haz_curve = models.HazardCurve(
                    output=hco,
                    lt_realization=rlz,
                    investigation_time=self.hc.investigation_time,
                    imt=hc_im_type,
                    imls=imls,
                    sa_period=sa_period,
                    sa_damping=sa_damping,
                )
                haz_curve.save()

                with transaction.commit_on_success(using='reslt_writer'):
                    cursor = connections['reslt_writer'].cursor()

                    # TODO(LB): I don't like the fact that we have to pass
                    # potentially huge arguments (100k sites, for example).
                    # I would like to be able to fetch this site data from
                    # the stored function, but at the moment, the only form
                    # available is a pickled `SiteCollection` object, and I've
                    # experienced problems trying to import third-party libs
                    # in a DB function context and could not get it to reliably
                    # work.
                    # As a fix, in addition to caching the pickled
                    # SiteCollection in the DB, we could store also arrays for
                    # lons and lats. It's duplicated information, but we have a
                    # relatively low number of HazardCalculation records, so it
                    # shouldn't be a big deal.
                    cursor.execute(
                        """
                        SELECT hzrdr.finalize_hazard_curves(
                            %s, %s, %s, %s, %s, %s)
                        """,
                        [self.hc.id, rlz.id, haz_curve.id, imt, lons, lats]
                    )

    def initialize_sources(self):
        """
        Parse and validation logic trees (source and gsim). Then get all
        sources referenced in the the source model logic tree, create
        :class:`~openquake.engine.db.models.Input` records for all of them,
        parse then, and save the parsed sources to the `parsed_source` table
        (see :class:`openquake.engine.db.models.ParsedSource`).
        """
        logs.LOG.progress("initializing sources")

        [smlt] = models.inputs4hcalc(
            self.hc.id, input_type='source_model_logic_tree')
        [gsimlt] = models.inputs4hcalc(
            self.hc.id, input_type='gsim_logic_tree')
        source_paths = logictree.read_logic_trees(
            self.hc.base_path, smlt.path, gsimlt.path)

        src_inputs = []
        for src_path in source_paths:
            full_path = os.path.join(self.hc.base_path, src_path)

            # Get or reuse the 'source' Input:
            inp = engine2.get_input(
                full_path, 'source', self.hc.owner, self.hc.force_inputs)
            src_inputs.append(inp)

            # Associate the source input to the calculation:
            models.Input2hcalc.objects.get_or_create(
                input=inp, hazard_calculation=self.hc)

            # Associate the source input to the source model logic tree input:
            try:
                models.Src2ltsrc.objects.get(lt_src=smlt, filename=src_path)
            except ObjectDoesNotExist:
                # If it doesn't exist, this is a new input and we're not
                # reusing an old one which is identical.
                # Only in this case do we parse the sources and populate
                # `hzrdi.parsed_source`.
                models.Src2ltsrc.objects.create(hzrd_src=inp, lt_src=smlt,
                                                filename=src_path)
                src_content = StringIO.StringIO(
                    inp.model_content.raw_content_ascii)
                sm_parser = nrml_parsers.SourceModelParser(src_content)
                src_db_writer = source.SourceDBWriter(
                    inp, sm_parser.parse(), self.hc.rupture_mesh_spacing,
                    self.hc.width_of_mfd_bin,
                    self.hc.area_source_discretization
                )
                src_db_writer.serialize()

    def initialize_site_model(self):
        """
        If a site model is specified in the calculation configuration, parse
        it and load it into the `hzrdi.site_model` table. This includes a
        validation step to ensure that the area covered by the site model
        completely envelops the calculation geometry. (If this requirement is
        not satisfied, an exception will be raised. See
        :func:`openquake.engine.calculators.hazard.general.validate_site_model`.)

        Then, take all of the points/locations of interest defined by the
        calculation geometry. For each point, do distance queries on the site
        model and get the site parameters which are closest to the point of
        interest. This aggregation of points to the closest site parameters
        is what we store in `htemp.site_data`. (Computing this once prior to
        starting the calculation is optimal, since each task will need to
        consider all sites.)
        """
        logs.LOG.progress("initializing site model")

        site_model_inp = get_site_model(self.hc.id)
        if site_model_inp is not None:
            # Explicit cast to `str` here because the XML parser doesn't like
            # unicode. (More specifically, lxml doesn't like unicode.)
            site_model_content = site_model_inp.model_content.raw_content_ascii

            # Store `site_model` records:
            store_site_model(
                site_model_inp, StringIO.StringIO(site_model_content))

            mesh = self.computation_mesh

            # Get the site model records we stored:
            site_model_data = models.SiteModel.objects.filter(
                input=site_model_inp)

            validate_site_model(site_model_data, mesh)

            store_site_data(self.hc.id, site_model_inp, mesh)

    # Silencing 'Too many local variables'
    # pylint: disable=R0914
    @transaction.commit_on_success(using='reslt_writer')
    def initialize_realizations(self, rlz_callbacks=None):
        """
        Create records for the `hzrdr.lt_realization` and
        `htemp.source_progress` records.

        This function works either in random sampling mode (when lt_realization
        models get the random seed value) or in enumeration mode (when weight
        values are populated). In both cases we record the logic tree paths
        for both trees in the `lt_realization` record, as well as ordinal
        number of the realization (zero-based).

        Then we create `htemp.source_progress` records for each source
        in the source model chosen for each realization,
        see :meth:`initialize_source_progress`.

        :param rlz_callbacks:
            Optionally, you can specify a list of callbacks for each
            realization.  In the case of the classical hazard calculator, for
            example, we would include a callback function to create initial
            records for temporary hazard curve result data.

            Callbacks should accept a single argument:
            A :class:`~openquake.engine.db.models.LtRealization` object.
        """
        logs.LOG.progress("initializing realizations")
        if self.job.hazard_calculation.number_of_logic_tree_samples > 0:
            # random sampling of paths
            self._initialize_realizations_montecarlo(
                rlz_callbacks=rlz_callbacks)
        else:
            # full paths enumeration
            self._initialize_realizations_enumeration(
                rlz_callbacks=rlz_callbacks)

    def initialize_pr_data(self):
        """Record the total/completed number of work items.

        This is needed for the purpose of providing an indication of progress
        to the end user."""
        stats.pk_set(self.job.id, "lvr", 0)
        rs = models.LtRealization.objects.filter(
            hazard_calculation=self.job.hazard_calculation)
        total = rs.aggregate(Sum("total_items"))
        done = rs.aggregate(Sum("completed_items"))
        stats.pk_set(self.job.id, "nhzrd_total", total.values().pop())
        if done > 0:
            stats.pk_set(self.job.id, "nhzrd_done", done.values().pop())

    def _initialize_realizations_enumeration(self, rlz_callbacks=None):
        """
        Perform full paths enumeration of logic trees and populate
        lt_realization table.

        :param rlz_callbacks:
            See :meth:`initialize_realizations` for more info.
        """
        hc = self.job.hazard_calculation
        [smlt] = models.inputs4hcalc(hc.id, input_type='source_model_logic_tree')
        ltp = logictree.LogicTreeProcessor(hc.id)
        hzrd_src_cache = {}

        for i, path_info in enumerate(ltp.enumerate_paths()):
            sm_name, weight, sm_lt_path, gsim_lt_path = path_info

            lt_rlz = models.LtRealization(
                hazard_calculation=hc,
                ordinal=i,
                seed=None,
                weight=weight,
                sm_lt_path=sm_lt_path,
                gsim_lt_path=gsim_lt_path,
                # we will update total_items in initialize_source_progress()
                total_items=-1)
            lt_rlz.save()

            if not sm_name in hzrd_src_cache:
                # Get the source model for this sample:
                hzrd_src = models.Src2ltsrc.objects.get(
                    lt_src=smlt.id, filename=sm_name).hzrd_src
                # and cache it
                hzrd_src_cache[sm_name] = hzrd_src
            else:
                hzrd_src = hzrd_src_cache[sm_name]

            # Create source_progress objects
            self.initialize_source_progress(lt_rlz, hzrd_src)

            # Run realization callback (if any) to do additional initialization
            # for each realization:
            if rlz_callbacks is not None:
                for cb in rlz_callbacks:
                    cb(lt_rlz)

    def _initialize_realizations_montecarlo(self, rlz_callbacks=None):
        """
        Perform random sampling of both logic trees and populate lt_realization
        table.

        :param rlz_callbacks:
            See :meth:`initialize_realizations` for more info.
        """
        # Each realization will have two seeds:
        # One for source model logic tree, one for GSIM logic tree.
        rnd = random.Random()
        seed = self.hc.random_seed
        rnd.seed(seed)

        [smlt] = models.inputs4hcalc(self.hc.id, input_type='source_model_logic_tree')

        ltp = logictree.LogicTreeProcessor(self.hc.id)

        hzrd_src_cache = {}

        # The first realization gets the seed we specified in the config file.
        for i in xrange(self.hc.number_of_logic_tree_samples):
            # Sample source model logic tree branch paths:
            sm_name, sm_lt_path = ltp.sample_source_model_logictree(
                rnd.randint(MIN_SINT_32, MAX_SINT_32))

            # Sample GSIM logic tree branch paths:
            gsim_lt_path = ltp.sample_gmpe_logictree(
                rnd.randint(MIN_SINT_32, MAX_SINT_32))

            lt_rlz = models.LtRealization(
                hazard_calculation=self.hc,
                ordinal=i,
                seed=seed,
                weight=None,
                sm_lt_path=sm_lt_path,
                gsim_lt_path=gsim_lt_path,
                # we will update total_items in initialize_source_progress()
                total_items=-1
            )
            lt_rlz.save()

            if not sm_name in hzrd_src_cache:
                # Get the source model for this sample:
                hzrd_src = models.Src2ltsrc.objects.get(
                    lt_src=smlt.id, filename=sm_name).hzrd_src
                # and cache it
                hzrd_src_cache[sm_name] = hzrd_src
            else:
                hzrd_src = hzrd_src_cache[sm_name]

            # Create source_progress objects
            self.initialize_source_progress(lt_rlz, hzrd_src)

            # Run realization callback (if any) to do additional initialization
            # for each realization:
            if rlz_callbacks is not None:
                for cb in rlz_callbacks:
                    cb(lt_rlz)

            # update the seed for the next realization
            seed = rnd.randint(MIN_SINT_32, MAX_SINT_32)
            rnd.seed(seed)

    @staticmethod
    def initialize_source_progress(lt_rlz, hzrd_src):
        """
        Create ``source_progress`` models for given logic tree realization
        and set total sources of realization.

        :param lt_rlz:
            :class:`openquake.engine.db.models.LtRealization` object to
            initialize source progress for.
        :param hztd_src:
            :class:`openquake.engine.db.models.Input` object that needed parsed
            sources are referencing.
        """
        cursor = connections['reslt_writer'].cursor()
        src_progress_tbl = models.SourceProgress._meta.db_table
        parsed_src_tbl = models.ParsedSource._meta.db_table
        lt_rlz_tbl = models.LtRealization._meta.db_table
        cursor.execute("""
            INSERT INTO "%s" (lt_realization_id, parsed_source_id, is_complete)
            SELECT %%s, id, FALSE
            FROM "%s" WHERE input_id = %%s
            ORDER BY id
            """ % (src_progress_tbl, parsed_src_tbl),
            [lt_rlz.id, hzrd_src.id])
        cursor.execute("""
            UPDATE "%s" SET total_items = (
                SELECT count(1) FROM "%s" WHERE lt_realization_id = %%s
            )""" % (lt_rlz_tbl, src_progress_tbl),
            [lt_rlz.id])
        transaction.commit_unless_managed()

    def initialize_hazard_curve_progress(self, lt_rlz):
        """
        As a calculation progresses, workers will periodically update the
        intermediate results. These results will be stored in
        `htemp.hazard_curve_progress` until the calculation is completed.

        Before the core calculation begins, we need to initalize these records,
        one data set per IMT. Each dataset will be stored in the database as a
        pickled 2D numpy array (with number of rows == calculation points of
        interest and number of columns == number of IML values for a given
        IMT).

        We will create 1 `hazard_curve_progress` record per IMT per
        realization.

        :param lt_rlz:
            :class:`openquake.engine.db.models.LtRealization` object to
            associate with these inital hazard curve values.
        """
        num_points = len(self.computation_mesh)

        im_data = self.hc.intensity_measure_types_and_levels
        for imt, imls in im_data.items():
            hc_prog = models.HazardCurveProgress()
            hc_prog.lt_realization = lt_rlz
            hc_prog.imt = imt
            hc_prog.result_matrix = numpy.zeros((num_points, len(imls)))
            hc_prog.save()

    def export(self, *args, **kwargs):
        """
        If requested by the user, automatically export all result artifacts to
        the specified format. (NOTE: The only export format supported at the
        moment is NRML XML.

        :returns:
            A list of the export filenames, including the absolute path to each
            file.
        """
        exported_files = []

        logs.LOG.debug('> starting exports')
        if 'exports' in kwargs and 'xml' in kwargs['exports']:
            outputs = export_core.get_outputs(self.job.id)

            for output in outputs:
                exported_files.extend(hazard_export.export(
                    output.id, self.job.hazard_calculation.export_dir))

            for exp_file in exported_files:
                logs.LOG.debug('exported %s' % exp_file)
        logs.LOG.debug('< done with exports')

        return exported_files

    def record_init_stats(self):
        """
        Record some basic job stats, including the number of sites,
        realizations (end branches), and total number of tasks for the job.

        This should be run between the `pre-execute` and `execute` phases, once
        the job has been fully initialized.
        """
        # Record num sites, num realizations, and num tasks.
        num_sites = len(self.computation_mesh)
        realizations = models.LtRealization.objects.filter(
            hazard_calculation=self.hc.id)
        num_rlzs = realizations.count()

        # Compute the number of tasks.
        block_size = int(config.get('hazard', 'block_size'))
        num_tasks = 0
        for lt_rlz in realizations:
            # Each realization has the potential to choose a random source
            # model, and thus there may be a variable number of tasks for each
            # realization (depending on the number of the sources in the model
            # which was chosen for the realization).
            num_sources = models.SourceProgress.objects.filter(
                lt_realization=lt_rlz).count()
            num_tasks += math.ceil(float(num_sources) / block_size)

        [job_stats] = models.JobStats.objects.filter(oq_job=self.job.id)
        job_stats.num_sites = num_sites
        job_stats.num_tasks = num_tasks
        job_stats.num_realizations = num_rlzs
        job_stats.save()
