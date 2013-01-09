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

import os
from nose.plugins.attrib import attr
from unittest import skip
from numpy.testing import assert_almost_equal

from openquake import export
from qa_tests import _utils as qa_utils


# this test is skipped because of a bug in hazard.general.validate_site_model
# which breaks if the points in the site_model are in a line, see
# https://bugs.launchpad.net/openquake/+bug/1092056
# once the bug is fixed we can restore the test
@skip
class ScenarioHazardCase4TestCase(qa_utils.BaseQATestCase):

    @attr('qa', 'hazard', 'scenario')
    def test(self):
        cfg = os.path.join(os.path.dirname(__file__), 'job.ini')
        job = self.run_hazard(cfg)
        [output] = export.core.get_outputs(job.id)
        actual = list(qa_utils.get_medians(output, 'PGA'))
        expected_medians = [0.41615372, 0.22797466, 0.1936226]

        assert_almost_equal(actual, expected_medians, decimal=2)