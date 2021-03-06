# Copyright (c) 2013, GEM Foundation.
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

import itertools
import math
from openquake.engine.db import models


def joint_prob_of_occurrence(gmvs_site_1, gmvs_site_2, gmv, time_span,
                             num_ses, delta_gmv=0.1):
    """
    Compute the Poissonian probability of a ground shaking value to be in the
    range [``gmv`` - ``delta_gmv`` / 2, ``gmv`` + ``delta_gmv`` / 2] at two
    different locations within a given ``time_span``.

    :param gmvs_site_1, gmvs_site_2:
        Lists of ground motion values (as floats) for two different sites.
    :param gmv:
        Reference value for computing joint probability.
    :param time_span:
        `investigation_time` parameter from the calculation which produced
        these ground motion values.
    :param num_ses:
        `ses_per_logic_tree_path` parameter from the calculation which produced
        these ground motion values. In other words, the total number of
        stochastic event sets.
    """
    assert len(gmvs_site_1) == len(gmvs_site_2)
    total_gmvs = len(gmvs_site_1)

    half_delta = float(delta_gmv) / 2
    gmv_close = lambda v: (gmv - half_delta <= v
                           <= gmv + half_delta)
    count = 0
    for gmv_site_1, gmv_site_2 in itertools.izip(gmvs_site_1, gmvs_site_2):
        if gmv_close(gmv_site_1) and gmv_close(gmv_site_2):
            count += 1

    prob = 1 - math.exp( - (float(count) / (time_span * num_ses)) * time_span)
    return prob


def get_gmvs_for_location(location, job_id):
    """
    Get a list of GMVs (as floats) for a given ``location`` and ``job_id``.

    :param str location:
        Location as POINT WKT
    :param int job_id:
        Job ID
    :returns:
        `list` of ground motion values, as floats
    """
    gmf_sets = models.GmfSet.objects.filter(
        gmf_collection__output__oq_job=job_id
    )
    gmfs = list(itertools.chain(*(s.iter_gmfs(location=location)
                                  for s in gmf_sets)))

    gmf_nodes = list(itertools.chain(*[list(x) for x in gmfs]))

    gmvs = [x.gmv for x in gmf_nodes]

    return gmvs
